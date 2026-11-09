# =============================================================================
# 文件：test/test_line_wrap.py
# 作用：测试输入层的「软换行 / 跨行编辑」——位置换算 + 受控分帧重绘 + 屏幕无残字。
#
# 【第 18.8 轮重写了验证方式】
#   18.6 轮靠"数输出里 \x1b[1A 出现几次"来验跨行退格。这个断言太弱：
#   纯函数和端到端都绿了，真机 conhost 上还是删不干净。三个真根因都没被覆盖：
#     ① theme.prompt_text() 带颜色转义码，旧 display_width 把它算成十几列；
#     ② \b 在行首不跨行，被删的字符留在下一行没人擦；
#     ③ CHA \x1b[nG 与 conhost 的 autowrap 时机算不准行列。
#   现在生产实现改成"受控分帧重绘"（自己切行、自己下移光标），
#   测试也跟着升级：除了纯函数算例，还用一个假的二维终端按 VT 语义执行
#   真实输出序列，最后直接检查屏幕上有没有残留字符。
#   "buffer 删空了"不等于"屏幕上没字了"——用户报的正是后者。
#
# 为什么要注入固定宽度（cols=10）：
#   真实终端宽度取决于跑测试那台机器的窗口大小。不注入的话，
#   同一份断言在 80 列和 120 列下结果不同，测试会变成"在我机器上能过"。
#
# 全程离线：不启终端、不读键盘、不调模型。
# 运行方式（项目根目录）：
#   python test/test_line_wrap.py
# =============================================================================

import os  # 拼路径
import sys  # 改模块搜索路径 + 失败时非 0 退出
import traceback  # 失败时打印现场
import unicodedata  # 假终端里判断中文占两列

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from ui import theme  # 拿真机同款提示符（带 ANSI 颜色码）——根因回归的关键
from ui.line_editor import (  # noqa: E402
    LineEditor,
    KEY_BACKSPACE,
    display_width,
    strip_ansi,
    split_visible_lines,
)


class FakeScreen:
    """一个最小的二维终端模拟器（第 18.8 轮新增）

    为什么需要：18.6 轮的教训是"数 \x1b[1A 的次数"证明不了屏幕干净。
    用户报的是"第一行留下最后一个字符"——那是**视觉残留**，buffer 其实已经删空。
    只有把程序真正写出去的字节流按终端语义执行一遍，才能断言"屏幕上没字了"。

    支持本实现用到的全部指令：\r 回行首、\n 下移一行、\b 退一格、
    CSI A/B/C（上移/下移/右移）、SGR 颜色码（忽略但绝不当成可见字符）、
    普通字符（中文占两列）。不支持的指令一律忽略，不影响本题结论。

    注意：这里模拟的是"光标移动指令都生效"的理想终端。如果某个终端连
    \x1b[1B 都不认，真机仍可能异常——但那是终端能力问题，不是这段渲染逻辑的问题。
    """

    def __init__(self, cols):
        self.cols = cols
        self.rows = {}  # row -> 该行的字符数组（每个 cell 一列）
        self.r = 0
        self.c = 0

    def _cells(self, row):
        return self.rows.setdefault(row, [" "] * self.cols)

    def write(self, chunk):
        i = 0
        n = len(chunk)
        while i < n:
            ch = chunk[i]
            if ch == "\x1b" and i + 1 < n and chunk[i + 1] == "[":
                # CSI：跳过参数字节，读最终字节；只解释会改光标位置的 A/B/C/D。
                j = i + 2
                while j < n and not ("@" <= chunk[j] <= "~"):
                    j += 1
                if j >= n:
                    break
                params, final = chunk[i + 2:j], chunk[j]
                if final in "ABCD":
                    try:
                        count = int(params.split(";")[0] or "1")
                    except ValueError:
                        count = 1
                    if final == "A":
                        self.r = max(0, self.r - count)
                    elif final == "B":
                        self.r += count
                    elif final == "C":
                        self.c = min(self.cols - 1, self.c + count)
                    else:
                        self.c = max(0, self.c - count)
                i = j + 1
                continue
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.r += 1
                self.c = 0
            elif ch == "\b" or ch == "\x08":
                self.c = max(0, self.c - 1)
            else:
                w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                cells = self._cells(self.r)
                if self.c < self.cols:
                    cells[self.c] = ch
                    if w == 2 and self.c + 1 < self.cols:
                        cells[self.c + 1] = ""  # 双宽字符占掉的第二格
                self.c += w
            i += 1

    def visible(self):
        """返回所有行拼起来的可见文本（双宽占位符与空格不算内容）"""
        return "\n".join(
            "".join(self.rows.get(r, [" "] * self.cols))
            for r in sorted(self.rows)
        )


