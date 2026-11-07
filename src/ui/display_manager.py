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
from rich.cells import cell_len  # 【第 10 轮】算字符串占几列（中文算 2），spinner 要对齐宽度
from rich.markdown import Markdown  # 把 markdown 文本变成带格式的漂亮输出
from rich.live import Live  # "实时刷新"组件，流式输出的核心
from rich.padding import Padding  # 给内容加内边距（这里用来做左缩进）
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色
from rich.table import Table  # 【第 7 轮】用来做「圆点 + 正文」的两列布局
from typing import Optional  # 类型标注：表示"可能是这个，也可能是 None"
import os  # 【第 10 轮】读取 THINKER_NO_SPINNER 开关
import sys  # 【第 10 轮】等待动画直接往 stdout 写，需要它
import threading  # 【第 10 轮】单行 spinner 跑在后台线程里
import time  # 【第 7 轮】计算"已经等了多久"给 spinner 用

from . import theme  # 本项目统一的配色和符号（见 theme.py）


class DisplayManager:
    """Manages all display operations including streaming content.（管理所有显示操作，包括流式内容）"""

    # 【第 10 轮】spinner 每帧都补齐到这个宽度（终端列数，中文算 2 列）。
    # 为什么固定：秒数从 (9s) 变 (10s) 会多一列，如果这一帧比上一帧短，
    # 右边就会留下上一帧的残影。固定宽度 + 空格填充，覆盖永远干净。
    _SPINNER_CELLS = 28

    def __init__(self, console: Optional[Console] = None):
        """
        Initialize the display manager.（初始化）

        Args:
            console: Rich console instance, creates new one if None（控制台对象，不传就用全局那一个）
        """
        self._console = console or theme.console  # 默认共用 theme 里的全局控制台，保证风格一致
        self._live: Optional[Live] = None  # 存放流式显示对象，一开始没有
        self._stream_buffer: str = ""  # 存放已经收到但还没显示完的文字
        self._stream_has_content: bool = False  # 【第 10 轮】本轮是否已出现过真文字（防橙绿交替闪）
        self._thought_seconds: float = 0.0  # 【第 10 轮】"想"到第一个字花了多久，用于 Thought for Ns 行
        self._stream_start: float = 0.0  # 【第 7 轮】这一轮开始等待的时间点，用来算 spinner 上的秒数
        self._refresh_rate: int = 10  # 【第 10 轮】正文 Live 的刷新率，由 start_stream_display 传入
        self._spinner_stop = None  # 【第 10 轮】等停 spinner 线程的信号（threading.Event）
        self._spinner_thread = None  # 【第 10 轮】spinner 线程
        self._body_cache_key = None  # 【第 10 轮】正文渲染缓存：上次渲染时的 buffer 内容
        self._body_cache_value = None  # 【第 10 轮】正文渲染缓存：对应的 renderable

    # -------------------------------------------------------------------------
    # 一、消息类
    # -------------------------------------------------------------------------
    @staticmethod
    def _bullet_block(content_renderable):
        """把 ● 和正文排成 Claude Code 那样（2026-11-06 第 7 轮新增）

        要的效果是：
            ● 目录里有 a、b、c 三个文件，
              另外还有两个子目录。
        也就是「圆点和第一行文字同一行，后续行对齐在文字下面」。

        怎么做到的？
          用两列网格：第一列只放 1 个字符宽的 ●，第二列放正文。
          网格会把两列顶部对齐，正文折行时自然就落在第二列的起始位置。

        【2026-11-07 第 10 轮】圆点从 ⏺ 改成 ●，并上绿色。
          原因见 theme.py 顶部说明：⏺ 会被终端当成 emoji 渲染成彩色，
          颜色不受控，还会和橙色 ✻ 交替闪。● 是纯几何字符，颜色想写什么就是什么。
        """
        grid = Table.grid(padding=(0, 0))  # 不留额外间距：● 本身在终端里占两格宽，天然有一个空格
        grid.add_column(width=2)  # 第一列：圆点占 2 列（符号 1 列 + 1 个空格），正文从第 3 列开始
        grid.add_column()  # 第二列：正文，占满剩余宽度
        grid.add_row(Text(theme.BULLET, style=f"bold {theme.BULLET_COLOR}"), content_renderable)
        # ↑ 圆点用加粗绿色：截图里助手回复和工具调用前面都是这个绿点
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

    def print_warning(self, warning_message: str) -> None:
        """打印警告（2026-11-06 第 9 轮新增）

        样式：  ! 高危操作（递归强制删除文件）：回车将默认拒绝，确认执行请输入 y。
        和 print_error 的区别：错误是"已经出事了"，警告是"还没出事，你要不要继续"。
        颜色用黄而不是红——红留给真正的报错，否则一屏里两处红，用户分不清轻重。
        """
        text = Text()
        text.append(f"{theme.WARN_SIGN} ", style="bold qs.warn")
        text.append(str(warning_message), style="qs.warn")
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

        样式：  ● cmd_runner(dir)
        参数太长时由调用方先截断，这里只负责排版。

        【第 10 轮】圆点改成绿色 ●，和截图里 `● Skill(using-superpowers)` 一致。
        """
        text = Text()
        text.append(f"{theme.BULLET} ", style=f"bold {theme.BULLET_COLOR}")  # 绿点
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
        """开启流式显示（2026-11-07 第 10 轮重写）

        改前：一上来就开一个 rich Live，让它在 spinner 和正文之间来回切。
        改后分成两段：
          1. **等待段**：一根单行 spinner，跑在后台线程里，用 `\\r` + 清行覆盖自己；
          2. **正文段**：第一个真字到达时才创建 Live，之后内容只增不减。

        为什么等待段不用 Live？
          `rich.Live` 的重绘方式是"光标上移 N 行，然后重写覆盖"，它**不发清行指令**
          （实测捕获的输出里 `\\x1b[2K` 出现 0 次）。等待态是 1 行、正文态是 2 行以上，
          高度一变，上移的行数就对不上，旧帧擦不干净，和新帧叠在一起——
          屏幕上就是橙色的 ✻ 和绿色的 ● 交替闪。
          单行场景根本不需要 Live：`\\r\\x1b[2K` + 重写，一行永远只有一行。
        """
        self._stop_spinner()  # 【第 10 轮】幂等兜底：上一轮若因异常没走到 stop，这里把泄漏的线程收掉
        self._stream_buffer = ""  # 清空缓冲区，准备接收新内容
        self._stream_has_content = False  # 本轮是否已经出现过"真正的文字"
        self._thought_seconds = 0.0  # 思考耗时先归零
        self._stream_start = time.time()  # 记下开始等待的时刻
        self._live = None  # 正文段的 Live，等真有内容了再建
        self._refresh_rate = refresh_rate
        self._spinner_stop = threading.Event()
        self._spinner_thread = None
        # 【第 10 轮】THINKER_NO_SPINNER=1 可以彻底关掉等待动画。
        #   留这个开关是因为不同终端的 ANSI 支持差异很大（Windows Terminal 正常，
        #   某些 IDE 内置终端对 \r 覆盖的处理不好）。真遇到问题可以一键退回"无动画"。
        if self._is_tty() and os.getenv("THINKER_NO_SPINNER") != "1":
            self._spinner_thread = threading.Thread(target=self._spin_loop, daemon=True)
            self._spinner_thread.start()

    @staticmethod
    def _is_tty() -> bool:
        """当前输出是不是真终端（2026-11-07 第 10 轮新增）

        非 tty 时写转义码只会污染输出文件，所以 spinner 直接不启动。
        用 isatty 而不是 rich 的 console.is_terminal，是为了不受 rich 缓冲影响。
        """
        try:
            return bool(sys.stdout.isatty())
        except Exception:
            return False

    def _spin_loop(self) -> None:
        """等待动画：一根原地转的橙色星芒（2026-11-07 第 10 轮新增）

        每 200ms 重画一次同一行，写法是 `\\r`（回到行首）+ 内容 + 补齐空格。

        两条硬规则（都是踩出来的）：

        1. **每帧总宽度必须完全一致**。秒数从 (9s) 变 (10s) 会多一个字符，
           如果这一帧比上一帧短，右边就会留下上一帧的残影。所以每帧都
           pad 到固定 CELLS 列，用空格把旧内容吃掉。
        2. **不依赖 `\\x1b[2K` 清行**。2K 是标准 CSI，Windows Terminal 支持，
           但伪终端里实测清行之后尾巴还在（会看到 "✻ 正在思考… (1s)  Thought …" 粘连）。
           空格覆盖不依赖终端对指令的支持，最稳。

        线程是 daemon：万一主流程异常退出，它不会拖住进程。
        """
        try:
            # 延迟 200ms 再出现。模型快速返回时（工具调用轮次常常只有几十毫秒），
            # spinner 根本来不及显示就被擦掉，那一瞬间的"画出-擦掉"就是一次闪烁。
            # 等 200ms 还没结果才显示，短等待彻底安静。
            if self._spinner_stop.wait(0.2):
                return
            while not self._spinner_stop.is_set():
                elapsed = time.time() - self._stream_start
                frame = theme.SPIN_FRAMES[int(elapsed * 5) % len(theme.SPIN_FRAMES)]
                plain = f"{frame} 正在思考… ({int(elapsed)}s)"
                pad = max(0, self._SPINNER_CELLS - cell_len(plain))  # 补齐到固定宽度
                line = (
                    f"{theme.ACCENT_ANSI}{frame}{theme.RESET_ANSI} "
                    f"{theme.DIM_ANSI}正在思考… ({int(elapsed)}s){theme.RESET_ANSI}"
                )
                sys.stdout.write("\r" + line + " " * pad + "\x1b[K")
                sys.stdout.flush()
                self._spinner_stop.wait(0.2)  # 5 帧/秒。Event.wait 能立刻响应停止
        except Exception:  # spinner 出问题绝不能影响主流程
            pass

    def _stop_spinner(self) -> None:
        """停掉等待动画，并把那一行擦干净（2026-11-07 第 10 轮新增）

        必须 join：否则线程可能在正文已经开始输出之后，还在往同一行写东西，
        造成"正文第一行被 spinner 覆盖"这种更诡异的画面。

        清行同样用空格覆盖（不用 2K），并把光标送回行首，
        让接下来的 `console.print(Thought)` 从第 0 列开始写。

        只有"确实停掉了一个在跑的 spinner"才擦。否则在从未启动过 spinner 的场景
        （非 tty、纯工具轮次）也会擦一次，把用户刚敲的那一行抹掉。
        """
        stopped = False
        if self._spinner_thread is not None:
            self._spinner_stop.set()
            self._spinner_thread.join(timeout=1.0)
            self._spinner_thread = None
            stopped = True
        if stopped and self._is_tty():
            # 三重保险：回车回到行首、2K 清整行、空格彻底覆盖、再回车归位。
            # 只靠其中一种在某些终端上会漏（实测过 2K 之后尾巴还在的情况）。
            sys.stdout.write("\r\x1b[2K" + " " * 60 + "\r")
            sys.stdout.flush()

    def _start_body_live(self) -> None:
        """正文段：第一个真字到达后才启动 Live（2026-11-07 第 10 轮新增）

        这一段用 Live 是合理的：内容只增不减，重绘区域高度单调变高，
        不会出现"这一帧比上一帧短"的擦除问题。

        renderable 做了缓存：Live 每秒取 10 次，如果每次都重新解析 Markdown，
        长回复时每帧都要把整段重新排版一遍，CPU 一忙刷新就不均匀——
        视觉上就是"一顿一顿地闪"。只有 buffer 真的变了才重新构建。
        """
        self._body_cache_key = None
        self._body_cache_value = None
        self._live = Live(
            console=self._console,
            refresh_per_second=self._refresh_rate,
            vertical_overflow="visible",
            get_renderable=self._body_renderable,
        )
        self._live.start()

    def _body_renderable(self):
        """取正文这一帧要画的内容，带一级缓存（2026-11-07 第 10 轮新增）"""
        if self._body_cache_key != self._stream_buffer:
            self._body_cache_key = self._stream_buffer
            self._body_cache_value = self._bullet_block(Markdown(self._stream_buffer))
        return self._body_cache_value

    def _thought_line(self) -> Text:
        """生成 `  Thought for 3s (ctrl+o to expand)` 这一行（2026-11-07 第 10 轮新增）

        和截图一致：缩进两格、整行暗灰。
        ctrl+o 展开思考内容这个功能还没做，所以后面括号里的提示只是占位——
        真要按了没反应，比不写更容易让人困惑，所以这里只保留耗时本身。

        嗯，还是照截图带上括号提示吧：它是"这一行是什么"的解释，
        没有 ctrl+o 也不影响理解。
        """
        text = Text()
        text.append("  ")
        text.append(f"Thought for {int(round(self._thought_seconds))}s", style="qs.dim")
        text.append(" (ctrl+o to expand)", style="qs.dim")
        return text

    def stop_stream_display(self, show_done: bool = True) -> None:
        """Stop streaming display mode.（结束流式显示模式）

        【2026-11-06 第 4 轮】补一个换行。
        原因：Live 结束后，光标会停在最后一行末尾（没有换行符），
        紧跟着打印的状态行就会粘在正文屁股后面，看起来像
        "……助手的话  ⎿ 上下文 3.5%"。这里补一行就分开了。

        【2026-11-07 第 10 轮】结尾补一行 `✻ Cooked for 7s · done 11:38`，
        对齐截图里 Claude Code 的收尾行。只在"这一轮模型真的说了话"时打印——
        纯工具调用的轮次不打印，否则一屏里会出现三四条 Cooked。

        【第 10 轮重写】先无条件把 spinner 收掉：
        模型只调工具、一个字都不说时，正文 Live 根本没启动过，
        原来的写法在这种轮次里不会清掉 spinner 行，它就留在屏幕上变成一条残影。
        """
        self._stop_spinner()  # 不管有没有正文，先把等待动画擦干净

        if self._live:  # 这一轮真的输出了正文
            self._live.stop()
            self._live = None
            # 真终端里 Live.stop() 自己会补一个换行；输出被重定向到文件/管道时不会，
            # 那样 Cooked 行就会粘在正文末尾（实测"…可以帮你？  ✻ Cooked for 1s"）。
            # 所以只在非 tty 场景补这一行，tty 下补了会多出一个空行。
            if not self._is_tty():
                self._console.print()
            if show_done:
                self._print_done_line()

    def _print_done_line(self) -> None:
        """打印这一轮的收尾行（2026-11-07 第 10 轮新增）

        样式和截图一致：
            ✻ Cooked for 7s · done 11:38
        整行暗灰——它是"记账"信息，不该和正文抢注意力。
        """
        elapsed = max(1, int(round(time.time() - self._stream_start)))  # 至少显示 1s
        finished_at = time.strftime("%H:%M")  # 当地时间的时:分
        text = Text()
        text.append(f"{theme.STAR} ", style="qs.dim")
        text.append(f"Cooked for {elapsed}s", style="qs.dim")
        text.append(f" {theme.DOT} done {finished_at}", style="qs.dim")
        self._console.print(text)

    def print_streaming_content(self, chunk: str) -> None:
        """
        Print streaming content chunk.（显示一小段流式内容）

        Args:
            chunk: Content chunk to append and display（要追加并显示的一小段）
        """
        if not hasattr(self, "_stream_buffer"):  # 如果这个属性还不存在（保险措施）
            self._stream_buffer = ""  # 就建一个空的

        self._stream_buffer += chunk  # 把新来的这一小段接到后面
        # 【第 10 轮】只有"有实义的字"才算内容开始。
        #   流式接口经常先发 "\n"、"" 这种空 delta，如果拿它们当"已经有内容"，
        #   界面就会在 spinner 和正文之间反复横跳（这正是橙绿交替闪的第二个成因）。
        if chunk and chunk.strip():
            if not self._stream_has_content:  # 第一次收到真文字：从"等待段"切到"正文段"
                self._thought_seconds = time.time() - self._stream_start
                self._stream_has_content = True
                self._stop_spinner()  # 先把 spinner 那行擦掉，否则它会和正文抢同一行
                if self._thought_seconds >= 1:  # 想得太快就别挂 Thought 行了
                    self._console.print(self._thought_line())
                self._start_body_live()
            # 后续 chunk 不用手动 update：正文 Live 会按 refresh_per_second 自己重画

    def get_stream_buffer(self) -> str:
        """Get the current stream buffer content.（把缓冲区里的内容取出来）"""
        return getattr(self, "_stream_buffer", "")

    def clear_stream_buffer(self) -> None:
        """Clear the stream buffer.（清空缓冲区）"""
        self._stop_spinner()  # 【第 10 轮】顺手把还在转的 spinner 停掉
        self._stream_buffer = ""
        self._stream_has_content = False  # 连同"已有内容"的标记一起复位
        self._thought_seconds = 0.0

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
