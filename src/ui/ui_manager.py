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
    async def get_user_input(self, prompt: str = None) -> str:
        """Get user input with specified prompt.（等用户输入；prompt 为 None 时用统一的 ❯）"""
        return await self.input_handler.get_user_input(prompt)

    async def wait_for_user_approval(self, content: str, emoji: str = "🤖") -> tuple[bool, str]:
        """Wait for user approval with optional denial message.（等用户确认）"""
        return await self.input_handler.wait_for_user_approval(content, emoji)

    async def get_choice_input(self, prompt: str, choices: list, case_sensitive: bool = False) -> Optional[str]:
        """Get user input with predefined choices.（让用户从固定选项里选）"""
        return await self.input_handler.get_choice_input(prompt, choices, case_sensitive)

    # Display delegation methods
    # ↓ 下面这一批：全是在转手，把活交给 display_manager
    def print_assistant_message(self, content: str, emoji: str = "", header: bool = True) -> None:
        """Print assistant message with markdown formatting.（打印模型回复）"""
        self.display_manager.print_assistant_message(content, emoji, header)

    def print_assistant_header(self) -> None:
        """只打印模型发言的起始圆点 ⏺（2026-11-06 第 4 轮新增）"""
        self.display_manager.assistant_header()

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

    def print_status(self, text: str) -> None:
        """打印状态行（2026-11-06 第 4 轮新增）

        用在每轮对话结束时显示"上下文用了多少、花了多少钱"。
        """
        self.display_manager.print_status(text)

    # Streaming methods
    # ↓ 流式显示相关，同样是转手
    def start_stream_display(self, refresh_rate: int = 10) -> None:
        """Start streaming display mode.（开始流式显示）"""
        self.display_manager.start_stream_display(refresh_rate)

    def stop_stream_display(self) -> None:
        """Stop streaming display mode.（结束流式显示）"""
        self.display_manager.stop_stream_display()

    def print_streaming_content(self, chunk: str) -> None:
        """Print streaming content chunk.（显示一小段流式内容）"""
        self.display_manager.print_streaming_content(chunk)

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
        result_preview = self.display_manager.preview_lines(result, 3, 70)  # 只取前 3 行做预览
        self.display_manager.tool_result(result_preview, success=success)

    def show_preparing_tool(self, tool_name: str, tool_args: dict) -> None:
        """
        Show that a tool is being prepared for execution.（显示"准备调用工具"）

        样式：  ⏺ cmd_runner(dir)
        """
        args_preview = self._args_preview(tool_args)  # 把参数字典压成一小段可读文字
        self.display_manager.tool_header(tool_name, args_preview)

    def _args_preview(self, tool_args) -> str:
        """把工具参数压成一行可读的摘要（2026-11-06 第 4 轮新增）

        为什么要做这件事？
          直接把字典打印出来是 cmd_runner({'command': 'dir', 'timeout': 30})，
          又长又难看，还带一堆引号和花括号。
          这里针对常见工具挑出"最能说明意图"的那个字段：
            cmd_runner  → 直接显示命令本身：dir
            task        → 显示任务描述：修复登录问题
            todo_write  → 显示条数：4 项
            其他        → 截断后的原样
        """
        if not tool_args:  # 没有参数
            return ""
        if isinstance(tool_args, dict):  # 最常见的情况：参数字典
            if tool_args.get("command"):  # 命令类工具
                return self.display_manager.shorten(tool_args["command"], 60)
            if tool_args.get("description"):  # 派活类工具
                return self.display_manager.shorten(tool_args["description"], 60)
            if isinstance(tool_args.get("todos"), list):  # 待办类工具
                return f"{len(tool_args['todos'])} 项"
        return self.display_manager.shorten(tool_args, 80)  # 兜底：整体截断

    # Todo display delegation method
    def display_todos(self, todos: list, emoji: str = "📋") -> None:
        """Display todo list with formatting.（画待办清单）"""
        self.display_manager.display_todos(todos, emoji)  # 转手
