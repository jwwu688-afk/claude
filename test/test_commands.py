#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_commands.py
# 作用：验证斜杠命令（2026-11-07 第 14 轮第一步）
#
# 覆盖三件事：
#   1. 解析：什么算命令、什么不算（路径里的 /、除法 3/4 都不能误判）
#   2. 四条命令各自的行为（/memory /resume /init /cost）
#   3. 最关键的一条：命令**不能进对话历史**——靠 _read_user_input 的循环保证
#
# 这个脚本不联网、不花钱：全部用假 UI + 临时目录。
# =============================================================================

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 构造 Conversation 会顺带建 APIClient，它要求三环境变量齐全。
# 用 setdefault 不覆盖真实配置，且本测试不会发任何请求。
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import core.auto_memory as auto_memory  # noqa: E402
import core.commands as commands  # noqa: E402
import core.conversation as conversation_module  # noqa: E402
import core.session as session_module  # noqa: E402
from core.commands import is_command, parse  # noqa: E402

passed = 0
total = 0


def check(name, condition, detail=""):
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"  ✓ {name}" + (f"：{detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"：{detail}" if detail else ""))


class FakeUI:
    """假的界面管理器：只记录打印了什么，不真的画到终端

    为什么不用真的 UIManager：它会去建 InputHandler / DisplayManager，
    而我们要验证的是"命令输出了什么"，不是"终端画得好不好看"。
    """

    def __init__(self, answers=None):
        self.lines = []
        self._answers = list(answers or [])  # 预置的输入队列（给 get_user_input 用）
        self.choice_title = None
        self.choice_items = None

    def print_info(self, text, emoji=""):
        self.lines.append(str(text))

    def print_success(self, text, emoji=""):
        self.lines.append(str(text))

    def print_error(self, text, emoji="❌"):
        self.lines.append(str(text))

    def print_warning(self, text):
        self.lines.append(str(text))

    def print_status(self, text, level="info"):
        self.lines.append(str(text))

    def text(self):
        return "\n".join(self.lines)

    async def get_numbered_choice(self, title, items):
        self.choice_title = title
        self.choice_items = list(items)
        return 0  # 测试里固定选第一项

    async def get_user_input(self, prompt=None):
        return self._answers.pop(0) if self._answers else ""


class FakeConv:
    """假对话：只放命令真正会用到的几个属性"""

    def __init__(self, ui, load_result=(3, 1234)):
        self._ui_manager = ui
        self._session = None
        self._task_depth = 0
        self.loaded = None
        self._load_result = load_result

    @property
    def is_subagent(self):
        return False

    def _load_session_into_history(self, path):
        self.loaded = path
        return self._load_result


def test_parse():
    print("测试: 命令解析")
    check("/cost 是命令", is_command("/cost"))
    check("/cost 是命令（带前导空格）", is_command("   /cost"))
    check("3/4 不是命令", not is_command("3/4"))
    check("D:/code 不是命令", not is_command("D:/code"))
    check("普通中文不是命令", not is_command("帮我看看这个文件"))
    check("空串不是命令", not is_command(""))

    check("解析命令名", parse("/cost")[0] == "cost")
    check("解析参数", parse("/memory foo.md")[1] == "foo.md")
    check("参数保留空格", parse("/init --global --force")[1] == "--global --force")
    check("大小写不敏感", parse("/COST")[0] == "cost")
    check("只有一个斜杠", parse("/")[0] is None)


def test_help():
    print("测试: /help")
    ui = FakeUI()
    asyncio.run(commands.cmd_help(FakeConv(ui), ""))
    text = ui.text()
    check("列出了 memory", "/memory" in text)
    check("列出了 resume", "/resume" in text)
    check("列出了 init", "/init" in text)
    check("列出了 cost", "/cost" in text)


def test_unknown_command():
    print("测试: 未知命令不发模型、给出提示")
    ui = FakeUI()
    asyncio.run(commands.handle_command("/memroy", FakeConv(ui)))
    text = ui.text()
    check("提示未知命令", "未知命令" in text)
    check("给出可用清单", "/memory" in text and "/cost" in text)


def test_cost():
    print("测试: /cost")

    class FakeApi:
        total_cost = 0.0  # 模拟"接口不返回 cost"的情况
        prompt_tokens = 12000
        cached_tokens = 3000
        cache_hit_rate = 25.0

    class FakeHistory:
        current_context_window = "7.3"

        def __init__(self):
            self.messages_history = [[1, 2, 3, 4]]

    ui = FakeUI()
    conv = FakeConv(ui)
    conv._api_client = FakeApi()
    conv._history_manager = FakeHistory()
    asyncio.run(commands.cmd_cost(conv, ""))
    text = ui.text()
    check("花费显示未提供而不是 $0.00", "未提供" in text, text.splitlines()[0])
    check("输入 token 带千分位", "12,000" in text)
    check("缓存命中率有百分号", "25.0%" in text)
    check("上下文占用有百分比", "7.3%" in text)
    check("消息数正确", "4" in text)


def test_memory(tmp_root):
    print("测试: /memory")

    # 把记忆根目录指到临时目录，别碰真实的 ~/.thinker
    old_projects_dir = auto_memory.PROJECTS_DIR
    auto_memory.PROJECTS_DIR = Path(tmp_root) / "projects"
    try:
        root = auto_memory.ensure_dir()
        (root / "MEMORY.md").write_text(
            "- [用户偏好简洁](user-prefers-concise.md) — 回答别太长\n", encoding="utf-8"
        )
        (root / "user-prefers-concise.md").write_text(
            "# 用户偏好\n\n回答尽量短，先给结论。\n", encoding="utf-8"
        )

        ui = FakeUI()
        conv = FakeConv(ui)
        asyncio.run(commands.cmd_memory(conv, ""))
        text = ui.text()
        check("显示目录", str(root) in text)
        check("显示条目数", "条目数量：1" in text)
        check("列出条目名", "user-prefers-concise.md" in text)
        check("显示索引内容", "回答别太长" in text)

        ui2 = FakeUI()
        asyncio.run(commands.cmd_memory(FakeConv(ui2), "user-prefers-concise"))
        check("带参数可看正文（扩展名可省）", "先给结论" in ui2.text())

        ui3 = FakeUI()
        asyncio.run(commands.cmd_memory(FakeConv(ui3), "../../secret"))
        check("越界路径被拒", "拒绝访问" in ui3.text())
    finally:
        auto_memory.PROJECTS_DIR = old_projects_dir


def test_resume(tmp_root, project_dir):
    print("测试: /resume")

    old_sessions_dir = session_module.SESSIONS_DIR
    old_cwd = commands._cwd
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions"
    commands._cwd = lambda: str(project_dir)  # 让"本项目"指的是临时项目
    try:
        store = session_module.SessionStore.create(cwd=str(project_dir), model="test-model")
        store.append_message(
            {"role": "user", "content": [{"type": "text", "text": "帮我改一下登录逻辑"}]}
        )
        store.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": "好的"}]}
        )

        ui = FakeUI()
        conv = FakeConv(ui)
        asyncio.run(commands.cmd_resume(conv, ""))
        check("列出了清单", ui.choice_items is not None and len(ui.choice_items) == 1)
        if ui.choice_items:
            label = ui.choice_items[0]
            check("清单带消息条数", "2 条" in label, label)
            check("清单带首句预览", "帮我改一下登录逻辑" in label)
        check("选中后调用了恢复逻辑", conv.loaded == store.path)
        check("提示已切换", "已切换" in ui.text())
    finally:
        session_module.SESSIONS_DIR = old_sessions_dir
        commands._cwd = old_cwd


