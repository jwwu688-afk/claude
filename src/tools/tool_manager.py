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
            self.register_mcp_task = asyncio.create_task(self._register_mcp_tool())
            # 上一行：在后台开一个"注册 MCP 工具"的任务，不阻塞主流程
            # asyncio.create_task 的意思是"派个后台工人去干这件事，我先往下走"
            self._register_tool(SmartContextCropper.get_tool_name(), SmartContextCropper())  # 登记裁剪工具
            self._register_tool(TodoWrite.get_tool_name(), TodoWrite())  # 登记待办工具
            self._register_tool(Task.get_tool_name(), Task())  # 登记子任务工具
            self._register_tool(CmdRunner.get_tool_name(), CmdRunner())  # 登记命令行工具（最重要，放最后）
            ToolManager._initialized = True  # 打标记

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
                print(f"[MCP] 获取工具列表失败：{e}（已跳过该服务器）")

    def _register_tool(self, name, tool_instance):
        self.tools[name] = tool_instance  # 就是往字典里塞一条

    async def get_tools_description(self, exclude=None):
        """Return the tool schemas to put in request["tools"].（生成要发给模型的工具清单）

        Args:
            exclude: tool names to hide (None = show all). Conversation passes
                     {"task"} for sub-agents so they cannot delegate further.
            参数 exclude：不想让模型看到的工具名集合。子 agent 会把 task 排除掉。
        """
        await self.register_mcp_task  # 先等后台那个"注册 MCP 工具"的任务干完
        exclude = exclude or set()  # 如果传了 None，就换成空集合（方便后面判断"在不在里面"）
        descriptions = []  # 准备结果列表
        for tool_name, tool_instance in self.tools.items():  # 遍历字典的每一对"键和值"
            if tool_name in exclude:  # 如果在"要隐藏"的名单里
                continue  # 跳过它
            descriptions.append(tool_instance.json_schema())  # 取它的"自我介绍"，加进清单
        return descriptions  # 返回清单

    # TODO：Array out of bounds should directly throw exception again
    # ↑ 原作者的待办：这里不该把异常吃掉，应该直接抛出去（见下面）
    async def run_tool(self, tool_name, **kwargs):
        # 执行工具。**kwargs 表示"可以接收任意多个命名参数"，会打包成一个字典
        tool = self.tools.get(tool_name)  # 按名字找工具，找不到返回 None
        try:  # 试
            if tool:  # 如果找到了
                return await tool.act(**kwargs)  # 把参数拆开交给它，等它干完返回结果
        except Exception as e:  # 如果工具内部抛错
            return f"Error occurred while running tool '{tool_name}': {str(e)}"
            # 注意：这里把错误"吞"成了字符串返回，没有继续往上抛。
            # 好处是程序不会崩，坏处是上层再也没法通过异常知道出事了。
        return "Tool not found"  # 走到这儿说明工具没找到

    def get_tool_status(self, tool_name):
        # 查某个工具的"状态"（目前只有 todo_write 用得上，用来生成提醒文字）
        tool = self.tools.get(tool_name)  # 按名字找
        if tool:  # 找到了
            return tool.get_status()  # 让它自己汇报
        return "Tool not found"  # 没找到
