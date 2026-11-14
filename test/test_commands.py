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
import json  # 【第 35 轮】/clean 的用例要手写会话文件（带 header 行）
import os
import shutil
import sys
import tempfile
import time  # 【第 35 轮】把文件 mtime 拨到几十天前，造"旧会话"
import types
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
from core.history.history_manager import HistoryManager  # noqa: E402

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

    def __init__(self, answers=None, choices=None):
        self.lines = []
        self._answers = list(answers or [])  # 预置的输入队列（给 get_user_input 用）
        # 【第 35 轮】预置的编号选择队列（给 get_numbered_choice 用）。
        #   为什么单独一个队列而不是复用 _answers：
        #   两者回答的不是同一种问题（一个是自由输入、一个是选编号），
        #   混在一起的话，/clean 这种"先选动作、再确认"的命令没法分别控制
        #   第一次和第二次选什么，测试就只能验到第一条分支。
        self._choices = list(choices or [])
        self.choice_title = None
        self.choice_items = None
        self.choice_count = 0  # 问了几次（/clean 的删除分支应该问两次）

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

    async def get_numbered_choice(self, title, items, hint=None):
        # hint 参数（第 35 轮补）：真实签名是 `hint="输入编号，直接回车取消"`，
        #   这里写默认 None 接住——少写的话，任何传了 hint 的命令都会 TypeError，
        #   而报错会指向"命令测试"这个毫不相干的地方。
        self.choice_title = title
        self.choice_items = list(items)
        self.choice_count += 1
        # 预置队列空了就默认选第一项：老用例（/resume 选会话）依赖"固定选第一项"，
        # 不能因为这一轮加了队列支持就把它们的行为改掉。
        return self._choices.pop(0) if self._choices else 0

    async def get_user_input(self, prompt=None, completer=None):
        # 参数 completer（第 16 轮加）：真实 UI 会对它做 Tab 补全，这里只需要接住，
        # 不能少写——少了就会 TypeError，而且报错信息在命令测试里显得莫名其妙。
        # 顺便把它记下来，方便将来验证"哪些调用点传了补全源"。
        self._last_completer = completer
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


def test_resume_order_by_modified(tmp_root, project_dir):
    """/resume 按"最后修改时间"倒序：最后动过的会话排最前（2026-11-14 第 36 轮）

    造三个会话，让**创建顺序**和**最后修改顺序**故意相反：
      早创建、晚修改  → 应该排第一
      晚创建、早修改  → 应该排最后
    改前的实现按文件名（=创建时间）排，会把"最后动过的"挤到下面去。
    """
    print("测试: /resume 按最后修改时间排序")
    old_sessions_dir = session_module.SESSIONS_DIR
    old_cwd = commands._cwd
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_order"
    session_module.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    commands._cwd = lambda: str(project_dir)
    try:
        # 用固定文件名造三个会话，文件名顺序 = A < B < C（=创建顺序）。
        # 这些文件的 header 里 cwd 会写成 sessions_order 目录（_write_old_session 的行为），
        # 而 /resume 按 project_dir 过滤——所以这里直接造在 project_dir 的 cwd 上，
        # 就得手写 header，不能复用 _write_old_session（它把 cwd 写成 directory）。
        # 干脆自己写三个最小会话文件，header.cwd 指向 project_dir。
        def _write(name, preview, mtime_delta):
            p = session_module.SESSIONS_DIR / name
            lines = [
                json.dumps({"type": "header", "cwd": str(project_dir), "started_at": "2026-01-01T10:00:00"}),
                json.dumps({"type": "message", "ts": 1763000000, "message": {"role": "user", "content": [{"type": "text", "text": preview}]}}, ensure_ascii=False),
            ]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stamp = time.time() + mtime_delta  # mtime_delta 为负 = 过去
            os.utime(p, (stamp, stamp))
            return p

        # 创建顺序 A→B→C（文件名递增），最后修改顺序 C→B→A（mtime 递增），正好相反。
        path_a = _write("20260101-000000-aaaa.jsonl", "最早创建", -10)
        path_b = _write("20260102-000000-bbbb.jsonl", "中间创建", -100)
        path_c = _write("20260103-000000-cccc.jsonl", "最晚创建", -1000)

        ui = FakeUI()
        conv = FakeConv(ui)
        asyncio.run(commands.cmd_resume(conv, ""))

        items = ui.choice_items or []
        check("列出了三个会话", len(items) == 3, f"实际 {len(items)}")
        if len(items) == 3:
            # 用预览里的独特词定位每一项在列表里的位置，比解析时间戳稳（本地时区无关）。
            pos_a = next(i for i, x in enumerate(items) if "最早创建" in x)
            pos_b = next(i for i, x in enumerate(items) if "中间创建" in x)
            pos_c = next(i for i, x in enumerate(items) if "最晚创建" in x)
            check("最后修改的排最前", pos_a < pos_b < pos_c, f"A/B/C 位置 {pos_a}/{pos_b}/{pos_c}")
        if items:
            # 时间列应显示修改时间而非创建时间：A 的 mtime 是"刚刚"（今天），
            # 而它的创建日期是 2026-01-01。列表里不该再出现那个旧日期。
            check("时间列显示修改时间而非创建时间", "2026-01-01" not in items[0], items[0])
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

        # 3) --global 不该被拦：它写的是 ~/.thinker，本来就跟着人走、不跟项目走。
        #    把全局路径临时改到 tmp 里，避免真的写进家目录。
        old_global = memory_module.GLOBAL_MEMORY_PATH
        memory_module.GLOBAL_MEMORY_PATH = Path(tmp_root) / "global_mem" / "THINKER.md"
        try:
            ui3 = FakeUI()
            asyncio.run(commands.cmd_init(FakeConv(ui3), "--global"))
            check("--global 不受主目录守卫影响", memory_module.GLOBAL_MEMORY_PATH.is_file())
        finally:
            memory_module.GLOBAL_MEMORY_PATH = old_global
    finally:
        os.chdir(old_cwd)
        memory_module.is_user_home = old_check


