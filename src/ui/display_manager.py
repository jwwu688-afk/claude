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
from rich.table import Table  # 【第 7 轮】用来做「圆点 + 正文」的两列布局
from typing import Optional  # 类型标注：表示"可能是这个，也可能是 None"
import time  # 【第 7 轮】计算"已经等了多久"给 spinner 用

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
        self._stream_start: float = 0.0  # 【第 7 轮】这一轮开始等待的时间点，用来算 spinner 上的秒数

    # -------------------------------------------------------------------------
    # 一、消息类
    # -------------------------------------------------------------------------
    @staticmethod
    def _bullet_block(content_renderable):
        """把 ⏺ 和正文排成 Claude Code 那样（2026-11-06 第 7 轮新增）

        要的效果是：
            ⏺ 目录里有 a、b、c 三个文件，
              另外还有两个子目录。
        也就是「圆点和第一行文字同一行，后续行对齐在文字下面」。

        怎么做到的？
          用两列网格：第一列只放 1 个字符宽的 ⏺，第二列放正文。
          网格会把两列顶部对齐，正文折行时自然就落在第二列的起始位置。
        """
        grid = Table.grid(padding=(0, 0))  # 不留额外间距：⏺ 本身在终端里占两格宽，天然有一个空格
        grid.add_column(width=2)  # 第一列：圆点占 2 列（符号 1 列 + 1 个空格），正文从第 3 列开始
        grid.add_column()  # 第二列：正文，占满剩余宽度
        grid.add_row(Text(theme.BULLET, style=""), content_renderable)
        return grid

    def print_assistant_message(self, content: str, emoji: str = "", header: bool = True) -> None:
        """打印模型回复（带 markdown 渲染）

        样式说明（第 7 轮改）：
          圆点 ⏺ 和正文的第一行在同一行，后续行对齐在文字下方——
          这是 Claude Code 的实际排版方式。
          header=False 用于"流式已经输出过、这里只补一段"的场景。
        """
        content = content.strip() if content else ""  # 去掉首尾空白；空内容保持空串
        if not content:  # 没内容就直接返回，别浪费屏幕
            return
        if header:  # 需要的话把圆点和正文排在一起
            self._console.print(self._bullet_block(Markdown(content)))
        else:  # 已经打过圆点了，只补正文
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
        text.append(f"  {theme.RESULT} ", style="qs.dim")  # 转折符号用暗灰
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
        text.append(f"{theme.BULLET} ", style="")  # 圆点：白色，Claude Code 里它不上色
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
        text.append(f"  {theme.RESULT}  ", style="qs.dim")  # 转折符号用暗灰，和 Claude Code 一致
        style = "qs.dim" if success else "qs.err"  # 成功用暗灰，失败用红
        text.append(str(result), style=style)
        self._console.print(text)

    # -------------------------------------------------------------------------
    # 二·五、授权提问（2026-11-06 第 8.2 轮新增）
    # -------------------------------------------------------------------------
    def approval_question(self, question: str, default_yes: bool = True) -> None:
        """画"要不要执行"这一次提问（2026-11-06 第 8.2 轮新增）

        改前是这样的三行英文，而且必须打字回 yes / no：
            🤖
            Please confirm if you want to execute the tool call: Tool: task, args: {...}
            Answer yes or no, if refusing please provide a reason

        改后只留一个问题 + 一行提示，直接回车就是同意：
            ⏺ cmd_runner(dir)
              ? 是否执行？ 回车确认 · 输入 n 拒绝 · 或直接输入你的想法

        为什么把选项压成"回车 / 一句话"两选：
          Claude Code 里的授权就是一次回车，多打一个 y 都是多余的摩擦。
          而"输入别的什么"天然表达"我有意见"，直接当拒绝理由交给模型，
          比让它先选 no、再单独问一遍理由少一次往返。
        """
        line = Text()
        line.append("  ? ", style=f"bold {theme.ACCENT}")  # 问号用品牌橙，在一屏暗灰里能立刻被看到
        line.append(question, style="")
        # 提示和问题放同一行：每次授权都多占一行，一屏里几次调用就挤没了。
        # 这里用暗灰小字跟在后面，读的时候可以忽略，需要时又不用去别处找。
        line.append("  ", style="")
        line.append("回车确认" if default_yes else "回车拒绝", style="qs.dim")
        line.append(" · 输入 " + ("n 拒绝" if default_yes else "y 允许"), style="qs.dim")
        line.append(" · 或直接说你想怎么改", style="qs.dim")
        self._console.print(line)

    def tool_denied(self, reason: str = "") -> None:
        """工具被用户拒绝时补一行说明（2026-11-06 第 8.2 轮新增）

        改前拒绝之后屏幕上什么都没有，只有下一轮模型的话。
        用户看不到"我这一下确实生效了"，会怀疑是不是没点上。
        """
        text = Text()
        text.append(f"  {theme.RESULT}  ", style="qs.dim")
        text.append("已拒绝", style="qs.warn")
        if reason:
            text.append(f"：{reason}", style="qs.dim")
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
        self._stream_start = time.time()  # 【第 7 轮】记下开始等待的时刻
        self._live = Live(
            console=self._console,
            refresh_per_second=refresh_rate,
            vertical_overflow="visible",  # 内容超过一屏时保留滚动，不要裁掉
            get_renderable=self._stream_renderable,  # 每次刷新都调用这个函数拿最新画面
        )
        self._live.start()  # 开始刷新

    def _stream_renderable(self):
        """根据当前缓冲区生成"这一帧"要显示的内容（2026-11-06 第 4 轮新增）

        【第 7 轮】缓冲区为空时不再显示空白，而是显示 Claude Code 那种等待动画：
            ✻ 正在思考… (3s)
        因为 Live 每秒钟会刷新十几次，这个函数会被反复调用，
        每次都用"当前时间 - 开始时间"算秒数，秒数就会自己往上走。
        """
        if not self._stream_buffer:  # 还没收到任何文字，说明模型还在想
            return self._spinner_renderable()
        return self._bullet_block(Markdown(self._stream_buffer))  # 圆点与正文同排（Claude Code 样式）

    def _spinner_renderable(self):
        """生成等待动画的一帧（2026-11-06 第 7 轮新增）

        星号在四个形状之间轮换，看起来像在转；后面跟着已经等了多少秒。
        """
        elapsed = time.time() - self._stream_start  # 已经等了多久
        frame = theme.SPIN_FRAMES[int(elapsed * 8) % len(theme.SPIN_FRAMES)]  # 每秒换 8 次
        line = Text()
        line.append(f"{frame} ", style=f"bold {theme.ACCENT}")  # 星号用 Claude 橙
        line.append("正在思考…", style="qs.dim")
        line.append(f" ({elapsed:.0f}s)", style="qs.dim")  # 秒数，给自己一个"还在动"的反馈
        return line

    def stop_stream_display(self) -> None:
        """Stop streaming display mode.（结束流式显示模式）

        【2026-11-06 第 4 轮】补一个换行。
        原因：Live 结束后，光标会停在最后一行末尾（没有换行符），
        紧跟着打印的状态行就会粘在正文屁股后面，看起来像
        "……助手的话  ⎿ 上下文 3.5%"。这里补一行就分开了。
        """
        if self._live:  # 如果当前确实在刷新
            had_content = bool(self._stream_buffer.strip())  # 之前有没有真的输出过文字
            if not had_content:  # 【第 7 轮】这一轮只有工具调用、没有文字
                # 把这块区域标记成"临时的"：rich 在 stop() 时会自动擦掉它，
                # 而不是把最后一帧 spinner 留在屏幕上。
                # 不能只 update(Text(""))——那样在非交互环境（输出被重定向）里会残留一行。
                self._live.transient = True
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