class ScriptedEditor(LineEditor):
    """用固定按键序列驱动 readline，并记录写出的每个片段

    为什么要端到端再测一次：
      `_cursor_rowcol` 算得对，不代表调用处判得对。这个 bug 第一版就栽在
      "判据写成删除前 col == 0"——纯函数测试全绿，真实退格还是删不掉。
      第 18.8 轮进一步把输出喂给 FakeScreen，直接看最终屏幕有没有残字。
    """

    def __init__(self, keys, cols=10, screen=None, rows=20):
        self._keys = list(keys)
        self.notes = []
        self._cols = cols
        self._rows = rows  # 【第 20.1 轮】留白按窗口高度算，测试必须注入固定行高
        self._screen = screen
        super().__init__(key_reader=self._next_key)

    def _next_key(self):
        if not self._keys:
            raise AssertionError("按键序列用完了")
        return self._keys.pop(0)

    def _write(self, text):
        self.notes.append(text)
        if self._screen is not None:
            self._screen.write(text)

    def _term_cols(self):  # 覆盖真实终端宽度，让结果不依赖跑测试的机器
        return self._cols

    def _term_rows(self):  # 覆盖窗口高度，让留白行数在测试里是确定值
        return self._rows


def _assert_screen_clean(editor, screen, marker):
    """公共断言：buffer 删空 + 屏幕无残字 + 不再发 18.6 轮那个 CHA 指令

    三条分别对应三个真机根因：
      · buffer 空：编辑逻辑本身对；
      · 屏幕无 marker：视觉上没有"第一行留下的最后一个字符"；
      · 无 CHA：确认没有回退到"上移一行 + 跳列"那条已被证伪的老路。
    """
    import re  # 只在这里用来找 CSI 序列，不值得提到文件头

    out = "".join(editor.notes)
    assert not re.search(r"\x1b\[[0-9;]*G", out), "又发出了 CHA \\x1b[nG，说明退回了 18.6 轮的老实现"
    assert "\x1b[1B" in out, "没有逐行下移指令，说明没走受控分帧重绘"
    assert marker not in screen.visible(), (
        "屏幕上还留着输入字符（用户看到的'第一行最后一个字符'）：\n"
        + screen.visible()
    )


def test_backspace_crosses_visual_line():
    """两行输入全部退格删空，屏幕上不许留下任何字符

    用真机同款提示符 theme.prompt_text()（带 ANSI 颜色码）：18.6 轮的测试
    全部传干净的 "> " 或空串，恰好把"颜色码被算成可见宽度"这个根因漏掉。
    """
    print("测试 折行: 端到端退格（两行，带真机提示符）")

    screen = FakeScreen(cols=10)
    editor = ScriptedEditor(
        ["a"] * 11 + [KEY_BACKSPACE] * 11 + ["\r"], cols=10, screen=screen
    )
    text = editor.readline(theme.prompt_text())

    assert text == "", f"11 次退格应该删空，实际剩下 {text!r}"
    _assert_screen_clean(editor, screen, "a")

    print("  ✓ 通过")


def test_backspace_crosses_two_lines():
    """占三行时退格同样要删得干净

    为什么单独测：行数越多，_frame_rows / _frame_cursor_row 这两个状态量
    越容易算错一次。18.6 轮只测了"上移指令出现几次"，这里直接看最终屏幕。
    """
    print("测试 折行: 端到端退格（三行）")

    screen = FakeScreen(cols=10)
    editor = ScriptedEditor(
        ["a"] * 21 + [KEY_BACKSPACE] * 21 + ["\r"], cols=10, screen=screen
    )
    text = editor.readline(theme.prompt_text())

    assert text == ""
    _assert_screen_clean(editor, screen, "a")

    print("  ✓ 通过")


def rowcol(prompt, text, cursor, cols=10):
    """简写：调被测函数"""
    return LineEditor._cursor_rowcol(prompt, text, cursor, cols)


def test_single_line():
    """短输入：永远在第 0 行，列数等于宽度"""
    print("测试 折行: 单行")

    assert rowcol("> ", "abc", 3) == (0, 5)  # "> " 2 列 + abc 3 列
    assert rowcol("", "", 0) == (0, 0)  # 空输入
    assert rowcol("> ", "abcd", 2) == (0, 4)  # 光标在中间时只算到 cursor

    print("  ✓ 通过")