def fresh_history_manager():
    """拿一个账本干净的 HistoryManager

    为什么要重置单例：HistoryManager 是共享的，上一个用例留下的消息和
    用量会让断言失真。这是本项目所有涉及历史管理器的测试的统一写法。
    """
    HistoryManager._instance = None
    HistoryManager._initialized = False
    return HistoryManager()


def test_compact():
    print("测试: /compact")
    hm = fresh_history_manager()
    calls = []

    async def fake_summary(messages):
        calls.append(len(messages))
        return "之前讨论了 A 方案，改了 a.py 的登录逻辑。"

    hm.set_summarizer(fake_summary)
    # 【第 25 轮改动】L5 从"每次压最老一轮"改成"保留最近 10 条（COMPACT_KEEP_RECENT）、
    #   其余整段一次压完"。原来这里只造了 2 轮共 5 条，全都落在保留区里，
    #   于是 /compact 会走"没有可压缩的较早对话"分支：一条不动、摘要模型也不调，
    #   "条数变少""确实调用了摘要模型"这两条断言必然失败——看起来像压缩坏了，
    #   其实是测试的数据量没跟上新阈值。
    #   现在造 8 轮 = 16 条 + system，比保留区（10 条）长，压缩才有东西可压。
    msgs = [{"role": "system", "content": [{"type": "text", "text": "系统提示词"}]}]
    for round_index in range(8):
        msgs.append({
            "role": "user",
            "content": [{"type": "text", "text": f"第{round_index}轮：" + "问题" * 200}],
        })
        msgs.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "回答" * 200}],
        })
    hm.messages_history[-1] = msgs
    before_count = len(hm.messages_history[-1])

    ui = FakeUI()
    conv = FakeConv(ui)
    conv._history_manager = hm
    asyncio.run(commands.cmd_compact(conv, ""))

    after = hm.messages_history[-1]
    text = ui.text()
    check("条数变少", len(after) < before_count, f"{before_count} → {len(after)}")
    check("系统提示词保留", after[0]["role"] == "system")
    check("摘要被插进历史", any("已被压缩" in str(m) for m in after))
    check("确实调用了摘要模型", len(calls) == 1, f"调用 {len(calls)} 次")
    check("输出带前后 token 对比", "已压缩" in text and "token" in text)
    check("提醒缓存会失效", "缓存会失效" in text)
    check("账本被重算（不是旧数字）", hm.history_token_usage[-1].total_tokens > 0)
    check("已压标记置位（防连环压缩）", hm._compressed_for_current_usage is True)


