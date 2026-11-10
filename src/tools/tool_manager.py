# =============================================================================
# 文件：src/tools/tool_manager.py
# 作用：工具的"登记处 + 调度台"。
#   1. 程序启动时，把所有内置工具登记到 self.tools 这个字典里
#   2. 同时去连 MCP 服务器（外部工具），把它们提供的工具也登记进来
#   3. 模型要用工具时，按名字找到对应的工具对象，让它去干活
#
# 一个"工具"本质上就是一个类，只要满足两点：
#   - 有个 act() 方法，负责真正干活
#   - 有个 json_schema() 方法，负责告诉模型"我叫什么、能干什么、要什么参数"
# =============================================================================

import asyncio  # 用来创建"后台任务"（连 MCP 服务器比较慢，不能卡住启动）
from tools.cmd_runner import CmdRunner  # 导入命令执行工具
from tools.file_tools import FileRead, FileWrite, FileEdit, FileGlob, FileGrep  # 【第 8 轮】五个文件专用工具
from tools.mcp_tool import McpTool  # 导入"MCP 工具包装器"（把外部工具包成我们能用的样子）
from tools.smart_context_cropper import SmartContextCropper  # 导入上下文裁剪工具
from tools.task import Task  # 导入子任务工具（让模型能派活）
from tools.todo_write import TodoWrite  # 导入待办清单工具
from .mcp_client.client import MCPClient  # 导入 MCP 客户端（负责连外部服务器）