def test_exactly_full_line():
    """刚好写满一行：col == cols（终端 autowrap 的"待换行"状态）

    这是最容易算错的边界：写满第 10 列时，光标还在第 10 列等着，
    并没有换行。如果实现写成 col >= cols 就换行，这里会错一列，
    紧接着的退格就会删到上一行去。
    """
    print("测试 折行: 正好写满")

    assert rowcol("", "a" * 10, 10) == (0, 10)  # 满行但没换行
    assert rowcol("", "a" * 11, 11) == (1, 1)  # 第 11 个字符换到第二行

    print("  ✓ 通过")


def test_wrap_to_second_line():
    """超出后换到第二行，行列都要对"""
    print("测试 折行: 跨行")

    assert rowcol("", "a" * 15, 15) == (1, 5)
    assert rowcol("", "a" * 20, 20) == (1, 10)  # 第二行也正好写满
    assert rowcol("", "a" * 21, 21) == (2, 1)  # 第三行

    print("  ✓ 通过")


def test_prompt_counts_toward_width():
    """提示符也算宽度——它占掉的列会让折行更早发生"""
    print("测试 折行: 提示符计入")

    # "> " 占 2 列，再写 8 个字符正好满 10 列
    assert rowcol("> ", "a" * 8, 8) == (0, 10)
    # 第 9 个字符换行
    assert rowcol("> ", "a" * 9, 9) == (1, 1)

    print("  ✓ 通过")


def test_wide_chars():
    """中文占两列：折行点比按字符数算得更早"""
    print("测试 折行: 中文双宽")

    # 10 列可以放 5 个中文
    assert rowcol("", "中" * 5, 5) == (0, 10)
    # 第 6 个中文换行
    assert rowcol("", "中" * 6, 6) == (1, 2)
    # 混合：3 个中文（6 列）+ 4 个 ASCII（4 列）= 10 列
    assert rowcol("", "中中中" + "abcd", 7) == (0, 10)

    print("  ✓ 通过")


def test_backspace_at_line_start_is_detected():
    """自然折行下的行列换算边界（第 18.8 轮起仅作诊断算例）

    生产渲染已不再依赖这个判据——conhost 的自动换行时机和它假设的不一样。
    保留这些算例是为了把"字符下标 → 行列"的规则钉住，排查显示问题时有对照。
    原先容易写错的判据是"删除前光标在第 0 列"——
    真实终端里第二行有 1 个字时，删掉它之后才回到上一行末尾，
    **删除前 col 是 1、不是 0**。所以要比较删除前后的 row：
      · 删前 (1, 1) → 删后 (0, 10)：row 从 1 变 0，跨行了，需要上移；
      · 同一行内退格：row 不变，走普通擦除。
    """
    print("测试 折行: 跨行判定")

    cols = 10

    # 第二行只有 1 个字符：删掉它要回到第一行末尾（满行）
    assert rowcol("", "a" * 11, 11, cols) == (1, 1)
    assert rowcol("", "a" * 10, 10, cols) == (0, 10)
    assert rowcol("", "a" * 10, 10, cols)[0] < rowcol("", "a" * 11, 11, cols)[0]

    # 同一行内退格：行号不变，不需要上移
    assert rowcol("", "a" * 15, 15, cols)[0] == rowcol("", "a" * 14, 14, cols)[0] == 1

    # 中文跨行：第 6 个中文换行，删掉它要回到第一行最后一列（第 10 列）
    assert rowcol("", "中" * 6, 6, cols) == (1, 2)
    assert rowcol("", "中" * 5, 5, cols) == (0, 10)

    print("  ✓ 通过")


def test_no_negative_or_overflow():
    """cursor 传越界值不能崩（防御性断言）"""
    print("测试 折行: 越界防御")

    # cursor 超过文本长度时按全文算（切片本身就会截断，不会抛异常）
    assert rowcol("", "abc", 99, 10) == (0, 3)
    # cols 最小 1，不能除零
    assert rowcol("", "abc", 3, 1) == (2, 1)

    print("  ✓ 通过")


