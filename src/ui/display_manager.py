# =============================================================================
# 文件：src/ui/display_manager.py
# 作用：管"往屏幕上打东西"。所有你看到的文字、清单、状态行，都是这里画出来的。
#
# 用到的库叫 rich：
#   - Markdown：把 **粗体**、`代码`、列表渲染成好看的样子
#   - Live：可以"原地不断刷新"一块区域，这就是流式输出的效果
#   - Padding：给内容加左缩进，让回答看起来是从符号下面"展开"的
#
# 【2026-11-06 第 4 轮】整体换了视觉风格，参考 Claude Code：
#   模型说话   ⏺  后面跟缩进两格的正文
#   工具调用   ⏺ 工具名(参数)
#   工具结果     ⎿  一行预览
#   状态栏       ⎿  上下文 x% · 花费 $y
#   错误       ✗ （红色）
#   提示       · （暗灰）
# =============================================================================

"""
Display management functionality for outputting content to users.（负责往屏幕输出内容）
"""

from rich.console import Console  # rich 库的"控制台"对象，是打印的入口
from rich.markdown import Markdown  # 把 markdown 文本变成带格式的漂亮输出
from rich.live import Live  # "实时刷新"组件，流式输出的核心
from rich.padding import Padding  # 给内容加内边距（这里用来做左缩进）
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色
from rich.console import Group  # 把多个可渲染对象竖着叠在一起
from typing import Optional  # 类型标注：表示"可能是这个，也可能是 None"

from . import theme  # 本项目统一的配色和符号（见 theme.py）


