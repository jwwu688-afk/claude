# =============================================================================
# 文件：src/ui/ui_manager.py
# 作用：界面的"总前台"。程序其他部分只跟它打交道，不直接碰输入和显示。
#
# 为什么要有这一层？
#   因为"输入"和"显示"是两件不同的事，分给了 InputHandler 和 DisplayManager。
#   但对话逻辑不该关心这些细节，它只想说"给我一句话"或"把这段显示出来"。
#   UIManager 就是那个"接单的前台"：接到单，转给对应的部门。
#   这种写法叫"外观模式（Facade）"——用一个简单接口包住一堆复杂的东西。
#
# 下面绝大部分方法都只有一行，就是纯粹转手，看到别怀疑，它就是转手。
# =============================================================================

"""
Main UI manager that coordinates input and display operations.（协调输入与显示的主界面管理器）
"""

from typing import Optional  # 类型标注
from rich.console import Console  # rich 的控制台对象
from rich.text import Text  # 【第 33 轮】/mode 列表要按模式上色，需要富文本
from .input_handler import InputHandler  # 导入输入处理器
from .display_manager import DisplayManager  # 导入显示管理器
from . import theme  # 【2026-11-06 第 4 轮】统一配色与符号


class UIManager:
    """
    Main UI manager that provides a unified interface for all UI operations.
    （提供统一的界面操作接口）
    This class coordinates between input handling and display management.
    （它负责协调输入和显示）
    """

    _instance: Optional['UIManager'] = None  # 单例：唯一实例
    _initialized: bool = False  # 是否已初始化

    def __new__(cls) -> 'UIManager':
        """Singleton pattern implementation.（单例模式）"""
        if cls._instance is None:  # 第一次
            cls._instance = super(UIManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, console: Optional[Console] = None):
        """
        Initialize the UI manager with input handler and display manager.（把两个部门建起来）

        Args:
            console: Rich console instance, creates new one if None（控制台对象）
        """
        if not self._initialized:  # 只初始化一次
            self._console = console or theme.console  # 没传就用全局那一个（保证配色统一）
            self.input_handler = InputHandler()  # 建输入处理器
            self.display_manager = DisplayManager(self._console)  # 建显示管理器，并把控制台传给它（共用同一个）
            self._initialized = True  # 打标记

    # Input delegation methods
    # ↓ 下面这三个方法：全是在转手，把活交给 input_handler
    async def get_user_input(self, prompt: str = None, completer=None) -> str:
        """Get user input with specified prompt.（等用户输入；prompt 为 None 时用统一的 ❯）

        参数 completer（2026-11-08 第 16 轮新增）：Tab 补全的候选来源，
        由调用方（Conversation）注入，UI 层自己不依赖 core.commands——
        否则 ui 就反过来依赖 core 了，以后想单独测 UI 会很别扭。
        传 None 就是原来的行为，一行都不走自管模式。
        """
        return await self.input_handler.get_user_input(prompt, completer)

    def set_mode_state(self, mode: str) -> None:
        """把当前模式告诉输入层（2026-11-12 第 33 轮新增；33.3 轮去掉 refresh）

        为什么由 UIManager 转一手、而不是让 Conversation 直接摸 input_handler：
          项目一直守着"其他模块只跟门面打交道"这条约定（见 get_numbered_choice 的说明）。
          多一行转发，换来的是以后换输入实现时只需要改这里。

        为什么没有 refresh 参数：底栏不再显示模式（模式归输入行上方那条状态行），
        "模式变了要重画底栏"这件事不存在了。
        """
        self.input_handler.set_mode_state(mode)

    def print_mode_status(self, mode: str, text: str) -> None:
        """用当前档位的颜色打一行模式状态（2026-11-13 第 33.3 轮新增）

        谁会用：`/mode` 切换成功后。快捷键那条路自己会把它画在输入行上方，
        而命令这条路输入行已经结束，需要在这里补一行，否则用户敲完 /mode plan
        屏幕上什么变化都看不到。

        格式与 LineEditor 里那条保持一致（`▸▸ 计划模式 · 只读，用来出方案`）：
        同一个状态在两个入口有两种长相，是最容易让人觉得"这不是一个功能"的细节。
        """
        self._console.print(Text("  " + text, style=theme.mode_color(mode)))

    def set_mode_cycler(self, cycler) -> None:
        """注入快捷键切换模式的回调（2026-11-12 第 33 轮新增）

        cycler 的签名是 () -> (bool, str)，由 Conversation 提供。
        UI 层不反向依赖核心层，所以只能这样注入，不能自己 new 一个 Conversation。
        """
        self.input_handler.set_mode_cycler(cycler)

    def print_mode_line(self, mode: str, text: str) -> None:
        """用某一档模式的颜色打一行（2026-11-12 第 33 轮新增）

        谁会用：/mode 的列表。四档各占一行，用各自颜色显示，
        用户一眼就能把"名字"和"底栏那个颜色"对上号。
        为什么不做成通用的 print_colored：那等于给 UI 层开一个"随便传样式"的口子，
        迟早出现七八种散落的自定义颜色；只开这一个窄接口，配色仍旧只有 theme 一处。
        """
        self._console.print(Text(f"  ● {text}", style=theme.mode_color(mode)))

    async def wait_for_user_approval(self, question: str = "是否执行？", default_yes: bool = True) -> tuple[bool, str]:
        """问一次"要不要执行"，等用户回车或给一句反馈（2026-11-06 第 8.2 轮重写）

        三件事按顺序做：
          1. 让显示层画问题（⏺ 头部上面已经画过了，这里只画 ? 那一行）
          2. 让输入层读一行
          3. 把这一行解释成（是否执行, 拒绝理由）

        改前的用法是传一整句 "Tool: task, args: {...}"，界面上会把整个参数字典
        原样铺出来，长参数直接占满一屏。现在参数由 format_tool_call 压成
        "cmd_runner(dir)" 这种一行摘要，问题本身只问"是否执行"。
        """
        self.display_manager.approval_question(question, default_yes)  # 画问题 + 提示
        return await self.input_handler.wait_for_user_approval(default_yes)  # 读一行并解释

    def format_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """把一次工具调用压成一行可读文字（2026-11-06 第 8.2 轮新增）

        返回形如 "cmd_runner(dir)"、"task(修复登录问题)"。
        批准提问、日志、拒绝提示都复用这一个格式，屏幕上不会三种写法并存。
        """
        preview = self._args_preview(tool_args)  # 复用已有的"挑最关键字段"逻辑
        return f"{tool_name}({preview})" if preview else tool_name

    def show_tool_denied(self, reason: str = "") -> None:
        """显示"这次工具调用被拒绝了"（2026-11-06 第 8.2 轮新增）"""
        self.display_manager.tool_denied(reason)

    async def get_choice_input(self, prompt: str, choices: list, case_sensitive: bool = False) -> Optional[str]:
        """Get user input with predefined choices.（让用户从固定选项里选）"""
        return await self.input_handler.get_choice_input(prompt, choices, case_sensitive)

    async def get_numbered_choice(self, title: str, items: list) -> Optional[int]:
        """列编号清单，返回用户选中的下标（2026-11-07 第 14 轮新增）

        /resume 用它选会话。只在 UIManager 这里转一手、不直接调 InputHandler，
        是为了维持"其他模块只跟门面打交道"这条约定——
        不然每个想画清单的地方都要自己 import InputHandler。
        """
        return await self.input_handler.get_numbered_choice(title, items)

    # Display delegation methods
    # ↓ 下面这一批：全是在转手，把活交给 display_manager
    def print_assistant_message(self, content: str, emoji: str = "", header: bool = True) -> None:
        """Print assistant message with markdown formatting.（打印模型回复）"""
        self.display_manager.print_assistant_message(content, emoji, header)

    def print_simple_message(self, message: str, emoji: str = "") -> None:
        """Print simple message without markdown formatting.（打印纯文本）"""
        self.display_manager.print_simple_message(message, emoji)

    def print_error(self, error_message: str, emoji: str = "❌") -> None:
        """Print error message.（打印错误）"""
        self.display_manager.print_error(error_message, emoji)

    def print_success(self, success_message: str, emoji: str = "") -> None:
        """Print success message.（打印成功）"""
        self.display_manager.print_success(success_message, emoji)

    def print_info(self, info_message: str, emoji: str = "") -> None:
        """Print info message.（打印提示）"""
        self.display_manager.print_info(info_message, emoji)

    def print_warning(self, warning_message: str) -> None:
        """打印警告（2026-11-06 第 9 轮新增）

        用在"高危工具调用"确认之前：告诉用户这次操作危险在哪、
        以及"回车等于拒绝"。和 print_error 分开，是因为此时还没出事。
        """
        self.display_manager.print_warning(warning_message)

    def print_status(self, text: str, level: str = "info") -> None:
        """打印状态行（2026-11-06 第 4 轮新增，第 5 轮加了颜色分级）

        用在每轮对话结束时显示"上下文用了多少、花了多少钱"。
        level 决定颜色：info 暗灰 / warn 黄 / err 红。
        """
        self.display_manager.print_status(text, level)

    # Streaming methods
    # ↓ 流式显示相关，同样是转手
    def start_stream_display(self, refresh_rate: int = 10) -> None:
        """Start streaming display mode.（开始流式显示）"""
        self.display_manager.start_stream_display(refresh_rate)

    def stop_stream_display(self, show_done: bool = True) -> None:
        """Stop streaming display mode.（结束流式显示）

        参数 show_done（第 10 轮新增）：异常路径传 False，避免在报错信息后面
        再补一行 `✻ Cooked for Ns`——那时候根本没正常收尾，打这行只会误导。
        """
        self.display_manager.stop_stream_display(show_done)

    def print_streaming_content(self, chunk: str) -> None:
        """Print streaming content chunk.（显示一小段流式内容）"""
        self.display_manager.print_streaming_content(chunk)

    def note_thinking(self, chars: int) -> None:
        """告诉界面"模型又思考了一段"（2026-11-11 第 28 轮新增）

        只传字数、不传内容：界面要的是"它还在动"这个信号，
        思考原文既不进屏幕也不进历史。
        """
        self.display_manager.note_thinking(chars)

    def note_tool_call(self, chars: int) -> None:
        """告诉界面"模型正在生成工具参数"（2026-11-11 第 30 轮新增）

        和 note_thinking 是两件事：后者发生在正文出现之前，由等待段那一行
        spinner 接管；前者发生在正文出现之后，此时 spinner 已被停掉，
        界面得在正文下方另起一行画心跳，否则那段静默看起来就是死机。

        这里仍然只传字数、不传参数内容——参数里可能有文件内容或路径，
        既不该上屏，也不该进历史。
        """
        self.display_manager.note_tool_call(chars)

    def get_stream_buffer(self) -> str:
        """Get current stream buffer content.（取出缓冲区内容）"""
        return self.display_manager.get_stream_buffer()

    def clear_stream_buffer(self) -> None:
        """Clear the stream buffer.（清空缓冲区）"""
        self.display_manager.clear_stream_buffer()

    # Convenience methods that combine input and display
    # ↓ 这两个方法稍微多做了一点点事：把"输入"和"显示"组合起来
    async def confirm_action(self, action_description: str) -> tuple[bool, str]:
        """
        Show action description and ask for confirmation.（显示要做什么，然后等用户确认）

        Args:
            action_description: Description of the action to confirm（操作说明）

        Returns:
            Tuple of (approval_status, denial_message)（要不要执行 + 拒绝理由）
        """
        return await self.wait_for_user_approval(action_description)  # 直接转手给"等确认"方法

    def show_tool_execution(self, tool_name: str, tool_args: dict, success: bool = True, result: str = "") -> None:
        """
        Display tool execution information.（显示工具执行的结果）

        【2026-11-06 第 4 轮】改成 Claude Code 那种两行式：
            ⏺ cmd_runner(dir)      ← 由 show_preparing_tool 打印
              ⎿  a.py b.py         ← 这里只打印结果
        参数不再重复显示（上面那行已经带过了）。
        """
        # 只取前 3 行做预览。
        # 【第 26 轮】第三个参数不再传死值 70：行宽由 preview_lines 按当前终端宽度
        #   自动计算（控制台列数 - 7），窗口宽窄变化时不会再出现"预览比屏幕还宽、
        #   被终端硬折行、行号散架"的情况。
        result_preview = self.display_manager.preview_lines(result, 3)
        self.display_manager.tool_result(result_preview, success=success)

    def show_preparing_tool(self, tool_name: str, tool_args: dict) -> None:
        """
        Show that a tool is being prepared for execution.（显示"准备调用工具"）

        样式：  ⏺ cmd_runner(dir)
        """
        args_preview = self._args_preview(tool_args)  # 把参数字典压成一小段可读文字
        self.display_manager.tool_header(tool_name, args_preview)

    def _args_preview(self, tool_args) -> str:
        """把工具参数压成一行可读的摘要（2026-11-06 第 4 轮新增，第 8.2 轮补齐文件类工具）

        为什么要做这件事？
          直接把字典打印出来是 cmd_runner({'command': 'dir', 'timeout': 30})，
          又长又难看，还带一堆引号和花括号。
          这里针对常见工具挑出"最能说明意图"的那个字段：
            cmd_runner  → 直接显示命令本身：dir
            task        → 显示任务描述：修复登录问题
            file_*      → 显示路径或匹配模式：src/main.py、**/*.py
            todo_write  → 显示条数：4 项
            其他        → 截断后的原样

        【第 8.2 轮】补了 path / pattern 两类。
          不补的话，第 8 轮新增的五个文件工具在审批时是这样：
              ? 执行 file_edit({'path': 'a.py', 'old_string': 'hi', 'new_string': 'bye'})？
          模型可能塞进几百行 new_string，整屏都是转义引号。
          现在只显示 file_edit(a.py)——看路径就够判断该不该放行了。
        """
        if not tool_args:  # 没有参数
            return ""
        if isinstance(tool_args, dict):  # 最常见的情况：参数字典
            if tool_args.get("command"):  # 命令类工具
                return self.display_manager.shorten(tool_args["command"], 60)
            if tool_args.get("description"):  # 派活类工具
                return self.display_manager.shorten(tool_args["description"], 60)
            if tool_args.get("pattern"):  # 搜索类工具（file_grep / file_glob）
                return self.display_manager.shorten(tool_args["pattern"], 60)
            if tool_args.get("path"):  # 文件类工具（file_read / file_write / file_edit）
                return self.display_manager.shorten(tool_args["path"], 60)
            if isinstance(tool_args.get("todos"), list):  # 待办类工具
                return f"{len(tool_args['todos'])} 项"
        return self.display_manager.shorten(tool_args, 80)  # 兜底：整体截断

    # Todo display delegation method
    def display_todos(self, todos: list, emoji: str = "📋") -> None:
        """Display todo list with formatting.（画待办清单）"""
        self.display_manager.display_todos(todos, emoji)  # 转手