def test_init(tmp_root):
    print("测试: /init")
    workdir = Path(tmp_root) / "init_proj"  # 单独一个目录，别和 /resume 用的 proj 抢
    workdir.mkdir(exist_ok=True)
    old_cwd = os.getcwd()
    os.chdir(workdir)  # write_init_file 按 cwd 写 THINKER.md
    try:
        ui = FakeUI()
        asyncio.run(commands.cmd_init(FakeConv(ui), ""))
        target = workdir / "THINKER.md"
        check("文件已生成", target.is_file())
        check("提示已生成", "已生成" in ui.text())

        # 第二次不带 --force：必须拒绝覆盖
        before = target.read_text(encoding="utf-8")
        ui2 = FakeUI()
        asyncio.run(commands.cmd_init(FakeConv(ui2), ""))
        check("已存在时不覆盖", "已存在" in ui2.text())
        check("内容没被改掉", target.read_text(encoding="utf-8") == before)
    finally:
        os.chdir(old_cwd)


def test_init_in_home_guard(tmp_root):
    """主目录里必须拒绝生成"项目记忆"（2026-11-08 第 14.1 轮）

    这个测试守的是一个真实踩过的坑：用户从 `PS C:\\Users\\Jarvis>` 启动，
    执行 /init，于是 C:\\Users\\Jarvis\\THINKER.md 被生成，
    而它会成为所有"从主目录启动"的会话的项目记忆。

    怎么模拟"当前是主目录"：把 is_user_home 打补丁成恒为 True。
    为什么不去 chdir 到真的家目录：那会在用户家里留下文件，
    测试不该有这种副作用。
    """
    print("测试: 主目录守卫")
    import core.prompt.memory as memory_module

    workdir = Path(tmp_root) / "fake_home"
    workdir.mkdir(exist_ok=True)
    old_cwd = os.getcwd()
    old_check = memory_module.is_user_home
    os.chdir(workdir)
    memory_module.is_user_home = lambda start=None: True
    try:
        # 1) /init 在主目录：必须拒绝，且不留下文件
        ui = FakeUI()
        asyncio.run(commands.cmd_init(FakeConv(ui), ""))
        check("拒绝生成", not (workdir / "THINKER.md").exists())
        check("提示先 cd 到项目目录", "请先 cd" in ui.text())

        # 2) /init --here：显式要求才放行
        ui2 = FakeUI()
        asyncio.run(commands.cmd_init(FakeConv(ui2), "--here"))
        check("--here 放行", (workdir / "THINKER.md").is_file())

        # 3) 命令行那条路径必须和 /init 一致：CLI 被拦、会话里能写，会更糊涂
        (workdir / "THINKER.md").unlink()
        from main import run_init

        run_init()  # 主目录，无 --here
        check("CLI --init 同样拒绝", not (workdir / "THINKER.md").exists())

        run_init(here=True)
        check("CLI --here 放行", (workdir / "THINKER.md").is_file())

        # 4) --global 不该被拦：它写的是 ~/.thinker，本来就跟着人走、不跟项目走。
        #    把全局路径临时改到 tmp 里，避免真的写进家目录。
        old_global = memory_module.GLOBAL_MEMORY_PATH
        memory_module.GLOBAL_MEMORY_PATH = Path(tmp_root) / "global_mem" / "THINKER.md"
        try:
            run_init(scope="global")
            check("--global 不受主目录守卫影响", memory_module.GLOBAL_MEMORY_PATH.is_file())
        finally:
            memory_module.GLOBAL_MEMORY_PATH = old_global
    finally:
        os.chdir(old_cwd)
        memory_module.is_user_home = old_check


