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
from pathlib import Path  # 路径处理，用来判断配置文件在不在

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
        # 资源栈：把所有"需要关闭的东西"（服务器进程、会话）都登记在这里，方便最后一次性全部关掉
        self.config = MCPConfig(str(self._resolve_config_path()))
        # 读配置文件。~ 会被展开成你的用户目录，所以实际路径是 C:/Users/你的名字/.thinker/mcp.json
        # 文件不存在会降级处理（见 server_config.py），不会抛异常

        # 【2026-11-06 第 2 轮优化】新增两个"信号灯"
        self._ready = asyncio.Event()  # 连接阶段结束（不管成功失败）就点亮
        self._shutdown = asyncio.Event()  # 有人要求退出时点亮
        self._closed = False  # 标记"已经关过了"，防止重复关闭

        # 【2026-11-06 第 2 轮优化】改用一个"总任务"来管连接和关闭
        #   原因：MCP 的 stdio 连接是用 AsyncExitStack 打开的，anyio 规定
        #   "在哪个任务里打开，就必须在哪个任务里关闭"，否则会报
        #   RuntimeError: Attempted to exit cancel scope in a different task ...
        #   所以让 _run() 这个任务负责"打开 → 待命 → 自己关掉"，全程同一个任务。
        self.connect_server_task = None
        # 【2026-11-06 第 2 轮优化】先留空，用到时再启动。
        #   原因：asyncio.create_task 必须在一个"正在运行的事件循环"里调用，
        #   如果有人在循环外面 new 了一个 MCPClient（比如写个脚本直接 import），
        #   原来那行会直接抛 RuntimeError: no running event loop。
        #   现在改成"有循环就现在启动，没有就等第一次用到时再启动"。
        self._ensure_started()

    @staticmethod
    def _resolve_config_path() -> Path:
        """决定去哪个路径读 MCP 配置（2026-11-07 改名兼容）

        项目从 QuickStar 改名为 Thinker，配置目录也跟着从 ~/.quickstar 换成 ~/.thinker。
        但老用户的配置文件还在旧目录里，直接切过去会导致"外部工具全部消失"。
        所以：新目录优先，新目录不存在而旧目录存在时，继续用旧的。
        """
        new_path = Path("~/.thinker/mcp.json").expanduser()
        if new_path.exists():
            return new_path
        legacy_path = Path("~/.quickstar/mcp.json").expanduser()
        if legacy_path.exists():
            return legacy_path
        return new_path  # 两个都没有：返回新路径，让 server_config 打印"未找到"提示

    def _ensure_started(self):
        """确保后台任务已经启动（2026-11-06 第 2 轮新增，可重复调用）"""
        if self.connect_server_task is not None:  # 已经启动过
            return
        try:
            asyncio.get_running_loop()  # 试着拿当前正在运行的循环
        except RuntimeError:  # 没有循环（说明现在不在 async 环境里）
            return  # 那就先不启动，等真正要用的时候再说
        self.connect_server_task = asyncio.create_task(self._run())  # 启动"连接 → 待命 → 关闭"总任务

    async def _run(self):
        """后台任务的完整生命周期：连接 → 待命 → 关闭（2026-11-06 新增）

        为什么要把三件事放在同一个函数里？
          因为"打开连接"和"关闭连接"必须在同一个任务中完成（anyio 的硬性要求）。
          如果像以前那样只在后台连接、却由别的地方关闭，退出时就会报 cancel scope 错误。
        """
        try:  # 先连接
            await self._connect_all()
        except Exception as e:  # 连接阶段出现意外（每个服务器已经单独 try 过，这里只是兜底）
            print(f"[MCP] 初始化异常：{e}")
        finally:
            self._ready.set()  # 不管连成没连成，都要把"就绪"信号点亮，否则 get_sessions 会一直等

        try:  # 然后待命，直到有人喊停
            await self._shutdown.wait()
        except asyncio.CancelledError:  # 被强行取消（比如用户直接关窗口）
            pass  # 忽略，继续走下面的关闭流程
        finally:
            await self._close_stack()  # 关键：在当前任务里关闭，不会触发 cross-task 报错

    async def _connect_all(self):
        """连接配置文件里列出的所有服务器（原 connect_to_server，2026-11-06 改名）"""
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
        """拿到所有已连接的会话（2026-11-06 改为等"就绪信号"而不是等任务结束）"""
        self._ensure_started()  # 【2026-11-06 第 2 轮新增】万一还没启动，这里补一下
        await self._ready.wait()  # 等连接阶段结束
        # 注意：不能再写 await self.connect_server_task 了——
        #   因为那个任务现在会一直挂着待命，等它"结束"就等于死等。
        return self.sessions  # 把所有会话交出去

    @property
    def is_closing(self) -> bool:
        """是否已经在收尾（2026-11-07 第 13.1 轮新增）

        为什么需要它：退出时后台任务是被"取消"的，此时还没干完的 list_tools
        会抛一个消息为空的异常。调用方分不清"真出错了"和"正在关闭"，
        就会在退出瞬间打印一行看着像故障的提示。
        把判断依据放在这里，调用方只需要问一句"现在是不是在关"。
        """
        return self._closed or self._shutdown.is_set()

    async def aclose(self):
        """主动关闭所有 MCP 连接（2026-11-06 新增）

        调用后会：点亮"停止"信号 → 等后台任务自己把资源栈关掉 → 外部服务器进程被回收。
        这样程序退出时就不会留下孤儿进程，也不会再报 cancel scope 错误。
        """
        if self._closed:  # 已经关过了
            return
        self._ensure_started()  # 【2026-11-06 第 2 轮新增】没启动过就没什么可关的，这里补一下以防万一
        self._shutdown.set()  # 喊停
        if self.connect_server_task is None:  # 确实从来没启动过
            self._closed = True  # 直接标记为已关闭
            return
        try:
            await asyncio.wait_for(self.connect_server_task, timeout=5)  # 最多等 5 秒让它收尾
        except asyncio.TimeoutError:  # 5 秒还没收完
            self.connect_server_task.cancel()  # 强行取消
        except Exception:  # 收尾时抛错也不用管，反正要退出了
            pass

    async def _close_stack(self):
        """关闭资源栈（2026-11-06 新增，只会在 _run 任务里被调用）"""
        if self._closed:  # 防止重复关闭
            return
        self._closed = True  # 先打标记，避免嵌套调用时重复执行
        try:
            await self.exit_stack.aclose()  # 依次关闭会话和服务器进程
        except Exception as e:  # 关闭失败不该阻止程序退出
            print(f"[MCP] 关闭连接时出现异常（可忽略）：{e}")

# =============================================================================
# 备注（这段代码的几个隐患，以及本轮处理情况）
#
# 1. 【2026-11-06 已修复】配置文件不存在就崩：
#    原来 MCPConfig 找不到 ~/.thinker/mcp.json 时会抛 FileNotFoundError，
#    而这个对象在 ToolsManager 创建时就 new 出来，导致整个 CLI 起不来。
#    现在改为"打印提示 + 跳过外部工具"，内置工具照常可用。
#
# 2. 【2026-11-06 已修复】一个服务器出错会连累全部：
#    现在 for 循环里给每个服务器单独套了 try，坏的跳过、好的继续。
#
# 3. 【2026-11-06 第 2 轮已修复】退出时不清理：
#    原来 exit_stack 从来没有被 aclose()，外部服务器进程会变成"孤儿进程"，
#    退出时还会打一段 RuntimeError: Attempted to exit cancel scope in a different task。
#    现在由 _run() 这个后台任务负责"连接 → 待命 → 关闭"全程（同一个任务内），
#    并由 aclose() 触发退出。Conversation.shutdown() → ToolManager.shutdown() 会调到它。
#
# 4. 只支持 stdio：HTTP / SSE 类型的服务器在 server_config 里被直接跳过了。
# =============================================================================