def test_bottom_padding_default():
    """不设环境变量时，留白按窗口高度给半屏（第 20.1 轮）

    用户第二次反馈"只能往上滚一点点、回不到提问那行"：conhost 的滚动范围
    ≈ 已写过的总行数 − 一屏，内容不足一屏时几乎滚不动，全靠底部留白撑。
    固定 4 行不够，改成 max(8, 行数//2)。这里注入 20 行窗口 → 10 行留白。
    """
    print("测试 留白: 默认半屏（20 行窗口 → 10 行）")

    editor = ScriptedEditor(["\r"], cols=10, rows=20)  # 什么都不输入，直接回车
    editor.readline("")
    out = "".join(editor.notes)

    assert out.startswith("\n" * 10 + "\x1b[10A"), f"留白序列不对：{out[:16]!r}"

    print("  ✓ 通过")


def test_padding_scales_with_window():
    """窗口越高留白越多；矮窗口至少保底 8 行"""
    print("测试 留白: 跟随窗口高度")

    for rows, expected in ((40, 20), (16, 8), (24, 12)):
        editor = ScriptedEditor(["\r"], cols=10, rows=rows)
        editor.readline("")
        out = "".join(editor.notes)
        assert out.startswith("\n" * expected + "\x1b[{}A".format(expected)), (
            f"{rows} 行窗口应留 {expected} 行，实际：{out[:20]!r}"
        )

    print("  ✓ 通过")


def test_padding_env_overrides_window():
    """显式设置 THINKER_INPUT_PADDING 时以它为准（想滚更远就自己调）"""
    print("测试 留白: 环境变量优先")

    os.environ["THINKER_INPUT_PADDING"] = "3"
    try:
        editor = ScriptedEditor(["\r"], cols=10, rows=40)
        editor.readline("")
        out = "".join(editor.notes)
        assert out.startswith("\n\n\n\x1b[3A"), f"环境变量没生效：{out[:16]!r}"
        assert not out.startswith("\n" * 20), "不该再按窗口高度算"
    finally:
        os.environ.pop("THINKER_INPUT_PADDING", None)

    print("  ✓ 通过")


def test_bottom_padding_can_be_disabled():
    """THINKER_INPUT_PADDING=0 时完全不写留白序列"""
    print("测试 留白: 可关闭")

    os.environ["THINKER_INPUT_PADDING"] = "0"
    try:
        editor = ScriptedEditor(["\r"], cols=10)
        editor.readline("")
        out = "".join(editor.notes)
        # 关掉留白后，整段输出就只剩"直接回车"自己写的那一个换行。
        # 这么断言比"不以 \n\n 开头"更严：连上移指令都不许漏出来。
        assert out == "\n", f"关闭后不该有任何留白序列：{out!r}"
    finally:
        os.environ.pop("THINKER_INPUT_PADDING", None)

    print("  ✓ 通过")


def test_ansi_prompt_width():
    """【根因回归】带颜色码的提示符只占 2 列，不能把转义码算进宽度

    18.6 轮所有测试都传干净的 "> "，所以完全没覆盖真机用的
    theme.prompt_text() = "\\x1b[38;5;209m>\\x1b[0m "。旧 display_width
    把它算成十几列，折行点提前、擦除宽度多算，屏幕上就会冒出孤立残字。
    """
    print("测试 根因: ANSI 提示符宽度")

    prompt = theme.prompt_text()
    assert "\x1b[" in prompt, "测试用的提示符必须是真机那种带颜色码的"
    assert strip_ansi(prompt) == "> "
    assert display_width(prompt) == 2, f"颜色码被算进宽度了：{display_width(prompt)} 列"

    # 提示符占 2 列，10 列宽的终端还能放 8 个字符；第 9 个才换行。
    assert rowcol(prompt, "a" * 8, 8) == (0, 10)
    assert rowcol(prompt, "a" * 9, 9) == (1, 1)

    print("  ✓ 通过")


def test_split_visible_lines():
    """分帧：每行不超过 cols-1 列、颜色码完整保留、中文不被劈开

    cols-1 是刻意的：写满最后一列会触发终端自动换行，而 conhost / xterm
    的换行时机不同；少一列就能让行与行之间的下移完全由代码控制。
    """
    print("测试 分帧: 切行规则")

    prompt = theme.prompt_text()
    text = "a" * 20
    lines = split_visible_lines(prompt + text, cols=10)
    assert len(lines) >= 3
    assert all(display_width(line) <= 9 for line in lines), "某行写满了，会触发自动换行"
    assert "".join(lines) == prompt + text, "切行不能丢字符、也不能拆坏颜色码"
    assert lines[0].startswith("\x1b[38;5;209m"), "颜色码整段被拆开/丢掉了"
    assert all("\x1b" not in line for line in lines[1:]), "颜色码被切到了后续行"

    cn = "中" * 10
    cn_lines = split_visible_lines(cn, cols=6)  # 每行上限 5 列 → 2 个中文
    assert all(display_width(line) <= 5 for line in cn_lines)
    assert "".join(cn_lines) == cn, "中文双宽字符没有被完整保留"

    print("  ✓ 通过")


