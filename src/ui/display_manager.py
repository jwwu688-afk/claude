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
import atexit  # 【第 19 轮】进程退出时兜底恢复光标（等待动画期间光标是隐藏的）
import os  # 【第 10 轮】读取 THINKER_NO_SPINNER 开关
import re  # 【第 29 轮】识别 markdown 表格的分隔行（| --- | --- |）
import sys  # 【第 10 轮】等待动画直接往 stdout 写，需要它
import threading  # 【第 10 轮】单行 spinner 跑在后台线程里
import time  # 【第 7 轮】计算"已经等了多久"给 spinner 用

from . import theme  # 本项目统一的配色和符号（见 theme.py）


def _human_seconds(elapsed: float) -> str:
    """把秒数变成人读得懂的写法（2026-11-11 第 28 轮新增）

    191 秒这个数字，用户得自己换算成"三分多钟"，而换算这件事本该由程序做。
    超过一分钟就显示 3m11s，一小时内够用。
    """
    total = int(elapsed)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _human_chars(count: int) -> str:
    """把"思考了多少字"压成短写法（2026-11-11 第 28 轮新增）

    为什么用 k：推理模型一次能想几千上万 token，写全四个数字会把 spinner
    撑得很宽，而宽度一变就得靠空格补齐——补得越多，那一行的闪烁越明显。
    """
    if count < 1000:
        return f"{count}"
    return f"{count / 1000:.1f}k"