def test_compact_nothing_to_do():
    print("测试: /compact 无可压内容")
    hm = fresh_history_manager()
    calls = []

    async def fake_summary(messages):
        calls.append(1)
        return "不该被调用"

    hm.set_summarizer(fake_summary)
    hm.messages_history[-1] = [{"role": "system", "content": [{"type": "text", "text": "系统"}]}]

    ui = FakeUI()
    conv = FakeConv(ui)
    conv._history_manager = hm
    asyncio.run(commands.cmd_compact(conv, ""))

    check("提前拦下并说明原因", "没有可压缩" in ui.text())
    check("没有白花一次摘要调用", calls == [], str(calls))
    check("历史没被动过", len(hm.messages_history[-1]) == 1)


def _write_old_session(directory, name, days_ago=40, text="老问题", messages=1):
    """造一个会话文件，并把 mtime 拨到 days_ago 天前（第 35 轮）

    为什么手写而不走 SessionStore.create：
      create 用当前时间起文件名，造不出"看起来是两个月前"的文件；
      而 /clean 的全部判断都建立在 mtime 上（见 usage_report 的说明）。

    参数 messages：写几条消息。压测分支要 30 条以上——只有几百字节的小会话
      压完不会变小（gzip 头部开销就二十来字节），compress_sessions 会
      把那种归档撤销、原样保留（skipped），用例就验不到"真的归档了"。
    """
    path = Path(directory) / name
    lines = [
        json.dumps({"type": "header", "cwd": str(directory), "started_at": "2026-11-01T10:00:00"}),
    ]
    for i in range(messages):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "ts": 1763000000 + i,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"{text} #{i}"}],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if days_ago:
        stamp = time.time() - days_ago * 86400
        os.utime(path, (stamp, stamp))
    return path


def test_clean_nothing_to_do(tmp_root):
    """没有超过阈值的老会话时，只报一句，不动任何文件"""
    print("测试: /clean 没有旧会话")
    old_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_clean_a"
    session_module.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _write_old_session(session_module.SESSIONS_DIR, "20261113-100000-aaaa.jsonl", days_ago=0)

        ui = FakeUI()
        asyncio.run(commands.cmd_clean(FakeConv(ui), ""))

        text = ui.text()
        check("报了总数和占用", "1 个文件" in text and "占" in text, text)
        check("提示暂时不用清理", "暂时不用清理" in text, text)
        check("没问要选什么动作", ui.choice_count == 0, str(ui.choice_count))
        check("文件没被动", len(list(session_module.SESSIONS_DIR.glob("*.jsonl"))) == 1)
    finally:
        session_module.SESSIONS_DIR = old_dir


def test_clean_compress(tmp_root):
    """选「压缩归档」：原文件变 .jsonl.gz，数据仍能读"""
    print("测试: /clean 压缩归档")
    old_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_clean_b"
    session_module.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path = _write_old_session(
            session_module.SESSIONS_DIR, "20260101-100000-bbbb.jsonl", days_ago=40, messages=40
        )

        ui = FakeUI(choices=[0])
        asyncio.run(commands.cmd_clean(FakeConv(ui), ""))

        archive = path.with_name(path.name + ".gz")
        check("先报了有几个旧会话", "1 个会话超过" in ui.text(), ui.text())
        check("选项文案是压缩优先", "压缩归档" in (ui.choice_items or [""])[0], str(ui.choice_items))
        check("报告已归档", "已归档 1 个会话" in ui.text(), ui.text())
        check("原文件变归档", archive.exists() and not path.exists())
        check("归档里数据还在", len(session_module._read_session(archive)[0]) == 40)
    finally:
        session_module.SESSIONS_DIR = old_dir


