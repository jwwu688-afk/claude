# =============================================================================
# 文件：test/test_tab_completion.py
# 作用：验证斜杠命令的 Tab 补全，以及自管行编辑的按键处理（第 16 轮 / 16.2 轮）
#
# 【为什么分成七组】
#   补全这件事拆开看是七块，出问题的方式完全不一样：
#     1. resolve_completion —— 纯函数，算"按了 Tab 变成什么"。错了补不全或补错。
#     2. _complete_command  —— 候选来源。错了表现为"能补，但列表里没有那条命令"。
#     3. _read_line         —— 分流：什么时候用自管、什么时候退回 input()。
#     4. readline 主体      —— 真正按键级的行为：追加、退格、Tab、Esc、Ctrl+C。
#     5. Tab 端到端         —— 从按下的键一路走到返回的字符串。
#     6. _handle_tab        —— 按下 Tab 往屏幕上做了什么。
#     7. display_width      —— 显示宽度（中文两列），退格擦除全靠它。
#
# 【第 4 组是 16.2 轮补的，也是最要紧的一组】
#   16 轮翻车就是因为这一组根本不存在：readline 依赖真键盘，测不了，
#   于是"提示符里那个字删不掉"这个问题一直没被发现——测试全绿而功能是坏的。
#   现在 LineEditor 支持注入按键来源（key_reader），这一整块终于能跑了。
#
# 【为什么不用 pytest】
#   和其他测试一致：独立脚本、零依赖。替换 builtins.input 用的是最朴素的赋值+还原，
#   没引 unittest.mock——这个项目连 pytest 都不装，没必要为 mock 多引一个概念。
# =============================================================================

import builtins
import os
import sys
import traceback
from pathlib import Path

