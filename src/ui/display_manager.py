# =============================================================================
# 文件：src/ui/display_manager.py
# 作用：管"往屏幕上打东西"。所有你看到的文字、表格、清单，都是这里画出来的。
#
# 用到的库叫 rich，它能让终端显示得更漂亮：
#   - Markdown：把 **粗体**、`代码`、列表这些东西渲染成好看的样子
#   - Live：可以"原地不断刷新"一块区域，这就是流式输出的效果
#     （模型吐一个字，就把整段重画一遍，看起来像在实时打字）
#
# 这个文件里的方法分四类：
#   1. 打印消息（assistant / simple / error / success / info）
#   2. 流式显示（start / stop / print）
#   3. 画待办清单（display_todos）
#   4. 内部小工具（_get_status_icon）
# =============================================================================

"""
Display management functionality for outputting content to users.（负责往屏幕输出内容）
"""

from rich.console import Console  # rich 库的"控制台"对象，是打印的入口
from rich.markdown import Markdown  # 把 markdown 文本变成带格式的漂亮输出
from rich.live import Live  # "实时刷新"组件，流式输出的核心
from typing import Optional  # 类型标注：表示"可能是这个，也可能是 None"


class DisplayManager:
    """Manages all display operations including streaming content.（管理所有显示操作，包括流式内容）"""

    def __init__(self, console: Optional[Console] = None):
        """
        Initialize the display manager.（初始化）

        Args:
            console: Rich console instance, creates new one if None（控制台对象，不传就新建一个）
        """
        self._console = console or Console()  # 如果传了就用传的，没传就新建（a or b 是"a 为空就用 b"）
        self._live: Optional[Live] = None  # 存放流式显示对象，一开始没有
        self._stream_buffer: str = ""  # 存放已经收到但还没显示完的文字

    def print_assistant_message(self, content: str, emoji: str = "🤖") -> None:
        """
        Print assistant message with markdown formatting.（打印模型回复，带 markdown 格式）

        Args:
            content: Content to display（要显示的内容）
            emoji: Emoji to display before content（前面的图标）
        """
        content = content.strip() if content else ""
        # 这行的意思：如果 content 有值，就去掉首尾空白；如果是空的，就保持空字符串
        if content:  # 如果内容不是空的（空的话就别浪费屏幕了）
            if emoji:  # 如果要图标
                print(emoji)  # 先打图标
            self._console.print(Markdown(content))  # 把内容当 markdown 渲染后打印

    def print_simple_message(self, message: str, emoji: str = "") -> None:
        """
        Print a simple message without markdown formatting.（打印纯文本，不做 markdown 渲染）
        """
        if emoji:  # 有图标就带图标一起打
            print(f"{emoji} {message}")
        else:  # 没图标就只打内容
            print(message)

    def start_stream_display(self, refresh_rate: int = 10) -> None:
        """
        Start streaming display mode.（开启流式显示模式）

        Args:
            refresh_rate: Refresh rate per second for live display（每秒刷新几次）
        """
        self._stream_buffer = ""  # 清空缓冲区，准备接收新内容
        self._live = Live(console=self._console, refresh_per_second=refresh_rate)  # 创建一个"实时刷新区域"
        self._live.start()  # 开始刷新（此时屏幕上会出现一块会被不断重画的地方）

    def stop_stream_display(self) -> None:
        """Stop streaming display mode.（结束流式显示模式）"""
        if self._live:  # 如果当前确实在刷新
            self._live.stop()  # 停掉它（不然它会一直占着屏幕）
            self._live = None  # 清空引用

    def print_streaming_content(self, chunk: str) -> None:
        """
        Print streaming content chunk.（显示一小段流式内容）

        Args:
            chunk: Content chunk to append and display（要追加并显示的一小段）
        """
        if not hasattr(self, "_stream_buffer"):  # 如果这个属性还不存在（保险措施）
            self._stream_buffer = ""  # 就建一个空的

        self._stream_buffer += chunk  # 把新来的这一小段接到后面
        if self._live:  # 如果正在流式显示
            self._live.update(Markdown(self._stream_buffer))  # 用"到目前为止的全部文字"重画一遍
            # 注意：每次都重画全部，而不是只追加。
            # 这么做开销大一点，但能保证 markdown 渲染正确（比如代码块跨了多行）

    def get_stream_buffer(self) -> str:
        """Get the current stream buffer content.（把缓冲区里的内容取出来）"""
        return getattr(self, "_stream_buffer", "")
        # getattr 的第三个参数是"取不到时的默认值"，这样就算属性不存在也不会报错

    def clear_stream_buffer(self) -> None:
        """Clear the stream buffer.（清空缓冲区）"""
        self._stream_buffer = ""

    def print_error(self, error_message: str, emoji: str = "❌") -> None:
        """
        Print error message with special formatting.（打印错误信息）
        """
        if emoji:
            print(f"{emoji} {error_message}")
        else:
            print(error_message)

    def print_success(self, success_message: str, emoji: str = "✅") -> None:
        """
        Print success message with special formatting.（打印成功信息）
        """
        if emoji:
            print(f"{emoji} {success_message}")
        else:
            print(success_message)

    def print_info(self, info_message: str, emoji: str = "ℹ️") -> None:
        """
        Print info message.（打印提示信息）
        """
        if emoji:
            print(f"{emoji} {info_message}")
        else:
            print(info_message)

    def display_todos(self, todos: list, emoji: str = "📋") -> None:
        """
        Display todo list with formatting.（把待办清单画出来）

        Args:
            todos: List of todo items（待办列表）
            emoji: Emoji to display before todo list（前面的图标）
        """
        if not todos:  # 如果清单是空的
            self.print_info("当前没有待办事项", emoji)  # 提示一句，然后返回
            return  # 直接结束，不往下画

        output = []  # 准备一个列表，一行一行往里加
        output.append("📋 待办清单：")  # 标题
        output.append("=" * 50)  # 一条分隔线（= 重复 50 次）

        for i, todo in enumerate(todos, 1):  # 遍历清单，i 从 1 开始（所以要传第二个参数 1）
            status_icon = self._get_status_icon(todo['status'])  # 根据状态取图标
            content = todo['content']  # 任务内容
            todo_id = todo['id']  # 任务编号

            output.append(f"{i:2d}. {status_icon} [{todo_id}] {content}")
            # {i:2d} 的意思是"把 i 显示成至少占 2 个字符宽度，不够补空格"，这样能对齐

        output.append("=" * 50)  # 底部线
        output.append(f"共 {len(todos)} 项")  # 统计总数

        self.print_simple_message("\n".join(output))  # 用换行把列表连成一整段，然后打印

    @staticmethod
    def shorten(text, limit: int = 600) -> str:
        """把过长的内容压成一行预览，避免刷屏（2026-11-06 第 2 轮新增）

        为什么需要？
          工具执行结果可能是一整份 npm install 日志（几千行）。
          原样打印会把屏幕刷满，用户根本看不到自己关心的那几行。
          这里把换行折叠成 ⏎ 符号，超长再截断，一眼就能看出"这行在讲什么"。
        """
        text = str(text)  # 保险：传进来的可能是字典或数字
        if len(text) <= limit:  # 没超长，原样返回（短内容保留换行更好读）
            return text
        one_line = text.replace("\r\n", " ").replace("\n", " ⏎ ").replace("\r", " ")
        # 把换行折叠掉，整段压成一行
        head_length = max(0, limit - 40)  # 留 40 个字符的位置给后面的省略说明
        preview = one_line[:head_length].rstrip()  # 截取前面一段
        return f"{preview} …（共 {len(text)} 字符，已省略 {len(text) - head_length}）"

    def _get_status_icon(self, status: str) -> str:
        """
        Get icon for todo status.（根据状态返回对应图标）

        Args:
            status: Todo status（状态字符串）

        Returns:
            Icon string for the status（图标）
        """
        icons = {
            # 一个字典，把状态映射到图标
            'pending': '⏳',  # 待办：沙漏
            'in_progress': '🔄',  # 进行中：循环箭头
            'completed': '✅'  # 已完成：对勾
        }
        return icons.get(status, '❓')  # 取对应的图标，取不到就用问号（表示状态不认识）