class ToolManager:
    # 工具管理器。同样是单例，全程序共用一个
    _instance = None  # 唯一实例
    _initialized = False  # 是否已初始化
    _mcp_client = None  # MCP 客户端（类属性，所以所有实例共享）

    def __new__(cls):
        if cls._instance is None:  # 第一次创建
            cls._instance = super(ToolManager, cls).__new__(cls)
            cls._mcp_client = MCPClient()  # 顺手创建 MCP 客户端（它内部会开始连服务器）
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.tools = {}  # 字典：键是工具名，值是工具对象
            # Important tools should be placed lower, as this affects their position in the prompt.
            # 翻译：越重要的工具越晚登记（因为字典保持插入顺序，
            # 后面登记的工具在给模型的清单里排得越靠后，位置越显眼）
            self.register_mcp_task = None
            # 【2026-11-06 第 2 轮优化】先留空，等真正需要工具清单时再启动后台注册任务。
            #   原因同 MCPClient：asyncio.create_task 必须在"正在运行的事件循环"里调用，
            #   在循环外创建 ToolManager 会抛 RuntimeError: no running event loop。
            self._ensure_mcp_registration()  # 有循环就顺手启动
            self._register_tool(SmartContextCropper.get_tool_name(), SmartContextCropper())  # 登记裁剪工具
            self._register_tool(TodoWrite.get_tool_name(), TodoWrite())  # 登记待办工具
            self._register_tool(Task.get_tool_name(), Task())  # 登记子任务工具
            # 【2026-11-10 第 21 轮】技能工具**按需**登记：磁盘上一个技能都没有时不登记。
            #   为什么不像别的工具那样无条件登记：
            #     工具的 description 字段每一轮请求都要发，而 skill 的说明书里
            #     还嵌着「可用技能」清单。没有技能时那份说明书纯属白烧 token
            #     （约 400 token/轮），模型还会去调一个永远返回"没有技能"的工具。
            #   代价：/skills reload 之后新出现的技能要把工具补登记进去，
            #     那会让 tools 清单变一次、前缀缓存失效一次——一次性代价，
            #     由 refresh_skill_tool() 负责，已在该方法里写明。
            self.refresh_skill_tool()
            self._register_tool(CmdRunner.get_tool_name(), CmdRunner())  # 命令行工具：跑命令、跑测试、git 操作
            # 【2026-11-06 第 8 轮】五个文件工具排在最后登记。
            #   按上面"越重要越晚登记"的约定：现在读/改文件是最高频的操作，
            #   它们应该比 cmd_runner 更靠后、在给模型的清单里更显眼。
            #   顺序按"读 → 写 → 改 → 找文件 → 搜内容"排，和模型干活的思路一致。
            self._register_tool(FileRead.get_tool_name(), FileRead())  # 读文件（带行号）
            self._register_tool(FileWrite.get_tool_name(), FileWrite())  # 新建 / 整体覆盖文件
            self._register_tool(FileEdit.get_tool_name(), FileEdit())  # 精确替换一段（带 diff）
            self._register_tool(FileGlob.get_tool_name(), FileGlob())  # 按文件名找文件
            self._register_tool(FileGrep.get_tool_name(), FileGrep())  # 按内容搜索
            ToolManager._initialized = True  # 打标记

    def _ensure_mcp_registration(self):
        """确保"后台注册 MCP 工具"的任务已经启动（2026-11-06 第 2 轮新增，可重复调用）"""
        if self.register_mcp_task is not None:  # 已经启动过
            return
        try:
            asyncio.get_running_loop()  # 看看现在有没有事件循环
        except RuntimeError:  # 没有，就先不启动
            return
        self.register_mcp_task = asyncio.create_task(self._register_mcp_tool())

    async def _register_mcp_tool(self):
        # 这个方法是后台跑的：连上所有 MCP 服务器，把它们提供的工具都登记进来
        sessions = await self._mcp_client.get_sessions()  # 等所有服务器连接完成，拿到会话列表
        for session in sessions:  # 一个个服务器
            try:  # 【2026-11-06 优化】单个服务器"问工具清单"失败时不要连累其他服务器
                #   比如某个服务器启动成功但 list_tools 报错，
                #   原来会直接抛出异常，导致整个工具清单都拿不到。
                response = await session.list_tools()  # 问它："你都有什么工具？"
                tools = response.tools  # 拿到工具清单
                for tool in tools:  # 遍历每一个工具
                    mcp_tool = McpTool(tool, session)  # 包装成我们内部能用的工具对象
                    self._register_tool(tool.name, mcp_tool)  # 用工具原名登记
            except Exception as e:  # 这个服务器出问题了
                # 【2026-11-07 第 13.1 轮】退出瞬间的失败不是失败。
                #   程序收尾时后台任务被取消，没跑完的 list_tools 会抛一个 str 为空的异常，
                #   原来会打印成"[MCP] 获取工具列表失败：（已跳过该服务器）"——
                #   冒号后面什么都没有，看起来像故障，实际只是正在退出。
                #   真正的失败也补上异常类型：原来只有 {e}，遇到空消息就完全没线索。
                if self._mcp_client.is_closing:
                    return
                print(f"[MCP] 获取工具列表失败：{type(e).__name__}: {e}（已跳过该服务器）")

    def _register_tool(self, name, tool_instance):
        self.tools[name] = tool_instance  # 就是往字典里塞一条

    def refresh_skill_tool(self) -> bool:
        """按「磁盘上有没有技能」登记或摘掉 skill 工具（2026-11-10 第 21 轮新增）

        返回值：True 表示这次**新登记**了 skill 工具。

        为什么需要这个方法，而不是在 __init__ 里判断一次就完事：
          /skills reload 会重新扫目录。启动时 0 个技能、之后用户新建了一个，
          此时工具表里还没有 skill——不补登记的话，用户会遇到
          "技能列表里看得见、模型却调不动"这种自相矛盾的状态。

        为什么补登记要专门说一句"代价"：
          工具清单在提示词缓存的**前缀**里。清单从 9 个变 10 个，前缀变了，
          下一轮缓存命中归零、按原价计费一次。这是一次性代价，
          但改这块的人必须知道它存在，否则会把"reload 之后突然贵了一下"当 bug 查。

        摘掉那一路（0 个技能）现在其实是空转：reload 只能让技能变多或变少，
          真删到 0 个时工具留在表里也只是"说明书里写着没有可用技能"。
          但留着这一行是有意的——它让"工具表 = 技能数量"这个不变量成立，
          以后别处想直接改工具表时，先看到这里有一条现成的收口。
        """
        # 局部 import：skill 子包会读 core.limits，而 core.commands 又可能回调本模块拿工具表。
        # 放文件头会让"工具管理器 → 技能 → 命令"这条链在导入期成形，以后加东西容易成环。
        from tools.skill.skill_manager import SkillManager
        from tools.skill_tool import Skill

        try:
            has_skill = SkillManager().count() > 0
        except Exception as error:
            # 技能是**附加功能**：目录读不了（权限、编码、盘符被拔）时，
            # 用户的核心诉求"跟模型对话"完全不受影响，为此让启动崩掉是本末倒置。
            # 但也绝不能静默——静默降级等于把 bug 藏起来（第 16.1 轮的教训），
            # 所以往 stderr 打一行，带上异常类型。
            print(f"[skills] 扫描技能失败（本次不登记 skill 工具）：{type(error).__name__}: {error}")
            return False

        if has_skill and Skill.get_tool_name() not in self.tools:
            self._register_tool(Skill.get_tool_name(), Skill())
            return True
        if not has_skill:
            self.tools.pop(Skill.get_tool_name(), None)
        return False

    async def get_tools_description(self, exclude=None, allow=None):
        """Return the tool schemas to put in request["tools"].（生成要发给模型的工具清单）

        Args:
            exclude: tool names to hide (None = show all). Conversation passes
                     {"task"} for sub-agents so they cannot delegate further.
            allow: tool names to KEEP (None = 不限制). 子 agent 的类型化白名单从这里生效。
                   为什么需要它：改前只有 exclude（黑名单），子 agent 只能"被拿走 task"，
                   其余工具全给——想做一个只读的 explore agent 根本无从下手。
        参数 exclude：不想让模型看到的工具名集合。子 agent 会把 task 排除掉。
        参数 allow：【第 18 轮】白名单。None 表示不限制；给了集合就只发集合里的。
                allow 和 exclude 同时生效（先过白名单、再剔黑名单），
                这样"给 explore 三个只读工具"和"任何子 agent 都不给 task"
                这两条规则可以各管各的，不用在调用处手工求差集。
        """
        self._ensure_mcp_registration()  # 【2026-11-06 第 2 轮新增】确保后台注册任务已启动
        if self.register_mcp_task is not None:  # 启动了才需要等
            await self.register_mcp_task  # 先等后台那个"注册 MCP 工具"的任务干完
        exclude = exclude or set()  # 如果传了 None，就换成空集合（方便后面判断"在不在里面"）
        allow = None if allow is None else set(allow)  # None 保持 None，表示"不限制"
        descriptions = []  # 准备结果列表
        for tool_name, tool_instance in self.tools.items():  # 遍历字典的每一对"键和值"
            if allow is not None and tool_name not in allow:  # 不在白名单里
                continue  # 【第 18 轮】不给它，从源头掐断（比事后拦更可靠）
            if tool_name in exclude:  # 如果在"要隐藏"的名单里
                continue  # 跳过它
            descriptions.append(tool_instance.json_schema())  # 取它的"自我介绍"，加进清单
        return descriptions  # 返回清单

    async def run_tool(self, tool_name, **kwargs):
        """执行一个工具，出错直接往上抛（2026-11-07 第 13.1 轮）

        【改前】这里套了一层 try/except，把任何异常都转成一句
        "运行工具 'xxx' 时出错：……" 返回。看着"更稳"，实际有三个问题：

          1. 调用方 `Conversation._execute_tool` 自己也有一层 try/except，
             但异常在这里就被吞掉了，那一层永远走不到 —— 它写好的失败分支是死代码。
          2. **界面永远显示成功**：`_execute_tool` 只有进了 except 才会传
             `success=False`，所以哪怕工具炸了，用户看到的仍是绿色的成功结果。
          3. 模型拿到的只有一句"出错"，没有异常类型，很难判断该换参数还是换工具。

        这个项目是教学向的，"工具失败 = 给模型一条失败消息"是主线之一
        （见 `Conversation._execute_tool` 的注释），而这层吞异常恰好把主线掩盖了。

        【改后】这里只负责"找工具 → 调用"，不负责兜错：
          - 异常交给 `Conversation._execute_tool` 统一处理，
            它会同时做两件事：界面标红 + 把失败原因写进历史让模型自我纠正；
          - 找不到工具抛 KeyError 而不是返回一句中文，理由同上——
            调用方需要知道"这是失败"，而不是拿到一句长得像正常结果的话。

        **kwargs 表示"可以接收任意多个命名参数"，会打包成一个字典传下去。
        """
        tool = self.tools.get(tool_name)  # 按名字找工具，找不到返回 None
        if tool is None:  # 找不到就直接抛，不再伪装成"正常结果"
            available = "、".join(sorted(self.tools)) or "（无）"
            # 把可用清单带回给模型：它下次就会照着清单挑，而不是继续猜名字
            raise KeyError(f"找不到工具 '{tool_name}'。当前可用：{available}")
        return await tool.act(**kwargs)  # 把参数拆开交给它，等它干完返回结果

    def get_tool_status(self, tool_name):
        # 查某个工具的"状态"（目前只有 todo_write 用得上，用来生成提醒文字）
        tool = self.tools.get(tool_name)  # 按名字找
        if tool:  # 找到了
            return tool.get_status()  # 让它自己汇报
        return "找不到这个工具。"  # 没找到

    def is_tool_repeatable(self, tool_name):
        """判断一个工具的结果能否安全折叠成「重跑即可」（2026-11-09 五层压缩第 3 批新增）

        供 HistoryManager 的 L3 Microcompact 调用：压缩时只折叠"可重复查询"工具的结果。
        找不到工具（比如 MCP 工具还没注册完、或已经下线）时返回 False，走保守路径。

        为什么放在这里而不是让 HistoryManager 直接读工具类？
          HistoryManager 不该依赖 ToolManager（会循环依赖），所以这里提供一个
          独立的查询入口，由 Conversation 把它"注入"给 HistoryManager（见 set_repeatable_checker）。
        """
        tool = self.tools.get(tool_name)  # 按名字找
        if tool is None:  # 找不到
            return False  # 保守：未知工具按"不可折叠"处理，宁可多保留
        return bool(getattr(tool, "is_repeatable", False))  # 读工具的 is_repeatable 属性

    async def shutdown(self):
        """关闭所有外部资源（2026-11-06 第 2 轮新增）

        目前主要就是关掉 MCP 的那些外部服务器进程。
        程序退出前调用它，可以避免留下孤儿进程、避免退出时报 cancel scope 错误。
        """
        try:
            await self._mcp_client.aclose()  # 交给 MCP 客户端自己收尾
        except Exception as e:  # 收尾失败不该阻止程序退出
            print(f"[MCP] 关闭外部工具时出现异常（可忽略）：{e}")
