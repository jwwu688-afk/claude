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
            self._console = console or Console()  # 没有就新建一个控制台
            self.input_handler = InputHandler()  # 建输入处理器
            self.display_manager = DisplayManager(self._console)  # 建显示管理器，并把控制台传给它（共用同一个）
            self._initialized = True  # 打标记

    # Input delegation methods
    # ↓ 下面这三个方法：全是在转手，把活交给 input_handler
    async def get_user_input(self, prompt: str = "Enter: ") -> str:
        """Get user input with specified prompt.（等用户输入）"""
        return await self.input_handler.get_user_input(prompt)

    async def wait_for_user_approval(self, content: str, emoji: str = "🤖") -> tuple[bool, str]:
        """Wait for user approval with optional denial message.（等用户确认）"""
        return await self.input_handler.wait_for_user_approval(content, emoji)

    async def get_choice_input(self, prompt: str, choices: list, case_sensitive: bool = False) -> Optional[str]:
        """Get user input with predefined choices.（让用户从固定选项里选）"""
        return await self.input_handler.get_choice_input(prompt, choices, case_sensitive)

    # Display delegation methods
    # ↓ 下面这一批：全是在转手，把活交给 display_manager
    def print_assistant_message(self, content: str, emoji: str = "🤖") -> None:
        """Print assistant message with markdown formatting.（打印模型回复）"""
        self.display_manager.print_assistant_message(content, emoji)

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

        Args:
            tool_name: Name of the tool being executed（工具名）
            tool_args: Arguments passed to the tool（参数）
            success: Whether the execution was successful（是否成功）
            result: Result of the execution（执行结果）
        """
        if success:  # 如果成功
            self.print_success(f"Successfully called tool: {tool_name}, args: {tool_args}, result: {result}")
            # 打一行"成功调用 xx 工具，参数是…，结果是…"
            # ⚠️ 这里有个体验问题：如果命令输出很长（比如打印了 500 行日志），
            #    这行会刷满整个屏幕。应该截断，只显示前几百个字符。
        else:  # 如果失败
            self.print_error(f"Tool call failed: {tool_name}, error: {result}")  # 打错误信息

    def show_preparing_tool(self, tool_name: str, tool_args: dict) -> None:
        """
        Show that a tool is being prepared for execution.（显示"准备调用工具"）

        Args:
            tool_name: Name of the tool（工具名）
            tool_args: Arguments for the tool（参数）
        """
        self.print_info(f"Preparing to call tool: {tool_name}, args: {tool_args}")
        # 同样是全量打印参数，参数很长时会刷屏

    # Todo display delegation method
    def display_todos(self, todos: list, emoji: str = "📋") -> None:
        """Display todo list with formatting.（画待办清单）"""
        self.display_manager.display_todos(todos, emoji)  # 转手