def test_command_not_in_history():
    print("测试: 命令不进历史（最关键的一条）")
    # 模拟用户先打 /cost，再打一句真话。期望：只返回真话，命令被消费掉。
    ui = FakeUI(answers=["/cost", "帮我看看 main.py"])
    conv = conversation_module.Conversation.__new__(conversation_module.Conversation)
    conv._ui_manager = ui
    conv._task_depth = 0

    seen = []

    async def fake_handle(text, conversation):
        seen.append(text)

    original = conversation_module.handle_command
    conversation_module.handle_command = fake_handle
    try:
        result = asyncio.run(conv._read_user_input())
    finally:
        conversation_module.handle_command = original

    check("返回的是真话", result == "帮我看看 main.py", repr(result))
    check("命令被处理了一次", seen == ["/cost"], str(seen))
    check("命令本身没有作为输入返回", "/cost" not in result)


def test_read_input_passthrough():
    print("测试: 普通输入原样返回")
    ui = FakeUI(answers=["你好"])
    conv = conversation_module.Conversation.__new__(conversation_module.Conversation)
    conv._ui_manager = ui
    conv._task_depth = 0
    check("直接返回", asyncio.run(conv._read_user_input()) == "你好")


if __name__ == "__main__":
    print("🧪 斜杠命令\n")
    tmp_root = tempfile.mkdtemp(prefix="thinker_cmd_test_")
    project_dir = Path(tmp_root) / "proj"
    project_dir.mkdir()
    try:
        test_parse()
        print()
        test_help()
        print()
        test_unknown_command()
        print()
        test_cost()
        print()
        test_memory(tmp_root)
        print()
        test_resume(tmp_root, project_dir)
        print()
        test_init(tmp_root)
        print()
        test_init_in_home_guard(tmp_root)
        print()
        test_command_not_in_history()
        print()
        test_read_input_passthrough()
        print()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
