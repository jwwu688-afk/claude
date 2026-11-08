# =============================================================================
# 文件：test/test_history_recall.py
# 作用：验证"按 ↑ 复制上一个问题"（第 18 轮新增的输入历史翻页）
#
# 【为什么单开一个文件，而不是塞进 test_tab_completion.py】
#   那边测的是 Tab 补全，是第 16 轮的主题；这里测的是历史翻页，是第 18 轮新增。
#   两者出问题的方式不同：补全坏了是"命令打不出来"，历史坏了是"复制不了上一句"。
#
# 【为什么不用 pytest】
#   和其他测试一致：独立脚本、零依赖。直接 python test/test_history_recall.py。
# =============================================================================

import sys
import traceback
from pathlib import Path

src_path = str(Path(__file__).resolve().parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from ui.line_editor import LineEditor  # noqa: E402

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


UP = ["\xe0", "H"]  # ↑ 的两个字节
DOWN = ["\xe0", "P"]  # ↓ 的两个字节


class ScriptedEditor(LineEditor):
    """按预设键序列跑 readline，并记录屏幕输出与响铃次数

    和 test_tab_completion 里的那个一样：只替换"按键从哪来"和"往哪写"，
    readline 的逻辑一行不改。
    """

    def __init__(self, keys):
        self._keys = list(keys)
        self.notes = []
        self.bells = 0
        super().__init__(key_reader=self._next_key)

    def _next_key(self):
        if not self._keys:
            raise AssertionError("按键序列用完了——说明 readline 多读键或少判了分支")
        return self._keys.pop(0)

    def _write(self, text):
        self.notes.append(text)

    def _bell(self):
        self.bells += 1


def read_n(ed, n):
    """连续跑 n 次 readline，返回每次的结果

    历史是靠"提交"积累的，而提交就意味着一次 readline 返回。
    一个 readline 调用不可能既提交两句、又在第三行翻历史——回车就把控制权交回调用方了。
    """
    return [ed.readline("> ") for _ in range(n)]


def test_recall_previous_question() -> None:
    print("\n[1] ↑ 复制上一个问题")

    # 提交一句，然后按 ↑ 把上一句捞回来，直接回车提交
    ed = ScriptedEditor(list("帮我看看这个报错") + ["\r"] + UP + ["\r"])
    got = read_n(ed, 2)
    check("第一句正常提交", got[0] == "帮我看看这个报错", repr(got[0]))
    check("按 ↑ 复制出上一句", got[1] == "帮我看看这个报错", repr(got[1]))

    # 提交两句，按两次 ↑ 翻到最早那句
    ed = ScriptedEditor(list("第一句") + ["\r"] + list("第二句") + ["\r"] + UP + UP + ["\r"])
    got = read_n(ed, 3)
    check("连按两次 ↑ 翻到最早一条", got[2] == "第一句", repr(got[2]))


def test_draft_protection() -> None:
    print("\n[2] 打了一半按 ↑，再按 ↓ 能还回草稿")

    # 提交过一句，然后打一半（"半"），按 ↑ 翻历史，再按 ↓ 回到草稿
    ed = ScriptedEditor(list("完整的问题") + ["\r"] + list("半") + UP + DOWN + ["\r"])
    got = read_n(ed, 2)
    check("↑ 再 ↓ 还回原来打的一半", got[1] == "半", repr(got[1]))


def test_no_history_bell() -> None:
    print("\n[3] 没有历史时按 ↑：响铃、内容不变")

    ed = ScriptedEditor(list("x") + UP + ["\r"])
    got = ed.readline("> ")
    check("没历史时 ↑ 不改变内容", got == "x", repr(got))
    check("没历史时 ↑ 响铃一次", ed.bells == 1, str(ed.bells))


def test_empty_and_duplicate_not_recorded() -> None:
    print("\n[4] 空行和重复内容不进历史")

    # 空行不进历史：连按两次回车，再按 ↑ 应该还是"没有历史"（响铃）
    ed = ScriptedEditor(["\r", "\r"] + UP + ["\r"])
    got = read_n(ed, 3)
    check("空行不进历史", got[2] == "" and ed.bells == 1, f"{got}, bells={ed.bells}")

    # 连续两条相同：只记一条，↑ 一次就该回到那条（而不是原地）
    ed = ScriptedEditor(list("同样的话") + ["\r"] + list("同样的话") + ["\r"] + UP + ["\r"])
    got = read_n(ed, 3)
    check("重复内容去重", got[2] == "同样的话" and len(ed._history) == 1, f"{got}, history={ed._history}")


def run_all() -> int:
    print("开始运行输入历史翻页测试（第 18 轮：↑ 复制上一个问题）…")
    try:
        test_recall_previous_question()
        test_draft_protection()
        test_no_history_bell()
        test_empty_and_duplicate_not_recorded()
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
