# =============================================================================
# 文件：src/ui/line_editor.py
# 作用：自管的行编辑（逐字符读键盘），给斜杠命令做 Tab 补全（2026-11-08 第 16 轮）
#
# 【一、为什么非得自己实现】
#   input() 是个黑盒：它把一行文字一次性交给你，中间按了哪些键你完全不知道。
#   要在 `/cos` 后面按 Tab 补成 `/cost`，必须在**按键级别**接管输入。
#   标准库里没有可用的替代：readline 只在 Unix 有，Windows 要装 pyreadline3，
#   而本项目不加依赖（THINKER.md 写明"保持零依赖"）。
#   msvcrt 是 Windows 自带的，EscWatcher 已经在用它，这里沿用同一套。
#
# 【二、为什么只在"行首是 /"时才接管】
#   这台机器上用户大部分输入是中文。自管行编辑最难做对的就是输入法：
#   组字期间的按键、候选框、上屏时机，每个环节都可能出岔子。
#   而斜杠命令是英文的——为了给 `/cost` 补全，去承担整个中文输入链路的风险，不值得。
#
#   所以策略是：先读**第一个字符**（不阻塞地拿到就判断）
#     · 是 "/"  → 进入本文件的自管模式，享受 Tab 补全
#     · 不是 "/" → 立刻把控制权交回 input()，中文输入的环境和以前一模一样
#   这个判断的代价只有一次按键读取，而普通对话的风险是零。
#
# 【三、历史翻页（第 18 轮新增）】
#   ↑ / ↓ 在行首按，可以翻回之前提交过的输入，再按回车就能把上一句"复制"下来改。
#   这是 Claude Code 里最常用的输入习惯之一，所以补上。
#   只做"整行替换"这一种操作：光标移动、中间插入这些仍然不做——它们要维护光标列、
#   处理中文双宽、终端折行，是独立的坑，而且和"复制上一句"这件事没关系。
#
# 【四、环境不支持的降级】
#   非 Windows（没有 msvcrt）、stdin 不是终端（管道 / CI / 重定向）时，
#   available 为 False，调用方直接走原来的 input()。
#   绝不能让"补全"本身成为启动失败的原因。
# =============================================================================

import os  # commonprefix：求多个候选的最长公共前缀
import sys  # 判断是不是真终端、直接写 stdout
import unicodedata  # 判断一个字符在终端里占几列（中文是双宽）


# --- 按键常量 ---------------------------------------------------------------
# 为什么单独抽出来：散在 if 里全是 "\x08" 这种魔法值，改一处漏一处。
# 注意 Windows 控制台的几个值和其他平台不完全一样，这里是实测值。
KEY_ENTER = ("\r", "\n")  # 回车可能是 \r，也可能是 \n
KEY_BACKSPACE = "\x08"  # Windows 控制台里 Backspace 是 \x08，不是 \x7f
KEY_TAB = "\t"
KEY_CTRL_C = "\x03"  # Ctrl+C，语义要保持和 input() 一致：抛 KeyboardInterrupt
KEY_ESC = "\x1b"
# 方向键、F1-F12、Insert 这些是"两字节键"：先来一个前导字节，再来真正的键码。
# 不认这两个前导字节的话，后面那个键码会被当成普通字符插进输入里，屏幕上就出现乱码。
EXTENDED_PREFIX = ("\x00", "\xe0")

# 两字节键的第二个字节（Windows 控制台的实测值）。
# 注意：这些字母看起来像普通字符，但它们只出现在 EXTENDED_PREFIX 之后，
# 所以不会和"用户真的打了一个 H"混淆。这是第 18 轮为方向键历史补的。
EXT_UP = "H"  # ↑ 上一条历史
EXT_DOWN = "P"  # ↓ 下一条历史
EXT_LEFT = "K"  # ← 光标左移一个字符（第 18.1 轮）
EXT_RIGHT = "M"  # → 光标右移一个字符（第 18.1 轮）
EXT_HOME = "G"  # Home 跳到行首（第 18.1 轮）
EXT_END = "O"  # End 跳到行尾（第 18.1 轮）
EXT_DELETE = "S"  # Delete 删光标处的字符（第 18.1 轮）

