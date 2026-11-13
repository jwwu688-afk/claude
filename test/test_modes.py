# -*- coding: utf-8 -*-
"""工作模式（第 33 轮）的测试

为什么值得单独一份：
  模式改的是"工具能不能执行"，判错一条的后果是静默的——
  不会报错、不会崩，只会在某天发现"计划模式下它居然把文件改了"。
  所以这里按 4 档 × 6 类工具铺一张判定矩阵，逐格钉住。

为什么判定用纯函数测、不启动对话：
  classify() 不读键盘、不发请求、不碰磁盘（除了记忆路径判断），
  传工具名和参数就能验。把它按在纯函数这一层，矩阵才能一分钟跑完。

怎么跑（项目根目录）：
  python test/test_modes.py

约定：不依赖 pytest，不联网，不调用模型。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.modes import (  # noqa: E402
    MODE_ACCEPT,
    MODE_AUTO,
    MODE_DEFAULT,
    MODE_PLAN,
    MODE_SPECS,
    cycle_mode,
    get_spec,
    mode_reminder,
    normalize_mode,
)
from core.permissions import (  # noqa: E402
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    classify,
    is_readonly_command,
)

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print(f"[FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def act(tool, args, mode):
    """取某一档下某个调用的动作，省得每次写 .action"""
    return classify(tool, args, mode).action


# ---------------------------------------------------------------------------
# 1) 四档定义与循环顺序
# ---------------------------------------------------------------------------
check("四档齐备", [s.name for s in MODE_SPECS] == [MODE_DEFAULT, MODE_PLAN, MODE_ACCEPT, MODE_AUTO],
      str([s.name for s in MODE_SPECS]))

order = []
cursor = MODE_DEFAULT
for _ in range(5):
    cursor = cycle_mode(cursor)
    order.append(cursor)
check("循环 default→plan→accept→auto→default",
      order == [MODE_PLAN, MODE_ACCEPT, MODE_AUTO, MODE_DEFAULT, MODE_PLAN], str(order))
check("未知模式当作 default 再进一档", cycle_mode("nonsense") == MODE_PLAN)

# ---------------------------------------------------------------------------
# 2) 别名归一
# ---------------------------------------------------------------------------
aliases = {
    "acceptEdits": MODE_ACCEPT,
    "accept-edits": MODE_ACCEPT,
    "edits": MODE_ACCEPT,
    "bypassPermissions": MODE_AUTO,
    "bypass": MODE_AUTO,
    "manual": MODE_DEFAULT,
    "计划": MODE_PLAN,
}
for raw, expected in aliases.items():
    check(f"别名 {raw}", normalize_mode(raw) == expected, str(normalize_mode(raw)))
check("空串归一失败", normalize_mode("  ") is None)
check("乱写归一失败", normalize_mode("planmode") is None)

# ---------------------------------------------------------------------------
# 3) 判定矩阵（4 档 × 6 类）
# ---------------------------------------------------------------------------
READ = ("file_read", {"path": "a.py"})
WRITE = ("file_write", {"path": "a.py"})
CMD_QUERY = ("cmd_runner", {"command": "git log --oneline | head -20"})
CMD_WRITE = ("cmd_runner", {"command": "git commit -m x"})
CMD_DANGER = ("cmd_runner", {"command": "del /s /q C:/tmp"})
TASK = ("task", {"prompt": "看看这个模块"})
MCP = ("some_mcp_tool", {"q": 1})

cases = [
    ("读文件", READ),
    ("写文件", WRITE),
    ("查询命令", CMD_QUERY),
    ("写命令", CMD_WRITE),
    ("高危命令", CMD_DANGER),
    ("派活", TASK),
    ("MCP 工具", MCP),
]

# 每档一行，列的顺序和 cases 一一对应。
#   为什么用"并列列表 + zip"而不是 {case: action} 的字典：
#     case 里的 args 是 dict，不可哈希，没法当键。这条注释是给自己看的——
#     第一版就是这么写的，跑到这里直接 TypeError。
matrix = {
    MODE_DEFAULT: [ACTION_ALLOW, ACTION_ASK, ACTION_ASK, ACTION_ASK, ACTION_ASK, ACTION_ASK, ACTION_ASK],
    MODE_PLAN: [ACTION_ALLOW, ACTION_DENY, ACTION_ALLOW, ACTION_DENY, ACTION_DENY, ACTION_ALLOW, ACTION_ASK],
    MODE_ACCEPT: [ACTION_ALLOW, ACTION_ALLOW, ACTION_ASK, ACTION_ASK, ACTION_ASK, ACTION_ASK, ACTION_ASK],
    MODE_AUTO: [ACTION_ALLOW, ACTION_ALLOW, ACTION_ALLOW, ACTION_ALLOW, ACTION_ALLOW, ACTION_ALLOW, ACTION_ALLOW],
}

for mode, row in matrix.items():
    for (label, (tool, args)), expected in zip(cases, row):
        got = act(tool, args, mode)
        check(f"{mode}: {label}", got == expected, f"期望 {expected}，实际 {got}")

# 高危命令的风险等级要一直带着：默认模式用来决定"回车默认拒绝"，
# 自动模式用来在界面打一行提示。
check("默认模式高危标记", classify(*CMD_DANGER, MODE_DEFAULT).risk == "high")
check("自动模式仍标记高危", classify(*CMD_DANGER, MODE_AUTO).risk == "high")
check("自动模式不弹窗", classify(*CMD_DANGER, MODE_AUTO).need_approval is False)
check("自动模式高危仍是 allow", classify(*CMD_DANGER, MODE_AUTO).action == ACTION_ALLOW)

# 接受编辑模式下，模型自己要求"问一下"时要尊重（只能收紧）
check("accept 尊重模型加严",
      classify("file_write", {"path": "a.py", "need_user_approve": True}, MODE_ACCEPT).action == ACTION_ASK)

# 计划模式的拒绝必须带理由，否则模型不知道该改道
plan_write = classify("file_write", {"path": "a.py"}, MODE_PLAN)
check("plan 拒绝有理由", bool(plan_write.reason) and "计划模式" in plan_write.reason, plan_write.reason)

# ---------------------------------------------------------------------------
# 4) 只读命令判定（计划模式的命门）
#    第 33.5 轮从"白名单"翻成"黑名单"：只拦明确的写操作，其余放行。
#    这样 `node --check`、`Get-ChildItem`、`tsc --noEmit` 这些没被枚举的
#    只读命令也能跑——它们正是之前被误拦的那一批。
# ---------------------------------------------------------------------------
readonly_yes = [
    "dir",
    "Get-ChildItem -Force",          # PowerShell 的 ls，不在任何白名单里
    "node --check effects.js",       # 语法检查，只读
    "tsc --noEmit",                  # 类型检查，不产出文件
    "git status",
    "git log --oneline | head -20",
    "git remote -v",
    "git tag -l",
    "git branch",
    "findstr /n TODO *.py",
    "cat copy.py",                   # 文件名里有 copy，按命令位置判就不会误伤
    "python --version",
    "npm ls",
]
readonly_no = [
    "git commit -m x",
    "git push origin main",
    "git tag v1.0",
    "git branch -D main",
    "rm -rf build",
    "del /s /q x",
    "mkdir dist",
    "copy a b",
    "npm install",
    "npm run build",
    "npx create-react-app x",
    "pip install requests",
    "python main.py",
    "python -c \"open('x','w')\"",
    "node effects.js",
    "echo hi > out.txt",
    "git status && git reset --hard",
    "find . -delete",
]
for cmd in readonly_yes:
    check(f"只读放行: {cmd}", is_readonly_command(cmd) is True)
for cmd in readonly_no:
    check(f"写操作拒绝: {cmd}", is_readonly_command(cmd) is False)
check("空命令不放行", is_readonly_command("") is False)
check("残缺管道不放行", is_readonly_command("git log | ") is False)

# ---------------------------------------------------------------------------
# 5) 计划模式不再剪工具清单，改为执行层拦截（第 33.5 轮，对齐 Claude Code）
# ---------------------------------------------------------------------------
check("plan 下写工具仍被拒绝（清单不剪、执行层拦）",
      act("file_write", {"path": "a.py"}, MODE_PLAN) == ACTION_DENY)
check("plan 下 file_edit 仍被拒绝", act("file_edit", {"path": "a.py"}, MODE_PLAN) == ACTION_DENY)
check("plan 下上下文裁剪工具被拒绝", act("smart_context_cropper", {}, MODE_PLAN) == ACTION_DENY)
check("plan 下声明只读的 MCP 工具直接放行",
      classify("some_mcp_tool", {}, MODE_PLAN, True).action == ACTION_ALLOW)
check("plan 下没声明的 MCP 工具问用户一次",
      classify("some_mcp_tool", {}, MODE_PLAN, False).action == ACTION_ASK)
check("plan 下 node --check 可以跑",
      classify("cmd_runner", {"command": "node --check a.js"}, MODE_PLAN).action == ACTION_ALLOW)
check("plan 下 npm install 被拒绝",
      classify("cmd_runner", {"command": "npm install"}, MODE_PLAN).action == ACTION_DENY)

# ---------------------------------------------------------------------------
# 6) 给模型的说明
# ---------------------------------------------------------------------------
check("默认档说明含档名", "默认模式" in mode_reminder(MODE_DEFAULT))
check("计划档说明含只读约束", "只能读取" in mode_reminder(MODE_PLAN))
check("说明用 <mode> 包起来", mode_reminder(MODE_AUTO).startswith("<mode>"))
check("未知档退回默认", get_spec("nonsense").name == MODE_DEFAULT)

# ---------------------------------------------------------------------------
# 7) 快捷键：按模式切换不能丢用户正在输入的内容
# ---------------------------------------------------------------------------
from ui.line_editor import LineEditor  # noqa: E402


def feed(keys, on_mode_cycle, completer=None):
    """喂一串按键，返回 (提交的整行, 屏幕输出片段)"""
    iterator = iter(keys)
    editor = LineEditor(key_reader=lambda: next(iterator))
    chunks = []
    editor._write = lambda text: chunks.append(text)
    result = editor.readline(
        "> ", completer=completer or (lambda t: []), on_mode_cycle=on_mode_cycle
    )
    return result, "".join(chunks)


hits = []


def _cycle():
    hits.append(1)
    return "已进入计划模式"


text, screen = feed(["a", "b", "\x1b", "[", "Z", "\r"], _cycle)
check("Shift+Tab 保留已输入内容", text == "ab", repr(text))
check("Shift+Tab 真的调了回调", len(hits) == 1)
check("Shift+Tab 打了提示", "已进入计划模式" in screen)

text, _ = feed(["x", "\x00", "t", "\r"], _cycle)
check("Alt+T 保留已输入内容", text == "x", repr(text))

text, _ = feed(["q", "\x1b", "\r"], _cycle)
check("单独 Esc 仍然清空", text == "", repr(text))

# 不支持切换的路径（授权提问走 input()，不会到这；这里测"回调为 None"的降级）
text, _ = feed(["y", "\x1b", "[", "Z", "\r"], None)
check("无回调时退化为按普通字符处理", text == "[Z", repr(text))

# ---------------------------------------------------------------------------
# 8) Tab 兜底（第 33.1 轮）
#    conhost 传统模式下 Shift+Tab 和 Tab 送给 getwch 的都是 \t，
#    所以 "非 / 开头时按 Tab = 切模式" 是这一键在那种终端上唯一能生效的路。
# ---------------------------------------------------------------------------
hits.clear()

text, screen = feed(["\t", "\r"], _cycle)
check("空输入按 Tab 切模式", len(hits) == 1)
check("空输入按 Tab 打了提示", "已进入计划模式" in screen)

text, _ = feed(["你", "好", "\t", "\r"], _cycle)
check("Tab 兜底不丢已输入内容", text == "你好", repr(text))
check("Tab 兜底真的切了模式", len(hits) == 2, str(len(hits)))

# 斜杠开头必须还是补全：这条挡住了"切模式把 /cost 补全吃掉"的回归
before = len(hits)
text, _ = feed(["/", "c", "o", "s", "\t", "\r"], _cycle,
               completer=lambda t: ["/cost", "/clear", "/compact"])
check("/cos 按 Tab 是补全不是切模式", text == "/cost ", repr(text))
check("/cos 补全不触发模式切换", len(hits) == before, str(len(hits) - before))

# ---------------------------------------------------------------------------
# 9) 状态行是"覆盖"而不是"追加"（第 33.2 轮）
#    连按三次 Shift+Tab 应该在原地闪三下，不是堆三行长句。
# ---------------------------------------------------------------------------
_, screen = feed(["\t", "\t", "\r"], _cycle)
check("连按两次走覆盖路径（上移一行改写）", "\x1b[1A" in screen, repr(screen[-80:]))

# 新的一行开始时不能去覆盖上一行留下的状态行（那已经是历史输出了）
_, screen = feed(["\t", "\r"], _cycle)
check("新的 readline 不覆盖上一行的状态行", "\x1b[1A" not in screen, repr(screen[-80:]))

# ---------------------------------------------------------------------------
# 10) set_mode 本身：不再打事件日志，只返回一行状态 + 刷底栏
# ---------------------------------------------------------------------------
from core.conversation import Conversation  # noqa: E402
from core.modes import status_line  # noqa: E402


class _FakeUI:
    def __init__(self):
        self.infos = []
        self.states = []

    def print_info(self, message):
        self.infos.append(message)

    def set_mode_state(self, mode):
        self.states.append(mode)


conv = Conversation.__new__(Conversation)  # 裸对象：不触发 ToolManager / MCP
conv._ui_manager = _FakeUI()
conv._mode = MODE_DEFAULT

ok, message = conv.set_mode("plan")
check("set_mode 返回状态行", ok and message == status_line("plan"), message)
check("set_mode 不再打事件日志", conv._ui_manager.infos == [], str(conv._ui_manager.infos))
check("set_mode 把新档位同步给输入层", conv._ui_manager.states == ["plan"], str(conv._ui_manager.states))
check("状态行含档位名与短说明", "计划模式" in message and "只读" in message, message)

ok, message = conv.cycle_mode()
check("快捷键路径同样同步档位", conv._ui_manager.states[-1] == "accept", str(conv._ui_manager.states[-1]))
check("循环到下一档", conv.mode == MODE_ACCEPT, conv.mode)

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
