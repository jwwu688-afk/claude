# =============================================================================
# 文件：test/test_cursor_editing.py
# 作用：验证光标左右移动、中间插入/删除（第 18.1 轮新增）
#
# 【为什么单开一个文件】
#   历史翻页（test_history_recall.py）测的是"↑ 复制上一句"；
#   这里测的是"在句子中间改字"。两者都是输入层，但断言的对象不同：
#   前者断言返回的整行，后者还要断言光标位置和屏幕重画。
#
# 【怎么模拟终端屏幕】
#   readline 只返回"最终字符串"，看不见中间的光标位置。
#   要断言"中间插入后光标在哪、屏幕有没有残留空格"，得把 _write 记下的
#   控制字符（\r \b \x08 ANSI）按终端规则执行一遍，看最终屏幕行。
# =============================================================================

import sys
import traceback
from pathlib import Path

src_path = str(Path(__file__).resolve().parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from ui.line_editor import LineEditor, display_width  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


LEFT = ["\xe0", "K"]
RIGHT = ["\xe0", "M"]
HOME = ["\xe0", "G"]
END = ["\xe0", "O"]
DELETE = ["\xe0", "S"]


class ScriptedEditor(LineEditor):
    def __init__(self, keys):
        self._keys = list(keys)
        self.notes = []
        self.bells = 0
        super().__init__(key_reader=self._next_key)

    def _next_key(self):
        if not self._keys:
            raise AssertionError("按键序列用完了")
        return self._keys.pop(0)

    def _write(self, text):
        self.notes.append(text)

    def _bell(self):
        self.bells += 1


def render_screen(notes) -> str:
    """把屏幕上写过的字节流按终端规则执行，返回最终屏幕行（第 18.1 轮）

    为什么需要：_write 记下的是原始字节流，\r、\b、\x08、ANSI 都是控制指令，
    不是最终显示。只看返回字符串抓不到"屏幕残留空格"这类纯视觉 bug。
    """
    line = []
    col = 0
    for chunk in notes:
        i = 0
        n = len(chunk)
        while i < n:
            ch = chunk[i]
            if ch == "\r":
                col = 0
            elif ch == "\n":
                pass
            elif ch == "\x08":
                col = max(0, col - 1)
            elif ch == "\x1b":
                j = i + 2
                while j < n and not ("@" <= chunk[j] <= "~"):
                    j += 1
                i = j
            else:
                w = display_width(ch)
                while len(line) < col + w:
                    line.append(" ")
                line[col] = ch
                if w == 2:
                    line[col + 1] = ""
                col += w
            i += 1
    return "".join(line)


def test_insert_in_middle() -> None:
    print("\n[1] 中间插入")

    # 打 "ab"，光标左移一位到 a 和 b 之间，插入 "X" → "aXb"
    ed = ScriptedEditor(list("ab") + LEFT + ["X", "\r"])
    got = ed.readline("> ")
    check("← 后插入（改句子中间）", got == "aXb", repr(got))

    # Home 跳到行首插入
    ed = ScriptedEditor(list("bc") + HOME + ["a", "\r"])
    check("Home 跳到行首插入", ed.readline("> ") == "abc")

    # End 跳到行尾追加
    ed = ScriptedEditor(list("ab") + HOME + ["c"] + END + ["d", "\r"])
    check("Home/End 配合插入", ed.readline("> ") == "cabd")


def test_delete_in_middle() -> None:
    print("\n[2] 中间删除")

    # "abc"，光标左移一位（到 b 和 c 之间），退格删 b → "ac"
    ed = ScriptedEditor(list("abc") + LEFT + ["\x08", "\r"])
    check("光标在中间时退格删前一个字符", ed.readline("> ") == "ac")

    # Delete 删光标处的字符："abc"，Home 到行首，Delete 删 a → "bc"
    ed = ScriptedEditor(list("abc") + HOME + DELETE + ["\r"])
    check("Delete 删光标所在字符", ed.readline("> ") == "bc")


def test_cursor_boundary() -> None:
    print("\n[3] 光标边界")

    # → 在行尾不越界
    ed = ScriptedEditor(list("a") + RIGHT + ["b", "\r"])
    check("→ 在行尾不越界", ed.readline("> ") == "ab")

    # ← 在行首不越界
    ed = ScriptedEditor(list("a") + HOME + LEFT + ["b", "\r"])
    check("← 在行首不越界", ed.readline("> ") == "ba")


def test_no_residual_space() -> None:
    print("\n[4] 中间删除不留残留空格")

    # 中文中间删除："你好世界" 光标左移一位（到 界 前），退格删 世 → "你好界"
    ed = ScriptedEditor(list("你好世界") + LEFT + ["\x08", "\r"])
    ed.readline("> ")
    screen = render_screen(ed.notes)
    check("中文中间删除：屏幕无残留空格", screen.rstrip() == "> 你好界", repr(screen))

    # ASCII 中间删除："abc" 左移两次删 a → "bc"
    ed = ScriptedEditor(list("abc") + LEFT + LEFT + ["\x08", "\r"])
    ed.readline("> ")
    screen = render_screen(ed.notes)
    check("ASCII 中间删除：屏幕无残留空格", screen.rstrip() == "> bc", repr(screen))


def run_all() -> int:
    print("开始运行光标编辑测试（第 18.1 轮：左右移动 / 中间插入删除）…")
    try:
        test_insert_in_middle()
        test_delete_in_middle()
        test_cursor_boundary()
        test_no_residual_space()
    except Exception:
        print("\n❌ 测试执行中抛出异常：")
        traceback.print_exc()
        return 1

    print()
    if _failed == 0:
        print(f"🎉 所有测试通过（{_passed}/{_passed}）")
        return 0
    print(f"❌ 失败 {_failed} 项（通过 {_passed} 项）")
    return 1


if __name__ == "__main__":
    sys.exit(run_all())
