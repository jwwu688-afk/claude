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
import shutil  # 【第 32 轮】把配置里的裸命令（npx / python）解析成真实可执行路径
from typing import Dict, List, Optional  # 类型标注工具（Dict/List 供 /mcp 的状态快照使用）
from contextlib import AsyncExitStack  # "异步资源栈"：统一管理多个需要关闭的东西
from pathlib import Path  # 路径处理，用来判断配置文件在不在

from mcp import ClientSession, StdioServerParameters, LoggingLevel  # MCP 官方库的几个核心类
from mcp.client.stdio import stdio_client  # 专门负责"用标准输入输出方式连接"的函数

from anthropic import Anthropic  # 导入了但没用到（残留代码，可以删）
from dotenv import load_dotenv  # 读取 .env

from .server_config import MCPConfig  # 导入配置读取器
from tools.mcp_config import resolve_config_path  # 【2026-11-12】配置路径的唯一真源（与 `thinker mcp` 命令共用）

load_dotenv()  # load environment variables from .env（把配置读进来）


def resolve_command(command: str) -> str:
    """把配置里的启动命令解析成"真正能被拉起来的那个文件"（2026-11-12 第 32 轮新增）

    为什么需要它（这是实测出来的坑，不是理论问题）：
      Windows 的 CreateProcess 只会在裸命令后面自动补 `.exe`。
      而 npm 装出来的是 **`npx.cmd`**，没有 `npx.exe`。
      所以配置里写 "npx" 时，mcp 的 stdio_client 会直接抛
      `FileNotFoundError: [WinError 2] 系统找不到指定的文件`——
      表现在 /mcp 里是"连接失败"，但绝大多数人不会想到是扩展名的问题。

      shutil.which 会按 PATHEXT 依次尝试 .COM/.EXE/.BAT/.CMD，返回绝对路径；
      实测 which("npx") → C:\\Users\\...\\node\\...\\npx.CMD，且 anyio 能直接拉起它。

    为什么找不到时原样返回，而不是报错：
      命令可能确实是路径写错，也可能是"还没装、装完再启动"。
      这里只负责"能解析就解析"，把判断留给启动时的真实异常——
      那一层的报错信息更具体（能看到实际执行的命令行）。

    参数 command：配置里的 command，可能是裸名（npx）也可能是绝对路径。
    返回：解析到的路径；解析不到就返回原字符串。
    """
    return shutil.which(command) or command


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        # 【2026-11-12 第 31 轮】删掉了原来的 self.sessions（一个纯会话列表）。
        #   它只记"连上了几条"，丢了"哪一条是谁"，因此答不上"weather 连上了吗"——
        #   而这正是 /mcp 要回答的问题。现在统一由下面的 connections 承担：
        #   它是有序字典，遍历顺序仍等于配置文件顺序，原来按序处理的用法一字不用改。
        self.exit_stack = AsyncExitStack()
        # 资源栈：把所有"需要关闭的东西"（服务器进程、会话）都登记在这里，方便最后一次性全部关掉
        self.config = MCPConfig(str(self._resolve_config_path()))
        # 读配置文件。~ 会被展开成你的用户目录，所以实际路径是 C:/Users/你的名字/.thinker/mcp.json
        # 文件不存在会降级处理（见 server_config.py），不会抛异常

        # 【2026-11-12 第 31 轮】连接结果按「服务器名字」分开记，供会话内 /mcp 查看。
        #   为什么原来那个 self.sessions 不够用：
        #     它是一个纯列表，只记"连上了几条"，丢了"哪一条是谁"。
        #     而 /mcp 要回答的正是"哪个连上了、哪个没连上、为什么"，
        #     所以必须按名字建索引。两者都留着：sessions 供按序遍历（旧调用方），
        #     connections 供按名字查状态。
        self.connections: Dict[str, ClientSession] = {}  # 名字 → 会话（成功的）
        self.failures: Dict[str, str] = {}  # 名字 → 失败原因（连接/握手失败的）
        self.tool_names: Dict[str, List[str]] = {}  # 名字 → 它提供的工具名（连上之后由 ToolManager 回填）
        self.tool_errors: Dict[str, str] = {}  # 名字 → 取工具清单失败的原因（连上了但 list_tools 报错）

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
        """决定去哪个路径读 MCP 配置（2026-11-12 改为委托 tools/mcp_config.py）

        但老用户的配置文件还在旧目录里，直接切过去会导致"外部工具全部消失"。
        所以：新目录优先，新目录不存在而旧目录存在时，继续用旧的。

        【为什么搬走】`thinker mcp add/remove` 也要定位同一个文件。路径判断留在两份
        代码里，迟早出现"命令写进了 A、程序读的是 B"这种要查半天的问题。
        这里保留同名静态方法，只是不想让任何旧的调用方失效（目前只有本文件在用）。
        """
        return resolve_config_path()

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
            self._ready.set()  # 不管连成没连成，都要把"就绪"信号点亮，否则 get_connected 会一直等

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
                #   异常会一路抛到 get_connected()，导致所有外部工具都用不了，
                #   甚至让程序启动阶段就报错。现在改成"坏的跳过，好的继续"。
                server_params = StdioServerParameters(
                    # 【第 32 轮】先解析成真实路径再启动：Windows 上 "npx" 会被
                    #   CreateProcess 补成 npx.exe（不存在），而 npm 装的是 npx.cmd。
                    #   不解析的话，配置一字不差也会连接失败。详见 resolve_command 的注释。
                    command=resolve_command(server.command),  # 用哪个程序来启动它
                    args=server.args,  # 启动参数（比如要运行哪个 .py 文件）
                    env=None  # 环境变量：None 表示直接继承当前程序的环境变量
                )

                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                # 上面这行做两件事：
                #   1. 真的去启动那个外部程序，并建立输入输出通道，就是 stdin/stdout 通道
                #   2. 把这个通道登记到资源栈里，以后统一关闭
                # stdio, write 是通道的两端：一个用来读，一个用来写
                stdio, write = stdio_transport  # 把返回的两个对象分别取出来
                session = await self.exit_stack.enter_async_context(ClientSession(stdio, write))
                # 在通道上建立一个"会话"，同样登记到资源栈
                # ClientSession 是 MCP 协议的会话层，包装上面的读写通道，提供 initialize()、call_tool()、list_tools() 等方法

                await session.initialize()  # 握手：双方交换版本和能力信息（必须做，不然对方不认你）
                self.connections[name] = session  # 【第 31 轮】按名字存，/mcp 靠它判断"这个连上了"
            except Exception as e:  # 这个服务器连接失败
                print(f"[MCP] 连接服务器 '{name}' 失败：{e}（已跳过，其他工具不受影响）")
                # 打印原因后继续下一个，不中断整体流程
                # 【第 31 轮】把原因也记下来：启动时那一行滚过去就没了，
                # 用户回头看 /mcp 时得能看到"为什么这个没连上"。
                # 带上异常类型：有些异常（比如被取消）str 是空的，只留 {e} 会得到一行空白。
                self.failures[name] = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__

    # 【2026-11-12 第 31 轮】这里原来有一个 get_sessions()，返回纯会话列表。
    #   现在删掉了：调用方（ToolManager）拿不到每条会话对应的服务器名字，
    #   也就没法把"这个服务器提供了几个工具"记到 /mcp 能读到的地方。
    #   职责已并入下面的 get_connected()——它返回「名字 → 会话」，同样能按序遍历。

    # -------------------------------------------------------------------------
    # 【2026-11-12 第 31 轮】给会话内 /mcp 用的状态查询。
    #   和 get_connected 的区别：那个会 await 就绪信号（可能等几秒），
    #   而 /mcp 是用户敲一下就要立刻有输出，所以下面的快照方法一律不等待、
    #   不阻塞，连到哪儿就显示到哪儿。

    @property
    def is_ready(self) -> bool:
        """连接阶段是否已经结束（成功和失败都算结束）

        为什么单独暴露这个：status_snapshot 需要区分
          「还在连」和「连完了但没这条记录」——
        前者该显示"连接中…"，后者意味着有 bug。
        """
        return self._ready.is_set()

    async def get_connected(self) -> Dict[str, ClientSession]:
        """等连接阶段结束后，返回 名字 → 会话 的映射（第 31 轮新增）

        ToolManager 改用这个方法而不是按序拿会话列表：
          它要把每个服务器提供的工具记到**名字**名下（note_tools），
          光有会话列表不知道哪个会话对应哪个服务器，就记不成。
        """
        self._ensure_started()
        await self._ready.wait()
        return self.connections

    def note_tools(self, name: str, tool_names: List[str]) -> None:
        """登记某个服务器提供了哪些工具（ToolManager 在 list_tools 成功后调用）

        为什么要由 ToolManager 回填、客户端不自己查：
          客户端只负责"连"，list_tools 的异常处理（单条失败不连累全部）
          已经在 ToolManager 里写好了。在这里再查一遍等于两处逻辑两套错误文案。
        """
        self.tool_names[name] = list(tool_names)
        self.tool_errors.pop(name, None)  # 这次成功，清掉可能残留的失败痕迹

    def note_tool_error(self, name: str, reason: str) -> None:
        """登记「连上了，但工具清单拿不到」

        这种情况最容易被误判成"服务器没问题"：连接是通的，可工具一个都没出现。
        /mcp 必须把它和"连不上"分开显示，否则用户会去查服务器启动参数，
        而真正的原因在 list_tools（比如服务器实现了握手但没实现工具列举）。
        """
        self.tool_errors[name] = reason

    def status_snapshot(self) -> List[Dict[str, str]]:
        """把「每个服务器现在什么状态」整理成一张表（第 31 轮新增）

        返回：[{"name": 名字, "state": 状态, "detail": 给人看的一句话}]

        排序：配置里正常的在前（保持文件里的顺序），被跳过的排在最后。
          为什么把跳过项放后面：它们是"配了但本版本用不了"的，属于附录性质；
          混在正常项里会让用户以为它们也在连。

        状态枚举（/mcp 按它选符号）：
          connected  —— 已连接。detail 里带工具数，或"正在取工具清单"
          failed     —— 连接或握手失败，detail 是异常
          connecting —— 连接阶段还没结束，这一条还没轮到 / 还没出结果
          idle       —— 连接阶段结束了，却既没连上也没失败（理论上不该出现，出现了就是 bug）
          skipped    —— 在配置加载阶段就被跳过（非 stdio、缺 command 等）
        """
        items: List[Dict[str, str]] = []

        for name in self.config.servers:  # 只遍历"有效配置"，顺序就是文件里的顺序
            if name in self.connections:
                tools = self.tool_names.get(name)  # None = ToolManager 还没跑完 list_tools
                if name in self.tool_errors:
                    detail = f"已连接，但取工具清单失败 · {self.tool_errors[name]}"
                elif tools is None:
                    detail = "已连接，正在取工具清单…"
                else:
                    # 0 个工具也要说清楚：那不是故障，是这个服务器确实只提供了资源/提示词
                    detail = f"{len(tools)} 个工具" if tools else "没有提供工具"
                items.append({"name": name, "state": "connected", "detail": detail})
            elif name in self.failures:
                items.append({"name": name, "state": "failed", "detail": self.failures[name]})
            elif not self._ready.is_set():
                items.append({"name": name, "state": "connecting", "detail": "连接中…"})
            else:
                items.append({"name": name, "state": "idle", "detail": "没有连接，也没有失败记录"})

        for name, reason in self.config.skipped.items():  # 被配置层丢掉的，附在最后
            items.append({"name": name, "state": "skipped", "detail": reason})

        return items

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
