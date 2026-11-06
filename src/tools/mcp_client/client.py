# =============================================================================
# 文件：src/tools/mcp_client/client.py
# 作用：连接外部 MCP 服务器。连上以后，就能用那些服务器提供的工具。
#
# 先搞懂三个角色：
#   我们的程序  = 客户端（Client），主动去连别人
#   MCP 服务器  = 一个独立的小程序，提供某些能力（查天气、算数、查数据库……）
#   会话 Session = 连上之后建立的一条"通话线路"，后面所有交流都走它
#
# 通信方式（stdio）：我们的程序启动那个小服务器程序，
#   然后双方通过"标准输入/标准输出"互相发消息——就像两个人在同一个终端里对话。
#
# 为什么连服务器要"异步 + 后台任务"？
#   因为启动一个外部程序、等它初始化，可能要好几秒。
#   如果卡在这里，程序启动就会明显变慢。
#   所以是"派个后台工人去连"，主流程继续往下走，需要工具清单时再等它。
# =============================================================================

# adapt from https://modelcontextprotocol.io/docs/develop/build-client
# 上面说明：这段是照 MCP 官方文档的示例改的

import asyncio  # 用来创建后台任务
from typing import Optional  # 类型标注工具（这里其实没用到）
from contextlib import AsyncExitStack  # "异步资源栈"：统一管理多个需要关闭的东西

from mcp import ClientSession, StdioServerParameters, LoggingLevel  # MCP 官方库的几个核心类
from mcp.client.stdio import stdio_client  # 专门负责"用标准输入输出方式连接"的函数

from anthropic import Anthropic  # 导入了但没用到（残留代码，可以删）
from dotenv import load_dotenv  # 读取 .env

from .server_config import MCPConfig  # 导入配置读取器

load_dotenv()  # load environment variables from .env（把配置读进来）


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.sessions: list[ClientSession] = []  # 存所有已经连上的会话（每个服务器一条）
        self.exit_stack = AsyncExitStack()
        # 资源栈：把所有"需要关闭的东西"（服务器进程、会话）都登记在这里，
        # 方便最后一次性全部关掉。不过目前代码里其实没有地方调用它的关闭方法（见文末备注）
        self.config = MCPConfig("~/.quickstar/mcp.json")
        # 读配置文件。~ 会被展开成你的用户目录，所以实际路径是 C:/Users/你的名字/.quickstar/mcp.json
        # ⚠️ 如果这个文件不存在，构造时就会直接抛异常，整个程序起不来

        self.connect_server_task = asyncio.create_task(self.connect_to_server())
        # 派一个后台任务去连接所有服务器，不等它完成，先把对象造出来

    async def connect_to_server(self):
        """Connect to all MCP servers（连接配置文件里列出的所有服务器）"""
        mcp_servers = self.config.list_servers()  # 拿到所有服务器名字的列表
        if not mcp_servers:  # 【2026-11-06 优化】没有配置任何服务器
            return  # 直接返回，连日志都不用打（配置缺失的情况已经在 server_config 里提示过了）

        for name in mcp_servers:  # 一个个连
            server = self.config.get_server(name)  # 按名字取出这个服务器的配置
            if server is None:  # 【2026-11-06 优化】理论上不会发生，但防一手
                continue  # 跳过

            try:  # 【2026-11-06 优化】每个服务器单独 try：
                #   原来只要有一个服务器启动失败（路径写错、依赖没装），
                #   异常会一路抛到 get_sessions()，导致所有外部工具都用不了，
                #   甚至让程序启动阶段就报错。现在改成"坏的跳过，好的继续"。
                server_params = StdioServerParameters(
                    # 把配置包装成 MCP 库要求的格式
                    command=server.command,  # 用哪个程序来启动它（比如 D:\Anaconda\envs\claude\python.exe）
                    args=server.args,  # 启动参数（比如要运行哪个 .py 文件）
                    env=None  # 环境变量：None 表示直接继承当前程序的环境变量
                )

                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                # 上面这行做两件事：
                #   1. 真的去启动那个外部程序，并建立输入输出通道
                #   2. 把这个通道登记到资源栈里，以后统一关闭
                # stdio, write 是通道的两端：一个用来读，一个用来写
                stdio, write = stdio_transport  # 把返回的两个对象分别取出来
                session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
                # 在通道上建立一个"会话"，同样登记到资源栈

                await session.initialize()  # 握手：双方交换版本和能力信息（必须做，不然对方不认你）
                self.sessions.append(session)  # 把这条会话存起来备用
            except Exception as e:  # 这个服务器连接失败
                print(f"[MCP] 连接服务器 '{name}' 失败：{e}（已跳过，其他工具不受影响）")
                # 打印原因后继续下一个，不中断整体流程

    async def get_sessions(self) -> list[ClientSession]:
        await self.connect_server_task  # 等后台那个连接任务全部完成
        return self.sessions  # 把所有会话交出去

# =============================================================================
# 备注（这段代码的几个隐患，以及本轮处理情况）
#
# 1. 【2026-11-06 已修复】配置文件不存在就崩：
#    原来 MCPConfig 找不到 ~/.quickstar/mcp.json 时会抛 FileNotFoundError，
#    而这个对象在 ToolsManager 创建时就 new 出来，导致整个 CLI 起不来。
#    现在改为"打印提示 + 跳过外部工具"，内置工具照常可用。
#
# 2. 【2026-11-06 已修复】一个服务器出错会连累全部：
#    现在 for 循环里给每个服务器单独套了 try，坏的跳过、好的继续。
#
# 3. 退出时不清理：exit_stack 从头到尾没有被 aclose()，
#    所以程序结束时那些外部服务器进程可能会变成"孤儿进程"留在后台。
#    这块留到第 2 轮优化（要在同一个事件循环任务里关闭，否则会报
#    "Attempted to exit cancel scope in a different task"）。
#
# 4. 只支持 stdio：HTTP / SSE 类型的服务器在 server_config 里被直接跳过了。
# =============================================================================