# 历史最多留多少条。500 条足够翻回很久之前的输入，又不会吃内存。
HISTORY_LIMIT = 500


def display_width(text: str) -> int:
    """这段文字在终端里占几列（2026-11-08 第 16.2 轮新增）

    为什么需要它：终端是"按列"排版的，而中文、全角标点、部分 emoji 占**两列**。
    退格 `\\b` 只退一列，所以：
      · 擦一个中文要退两格，否则会留下半个字的残影；
      · 算"已经显示了多宽"时也不能用 len()，否则光标位置会算错。

    unicodedata.east_asian_width 的返回值：
      W（Wide）和 F（Fullwidth）是双宽——中文、日文、全角标点都在这里；
      A（Ambiguous）在中文环境下通常按双宽显示，但不同终端不一致，
      这里按单宽算（宁可少擦一格让人看到残留，也不要多擦把提示符擦掉）。
    控制字符理论上宽度是 0，但本模块只会传可打印内容，不做特殊处理。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


class LineEditor:
    """逐字符读键盘、支持 Tab 补全的行编辑器（2026-11-08 第 16 轮新增）

    用法（只应由 InputHandler 调用，不要在外面直接 new）：

        editor = LineEditor()
        if editor.available:
            text = editor.readline("> ", completer=my_completer)

    测试时可以注入按键来源，不必真的敲键盘：

        editor = LineEditor(key_reader=iter(["帮", "你", "好", "\\r"]).__next__)

    completer 的契约：接收"当前已经输入的整行文字"，返回候选列表（完整字符串，
    不需要自己过滤前缀）。返回空列表表示没有候选。
    """

    def __init__(self, key_reader=None):
        """初始化

        参数 key_reader（第 16.2 轮新增）：读一个字符的可调用对象；None 表示真从
        Windows 控制台读。**这个参数是给测试用的**——readline 里的退格、回车、
        Tab、Esc、中文追加全都依赖"能拿到按键"，而自动化测试拿不到真键盘。
        留一个注入口，这些逻辑才能被真正验到。
        第 16 轮就是因为它一段都测不到，才会在"提示符不可编辑"上连翻两次车。
        """
        self._key_reader = key_reader
        self._msvcrt = None
        if key_reader is None:  # 只有真要读真键盘时才去 import
            try:
                import msvcrt  # 只有 Windows 有；其他平台直接降级

                self._msvcrt = msvcrt
            except ImportError:
                self._msvcrt = None

        # --- 历史状态（第 18 轮新增）---
        # 这些要跨"行"保留：一个 LineEditor 实例活多久，历史就记多久。
        # 因为 InputHandler 只在启动时建一个实例，所以效果是"本次运行内的输入都能翻回来"。
        self._history = []  # 本次运行内提交过的输入，最新的在末尾
        self._history_index = None  # 当前翻到第几条；None = 正在编辑自己的草稿
        self._draft = ""  # 翻历史之前手里那半句，按 ↓ 回到这里

    @property
    def available(self) -> bool:
        """当前环境能不能用自管行编辑

        两个条件：有按键来源（注入的，或 msvcrt）+ stdin 是真终端。
        最后一个条件很重要：输出被重定向到文件或管道时，逐字符读会直接卡死，
        因为没有"按键"这回事。
        """
        if self._key_reader is not None:
            return True  # 测试注入的按键来源，永远可用
        if self._msvcrt is None:
            return False
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except (ValueError, AttributeError):
            # stdin 被关掉、或者不是文件对象时 isatty 会抛异常
            return False

    def read_key(self) -> str:
        """读一个按键，返回 str（中文也是一个字符）

        真正的读取只有一行，包一层是为了给测试留注入口：有了它，
        测试可以喂一串预设按键，把 readline 的每个分支（退格、回车、Tab、
        Esc、中文追加）全部跑一遍。没有它，这些逻辑只能靠人肉敲键盘验证。

        【第 16.2 轮为什么要删掉 unget 那套】
          16.1 轮的想法是"先探一个字符判断是不是 /，不是就塞回控制台缓冲区"。
          实际做不到：
            · msvcrt.ungetch 要的是 **bytes**（b"a"），传 str 直接 TypeError；
            · 宽字符版 ungetwch 对中文也会抛 OSError（Errno 0）。
          异常被 except 吞掉后，代码静默退回 `input(prompt + 字符)` 那条老路，
          于是症状和没修一样——而且"吞异常 + 静默降级"让这个失败完全看不出来。
          现在改成：**所有输入都由本类自己读**，不再往 input() 交还字符，
          自然也就不需要"塞回去"这个动作。
        """
        if self._key_reader is not None:
            return self._key_reader()
        return self._msvcrt.getwch()  # 宽字符版，中文能正确返回

    # ------------------------------------------------------------------
    # 补全的核心计算（纯函数，可以脱离键盘单独测）
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_completion(text: str, candidates: list) -> tuple:
        """决定按 Tab 之后应该发生什么（2026-11-08 第 16 轮新增）

        返回 (new_text, show_list)：
          new_text  —— 补全后的整行文字（没有变化就原样返回 text）
          show_list —— 要不要把候选列出来

        三种情况，和 shell 的习惯保持一致：
          1. 没有匹配的候选 → 什么都不改，调用方响铃声提醒"没得补"
          2. 只有一个匹配   → 直接补成它
          3. 多个匹配       → 先补到"最长公共前缀"；如果已经到公共前缀了，再列出候选

        【必须先用 text 过滤，不能直接拿 candidates 算】
          这是第 16 轮写测试时抓到的 bug：一开始只判断 len(candidates)，于是
          输入 `/q` 按 Tab 会被补成 `/cost`——因为"只有一个候选"，但它和 `/q`
          根本没有前缀关系。真实的触发条件是"注册表里恰好只有一条命令"，
          或者补全源只返回了一条。
          更隐蔽的是：这种错误不会崩、不会响铃，用户只会看到自己打的字
          突然被改写成一个自己没想要的命令，然后按回车执行它。

        为什么第 3 种要分两步：`/c` 按 Tab 先补到 `/c`（`cost`/`clear`/`compact`
        的公共前缀就是 `/c`，等于没变），这时该列出来让用户看清有哪些；
        而 `/cos` 按 Tab 能补成唯一的 `/cost`，那就直接补。
        所以"能补就补、补不动就列"，比自己发明循环切换更符合直觉。
        """
        matches = [c for c in candidates if c.startswith(text)]
        if not matches:
            return text, False

        if len(matches) == 1:
            return matches[0], False

        common = os.path.commonprefix(matches)
        if len(common) > len(text):
            return common, False  # 公共前缀比现在长，说明有进展
        return text, True  # 补不动了，把候选列出来

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def readline(self, prompt: str, completer=None) -> str:
        """读一行，支持 Tab 补全、退格、Esc 清空（2026-11-08 第 16 轮新增）

        参数 prompt：要显示的提示符（本来要交给 input()，现在是本类自己写的，
                     所以颜色码照常生效——这也是"提示符消失"能一并修掉的原因之一：
                     屏幕上所有字符都由这一处控制，不会再有 rich 和 input() 抢顺序）
        参数 completer：候选来源，见类文档

        返回：用户输入的整行（不含末尾回车）
        异常：Ctrl+C 时抛 KeyboardInterrupt，和 input() 行为一致——
              上层（conversation.py）本来就有处理它的逻辑，不要改成别的异常类型。

        【第 16.2 轮：去掉了 initial 参数】
          它是 16 轮"先探一个字符"留下的产物。现在第一个字符也由本方法自己读，
          不存在"已经被别人读走"的情况，这个参数就没意义了。
          留着会让后来人以为还有"预填充"这回事，反而误导。
        """
        buffer = []  # 用 list 而不是 str：中间插入 / 删除都不用反复拼字符串
        cursor = 0  # 光标在 buffer 里的下标，0=行首，len(buffer)=行尾（第 18.1 轮）
        self._history_index = None  # 每次开始新的一行，历史位置归零
        self._draft = ""  # 草稿也清掉，避免把上一行的半句话带进来
        self._write(prompt)

        while True:
            ch = self.read_key()  # 阻塞，读一个字符（中文也能拿到）

            if ch in KEY_ENTER:
                self._write("\n")  # 自己换行：逐字符模式下回车不会自动换行
                text = "".join(buffer)
                self._remember(text)  # 先记历史再返回（顺序反了会漏记刚提交的这句）
                return text

            if ch == KEY_CTRL_C:
                # 保持 input() 的语义。这里不吞掉，交给上层决定（退出还是取消）
                raise KeyboardInterrupt

            if ch in EXTENDED_PREFIX:
                # 两字节键：把后面那个真正的键码读到，按方向键/Home/End/Delete 处理。
                code = self.read_key()
                old_cursor = cursor
                if code == EXT_UP:
                    self._history_up(buffer, prompt)
                    cursor = len(buffer)  # 翻历史后光标落到行尾
                elif code == EXT_DOWN:
                    self._history_down(buffer, prompt)
                    cursor = len(buffer)
                elif code == EXT_LEFT:
                    cursor = max(0, cursor - 1)
                elif code == EXT_RIGHT:
                    cursor = min(len(buffer), cursor + 1)
                elif code == EXT_HOME:
                    cursor = 0
                elif code == EXT_END:
                    cursor = len(buffer)
                elif code == EXT_DELETE:
                    if cursor < len(buffer):
                        old_text = "".join(buffer)
                        buffer.pop(cursor)
                        self._paint(prompt, old_text, "".join(buffer), cursor)
                # 光标动了就重定位（历史翻页/Delete 已自己重画，跳过）
                if code in (EXT_LEFT, EXT_RIGHT, EXT_HOME, EXT_END):
                    self._move_cursor("".join(buffer), old_cursor, cursor)
                # 其余两字节键（F1-F12、Insert 等）不处理，键码已吃掉
                continue

            if ch == KEY_BACKSPACE:
                if cursor == 0:
                    # 行首退格：没东西可删，什么都不做（和大多数编辑器一致）
                    continue
                old_text = "".join(buffer)
                removed = buffer.pop(cursor - 1)  # 删的是光标**前面**那个字符
                cursor -= 1
                self._paint(prompt, old_text, "".join(buffer), cursor)
                continue

            if ch == KEY_TAB:
                self._handle_tab(buffer, prompt, completer)
                cursor = len(buffer)  # 补全后光标统一落到行尾
                continue

            if ch == KEY_ESC:
                # 输入行里按 Esc：清空当前内容（和 Claude Code 一致）。
                # 注意这和"等模型回复时按 Esc 打断"是两件事，那个由 EscWatcher 负责，
                # 两者运行时机不重叠：一个在等用户打字，一个在等模型输出。
                if buffer:
                    self._erase_text("".join(buffer))
                    buffer.clear()
                    cursor = 0
                continue

            # 其余一律当普通字符（中文、空格、数字、符号）：在光标处插入
            old_text = "".join(buffer)
            buffer.insert(cursor, ch)
            cursor += 1
            self._paint(prompt, old_text, "".join(buffer), cursor)

    # ------------------------------------------------------------------
    # Tab 的具体处理
    # ------------------------------------------------------------------

    def _handle_tab(self, buffer: list, prompt: str, completer) -> None:
        """按了一次 Tab（2026-11-08 第 16 轮新增）

        参数 buffer 是**原地修改**的：这样调用方不用接收返回值，
        和 readline 里其他键的处理方式保持一致。
        """
        if completer is None:
            self._bell()
            return

        text = "".join(buffer)
        try:
            candidates = completer(text) or []
        except Exception:
            # 补全源出问题（比如读记忆目录失败）绝不能让整行输入崩掉——
            # 用户只是想打个命令，不该因为补全的副作用失去已经输入的内容。
            self._bell()
            return

        # 先按当前输入过滤一遍。resolve_completion 内部也会过滤（幂等），
        # 这里单独算是为了下面"列出候选"时只列**相关的**那几条：
        # 否则输入 `/c` 按 Tab 会把全部 7 条命令都刷到屏幕上。
        matches = [c for c in candidates if c.startswith(text)]
        new_text, is_show_list = self.resolve_completion(text, matches)

        if new_text != text:
            # 补全到了"一条完整候选"时，自动补一个空格。
            # 理由：`/cos` 补成 `/cost` 之后，用户接下来要做的事十有八九是输参数，
            # 而 `/memory`、`/init` 这些命令几乎都是带参数用的。让它省一次空格键。
            # 判断条件是 new_text in matches，不能用 len==1：
            # `/c` 补成 `/c`（多个候选的公共前缀，不是完整命令）时不能加空格，
            # 否则用户会被卡在 `/c ` 上，再也打不出 `/clear`。
            if new_text in matches:
                new_text += " "
            self._redraw(buffer, prompt, new_text)
            return

        if is_show_list:
            # 候选列在下一行，然后重画提示符和已输入内容，用户可以直接接着打
            self._write("\n" + "  ".join(matches) + "\n")
            self._write(prompt + text)
            return

        # 走到这里说明：有候选但一个都没法补（比如候选和当前输入完全相同），响一声
        self._bell()

    # ------------------------------------------------------------------
    # 历史翻页（第 18 轮新增）
    # ------------------------------------------------------------------

    def _remember(self, text: str) -> None:
        """把刚提交的一行记进历史（第 18 轮新增）

        三条规则都是有原因的：
          · 空行不记——按了一堆回车再来翻历史，翻到的全是空行，等于没有历史。
          · 和上一条相同不记——连着问两遍同一句话很常见，
            重复记录会让 ↑ 按两次才动一格，手感很差。
          · 历史满了从头部删——要淘汰的是最久远的，而 list.append 在尾部。
        """
        if not text.strip():
            return
        if self._history and self._history[-1] == text:
            return
        self._history.append(text)
        if len(self._history) > HISTORY_LIMIT:
            del self._history[0]

    def _history_up(self, buffer: list, prompt: str) -> None:
        """↑：往上翻一条历史（第 18 轮新增）

        第一次按 ↑ 时先把"手里正在打的半句话"存进 _draft，之后按 ↓ 能原样还回来。
        没有这一步的话，用户打了一半想看看上次问了什么，一按 ↑ 半句话就没了——
        这是所有行编辑器的经典坑，也是最招人烦的一个。
        """
        if not self._history:
            self._bell()  # 没有历史：响一声，别静默什么都不做
            return

        if self._history_index is None:
            self._draft = "".join(buffer)  # 存草稿
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            self._bell()  # 已经是最老的一条
            return

        self._replace_line(buffer, prompt, self._history[self._history_index])

    def _history_down(self, buffer: list, prompt: str) -> None:
        """↓：往下翻一条历史，翻到底就还回草稿（第 18 轮新增）"""
        if self._history_index is None:
            self._bell()  # 本来就在草稿上，下面没有东西了
            return

        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            text = self._history[self._history_index]
        else:
            self._history_index = None
            text = self._draft  # 回到用户原本打的那半句

        self._replace_line(buffer, prompt, text)

    def _replace_line(self, buffer: list, prompt: str, new_text: str) -> None:
        """整行换成 new_text（第 18 轮新增，供历史翻页用）

        复用 _redraw 的"擦旧写新"逻辑，语义就是"整行替换"。
        单独包一层是为了让历史翻页的意图更清楚，而不是散在 _history_up/down 里各写一遍。
        """
        self._redraw(buffer, prompt, new_text)

    # ------------------------------------------------------------------
    # 显示相关的小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _write(text: str) -> None:
        """直接写 stdout 并立刻刷新（2026-11-08 第 16 轮新增）

        为什么不用 print：print 每次会多一个换行，而这里大多数时候要的是
        "写一个字符、不换行"。为什么要 flush：不刷新的话字符会攒在缓冲区里，
        用户按键后屏幕上什么都不出现，观感上就是"卡住了"。
        """
        sys.stdout.write(text)
        sys.stdout.flush()

    def _bell(self) -> None:
        """响一声（表示"这里没有可补全的内容"）

        为什么要响铃而不是打印一行提示：提示会不断堆在屏幕上，把对话顶走。
        一声铃是终端的惯例，用户立刻能懂，而且不留痕迹。
        """
        self._write("\a")

    def _erase_text(self, text: str) -> None:
        """把屏幕上已经显示出来的这段文字擦掉（2026-11-08 第 16.2 轮重写）

        【16.2 轮改了什么】
          16 轮这里叫 _erase_display(count)，直接 `"\\b \\b" * count`——
          按**字符个数**擦，一个字符退一格。
          中文占两列，退一格只擦掉一半，屏幕上留下半个字的残影。
          用户主要输入中文，按退格就会花屏。

          现在按 display_width 算真实列数：中文退两格，ASCII 退一格。
          动作不变，仍然是"退格 → 画空格 → 再退格"这套标准三步，
          只是重复的次数从"字符数"变成了"列数"。
        """
        width = display_width(text)
        if width > 0:
            self._write("\b \b" * width)

    def _redraw(self, buffer: list, prompt: str, new_text: str) -> None:
        """把当前行改写成 new_text（2026-11-08 第 16 轮新增，16.2 轮简化）

        现在只有一个路径：按显示宽度擦掉旧的、再写新的，原地替换。

        16 轮这里分了两条路（纯 ASCII 原地擦、含中文换行重画），
        是因为当时擦除只能按字符数算、遇到中文会残留半截。
        有了 display_width 之后这个区别没必要了，而"换行重画"其实更差：
        它会在屏幕上留一行旧的，用户往上翻会看到一串中间状态。

        【第 18.1 轮】改成调 _paint，光标落到行尾。历史翻页 / Tab 补全还在用它。
        """
        self._paint(prompt, "".join(buffer), new_text, len(new_text))
        buffer.clear()
        buffer.extend(new_text)

    def _paint(self, prompt: str, old_text: str, new_text: str, cursor: int) -> None:
        """把屏幕上这一行从 old_text 改成 new_text，光标落在第 cursor 个字符后（第 18.1 轮新增）

        为什么要分快慢三路，而不是一律擦掉重画：
          一律重画 = 每敲一个字符都要"擦 40 列 + 重写 40 列"，中文长句下
          屏幕上会出现明显抖动。而绝大多数按键是"在末尾追加一个字符"，
          这种情况直接写那一个字符就够了，零重画。

        三条路按代价从低到高：
          1. 纯追加（光标在末尾、旧内容是新内容的前缀）→ 只写增量
          2. 末尾退格删一个字符 → 只擦那一个字符（按列宽）
          3. 其他（中间插入 / 中间删除 / 整行替换）→ 回到行首、空格覆盖、重写

        【擦除宽度取 max(删前, 删后)】——中间删除时删后更短，重写后行尾会残留
        "被删掉的那几列"空格，所以必须擦到删前和删后两者中更宽的那个。
        """
        if old_text == new_text:
            return  # 内容没变（光标移动由 _move_cursor 负责），别白画一遍

        # 路 1：纯追加
        if cursor == len(new_text) and new_text.startswith(old_text):
            self._write(new_text[len(old_text):])
            return

        # 路 2：末尾退格删一个字符
        if (
            cursor == len(new_text)
            and old_text.startswith(new_text)
            and len(old_text) - len(new_text) == 1
        ):
            self._erase_text(old_text[-1])
            return

        # 路 3：整行重画。total 取删前/删后的较大可见宽度（含提示符），
        # 少算会留上一行的尾巴，多算会把左边的输出擦掉。
        old_width = display_width(prompt) + display_width(old_text)
        new_width = display_width(prompt) + display_width(new_text)
        total = max(old_width, new_width)
        self._write("\r" + " " * total + "\r")
        self._write(prompt + new_text)
        # 写完后光标在行尾，退回目标位置：数一下光标右边还有多少列
        back = display_width(new_text[cursor:])
        if back > 0:
            self._write("\b" * back)

    def _move_cursor(self, text: str, old: int, new: int) -> None:
        """只移动光标，不改内容（第 18.1 轮新增）

        为什么不复用 _paint 的"整行重画"：按一下 ← 就重画整行，屏幕会闪一下，
        连按几下特别明显。而移动光标有更便宜的做法：
          · 往左：按"被跨过的字符的列宽"退格（中文退两格）
          · 往右：把跨过的那段**重写一遍**，光标自然落到目标位置
                  看起来像重画，但只写一个字符，且写的是同一内容，看不出变化。

        边界：往左退格到列 0 就停（\b 不会换行），所以这一行如果折行了，
        Home 只会退到"当前这一屏行"的行首。这是不用 ANSI 的代价，折行是少数情况。
        """
        if new < old:
            self._write("\b" * display_width(text[new:old]))
        elif new > old:
            self._write(text[old:new])