src_path = str(Path(__file__).resolve().parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from core.conversation import Conversation  # noqa: E402
from ui.input_handler import InputHandler  # noqa: E402

# 【第 18.6 轮】关掉输入行下方的留白，让本文件专注测补全逻辑。
#   为什么要显式关：留白会在提示符之前先写 "\n\n\x1b[2A"，而本文件有多处断言
#   在看"写出的第一个片段是不是提示符"、以及把字节流渲染成屏幕。留着空白会
#   让这些断言全部错位——但错的是断言的前提，不是补全逻辑本身。
#   留白自己的行为由 test_line_wrap.py 覆盖，两件事分开测。
os.environ.setdefault("THINKER_INPUT_PADDING", "0")
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


# -----------------------------------------------------------------------------
# 一、补全计算（纯函数）
# -----------------------------------------------------------------------------

def test_resolve_completion() -> None:
    print("\n[1] resolve_completion：按了 Tab 变成什么")

    new, show = LineEditor.resolve_completion("/cos", ["/cost"])
    check("唯一候选直接补全", new == "/cost" and show is False, f"{new}, {show}")

    new, show = LineEditor.resolve_completion("/xyz", [])
    check("没有候选：原样返回", new == "/xyz" and show is False, f"{new}, {show}")

    new, show = LineEditor.resolve_completion("/co", ["/cost", "/compact"])
    check("多个候选补到公共前缀", new == "/co" and show is True, f"{new}, {show}")

    new, show = LineEditor.resolve_completion("/", ["/cost", "/clear", "/compact"])
    check("公共前缀更长时补进去", new == "/c" and show is False, f"{new}, {show}")

    new, show = LineEditor.resolve_completion("/c", ["/cost", "/clear"])
    check("补不动了才列清单", new == "/c" and show is True, f"{new}, {show}")

    # 关键回归：只有一个候选时也必须先做前缀过滤。
    # 少了这一步，输入 /q 按 Tab 会被补成 /cost，而且不报错、不响铃。
    new, show = LineEditor.resolve_completion("/q", ["/cost"])
    check(
        "没有前缀匹配：不变（不能因为'只有一个候选'就乱补）",
        new == "/q" and show is False,
        f"{new}, {show}",
    )


# -----------------------------------------------------------------------------
# 二、候选来源
# -----------------------------------------------------------------------------

def test_completer() -> None:
    print("\n[2] _complete_command：候选从哪儿来")

    all_cmds = Conversation._complete_command("/")
    check("'/' 返回全部命令", len(all_cmds) >= 7, str(all_cmds))
    check("每条都以 / 开头", all(c.startswith("/") for c in all_cmds), str(all_cmds))
    for must in ("/cost", "/clear", "/compact", "/help", "/memory", "/resume", "/init"):
        check(f"列表里有 {must}", must in all_cmds, str(all_cmds))

    check("普通中文不补全", Conversation._complete_command("你好") == [])
    check("空串不补全", Conversation._complete_command("") == [])
    check("已在输参数就不补命令名", Conversation._complete_command("/memory 笔记.md") == [])

    from core.commands import REGISTRY

    check(
        "候选数量与注册表一致",
        len(all_cmds) == len(REGISTRY),
        f"{len(all_cmds)} vs {len(REGISTRY)}",
    )


# -----------------------------------------------------------------------------
# 三、_read_line 的分流：什么时候走自管、什么时候退回 input()
# -----------------------------------------------------------------------------

class RecordingEditor:
    """假的编辑器：只记录 readline 有没有被调用（不模拟按键）

    分流层只需要知道"走没走自管"，按键级行为由第四组负责。
    """

    def __init__(self, available: bool = True):
        self.available = available
        self.called = None

    def readline(self, prompt, completer=None):
        self.called = (prompt, completer)
        return "/cost"


def test_read_line_dispatch() -> None:
    print("\n[3] _read_line：分流")

    real_input = builtins.input
    seen = []

    def fake_input(prompt=""):
        seen.append(prompt)
        return "原样返回"

    try:
        builtins.input = fake_input

        # 1) 需要补全 + 环境支持 → 走自管
        handler = InputHandler()
        editor = RecordingEditor()
        handler._editor = editor
        got = handler._read_line("> ", lambda t: ["/cost"])
        check("有补全需求：走自管", got == "/cost", repr(got))
        check(
            "有补全需求：prompt 原样交给 readline",
            editor.called and editor.called[0] == "> ",
            str(editor.called),
        )

        # 2) completer 为 None（授权提问、选会话编号）→ 走 input()
        editor = RecordingEditor()
        handler._editor = editor
        seen.clear()
        got = handler._read_line("> ", None)
        check("无 completer：走 input()", got == "原样返回", repr(got))
        check("无 completer：没进自管", editor.called is None)
        check("无 completer：prompt 干净", seen == ["> "], str(seen))

        # 3) 环境不支持 → 走 input()
        editor = RecordingEditor(available=False)
        handler._editor = editor
        seen.clear()
        got = handler._read_line("> ", lambda t: ["/cost"])
        check("环境不支持：降级 input()", got == "原样返回", repr(got))
        check("环境不支持：没进自管", editor.called is None)

        # 4) 逃生开关 → 走 input()
        #    这个开关是给"自管模式在某个终端/输入法下表现异常"时用的，
        #    必须真的能拦住，否则用户没有退路。
        os.environ["THINKER_PLAIN_INPUT"] = "1"
        try:
            editor = RecordingEditor()
            handler._editor = editor
            seen.clear()
            got = handler._read_line("> ", lambda t: ["/cost"])
            check("THINKER_PLAIN_INPUT=1：强制走 input()", got == "原样返回", repr(got))
            check("THINKER_PLAIN_INPUT=1：没进自管", editor.called is None)
        finally:
            os.environ.pop("THINKER_PLAIN_INPUT", None)

    finally:
        builtins.input = real_input


# -----------------------------------------------------------------------------
# 四、readline 主体：按键级行为（16.2 轮新增，最要紧的一组）
# -----------------------------------------------------------------------------

class ScriptedEditor(LineEditor):
    """按预设键序列跑 readline，并把"往屏幕上写了什么"记下来

    为什么继承 LineEditor 而不是 mock 掉它：
      这里要测的就是 readline 的真实逻辑（追加、退格、Tab、Esc、Ctrl+C），
      整个换掉等于什么都没测。只替换"按键从哪来"和"往哪写"两个边界。
    """

    def __init__(self, keys):
        self._keys = list(keys)
        self.notes = []  # 每次往屏幕写的内容
        self.bells = 0
        super().__init__(key_reader=self._next_key)

    def _next_key(self):
        if not self._keys:
            raise AssertionError("按键序列用完了——说明 readline 多读了一个键")
        return self._keys.pop(0)

    def _write(self, text):
        self.notes.append(text)

    def _bell(self):
        self.bells += 1

    def screen(self) -> str:
        """把写过的内容拼起来，方便断言"屏幕上出现了什么"""
        return "".join(t for t in self.notes if isinstance(t, str))


def test_readline_basics() -> None:
    print("\n[4] readline：按键级行为")

    # --- 中文逐字追加，回车结束 ---
    ed = ScriptedEditor(["帮", "我", "看", "看", "\r"])
    got = ed.readline("> ")
    check("中文可正常输入并返回", got == "帮我看看", repr(got))
    check("提示符被写出来了", ed.screen().startswith("> "), repr(ed.screen()[:10]))

    # --- 退格删到空，仍能继续输入（16 轮那个 bug 的正面回归） ---
    # 场景：打一个字、退格删掉、再打一个字、回车。
    # 改前第一个字会跑到提示符里，删不掉；这里断言它能被删干净。
    ed = ScriptedEditor(["帮", "\x08", "\x08", "好", "\r"])
    got = ed.readline("> ")
    check("删到空之后仍能继续输入", got == "好", repr(got))

    # --- 退格删中文按两列擦（否则屏幕上留半个字） ---
    ed = ScriptedEditor(["帮", "\x08", "\r"])
    ed.readline("> ")
    # 一个中文字占 2 列 → 应写两组 "\b \b"
    check("删中文按两列擦", ed.screen().count("\b \b") == 2, repr(ed.screen()))

    # --- ASCII 退格只擦一列 ---
    ed = ScriptedEditor(["a", "\x08", "\r"])
    ed.readline("> ")
    check("删 ASCII 按一列擦", ed.screen().count("\b \b") == 1, repr(ed.screen()))

    # --- Esc 清空当前行 ---
    ed = ScriptedEditor(["你", "好", "\x1b", "行", "\r"])
    got = ed.readline("> ")
    check("Esc 清空后重新输入", got == "行", repr(got))

    # --- 方向键是两字节，两个都要吃掉，不能进入内容 ---
    ed = ScriptedEditor(["a", "\x00", "H", "b", "\r"])
    got = ed.readline("> ")
    check("方向键不污染输入内容", got == "ab", repr(got))

    # --- Ctrl+C 抛 KeyboardInterrupt（和 input() 一致） ---
    ed = ScriptedEditor(["a", "\x03"])
    try:
        ed.readline("> ")
        check("Ctrl+C 应抛出 KeyboardInterrupt", False, "没抛异常")
    except KeyboardInterrupt:
        check("Ctrl+C 抛 KeyboardInterrupt", True)

    # --- 直接回车返回空串 ---
    ed = ScriptedEditor(["\r"])
    check("直接回车返回空串", ed.readline("> ") == "")


def test_readline_tab() -> None:
    print("\n[5] readline：Tab 补全（端到端，从按键到结果）")

    completer = Conversation._complete_command

    # 打 /cos 再按 Tab → 补成 /cost（带空格），回车提交
    ed = ScriptedEditor(["/", "c", "o", "s", "\t", "\r"])
    got = ed.readline("> ", completer)
    check("Tab 把 /cos 补成 '/cost '（含空格）", got == "/cost ", repr(got))

    # 打 /c 再按 Tab → 只到公共前缀，没进展就列出候选
    ed = ScriptedEditor(["/", "c", "\t", "\r"])
    got = ed.readline("> ", completer)
    screen = ed.screen()
    check("/c+Tab：内容不变", got == "/c", repr(got))
    check("/c+Tab：列出了 /clear", "/clear" in screen, screen[-60:])
    check("/c+Tab：列出了 /cost", "/cost" in screen, screen[-60:])

    # 打 /zzz 再按 Tab → 响铃、内容不变
    ed = ScriptedEditor(["/", "z", "z", "z", "\t", "\r"])
    got = ed.readline("> ", completer)
    check("/zzz+Tab：内容不变", got == "/zzz", repr(got))
    check("/zzz+Tab：响铃一次", ed.bells == 1, str(ed.bells))


# -----------------------------------------------------------------------------
# 六、_handle_tab 的屏幕输出
# -----------------------------------------------------------------------------

class RecordingHandleEditor(LineEditor):
    """只记录 _handle_tab 往屏幕上做了什么"""

    def __init__(self):
        super().__init__()
        self.events = []
        self.bells = 0

    def _write(self, text):
        self.events.append(("write", text))

    def _bell(self):
        self.bells += 1

    def _redraw(self, buffer, prompt, new_text):
        self.events.append(("redraw", new_text))
        buffer.clear()
        buffer.extend(new_text)


def test_handle_tab() -> None:
    print("\n[6] _handle_tab：按下 Tab 实际做了什么")

    # --- 唯一匹配：补全 + 补一个空格 ---
    ed = RecordingHandleEditor()
    buf = list("/cos")
    ed._handle_tab(buf, "> ", Conversation._complete_command)
    redraws = [e for e in ed.events if e[0] == "redraw"]
    check("唯一匹配：发生重画", len(redraws) == 1, str(ed.events))
    check("唯一匹配：补成 /cost 并带空格", redraws and redraws[0][1] == "/cost ", str(redraws))
    check("唯一匹配：buffer 同步更新", "".join(buf) == "/cost ", repr("".join(buf)))

    # --- 多个候选且补不动：列出**相关的**候选，不能刷全部 ---
    ed = RecordingHandleEditor()
    buf = list("/c")
    ed._handle_tab(buf, "> ", Conversation._complete_command)
    dumped = " ".join(t for kind, t in ed.events if kind == "write")
    check("多候选：列出了 /clear", "/clear" in dumped, dumped)
    check("多候选：列出了 /cost", "/cost" in dumped, dumped)
    check("多候选：没把 /memory 也列出来", "/memory" not in dumped, dumped)
    check("多候选：buffer 没被改动", "".join(buf) == "/c", repr("".join(buf)))

    # --- 再多一个字母就变成唯一匹配 ---
    ed = RecordingHandleEditor()
    buf = list("/cl")
    ed._handle_tab(buf, "> ", Conversation._complete_command)
    redraws = [e for e in ed.events if e[0] == "redraw"]
    check("/cl 唯一匹配：直接补成 /clear", redraws and redraws[0][1] == "/clear ", str(redraws))

    # --- 没有任何匹配：响铃，什么都不改 ---
    ed = RecordingHandleEditor()
    buf = list("/zzz")
    ed._handle_tab(buf, "> ", Conversation._complete_command)
    check("无匹配：响铃一次", ed.bells == 1, str(ed.bells))
    check("无匹配：没有重画", not any(e[0] == "redraw" for e in ed.events), str(ed.events))

    # --- 补全源抛异常：响铃，但不崩、内容不丢 ---
    def boom(_text):
        raise RuntimeError("补全源炸了")

    ed = RecordingHandleEditor()
    buf = list("/co")
    ed._handle_tab(buf, "> ", boom)
    check("补全源异常：响铃", ed.bells == 1, str(ed.bells))
    check("补全源异常：已输入内容还在", "".join(buf) == "/co", repr("".join(buf)))

    # --- completer 为 None：不崩，响铃 ---
    ed = RecordingHandleEditor()
    buf = list("/")
    ed._handle_tab(buf, "> ", None)
    check("completer 为 None：响铃而不是报错", ed.bells == 1, str(ed.bells))


# -----------------------------------------------------------------------------
# 七、显示宽度与环境探测
# -----------------------------------------------------------------------------

def test_display_width() -> None:
    print("\n[7] display_width / available")

    check("ASCII 一列一个", display_width("abc") == 3, str(display_width("abc")))
    check("中文两列一个", display_width("你好") == 4, str(display_width("你好")))
    check("空串零列", display_width("") == 0)
    check("混合内容", display_width("/帮助") == 5, str(display_width("/帮助")))

    value = LineEditor().available
    check("available 返回 bool，不抛异常", isinstance(value, bool), repr(value))
    print(f"    （当前测试环境 available={value}；在真实终端里应为 True）")

    injected = LineEditor(key_reader=lambda: "x")
    check("注入了按键来源就一定可用", injected.available is True)


def run_all() -> int:
    print("开始运行 Tab 补全 / 行编辑测试…")
    try:
        test_resolve_completion()
        test_completer()
        test_read_line_dispatch()
        test_readline_basics()
        test_readline_tab()
        test_handle_tab()
        test_display_width()
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