def test_clean_delete_needs_confirm(tmp_root):
    """选「删除」之后必须再确认一次；第二次选取消，文件必须还在"""
    print("测试: /clean 删除要二次确认")
    old_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_clean_c"
    session_module.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path = _write_old_session(session_module.SESSIONS_DIR, "20260101-100000-cccc.jsonl", days_ago=40)

        # 第一次选 1（删除），第二次选 1（算了，取消）
        ui = FakeUI(choices=[1, 1])
        asyncio.run(commands.cmd_clean(FakeConv(ui), ""))

        check("问了两次", ui.choice_count == 2, str(ui.choice_count))
        check("提示了不可恢复", "无法恢复" in ui.text(), ui.text())
        check("取消后文件还在", path.exists())
        check("没报告删除", "已删除" not in ui.text(), ui.text())
    finally:
        session_module.SESSIONS_DIR = old_dir


def test_clean_delete_confirmed(tmp_root):
    """二次确认选「确认删除」：文件真的没了"""
    print("测试: /clean 确认删除")
    old_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_clean_d"
    session_module.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path = _write_old_session(session_module.SESSIONS_DIR, "20260101-100000-dddd.jsonl", days_ago=40)

        ui = FakeUI(choices=[1, 0])
        asyncio.run(commands.cmd_clean(FakeConv(ui), ""))

        check("文件被删了", not path.exists())
        check("报告已删除", "已删除 1 个会话" in ui.text(), ui.text())
    finally:
        session_module.SESSIONS_DIR = old_dir


def test_clear(tmp_root):
    print("测试: /clear")
    old_sessions_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions_clear"  # 别污染真实会话目录
    try:
        hm = fresh_history_manager()
        conv = conversation_module.Conversation.__new__(conversation_module.Conversation)
        conv._history_manager = hm
        conv._task_depth = 0
        # cmd_clear 要用到 model（写进新会话 header）和系统提示词
        conv._api_client = types.SimpleNamespace(model="test-model")
        conv._prompt_manager = types.SimpleNamespace(get_system_prompt=lambda: "系统提示词")
        conv._session = session_module.SessionStore.create(cwd=str(tmp_root), model="test-model")

        old_path = conv._session.path
        # 按真实顺序铺三条：系统提示词 + 一问一答。
        # 为什么不省掉 system：reset_conversation 会先把整页清空、再把 system 放回，
        # 少了它就看不出"清空后仍然保留系统提示词"这件事。
        conv.add_message(conv.build_system_message())
        conv.add_message({"role": "user", "content": [{"type": "text", "text": "你好"}]})
        conv.add_message({"role": "assistant", "content": [{"type": "text", "text": "你好呀"}]})
        before_count = len(hm.messages_history[-1])

        ui = FakeUI()
        conv._ui_manager = ui
        asyncio.run(commands.cmd_clear(conv, ""))

        after = hm.messages_history[-1]
        check("清空前确实有多条", before_count == 3, str(before_count))
        check("只剩系统提示词", len(after) == 1 and after[0]["role"] == "system")
        check("清空后不立刻建新文件（懒创建，第 21.7 轮）", conv._session is None)
        check("旧文件仍保留（能 /resume 回去）", old_path.is_file())
        check("提示下次提问时才创建", "下一次真正提问" in ui.text())
        check("提示旧会话还在", "旧会话仍在" in ui.text())
        # 懒创建的另一半：clear 之后第一条真正的消息要建出新文件，
        #   内容 = header + system + 这条消息（system 由 create 的 messages 参数补写）。
        conv.add_message({"role": "user", "content": [{"type": "text", "text": "clear 之后的第一句"}]})
        check("clear 后第一条消息触发建新文件",
              conv._session is not None and conv._session.path != old_path,
              conv._session.path.name if conv._session else "None")
        new_lines = conv._session.path.read_text(encoding="utf-8").strip().splitlines()
        check("新文件 = header + system + 第一条消息", len(new_lines) == 3, str(len(new_lines)))
        check("账本重算成新占用", hm.history_token_usage[-1].total_tokens > 0)
    finally:
        session_module.SESSIONS_DIR = old_sessions_dir


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
        test_resume_order_by_modified(tmp_root, project_dir)
        print()
        test_init(tmp_root)
        print()
        test_init_in_home_guard(tmp_root)
        print()
        test_compact()
        print()
        test_compact_nothing_to_do()
        print()
        test_clean_nothing_to_do(tmp_root)
        print()
        test_clean_compress(tmp_root)
        print()
        test_clean_delete_needs_confirm(tmp_root)
        print()
        test_clean_delete_confirmed(tmp_root)
        print()
        test_clear(tmp_root)
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