def build_spinner_frame(elapsed: float, thinking_chars: int = 0):
    """按等待秒数算出这一帧的（纯文本, 带颜色的文本）（2026-11-09 第 19 轮新增）

    为什么抽成模块级纯函数：
      `_spin_loop` 跑在后台线程里、直接往 stdout 写转义码，几乎没法断言。
      把"这一帧长什么样"抽出来之后，宽度不超标、动词轮换、省略号循环
      这些规则都能离线测（见 test/test_spinner_frame.py）。
      这和 `_render_screen` 的思路一致：把"算"和"写"分开，"算"才可测。

    【第 28 轮】thinking_chars：模型这一轮已经"想"了多少字。
      推理模型（讯飞 spark-x2.6 等）在真正开口之前会思考很久，实测有过 191 秒。
      这期间正文一个字都没有，屏幕上只有一个转轮——转轮虽然一直在动，
      但它传达的信息量等于"不知道在干嘛"。把思考字数实时显示出来之后，
      数字每秒都在涨，就是"系统活着"最直接的证据。
      普通模型拿不到 reasoning_content，thinking_chars 恒为 0，文案退回原样。

    返回二元组：
      plain  —— 不带转义码的版本，专门用来算显示宽度（cell_len 会忽略 ANSI、
                但中文算 2 列，只有拿纯文本算才准）；
      styled —— 真正写进终端的版本。

    动态感从三个周期叠出来（周期互质，不会整帧重复）：
      · 转轮每 100ms 一帧（8 帧 → 0.8s 一圈）
      · 省略号每 500ms 一格（0~3 个点 → 2s 一个循环）
      · 动词每 3s 换一个（5 个词 → 15s 一个循环）
      三个周期错开，看起来就是"一直在动"，而不是一张循环图在闪。
    """
    frame = theme.SPIN_FRAMES[int(elapsed * 8) % len(theme.SPIN_FRAMES)]
    verb = theme.WAIT_VERBS[int(elapsed // 3) % len(theme.WAIT_VERBS)]
    dots = "." * (int(elapsed * 2) % 4)  # 0/1/2/3 个点
    secs = _human_seconds(elapsed)
    if thinking_chars > 0:
        # 有思考字数时不再挂动词：数字本身在变，就是最有力的"还在动"
        tail = f"正在推理 {_human_chars(thinking_chars)} 字 · {secs}"
    else:
        tail = f"{verb}{dots} ({secs})"

    plain = f"{frame} {tail}"
    styled = (
        f"{theme.ACCENT_ANSI}{frame}{theme.RESET_ANSI} "
        f"{theme.DIM_ANSI}{tail}{theme.RESET_ANSI}"
    )
    return plain, styled


def _is_table_row(line: str) -> bool:
    """这一行是不是 markdown 表格的一行（2026-11-11 第 29 轮新增）

    判定放宽到"以 | 开头、且至少两个 |"：
      真正的表格行是 `| a | b |`，但流还没收完时可能只有 `| a | b`（结尾的 |
      还在路上）。宽松判定的代价是把少数以 | 开头的普通文本也当成表格，
      而那种行被重新对齐一次也无伤大雅；反过来（漏判）就会让表格错位，
      正是用户截图里抱怨的问题。
    """
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_cells(line: str) -> list:
    """把 `| a | b |` 拆成 ['a', 'b']（2026-11-11 第 29 轮新增）

    只去掉最外层的两个竖线，中间的空格也一并 strip：
    留着空格会导致同一列里有的格子多一格、有的少一格，还没对齐就先错位了。
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_markdown_table(rows: list) -> str:
    """把几行原始 markdown 表格重新排成"等宽对齐"的文本（2026-11-11 第 29 轮新增）

    【为什么需要它】
      流式正文走的是 plain 模式（不做 markdown 渲染，见第 11 轮的取舍），
      所以模型吐出来的 `| 改动文件 | 内容 |` 是原样打到屏幕上的。
      而原始 markdown 的列宽是"随手写的"，中文字符又占两列，
      于是 `| --- | --- |` 和内容行永远对不齐——用户看到的就是截图里那种锯齿。

    【怎么做】
      1. 拆单元格；
      2. 认出分隔行（`| --- | --- |`），它在渲染时固定输出短横线；
      3. 每一列取"所有格子里最宽的那个"作为列宽，**按显示列算**（中文算 2），
         不够的补空格；
      4. 重新拼回 `| a | b |` 的形式。

    为什么按显示列而不是按字符数：
      `cell_len("改动文件")` 是 8，`len()` 是 4。按字符补空格的话，
      中文列看上去还是会短一截，对齐等于没做。

    返回带结尾换行的字符串，宽度不够的格子用空格补，绝不截断内容。
    """
    parsed = [_split_cells(row) for row in rows]
    ncols = max(len(cells) for cells in parsed)
    for cells in parsed:  # 补齐成同样的列数，否则后面按列取宽度会越界
        cells.extend([""] * (ncols - len(cells)))

    # 找分隔行：每个格子都是 --- / :--- / ---: / :---: 这种
    separator_index = None
    for index, cells in enumerate(parsed):
        if cells and all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells):
            separator_index = index
            break

    widths = [3] * ncols  # 最小 3 列，保证 `---` 放得下
    for index, cells in enumerate(parsed):
        if index == separator_index:
            continue  # 分隔行不参与宽度计算，否则全是短横线会把列撑得没意义
        for col, cell in enumerate(cells):
            widths[col] = max(widths[col], cell_len(cell))

    lines = []
    for index, cells in enumerate(parsed):
        if index == separator_index:
            lines.append("| " + " | ".join("-" * width for width in widths) + " |")
        else:
            padded = [
                cell + " " * (widths[col] - cell_len(cell))  # 按显示列补空格
                for col, cell in enumerate(cells)
            ]
            lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines) + "\n"


class DisplayManager:
    """Manages all display operations including streaming content.（管理所有显示操作，包括流式内容）"""

    # 【第 10 轮】spinner 每帧都补齐到这个宽度（终端列数，中文算 2 列）。
    # 为什么固定：秒数从 (9s) 变 (10s) 会多一列，如果这一帧比上一帧短，
    # 右边就会留下上一帧的残影。固定宽度 + 空格填充，覆盖永远干净。
    # 【第 28 轮】28 → 40：加了"正在推理 12.3k 字 · 3m11s"这种更长的文案。
    #   实际宽度还会和终端列数取小（见 _spin_loop），窄窗口下不会被顶到折行。
    _SPINNER_CELLS = 40

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
        # 【2026-11-07 第 13.1 轮】这两个属性原来只在 start_stream_display 里赋值，
        #   于是"没走 start 就直接喂内容"的调用方（脚本、测试、或将来新增的调用点）
        #   会在 print_streaming_content 里撞 AttributeError: '_stream_mode'。
        #   默认值取 plain + 未开始，让这类调用退化成"逐段打印"，而不是直接崩。
        self._stream_mode: str = "plain"  # plain = 追加式打印（默认）；live = rich Live
        self._body_started: bool = False  # plain 模式下正文是否已开始（● 是否已打印）
        # 【第 28 轮】模型这一轮"思考"了多少字（只用来显示，不参与任何判断）。
        #   推理模型在开口前可能思考几分钟，这期间正文一个字都没有；
        #   把这个数字打在 spinner 上，用户就能看到"它还在想，且想得越来越多"。
        self._reasoning_chars: int = 0
        # 【第 29 轮】plain 模式的"表格对齐"状态。
        #   为什么需要这几项：表格必须等整张收完才能算列宽（见 _render_markdown_table），
        #   所以遇到以 | 开头的行要先把内容攒起来、不立刻打到屏幕上。
        self._plain_at_line_start: bool = True  # 下一个字符是不是在行首（判断表格只能从行首认）
        self._plain_pending: str = ""  # 已经收到、还没决定怎么输出的残段
        self._in_table: bool = False  # 当前是否正在攒一张表格
        self._table_rows: list = []  # 攒着的原始表格行
        self._table_limit: int = 40  # 超过这么多行就不再等，先输出（防止超大表格迟迟不显示）

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
        # 【2026-11-07 第 11 轮】必须显式给 overflow="fold"！
        #   rich 的 Table 列默认 overflow="ellipsis"——放在单元格里的大段 Markdown
        #   不折行，整段被截成一行、行尾挂一个 "…"（实测复现：60 列下 6 行内容变 1 行）。
        #   这就是"回答不换行、结尾全是省略号"的根因。fold = 强制按宽度折行。
        grid.add_column(overflow="fold")  # 第二列：正文，占满剩余宽度
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
        """打印工具执行结果（2026-11-06 第 4 轮新增，2026-11-11 第 26 轮改成多行对齐）

        样式（单行结果，最常见）：
            ⎿  待办清单已更新，共 5 项。
        样式（多行结果）：
            ⎿  1  /** ===== 战斗核心：固定逻辑坐标与秒制时间 ===== */
               2  * 【2026-11-10】输入交给 main，画面交给 effects
               3  * 死亡标记在本帧立即生效，帧末统一回收
               …（共 108 行）

        为什么第 26 轮要改成"多行分别打印"：
          改前 `preview_lines` 把 3 行结果用 " ⏎ " 拼成**一整行**再交给 rich。
          rich 的 Text 只会在终端边界处硬折行，而折行的位置是按**显示列**算的，
          中文占 2 列、行号后面的 \\t 又会跳到下一个 8 列边界——
          于是"1/**/2/*/3/*"这几段被折成一团，行号跑到悬在半空的地方，
          终端一屏看下去就是"字全是乱的、读不出哪行属于哪个文件"。
          现在每行独立成行、续行缩进对齐 ⎿ 之后的位置，行号永远在行首，
          折行也不会跨行粘连（每行长度在 preview_lines 里已按终端宽度截好）。

        失败时整行变红，成功时暗灰——这样一眼就能从满屏输出里找到出问题的那次调用。
        """
        style = "qs.dim" if success else "qs.err"  # 成功用暗灰，失败用红
        # 统一换行符后按行拆开：rich 的 Text 支持 "\\n"，但续行会从第 0 列开始，
        # 所以缩进必须我们自己补进去（补在字符串里，而不是靠 Padding）。
        body = str(result).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # 去掉结尾的空行：结果文本以 "\n" 结尾时 split 会多出一个 ""，
        # 不去掉的话每次工具结果后面都会拖一个空行，一屏里几次调用就散成一片。
        while len(body) > 1 and not body[-1]:
            body.pop()
        text = Text()
        text.append(f"  {theme.RESULT}  ", style="qs.dim")  # 转折符号用暗灰，和 Claude Code 一致
        text.append(body[0], style=style)
        # 续行缩进 5 格 = 上面前缀 "  ⎿  " 的显示宽度（2 空格 + ⎿ + 2 空格），
        # 让多行结果像"从 ⎿ 后面展开"，而不是每行都顶着左边框。
        for line in body[1:]:
            text.append("\n", style=style)
            text.append(f"     {line}" if line else "", style=style)
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
          2. **正文段**：第一个真字到达时才开始输出。

        【2026-11-07 第 11 轮】正文段从 rich Live 改成"追加式打印"。
          原因：Live 的重绘靠"光标上移 N 行 + 重写覆盖"擦掉旧帧，
          这在 Anaconda PowerShell Prompt（conhost）里不可靠——旧帧擦不干净，
          每 100ms 一帧全叠在屏幕上。加上省略号 bug 让每帧只有一行，
          看起来就是"同一句话重复显示五六遍"（spinner 那轮踩过同一类坑）。
          追加式打印内容只增不减、从不回头擦，任何终端都不会重影；
          代价是流式过程中显示纯文本（markdown 原始记号可见），打完即定格。
          想要旧的 Live 实时渲染：设环境变量 THINKER_STREAM=live。

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
        self._reasoning_chars = 0  # 【第 28 轮】思考字数从 0 开始数
        # 【第 30 轮】工具参数心跳行的状态。三个字段每轮都在这里重置，
        #   保证上一轮异常退出留下的 active=True 不会让新一轮一上来就去清别人的行。
        self._tool_hint_active = False
        self._tool_hint_chars = 0
        self._tool_hint_start = 0.0
        self._thought_seconds = 0.0  # 思考耗时先归零
        self._stream_start = time.time()  # 记下开始等待的时刻
        self._live = None  # live 模式的正文 Live，等真有内容了再建
        self._refresh_rate = refresh_rate
        self._spinner_stop = threading.Event()
        self._spinner_thread = None
        # 【第 11 轮】正文输出模式：plain = 追加式打印（默认，稳）；live = rich Live（要好看自行开启）
        self._stream_mode = "live" if os.getenv("THINKER_STREAM") == "live" else "plain"
        self._body_started = False  # 【第 11 轮】plain 模式下正文是否已经开始输出（圆点是否已打印）
        # 【第 29 轮】表格对齐的缓冲状态，每轮开始时清干净
        self._plain_at_line_start = True
        self._plain_pending = ""
        self._in_table = False
        self._table_rows = []
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
        """等待动画：转轮 + 动词 + 省略号 + 秒数（2026-11-07 第 10 轮新增，第 19 轮重做）

        每 100ms 重画一次同一行：`\\r`（回到行首）+ 内容 + 补齐空格。

        三条硬规则，都是踩出来的：

        1. **每帧总宽度必须完全一致**。秒数从 (9s) 变 (10s) 会多一列，
           如果这一帧比上一帧短，右边就会留下上一帧的残影。所以每帧都
           pad 到固定 CELLS 列，用空格把旧内容吃掉。
        2. **不依赖 `\\x1b[2K` 清行**。2K 是标准 CSI，Windows Terminal 支持，
           但伪终端里实测清行之后尾巴还在（只能看到粘连的残字）。
           空格覆盖不依赖终端对指令的支持，最稳。
        3. **等待期间隐藏光标**（第 19 轮新增）。规则 1 要补空格，补出来的空白
           会把光标推到文字右侧很远的地方——屏幕上只剩一个方块在空白里闪，
           看起来就是"光标位置不对"。隐藏之后视觉焦点落在转轮上。
           恢复放在 finally，线程异常退出也不会把光标永久藏起来。

        线程是 daemon：万一主流程异常退出，它不会拖住进程。
        """
        try:
            # 延迟 200ms 再出现。模型快速返回时（工具调用轮次常常只有几十毫秒），
            # spinner 根本来不及显示就被擦掉，那一瞬间的"画出-擦掉"就是一次闪烁。
            # 等 200ms 还没结果才显示，短等待彻底安静。
            if self._spinner_stop.wait(0.2):
                return
            # 藏在延迟之后：短等待连一次光标闪动都不会有。
            sys.stdout.write(theme.CURSOR_HIDE)
            sys.stdout.flush()
            # 【第 28 轮】补齐宽度取"固定值"和"终端能放下"的较小者。
            #   文案里带了思考字数（12.3k 这类），长度会变；窗口太窄时如果还按
            #   40 列补齐，补齐的空格会把这一行顶折，转轮就变成两行残影。
            width_cap = min(self._SPINNER_CELLS, max(20, self._console.width - 2))
            while not self._spinner_stop.is_set():
                elapsed = time.time() - self._stream_start
                # 帧内容抽在模块级，可离线测；第 28 轮起把思考字数一起传进去
                plain, styled = build_spinner_frame(elapsed, self._reasoning_chars)
                pad = max(0, width_cap - cell_len(plain))  # 补齐到固定宽度
                sys.stdout.write("\r" + styled + " " * pad + "\x1b[K")
                sys.stdout.flush()
                # 10 帧/秒。之前降成 5 帧是想减少重绘，结果转轮切换得太慢，
                # 视觉上反而"一顿一顿"更像在闪——转得连续才不像故障。
                self._spinner_stop.wait(0.1)
        except Exception:  # spinner 出问题绝不能影响主流程
            pass
        finally:
            # 无论正常结束还是异常，都要把光标还回去。
            # 为什么不用 atexit：它只在进程正常退出时跑，被强杀时也不保证执行；
            # 线程自己是最先知道"我不转了"的，放 finally 覆盖面最大。
            try:
                sys.stdout.write(theme.CURSOR_SHOW)
                sys.stdout.flush()
            except Exception:
                pass

    def _flush_table(self) -> None:
        """把攒着的表格对齐后一次性打出去（2026-11-11 第 29 轮新增）"""
        if not self._table_rows:
            return
        text = _render_markdown_table(self._table_rows)
        self._table_rows = []
        self._console.file.write(text)
        self._console.file.flush()
        self._plain_at_line_start = True

    def _write_plain(self, chunk: str) -> None:
        """plain 模式的正文输出：普通文字照旧逐字吐，表格先攒齐再对齐（2026-11-11 第 29 轮新增）

        【为什么不能简单地把 chunk 直接写出去】
          模型写表格是逐行吐的，`| 改动文件 | 内容 |` 这一行本身没有错，
          错在"每行的竖线位置由模型随手决定 + 中文占两列"。
          要对齐就必须知道**整张表**每一列最宽是多少，也就是必须等表格结束。
          所以这里的策略是：普通文本一个字都不攒（保持逐字手感），
          只有"行首是 |"的时候才切到攒表模式。

        【状态机】
          行首 + 下一个字符是 |   → 进入表格模式，开始攒行（不输出）
          表格模式 + 又一行 |     → 继续攒
          表格模式 + 遇到非 | 行  → 表格结束，先对齐输出整张表，再输出这一行
          不在表格 + 普通文本       → 原样写出去

        【一个刻意的取舍】
          表格从第一行到最后一行之间，屏幕上什么都不显示（只等一两秒）。
          换来的是整张表一次性对齐出现，而不是先歪着出现再跳一下。
          超过 _table_limit 行就不再等，先输出——防止模型写一张巨表时界面看起来卡住。
        """
        self._plain_pending += chunk
        while self._plain_pending:
            if self._in_table:
                newline = self._plain_pending.find("\n")
                if newline < 0:
                    return  # 这一行还没收完，继续等
                line = self._plain_pending[:newline]
                self._plain_pending = self._plain_pending[newline + 1:]
                if _is_table_row(line):
                    self._table_rows.append(line)
                    if len(self._table_rows) >= self._table_limit:
                        # 表太大，不再等"结束"信号，先输出已攒的部分
                        self._flush_table()
                        self._in_table = False
                    continue
                # 遇到第一行非表格：说明表格已经结束，先把它吐出去
                self._flush_table()
                self._in_table = False
                self._console.file.write(line + "\n")
                self._console.file.flush()
                self._plain_at_line_start = True
                continue

            # 不在表格里：行首且以 | 开头，才可能是表格
            if self._plain_at_line_start and self._plain_pending.startswith("|"):
                self._in_table = True
                continue

            newline = self._plain_pending.find("\n")
            if newline < 0:
                # 这半行还没结束，又不是表格：立刻写出去，保住逐字效果
                self._console.file.write(self._plain_pending)
                self._console.file.flush()
                self._plain_at_line_start = False
                self._plain_pending = ""
                return

            piece = self._plain_pending[:newline + 1]
            self._plain_pending = self._plain_pending[newline + 1:]
            self._console.file.write(piece)
            self._console.file.flush()
            self._plain_at_line_start = True

    def _flush_plain(self) -> None:
        """收尾：把还压在缓冲区里的半行/未闭合的表格全部吐出去（2026-11-11 第 29 轮新增）

        为什么必须有这一步：
          模型完全可能"说完一张表就结束"，没有下一行来触发"表格结束"。
          不主动 flush 的话，最后那张表就永远留在内存里，屏幕上什么都看不到——
          用户会以为模型偷懒没写。
        """
        if self._in_table:
            # 表格在半路结束（流断了或者就到此为止）：把最后半行也当表格行处理
            if self._plain_pending.strip():
                self._table_rows.append(self._plain_pending)
            self._plain_pending = ""
            self._flush_table()
            self._in_table = False
            return
        if self._plain_pending:
            self._console.file.write(self._plain_pending)
            self._console.file.flush()
            self._plain_at_line_start = self._plain_pending.endswith("\n")
            self._plain_pending = ""

    def note_thinking(self, chars: int) -> None:
        """记下模型又"想"了多少字（2026-11-11 第 28 轮新增）

        调用方是 conversation 的流式循环：它收到 ThinkingChunk（api_client 从
        reasoning_content 里剥出来的纯计数）就转手调这里。

        为什么只累加、不保存内容：
          界面上要的只是"它在动"，不是"它在想什么"——把思考原文写进屏幕，
          一屏全是内心独白，正文反而找不到了；而且思考内容本来就不该进历史。
          只要数字在涨，用户就能确认进程没死。

        为什么不用加锁：
          只有主线程写、spinner 线程按 100ms 读一次；最坏情况是读到上一帧的旧值，
          下一帧就追上了，对一个"证明我还活着"的计数器来说完全够用。
          加锁反而会在每 100ms 一帧的热路径上引入争用。
        """
        if chars > 0:
            self._reasoning_chars += chars

    def note_tool_call(self, chars: int) -> None:
        """模型正在生成工具参数：在正文下方单独一行画心跳（2026-11-11 第 30 轮新增）

        为什么需要它：
          模型说完"我继续改 effects.js"之后，可能还要好几秒才把 file_edit 的
          参数吐完。这段时间里 api_client 只往本地拼参数、一个正文字都没有，
          而等待段的 spinner 早在第一个正文字到达时就被 print_streaming_content
          停掉且不会重启——屏幕上什么也不动，用户只能判断为死机（实测 6 秒）。
          ToolCallChunk 把"参数又长了一截"送进来，这里就用它刷新一行心跳。

        四条只在同时成立时才画，每一条都是踩过的坑：
          · 正文已经开始：没开始时等待段 spinner 还在转，再画一行会变成两行动画打架；
          · plain 模式：live 模式由 rich Live 重绘正文区，往同一片区域写 \\r
            会覆盖它的帧（第 11 轮把 Live 换成追加式打印就是因为这个）；
          · 真终端：非 tty 时输出常进文件或管道，ANSI 转义码只会变成乱码；
          · THINKER_NO_SPINNER 未开：用户明确关了动画就一行都不该多打。

        参数 chars 是本轮累积到的参数字符数，只用来显示一个递增的数字；
        参数正文一个字都不上屏（里面可能有文件内容或路径）。
        """
        if (not self._stream_has_content
                or self._stream_mode != "plain"
                or not self._is_tty()
                or os.getenv("THINKER_NO_SPINNER") == "1"):
            return

        self._tool_hint_chars += max(0, chars)
        now = time.time()

        if not self._tool_hint_active:
            # 正文最后一行没有换行符，光标停在句子中间。先 _flush_plain 把可能
            # 压着的半行 / 未闭合表格吐完，再写 \n 换到【独立的新行】。
            # 绝不能直接在当前行用 \r 覆盖——那会把刚说出口的正文整个抹掉，
            # 正是第 10 轮"spinner 和正文抢同一行"那个坑。
            self._flush_plain()
            self._console.file.write("\n")
            self._console.file.flush()
            # 镜像进缓冲：stop_stream_display 靠 _stream_buffer 判断"要不要补换行"，
            # 不记账的话它还会再补一次，正文与 Cooked 之间就白多出一行。
            self._stream_buffer += "\n"
            self._tool_hint_active = True
            self._tool_hint_start = now

        elapsed = max(0.0, now - self._tool_hint_start)
        frame = theme.SPIN_FRAMES[int(elapsed * 8) % len(theme.SPIN_FRAMES)]
        tail = f"正在准备工具调用 {_human_chars(self._tool_hint_chars)} 字 · {_human_seconds(elapsed)}"
        plain = f"{frame} {tail}"
        styled = (
            f"{theme.ACCENT_ANSI}{frame}{theme.RESET_ANSI} "
            f"{theme.DIM_ANSI}{tail}{theme.RESET_ANSI}"
        )
        # 和 spinner 同一套宽度口径：补齐到固定列，短帧才不会留下上一帧的残影。
        width_cap = min(self._SPINNER_CELLS, max(20, self._console.width - 2))
        pad = max(0, width_cap - cell_len(plain))
        # \r 只回到【心跳这一行】的行首，正文在上面一行，永远碰不到。
        self._console.file.write("\r" + styled + " " * pad + "\x1b[K")
        self._console.file.flush()

    def _stop_tool_hint(self) -> bool:
        """擦掉工具心跳那一行，返回"本轮确实画过心跳"

        返回值的用途：stop_stream_display 靠它判断"正文与 Cooked 之间的空行
        已经由这一行腾出来了"，从而跳过原本要补的 \\n 和 print()，避免多出一个空行。
        """
        if not getattr(self, "_tool_hint_active", False):
            return False
        self._tool_hint_active = False
        # 空格覆盖而不是只发 2K：第 10 轮实测伪终端里 2K 之后尾巴还在。
        # 空格数按终端宽度收敛，写死 100 个在窄窗口里会自己折行，反而多出一行。
        pad = min(100, max(20, self._console.width - 2))
        # 清完再换行：光标落到下一行行首，被清空的这行正好充当正文与
        # Cooked 之间的空行，不多不少。
        self._console.file.write("\r" + " " * pad + "\r" + "\n")
        self._console.file.flush()
        return True

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
            sys.stdout.write("\r\x1b[2K" + " " * 100 + "\r" + theme.CURSOR_SHOW)
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
        """生成 `  Thought for 3s` 这一行（2026-11-07 第 10 轮新增，第 18.3 轮精简）

        和截图一致：缩进两格、整行暗灰。

        【第 18.3 轮】删掉了原来的 "(ctrl+o to expand)"。
          那个提示对应"展开思考内容"的功能，但从第 10 轮起就没实现过——
          显示一个按了没反应的提示，比不显示更让人困惑：用户会以为是自己没按对。
          功能真做出来了，再把提示加回来也不迟。
        """
        text = Text()
        text.append("  ")
        text.append(f"Thought for {int(round(self._thought_seconds))}s", style="qs.dim")
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
        # 【第 30 轮】正文下方那行"正在准备工具调用"也要收掉，不能把过程提示
        #   永久留在屏幕上。返回值告诉下面：正文与 Cooked 之间的空行已经腾出来了。
        had_tool_hint = self._stop_tool_hint()

        if self._stream_mode == "plain" and self._body_started:
            # 【第 29 轮】先把压在缓冲里的内容吐完（尤其是"说完表格就结束"的情况，
            #   否则最后那张表永远出不来）。这一步必须在补换行之前，
            #   因为 _flush_plain 自己会把结尾补齐。
            self._flush_plain()
            # 【第 11 轮】plain 模式收尾：最后一行还没有换行符（chunk 是续着写的），
            #   这里补一个换行把光标送回行首，后面的空行和 Cooked 行才不会粘在正文上。
            # 【第 26 轮】改成"只在需要时补"：
            #   模型最后一段经常自带结尾的 "\n"（比如以列表/代码块结束），
            #   原来无条件再写一个，正文和空行之间就白多出一整行，
            #   一屏里"● 正文 → 两个空行 → Cooked"看着很散。
            #   判断依据用流式缓冲区，而不是终端光标——缓冲区是我们自己攒的，
            #   一定能准确反映"最后写出去的字符是什么"。
            # 【第 30 轮】画过心跳时：note_tool_call 已写过一个 \n 结束正文行，
            #   _stop_tool_hint 又写过一个 \n 把心跳行清空、腾出一个空行，
            #   两步合起来正好等于原来的"补换行 + print() 空行"。
            #   所以这里两步都跳过，否则正文与 Cooked 之间会白多出两行。
            if not had_tool_hint and not self._stream_buffer.endswith("\n"):
                self._console.file.write("\n")
                self._console.file.flush()
            self._body_started = False
            if not had_tool_hint:
                self._console.print()  # 正文和 Cooked 之间留一个空行
            if show_done:
                self._print_done_line()
                self._console.print()  # Cooked 之后也留一行，和下一轮内容分开
            return

        if self._live:  # 这一轮真的输出了正文（live 模式）
            self._live.stop()
            self._live = None
            # 【第 10 轮】正文和 Cooked 之间留一个空行，对齐 Claude Code 的呼吸感。
            # 为什么是两次 print：
            #   Live 结束时正文行末尾没有换行符，第一次 print 只是"结束这一行"，
            #   第二次才产生真正的空行。tty 下 Live.stop 已经补过一次换行，
            #   所以会变成两个空行——正好，间距再大一点也不挤。
            self._console.print()
            self._console.print()
            if show_done:
                self._print_done_line()
                self._console.print()  # Cooked 之后也留一行，和下一轮内容分开

    def _print_done_line(self) -> None:
        """打印这一轮的收尾行（2026-11-07 第 10 轮新增）

        样式：
            Cooked for 7s · done 11:38

        为什么前面不用 ✻（Claude Code 原版是 `✻ Cooked for 7s`）：
          整个界面上"星号"只应该有一个含义——"正在等模型"。收尾行再挂一个灰 ✻，
          视觉上和等待时的橙 ✻ 撞车，用户会以为"标志在橙和灰之间换色"。
          去掉之后，行首符号只剩 `●`（回复）、`⎿`（结果/状态），
          橙 ✻ 只在真正等待时出现。
        """
        elapsed = max(1, int(round(time.time() - self._stream_start)))  # 至少显示 1s
        finished_at = time.strftime("%H:%M")  # 当地时间的时:分
        text = Text()
        text.append("  ")
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

        # 【第 30 轮】正文与工具参数交错：少数模型 / 中转会在 tool_calls 分片之间
        #   又吐几个字。心跳行是靠 \r 反复覆盖的，必须先把它"定稿"（补换行、
        #   关掉标记），否则后续文字会写在同一行上、再被下一次覆盖抹掉，正文就丢了。
        #   这里不清掉心跳文字、只结束这一行：宁可多留一行过程提示，也不丢内容。
        if getattr(self, "_tool_hint_active", False):
            self._console.file.write("\n")
            self._console.file.flush()
            self._stream_buffer += "\n"
            self._tool_hint_active = False

        self._stream_buffer += chunk  # 把新来的这一小段接到后面
        # 【第 10 轮】只有"有实义的字"才算内容开始。
        #   流式接口经常先发 "\n"、"" 这种空 delta，如果拿它们当"已经有内容"，
        #   界面就会在 spinner 和正文之间反复横跳（这正是橙绿交替闪的第二个成因）。
        # 【第 11 轮】切入正文的判定用 strip()（空白 chunk 不算"有实义的字"），
        #   但正文开始后，空白 chunk（比如段落间的 '\n\n'）必须照写不误——
        #   否则段落之间的空行被吞掉，两段话会粘在同一行上。
        if chunk and chunk.strip() and not self._stream_has_content:
            # 【2026-11-07 第 13.1 轮】_stream_start 为 0 表示没走过 start_stream_display。
            #   此时算"想了多久"没有基准，会得出一个几十亿秒的荒唐数字，
            #   所以只在这种"临时打印"场景下跳过计时，其余逻辑照旧。
            if self._stream_start:
                self._thought_seconds = time.time() - self._stream_start
            self._stream_has_content = True
            self._stop_spinner()  # 先把 spinner 那行擦掉，否则它会和正文抢同一行
            self._console.print()  # 回复块和上面的输入区之间留一个空行
            if self._thought_seconds >= 1:  # 想得太快就别挂 Thought 行了
                self._console.print(self._thought_line())
            if self._stream_mode == "plain":
                # 【第 11 轮】plain 模式：打印绿色圆点后不再换行，
                #   后面的 chunk 直接续写在同一行上，终端自己负责折行。
                self._console.print(
                    f"{theme.BULLET} ", style=f"bold {theme.BULLET_COLOR}", end=""
                )
                self._body_started = True
            else:
                self._start_body_live()  # live 模式：维持原来的 Live 实时渲染
        if self._stream_mode == "plain" and self._body_started and chunk:
            # 【第 11 轮】追加式输出：只往后写、从不回头擦。
            #   写终端而不是走 rich，有两个原因：
            #   1. rich 的每次 print 都按"整行从第 0 列开始"算折行，
            #      不知道光标实际停在哪一列，续写长段会折错位置；
            #   2. 终端原生折行对中文双宽字符的处理最可靠。
            #   写 console.file 而不是直接写 sys.stdout，
            #   是为了让测试可以注入 StringIO 假终端。
            # 【第 29 轮】改走 _write_plain：它会顺手把 markdown 表格攒齐、
            #   按显示列对齐后再输出；普通文字仍然是一来就写，手感不变。
            self._write_plain(chunk)
        # live 模式的后续 chunk 不用手动 update：Live 会按 refresh_per_second 自己重画

    def get_stream_buffer(self) -> str:
        """Get the current stream buffer content.（把缓冲区里的内容取出来）"""
        return getattr(self, "_stream_buffer", "")

    def clear_stream_buffer(self) -> None:
        """Clear the stream buffer.（清空缓冲区）"""
        self._stop_spinner()  # 【第 10 轮】顺手把还在转的 spinner 停掉
        self._stream_buffer = ""
        self._stream_has_content = False  # 连同"已有内容"的标记一起复位
        self._thought_seconds = 0.0
        self._body_started = False  # 【第 11 轮】正文输出标记一并复位
        self._reasoning_chars = 0  # 【第 28 轮】思考字数也清零，下一轮从 0 开始
        # 【第 30 轮】心跳行若还挂在屏幕上，先擦掉再复位状态；
        #   不擦的话，下一轮的第一句输出会直接接在那行后面。
        self._stop_tool_hint()
        self._tool_hint_chars = 0
        self._tool_hint_start = 0.0
        # 【第 29 轮】表格缓冲一并清空：这里不 flush，
        #   clear 的语义就是"这輪内容不要了"，把半张表打出去反而更奇怪。
        self._plain_pending = ""
        self._in_table = False
        self._table_rows = []
        self._plain_at_line_start = True

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
    def _take_cells(text: str, cells: int) -> str:
        """从开头取不超过 cells 列的内容（2026-11-11 第 26 轮新增）

        和 _clip_to_cells 的区别：这里**不加省略号**，纯粹按列切。
        给 shorten 用——它要自己拼"……（共 N 字符）"的尾巴，
        如果先加个省略号再拼，就会变成 "……（共 N 字符）…" 两个结尾。
        """
        if cells <= 0:
            return ""
        used = 0  # 已经占用的列数
        kept = []
        for ch in text:
            width = cell_len(ch)
            if used + width > cells:  # 再放一个就超了，直接停（不会把一个宽字符切成半个）
                break
            kept.append(ch)
            used += width
        return "".join(kept)

    @staticmethod
    def _clip_to_cells(line: str, max_width: int) -> str:
        """按"显示列"截断一行（2026-11-11 第 26 轮新增）

        为什么不用 `line[:max_width]`：
          Python 的 len() 数的是字符个数，中文在终端里占 2 列。
          "战斗核心：固定逻辑坐标" 只有 12 个字符，屏幕上却占 24 列——
          按 len 截断会以为够短，实际早就顶出终端边界被硬折行。
          cell_len 逐字符算列宽（中文 2、ASCII 1、emoji 2），和终端对齐。
        """
        if cell_len(line) <= max_width:
            return line
        # 留 1 列给结尾的省略号，避免"截断后反而比上限宽"
        return DisplayManager._take_cells(line, max_width - 1).rstrip() + "…"

    def preview_lines(self, text, max_lines: int = 3, max_width: Optional[int] = None) -> str:
        """取结果的前几行做预览（2026-11-06 第 4 轮新增，第 26 轮重做）

        为什么不用 shorten 那种"整段折成一行"？
          像 dir、git status 这类输出，整段折行会变成一坨没法读的长句。

        【第 26 轮】改了三件事，都是"工具结果乱成一团"的直接原因：
          1. **不再用 " ⏎ " 把几行拼成一行**。拼接后的总长度立刻超过终端宽度，
             rich/终端只能硬折行，折出来的第二段从第 0 列重新开始，
             行号和内容错位，"1 2 3" 全散在屏幕各处。现在每行独立成行。
          2. **\\t 先展开成空格**。file_read 的行号是 f"{n:>6}\\t内容"，
             终端把 \\t 跳到下一个 8 列边界；行号位数一变（9→10），
             内容起点就跟着跳一列，看着像"行号在抖"。expandtabs(4) 固定成 8 列。
          3. **按显示列截断，宽度随终端自适应**。原来是死值 70 个**字符**，
             中文实际占 140 列，窗口窄一点就折成两行。现在 max_width 默认取
             `控制台宽度 - 7`（⎿ 前缀 5 列 + 2 列余量），保证一行永远放得下。

        参数：
          max_lines —— 最多显示几行正文（不含结尾的"共 N 行"提示）
          max_width —— 每行最多占多少列；None 表示按当前终端宽度自动算
        """
        raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
        # 统一换行后展开 tab、去掉行尾空白，并滤掉空行：
        # 空行占一格却没有任何信息，留着一个空行会让人觉得"结果没打完"。
        lines = [line.rstrip().expandtabs(4) for line in raw.split("\n") if line.strip()]
        if not lines:  # 没有任何内容
            return "（无输出）"
        if max_width is None:
            # 下限 30：终端被压到极窄时，至少还能看出半句话，不至于每行只剩省略号。
            max_width = max(30, self._console.width - 7)
        picked = [self._clip_to_cells(line, max_width) for line in lines[:max_lines]]
        if len(lines) > max_lines:  # 后面还有，就补一句提示（单独一行，不挤在结尾）
            picked.append(f"…（共 {len(lines)} 行）")
        return "\n".join(picked)

    @staticmethod
    def shorten(text, limit: int = 600) -> str:
        """把过长的内容压成一行预览，避免刷屏（2026-11-06 第 2 轮新增）

        为什么需要？
          工具执行结果可能是一整份 npm install 日志（几千行）。
          原样打印会把屏幕刷满，用户根本看不到自己关心的那几行。
          这里把换行折叠成 ⏎ 符号，超长再截断，一眼就能看出"这行在讲什么"。

        【第 26 轮】limit 的单位从"字符"改成"显示列"：
          这个函数的输出最终都会拼在工具标题行（● task(描述)）后面，
          而中文一个字占 2 列。按 60 个字符截，屏幕上其实是 120 列，
          窗口一窄就被终端硬折成两行，第二行从第 0 列顶出来——
          工具标题和它的结果预览就此错位，看着就是"排版全乱"。
          现在按 cell_len 切，60 就是实打实的 60 列。

        注意：`text` 是 dict 之类的兜底情况会先 str() 化，\\r\\n 也一并折叠——
        `_args_preview` 的最后一行就是靠这个兜底，不能因为"不是字符串"就原样吐出。
        """
        text = str(text)  # 保险：传进来的可能是字典或数字
        if cell_len(text) <= limit:  # 没超长（按显示列算），原样返回
            return text
        # 把换行折叠成可见的 ⏎，整段压成一行
        one_line = text.replace("\r\n", " ").replace("\n", " ⏎ ").replace("\r", " ")
        # 留 40 列给后面的「……（共 N 字符，已省略 M）」说明。
        # 用 _take_cells 而不是切片：切片按字符数，中文会被多切一倍宽度。
        head_budget = max(0, limit - 40)
        preview = DisplayManager._take_cells(one_line, head_budget).rstrip()
        omitted = max(0, cell_len(one_line) - cell_len(preview))
        return f"{preview} …（共 {cell_len(one_line)} 列，已省略 {omitted} 列）"


def _restore_cursor_on_exit() -> None:
    """进程退出时兜底恢复光标（2026-11-09 第 19 轮新增）

    为什么需要这层兜底：
      等待模型时我们把光标隐藏了（否则它会悬在补齐空格的右边闪）。
      正常情况下 `_spin_loop` 的 finally 会恢复它，但那只覆盖"线程正常退出"。
      如果主流程直接抛异常退出（Ctrl+C、未捕获异常），daemon 线程在解释器退出时
      会被直接杀掉，finally 不保证执行——终端就会一直看不见光标，
      用户只能 `reset` 或者重开窗口。

    why atexit 而不是把恢复写进 main 的 finally：
      main 里已经有好几层 try/finally，再塞一条恢复语句，以后新增退出路径
      必然漏一处。atexit 只注册一次，所有正常退出路径都会经过它。

    注意：非 tty（重定向到文件）时什么都不写，避免把转义码写进输出文件。
    """
    try:
        if sys.stdout.isatty():
            sys.stdout.write(theme.CURSOR_SHOW)
            sys.stdout.flush()
    except Exception:  # 退出路径上的任何异常都不该阻止进程退出
        pass


atexit.register(_restore_cursor_on_exit)