def test_frame_pos():
    """光标定位：段末与下一行行首是**两个不同位置**

    remaining 恰好等于本行宽度时，光标指的是下一行第一个字符，
    写成 <= 会让它在上一行末尾多停一行，接着打字就叠在行尾。
    """
    print("测试 分帧: 光标定位边界")

    lines = split_visible_lines("> " + "a" * 11, cols=10)  # 段0 宽 9、段1 宽 4
    assert len(lines) == 2
    assert LineEditor._frame_pos(lines, "> ", "a" * 11, 0) == (0, 2)
    assert LineEditor._frame_pos(lines, "> ", "a" * 11, 7) == (1, 0), "段末必须落到下一行行首"
    assert LineEditor._frame_pos(lines, "> ", "a" * 11, 11) == (1, 4)

    print("  ✓ 通过")


def test_partial_delete_screen_matches():
    """删到一半时，屏幕上的字数必须和 buffer 完全一致

    只断言"删空后没残字"还不够：擦过头（把不该擦的字抹掉）也是错。
    这里数屏幕上 'a' 的个数，多了是残字、少了是擦过头。
    """
    print("测试 屏幕: 部分删除后字数一致")

    screen = FakeScreen(cols=10)
    editor = ScriptedEditor(
        ["a"] * 15 + [KEY_BACKSPACE] * 6 + ["\r"], cols=10, screen=screen
    )
    text = editor.readline(theme.prompt_text())

    assert text == "a" * 9
    assert screen.visible().count("a") == 9, (
        f"屏幕上的字数对不上（残字或擦过头）：{screen.visible()!r}"
    )

    print("  ✓ 通过")


def test_chinese_multiline_clean():
    """中文双宽输入跨行后退格，同样不许留半截字"""
    print("测试 屏幕: 中文跨行退格无残字")

    chars = "中文输入测试一二三四"
    screen = FakeScreen(cols=10)
    editor = ScriptedEditor(
        list(chars) + [KEY_BACKSPACE] * len(chars) + ["\r"],
        cols=10,
        screen=screen,
    )
    text = editor.readline(theme.prompt_text())

    assert text == ""
    for ch in chars:
        assert ch not in screen.visible(), f"屏幕上残留了 {ch!r}：{screen.visible()!r}"

    print("  ✓ 通过")


def test_short_input_stays_fast_path():
    """短输入不许退化成整段重画（保住"敲一个字只写一个字"的手感）"""
    print("测试 性能: 短输入仍走快速路径")

    editor = ScriptedEditor(list("abc") + ["\r"], cols=10)
    editor.readline("> ")
    out = "".join(editor.notes)

    assert "\x1b[1B" not in out, "短输入不该进入受控分帧重绘"
    assert out.endswith("abc\n"), "末尾追加应当只写增量字符"

    print("  ✓ 通过")


def run_all_tests():
    """逐个跑；有失败就用非 0 退出码（约定见 THINKER.md 的「已知的坑」）"""
    print("开始运行输入层折行测试（全程离线）...\n")

    cases = [
        test_single_line,
        test_exactly_full_line,
        test_wrap_to_second_line,
        test_prompt_counts_toward_width,
        test_wide_chars,
        test_backspace_at_line_start_is_detected,
        test_no_negative_or_overflow,
        test_backspace_crosses_visual_line,
        test_backspace_crosses_two_lines,
        test_bottom_padding_default,
        test_bottom_padding_can_be_disabled,
        test_padding_scales_with_window,
        test_padding_env_overrides_window,
        test_ansi_prompt_width,
        test_split_visible_lines,
        test_frame_pos,
        test_partial_delete_screen_matches,
        test_chinese_multiline_clean,
        test_short_input_stays_fast_path,
    ]

    failures = []
    for case in cases:
        try:
            case()
        except Exception as e:
            failures.append((case.__name__, e))
            print(f"❌ {case.__name__} 失败：{e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {len(failures)} 个用例失败（通过 {len(cases) - len(failures)}/{len(cases)}）")
        for name, _ in failures:
            print(f"   - {name}")
        sys.exit(1)

    print(f"🎉 所有测试通过（{len(cases)}/{len(cases)}）")


if __name__ == "__main__":
    run_all_tests()