class DisplayManager:
    """Manages all display operations including streaming content.（管理所有显示操作，包括流式内容）"""

    def __init__(self, console: Optional[Console] = None):
        """
        Initialize the display manager.（初始化）

        Args:
            console: Rich console instance, creates new one if None（控制台对象，不传就用全局那一个）
        """
        self._console = console or theme.console  # 默认共用 theme 里的全局控制台，保证风格一致
        self._live: Optional[Live] = None  # 存放流式显示对象，一开始没有
        self._stream_buffer: str = ""  # 存放已经收到但还没显示完的文字

    # -------------------------------------------------------------------------
    # 一、消息类
    # -------------------------------------------------------------------------
    def assistant_header(self) -> None:
        """只打印模型发言的起始标记 ⏺（2026-11-06 第 4 轮新增）

        为什么要单独一个方法？
          流式输出时，我们得先把 ⏺ 打出来，再让文字一点点往外冒。
          否则用户会先看到一串文字，末尾才出现圆点，看起来很怪。
        """
        self._console.print(Text(f"{theme.BULLET} ", style="qs.accent"), end="\n")

    def print_assistant_message(self, content: str, emoji: str = "", header: bool = True) -> None:
        """打印模型回复（带 markdown 渲染）

        样式说明：
          先打一个青色圆点 ⏺，再把正文缩进两格渲染。
          这样长回复在视觉上是"挂在圆点下面"的，和 Claude Code 一致。
          header=False 用于"流式已经打过圆点、这里只补正文"的场景。
        """
        content = content.strip() if content else ""  # 去掉首尾空白；空内容保持空串
        if not content:  # 没内容就直接返回，别浪费屏幕
            return
        if header:  # 需要的话先打圆点
            self.assistant_header()
        self._console.print(Padding(Markdown(content), (0, 0, 0, 2)))  # 正文左缩进 2 格

    def print_simple_message(self, message: str, emoji: str = "") -> None:
        """打印纯文本（不做 markdown 渲染）"""
        if not message:  # 空消息不打，避免留下空行
            return
        self._console.print(Padding(Text(message, style="qs.dim"), (0, 0, 0, 2)))
        # 一律用暗灰色 + 左缩进，避免和模型正文抢注意力

    def print_error(self, error_message: str, emoji: str = "") -> None:
        """打印错误信息：红色叉号 + 红色文字"""
        text = Text()
        text.append(f"{theme.CROSS} ", style="bold qs.err")  # 叉号加粗红色
        text.append(str(error_message), style="qs.err")  # 正文红色
        self._console.print(text)

    def print_success(self, success_message: str, emoji: str = "") -> None:
        """打印成功信息：绿色对勾"""
        text = Text()
        text.append(f"{theme.CHECK_ON} ", style="qs.ok")
        text.append(str(success_message), style="qs.dim")  # 正文仍然是暗色，只有符号点缀
        self._console.print(text)

    def print_info(self, info_message: str, emoji: str = "") -> None:
        """打印提示信息：暗色小圆点，最不抢眼"""
        text = Text()
        text.append(f"{theme.DOT} ", style="qs.dim")
        text.append(str(info_message), style="qs.dim")
        self._console.print(text)

    def print_status(self, text_content: str, level: str = "info") -> None:
        """打印状态行（2026-11-06 第 4 轮新增，第 5 轮加了按水位变色）

        样式：  ⎿  上下文 12.3% · 花费 $0.01
        颜色随上下文占用变化：<50% 暗灰、50%~75% 黄色、>75% 红色。
        这样"快满了"这件事会自己跳出来，不用你去读数字。
        """
        color = {"info": "qs.dim", "warn": "qs.warn", "err": "qs.err"}.get(level, "qs.dim")
        text = Text()
        text.append(f"  {theme.RESULT} ", style=f"bold {theme.ACCENT}")  # 转折符号用强调色，和正文区分
        text.append(text_content, style=color)  # 内容按水位变色
        self._console.print(text)

    # -------------------------------------------------------------------------
    # 二、工具调用（Claude Code 风格）
    # -------------------------------------------------------------------------
    def tool_header(self, tool_name: str, args_preview: str = "") -> None:
        """打印一行"工具调用"（2026-11-06 第 4 轮新增）

        样式：  ⏺ cmd_runner(dir)
        参数太长时由调用方先截断，这里只负责排版。
        """
        text = Text()
        text.append(f"{theme.BULLET} ", style="qs.accent")  # 圆点
        text.append(tool_name, style="bold")  # 工具名加粗
        if args_preview:  # 有参数就用暗色括起来
            text.append(f"({args_preview})", style="qs.dim")
        self._console.print(text)

    def tool_result(self, result: str, success: bool = True) -> None:
        """打印工具执行结果（2026-11-06 第 4 轮新增）

        样式：    ⎿  hello
        失败时整行变红，成功时暗灰——这样一眼就能从满屏输出里找到出问题的那次调用。
        """
        text = Text()
        text.append(f"  {theme.RESULT}  ", style=f"bold {theme.ACCENT}")  # 转折符号用强调色，方便定位
        style = "qs.dim" if success else "qs.err"  # 成功用暗灰，失败用红
        text.append(str(result), style=style)
        self._console.print(text)

    # -------------------------------------------------------------------------
    # 三、流式显示
    # -------------------------------------------------------------------------
    def start_stream_display(self, refresh_rate: int = 10) -> None:
        """
        Start streaming display mode.（开启流式显示模式）

        【2026-11-06 第 4 轮】圆点 ⏺ 现在画在"流式区域"内部，
        而不是事先单独打一行。好处是：模型只调工具、不说话时，
        屏幕上不会多出一个空圆点。
        """
        self._stream_buffer = ""  # 清空缓冲区，准备接收新内容
        self._live = Live(
            console=self._console,
            refresh_per_second=refresh_rate,
            vertical_overflow="visible",  # 内容超过一屏时保留滚动，不要裁掉
            get_renderable=self._stream_renderable,  # 每次刷新都调用这个函数拿最新画面
        )
        self._live.start()  # 开始刷新

    def _stream_renderable(self):
        """根据当前缓冲区生成"这一帧"要显示的内容（2026-11-06 第 4 轮新增）"""
        if not self._stream_buffer:  # 还没有任何文字
            return Text("")  # 显示空内容（屏幕上就是一片空白，不会留圆点）
        return Group(
            Text(f"{theme.BULLET} ", style="qs.accent"),  # 圆点
            Padding(Markdown(self._stream_buffer), (0, 0, 0, 2)),  # 缩进两格的正文
        )

    def stop_stream_display(self) -> None:
        """Stop streaming display mode.（结束流式显示模式）

        【2026-11-06 第 4 轮】补一个换行。
        原因：Live 结束后，光标会停在最后一行末尾（没有换行符），
        紧跟着打印的状态行就会粘在正文屁股后面，看起来像
        "……助手的话  ⎿ 上下文 3.5%"。这里补一行就分开了。
        """
        if self._live:  # 如果当前确实在刷新
            had_content = bool(self._stream_buffer.strip())  # 之前有没有真的输出过文字
            self._live.stop()  # 停掉它（不然它会一直占着屏幕）
            self._live = None  # 清空引用
            if had_content:  # 有文字才补换行（纯工具调用的轮次不需要）
                self._console.print()

    def print_streaming_content(self, chunk: str) -> None:
        """
        Print streaming content chunk.（显示一小段流式内容）

        Args:
            chunk: Content chunk to append and display（要追加并显示的一小段）
        """
        if not hasattr(self, "_stream_buffer"):  # 如果这个属性还不存在（保险措施）
            self._stream_buffer = ""  # 就建一个空的

        self._stream_buffer += chunk  # 把新来的这一小段接到后面
        # 不用手动 update：Live 会按 refresh_per_second 自己去取 _stream_renderable()

    def get_stream_buffer(self) -> str:
        """Get the current stream buffer content.（把缓冲区里的内容取出来）"""
        return getattr(self, "_stream_buffer", "")

    def clear_stream_buffer(self) -> None:
        """Clear the stream buffer.（清空缓冲区）"""
        self._stream_buffer = ""

    # -------------------------------------------------------------------------
    # 四、待办清单
    # -------------------------------------------------------------------------
    def display_todos(self, todos: list, emoji: str = "") -> None:
        """把待办清单画出来（2026-11-06 第 4 轮改成勾选样式）

        样式：
          ◻ 1  写解析器            ← 未开始（暗色）
          ◼ 2  跑测试              ← 进行中（青色加粗）
          ✔ 3  提交代码            ← 已完成（绿色，文字变暗）
        """
        if not todos:  # 如果清单是空的
            self.print_info("当前没有待办事项")  # 提示一句，然后返回
            return

        self._console.print()  # 前面空一行，把清单和正文分开
        for i, todo in enumerate(todos, 1):  # 遍历清单，i 从 1 开始
            status = todo.get('status', 'pending')  # 取状态，取不到就当未开始
            line = Text()
            if status == 'completed':  # 已完成
                line.append(f"{theme.CHECK_ON} ", style="qs.ok")  # 绿色对勾
                line.append(f"{i}  {todo['content']}", style="qs.dim")  # 文字变暗（做完了就不抢眼）
            elif status == 'in_progress':  # 进行中
                line.append(f"{theme.CHECK_RUN} ", style="qs.accent")  # 青色实心方块
                line.append(f"{i}  {todo['content']}", style="bold")  # 加粗，最显眼
            else:  # 未开始
                line.append(f"{theme.CHECK_OFF} ", style="qs.dim")  # 暗色空心方块
                line.append(f"{i}  {todo['content']}", style="")
            self._console.print(Padding(line, (0, 0, 0, 2)))  # 整体缩进两格

    # -------------------------------------------------------------------------
    # 五、小工具
    # -------------------------------------------------------------------------
    @staticmethod
    def preview_lines(text, max_lines: int = 3, max_width: int = 70) -> str:
        """取结果的前几行做预览（2026-11-06 第 4 轮新增）

        为什么不用 shorten 那种"整段折成一行"？
          像 dir、git status 这类输出，整段折行会变成一坨没法读的长句。
          取前 3 行、每行截断到 70 字，既能看出"跑成功了没、大概是什么"，又不会刷屏。
        """
        text = str(text)  # 保险：可能是字典或数字
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
        # 先统一换行，再滤掉空行
        if not lines:  # 没有任何内容
            return "（无输出）"
        picked = []  # 装挑出来的几行
        for line in lines[:max_lines]:  # 只看前几行
            if len(line) > max_width:  # 单行太长就截断
                line = line[:max_width] + "…"
            picked.append(line)
        if len(lines) > max_lines:  # 后面还有，就补一句提示
            picked.append(f"…（共 {len(lines)} 行）")
        return " ⏎ ".join(picked)  # 用 ⏎ 把几行连起来，表示"这里换过行"

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
