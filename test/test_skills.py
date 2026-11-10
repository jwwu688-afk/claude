#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_skills.py
# 作用：验证技能机制（2026-11-10 第 21 轮）
#
# 覆盖方案里列的全部用例（§9，17 条）：
#   1  两级目录        全局 2 个 + 项目 1 个 → 3 个，来源标注正确
#   2  同名覆盖        项目版本赢，全局那份记进 shadowed
#   3  名字非法        空格 / 大写 / 超长 → 全部跳过 + 警告，不抛异常
#   4  与内置命令重名  技能叫 cost → 跳过 + 警告，/cost 仍是内置那条
#   5  无 frontmatter  首行是 # 标题 → 整份当正文，名字取文件名
#   6  frontmatter 坏  name: [unclosed → 不崩、字段为空、有警告
#   7  缺 description  仍进清单（描述位置显示占位文字），模型可调用
#   8  $ARGUMENTS      有占位符原地替换；没有就末尾追加；没参数不追加
#   9  ${SKILL_DIR}    展开成绝对路径、正斜杠；$0 原样保留
#   10 超长正文        21000 → 截断到 20000，提示里带总长度
#   11 不认识的字段    一律忽略：仍可见、仍可调用、不产生警告
#   12 user-invocable: false     不进 REGISTRY，但仍在清单里
#   13 未知技能名      返回可用清单，不抛异常
#   14 斜杠调用        FakeConv 收到待发消息，且没有自己 add_message
#   15 reload 撤命令   删掉技能文件 → 命令消失，内置命令一条不少
#   16 子 agent 双闸   清单层 exclude 有 skill；执行层 _is_tool_allowed 为 False
#   17 is_repeatable   必须是 False（防止被 L3 折叠）
#
# 这个脚本**全程离线**：
#   · 不构造真的 ToolManager（它会起后台任务连 MCP，asyncio.run 收尾时会卡住）；
#   · 不 patch Path.home()（会污染同进程其它用例），改成替换
#     skill_manager.global_skills_dir / project_skills_dir 两个函数；
#   · 不调任何模型接口，不花钱。
# =============================================================================

import asyncio  # 斜杠命令的 handler 是 async，要在事件循环里跑
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 构造 APIClient 需要三个环境变量。用 setdefault 不覆盖真实配置，
# 而且本测试一个请求都不发，值是什么无所谓。
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import core.commands as commands  # noqa: E402  内置命令表（同名冲突那一轮要用）
import tools.skill.skill_manager as skill_manager  # noqa: E402  要替换它的两个目录函数
from core.conversation import Conversation  # noqa: E402  只用来验两道闸，不真的构造
from tools.skill.skill_manager import SkillManager  # noqa: E402
from tools.skill.skill_spec import render  # noqa: E402
from tools.skill_tool import Skill  # noqa: E402
from tools.subagent.agent_spec import AgentSpec  # noqa: E402

passed = 0
total = 0


def check(name, condition, detail=""):
    """记一条断言。detail 只在失败时最有用，成功时也打出来方便对照。"""
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"  ✓ {name}" + (f"：{detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"：{detail}" if detail else ""))


# -----------------------------------------------------------------------------
# 测试环境：一个临时根目录下放 global/ 和 project/ 两个"技能目录"
# -----------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="thinker_skills_"))
GLOBAL_DIR = TMP / "global"
PROJECT_DIR = TMP / "project"

# 关键一步：把模块里的两个目录函数换成临时目录。
#   为什么替换函数而不是 chdir 或改 Path.home()：
#     chdir 会影响同进程里的其它用例（/resume 按 cwd 找会话），
#     改 home 会写到用户真实的家目录。函数替换是最小侵入的接缝。
skill_manager.global_skills_dir = lambda: GLOBAL_DIR
skill_manager.project_skills_dir = lambda cwd=None: PROJECT_DIR


def reset_tree():
    """清空两个目录并重扫，让每个用例从干净状态开始"""
    shutil.rmtree(GLOBAL_DIR, ignore_errors=True)
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    GLOBAL_DIR.mkdir(parents=True)
    PROJECT_DIR.mkdir(parents=True)
    SkillManager().reload()


def fresh():
    """干净目录 + 顺手把上一用例登记过的技能命令撤掉

    为什么要撤：REGISTRY 是模块级全局，用例之间会互相影响。
      不撤的话，"上一个用例登记的 /pdf" 会让下一个用例的断言假失败，
      而那种失败看起来像功能坏了。
    """
    reset_tree()
    commands.register_skill_commands()  # 空目录 → 把所有技能命令清掉，内置命令保留


def rescan():
    """写完技能文件后必须重扫一次

    为什么要专门写这个：SkillManager 的扫描结果**是有缓存的**（_scanned）。
      fresh() 里清空目录时已经扫过一次，那次扫到的是空目录；之后新建文件
      不会自动被看见——真实使用里也是这个口径（改完要么重启，要么 /skills reload）。
      用例里补这一行，等于替用户按了一次 reload。
    """
    SkillManager().reload()


def write_skill(base: Path, name: str, meta: str = "", body: str = "", single: bool = False) -> Path:
    """造一份技能文件

    参数 meta：frontmatter 内部的行（不含两条 ---）。空字符串表示"不写 frontmatter"。
    参数 single：True 写 <名字>.md，False 写 <名字>/SKILL.md（两种形态都要测到）。
    """
    content = f"---\n{meta}\n---\n{body}" if meta else body
    if single:
        path = base / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    return path


class FakeUI:
    """假界面：只记录打印了什么，不碰终端"""

    def __init__(self):
        self.lines = []

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


class FakeConv:
    """假对话总管：只实现技能命令用到的那几个方法

    为什么不用真的 Conversation()：它的 __init__ 会建 ToolManager → 连 MCP 服务器，
    这个离线脚本会被卡在收尾阶段（THINKER.md 记过的坑）。
    """

    def __init__(self):
        self._ui_manager = FakeUI()
        self._pending = None
        self.messages = []

    def set_pending_input(self, text):
        self._pending = text

    def take_pending_input(self):
        pending = self._pending
        self._pending = None
        return pending

    def add_message(self, message):  # 技能命令**不该**调用它，用例 14 会断言这一点
        self.messages.append(message)


# -----------------------------------------------------------------------------
# 1 / 2：两级目录与同名覆盖
# -----------------------------------------------------------------------------
def test_two_levels():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "name: pdf\ndescription: 处理 PDF")
    write_skill(GLOBAL_DIR, "deploy", "description: 发布流程", single=True)
    write_skill(PROJECT_DIR, "review", "description: 代码审查")
    rescan()

    specs = {spec.name: spec for spec in SkillManager().list_specs()}
    check("两级目录：数量", len(specs) == 3, f"实际 {sorted(specs)}")
    check("两级目录：全局来源", specs.get("pdf") and specs["pdf"].source == "global")
    check("两级目录：项目来源", specs.get("review") and specs["review"].source == "project")
    check("两级目录：单文件形态也能加载", specs.get("deploy") is not None)
    check("两级目录：没有误报问题", SkillManager().warnings == [], str(SkillManager().warnings))


def test_shadowing():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "description: 全局版本")
    write_skill(PROJECT_DIR, "pdf", "description: 项目版本")
    rescan()

    manager = SkillManager()
    specs = manager.list_specs()
    check("同名覆盖：只剩一份", len(specs) == 1, f"实际 {len(specs)}")
    check("同名覆盖：项目赢", specs[0].source == "project", specs[0].description)
    check("同名覆盖：描述来自项目版本", specs[0].description == "项目版本")
    check(
        "同名覆盖：被盖住的记进 shadowed",
        len(manager.shadowed) == 1 and manager.shadowed[0][0] == "pdf",
        str(manager.shadowed),
    )


# -----------------------------------------------------------------------------
# 3 / 4：名字非法、与内置命令重名
# -----------------------------------------------------------------------------
def test_bad_names():
    fresh()
    write_skill(GLOBAL_DIR, "My Skill", "description: 有大写和空格")  # 目录名就是默认技能名
    write_skill(GLOBAL_DIR, "UPPER", "description: 全大写", single=True)
    write_skill(GLOBAL_DIR, "a" * 65, "description: 太长", single=True)
    rescan()

    manager = SkillManager()
    check("名字非法：一个都没加载", manager.count() == 0, f"实际 {manager.count()}")
    check("名字非法：每个都留了警告", len(manager.warnings) >= 3, str(manager.warnings))
    check(
        "名字非法：警告里说清了原因",
        any("只能用" in warning for warning in manager.warnings),
        str(manager.warnings[:1]),
    )


def test_builtin_conflict():
    fresh()
    write_skill(PROJECT_DIR, "cost", "description: 想顶掉内置命令")
    rescan()

    manager = SkillManager()
    check("重名：被跳过", manager.count() == 0, f"实际 {manager.count()}")
    check("重名：留了警告", any("重名" in warning for warning in manager.warnings), str(manager.warnings))
    check(
        "重名：内置 /cost 还在",
        commands.REGISTRY["cost"].handler.__name__ == "cmd_cost",
        commands.REGISTRY["cost"].handler.__name__,
    )


# -----------------------------------------------------------------------------
# 5 / 6 / 7：frontmatter 的三种边界
# -----------------------------------------------------------------------------
def test_no_frontmatter():
    fresh()
    write_skill(GLOBAL_DIR, "plain", body="# 标题\n\n正文内容", single=True)
    rescan()

    spec = SkillManager().get("plain")
    check("无 frontmatter：仍然加载", spec is not None)
    if spec:
        check("无 frontmatter：名字取文件名", spec.name == "plain", spec.name)
        body = render(spec)
        check("无 frontmatter：整份都是正文", "# 标题" in body and "正文内容" in body, body[:30])


def test_broken_frontmatter():
    fresh()
    # name 的内联列表没有右括号：解析器要"跳过这一行、保留其它行、留警告"
    write_skill(GLOBAL_DIR, "scratch", "name: [unclosed", "正文")
    rescan()

    manager = SkillManager()
    spec = manager.get("scratch")
    check("坏 frontmatter：没崩、技能仍加载", spec is not None)
    check("坏 frontmatter：字段为空时回退到目录名", spec is not None and spec.name == "scratch",
          spec.name if spec else "None")
    check("坏 frontmatter：有警告", any("括号" in warning for warning in manager.warnings),
          str(manager.warnings))


def test_missing_description():
    fresh()
    write_skill(GLOBAL_DIR, "quiet", body="按步骤做")  # 没写 frontmatter，自然没有 description
    write_skill(GLOBAL_DIR, "loud", "description: 有描述")
    rescan()

    manager = SkillManager()
    block = manager.available_block()
    check("缺描述：仍然加载", manager.count() == 2, f"实际 {manager.count()}")
    # 【第 21.1 轮】口径变了：没描述也进清单，只是描述位置显示占位文字。
    #   上一版这里是"不进清单"，那条规则已经被整个删掉了。
    check("缺描述：也进工具清单", "quiet" in block, block)
    check("缺描述：清单里给占位文字", "未写描述" in block, block)
    check("缺描述：有描述的那个照常显示描述", "有描述" in block, block)
    loaded = asyncio.run(Skill().act(skill="quiet"))
    check("缺描述：模型能直接调用它", "按步骤做" in loaded, loaded[:30])


# -----------------------------------------------------------------------------
# 8 / 9 / 10：变量替换与截断
# -----------------------------------------------------------------------------
def test_arguments():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "description: 处理 PDF\nargument-hint: [文件路径]", body="用户要处理：$ARGUMENTS")
    write_skill(GLOBAL_DIR, "plain", "description: 没有占位符", body="按步骤做。")
    rescan()

    manager = SkillManager()
    pdf_body = render(manager.get("pdf"), "a.pdf")
    check("有占位符：原地替换", "a.pdf" in pdf_body and "$ARGUMENTS" not in pdf_body, pdf_body)

    appended = render(manager.get("plain"), "x y")
    check("没占位符：参数追加到末尾", appended.rstrip().endswith("ARGUMENTS: x y"), appended[-30:])

    no_args = render(manager.get("plain"), "")
    check("没参数：不追加", "ARGUMENTS:" not in no_args, no_args)

    # argument-hint 的写法是 `[文件路径]`，假 YAML 会把它当内联列表。
    # 这里盯的是"还原后能不能照原样显示"——实现在 skill_spec._hint_text。
    commands.register_skill_commands()
    check(
        "argument-hint：/help 里显示成 [文件路径] 而不是列表",
        commands.REGISTRY["pdf"].usage == "/pdf [文件路径]",
        commands.REGISTRY["pdf"].usage,
    )


def test_skill_dir_variable():
    fresh()
    write_skill(
        GLOBAL_DIR,
        "pdf",
        "description: 处理 PDF",
        body="脚本：${SKILL_DIR}/scripts/x.py\n项目：${PROJECT_DIR}\n参数占位 $0 原样保留",
    )
    rescan()

    spec = SkillManager().get("pdf")
    body = render(spec)
    expected_dir = str(spec.base_dir).replace("\\", "/")
    check("${SKILL_DIR}：展开成绝对路径 + 正斜杠", expected_dir in body, body.splitlines()[0])
    check("${SKILL_DIR}：不含反斜杠", "\\" not in body.splitlines()[0])
    check("${PROJECT_DIR}：展开成当前目录", os.getcwd().replace("\\", "/") in body)
    check("$0：原样保留（证明没做索引参数）", "$0" in body)


def test_long_body():
    fresh()
    write_skill(GLOBAL_DIR, "huge", "description: 超长正文", body="x" * 21000)
    rescan()

    body = render(SkillManager().get("huge"))
    check("超长正文：被截断", len(body) < 21000, f"实际 {len(body)}")
    check("超长正文：保留前 20000 字符", body.startswith("x" * 100) and "x" * 20000 in body)
    check("超长正文：提示里带总长度", "全文共 21000" in body, body[-60:])


# -----------------------------------------------------------------------------
# 11 / 12：user-invocable 生效、disable-model-invocation 已降级为"未生效"
# -----------------------------------------------------------------------------
def test_unknown_fields_are_ignored():
    """第 21.1 / 21.2 轮：不认识的字段一律忽略

    21.1 砍掉了"模型不可见"，21.2 又把"未生效字段"的标注也删了。
    现在只剩一条规则：**认得的字段读进来，不认得的不报错、不展示**。
    所以这里要钉住三件事：
      · 带 disable-model-invocation 的技能照样进清单（模型看得见）；
      · 模型照样能调用（不再被拒）；
      · 带这些多余字段不会产生任何警告（拷别人的技能过来不该需要排错）。
    """
    fresh()
    write_skill(
        GLOBAL_DIR,
        "pdf",
        "description: 处理 PDF\ndisable-model-invocation: true",
        body="按步骤做",
    )
    rescan()

    manager = SkillManager()
    check("无用字段：仍然进工具清单", "pdf" in manager.available_block(), manager.available_block())

    loaded = asyncio.run(Skill().act(skill="pdf"))
    check("无用字段：模型仍可调用", "按步骤做" in loaded, loaded[:30])
    check("无用字段：不产生任何警告", manager.warnings == [], str(manager.warnings))

    commands.register_skill_commands()
    check("无用字段：斜杠命令照常登记", "pdf" in commands.REGISTRY)


def test_not_user_invocable():
    fresh()
    write_skill(
        GLOBAL_DIR,
        "auto",
        "description: 只给模型用\nuser-invocable: false",
        body="按步骤做",
    )
    rescan()

    manager = SkillManager()
    check("不进命令：仍在工具清单里", "auto" in manager.available_block(), manager.available_block())

    commands.register_skill_commands()
    check("不进命令：REGISTRY 里没有它", "auto" not in commands.REGISTRY, str(sorted(commands.REGISTRY)))


# -----------------------------------------------------------------------------
# 13 / 14 / 15：工具与命令
# -----------------------------------------------------------------------------
def test_unknown_skill():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "description: 处理 PDF", body="按步骤做")
    rescan()

    message = asyncio.run(Skill().act(skill="nope"))
    check("未知技能：不抛异常", isinstance(message, str), type(message).__name__)
    check("未知技能：说明不存在", "不存在" in message, message)
    check("未知技能：把可用清单交回去", "pdf" in message, message)


def test_slash_invocation():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "description: 处理 PDF", body="用户要处理：$ARGUMENTS")
    commands.register_skill_commands()

    handler = commands.REGISTRY["pdf"].handler
    conversation = FakeConv()
    asyncio.run(handler(conversation, "a.pdf"))

    pending = conversation.take_pending_input()
    check("斜杠调用：留下待发消息", pending is not None)
    check("斜杠调用：带来源标记", pending and "[技能已加载：pdf]" in pending, (pending or "")[:40])
    check("斜杠调用：参数已替换", pending and "a.pdf" in pending)
    check("斜杠调用：没有自己记历史", conversation.messages == [], str(conversation.messages))


def test_reload_drops_command():
    fresh()
    write_skill(GLOBAL_DIR, "pdf", "description: 处理 PDF", body="按步骤做")
    commands.register_skill_commands()
    check("reload 前：/pdf 存在", "pdf" in commands.REGISTRY)

    builtin_names = ["help", "memory", "resume", "init", "cost", "compact", "clear", "agents", "skills"]
    shutil.rmtree(GLOBAL_DIR / "pdf")  # 模拟用户删掉技能
    commands.register_skill_commands()

    check("reload 后：/pdf 消失", "pdf" not in commands.REGISTRY, str(sorted(commands.REGISTRY)))
    missing = [name for name in builtin_names if name not in commands.REGISTRY]
    check("reload 后：内置命令一条不少", not missing, str(missing))


# -----------------------------------------------------------------------------
# 16 / 17：子 agent 两道闸、可重复查询标记
# -----------------------------------------------------------------------------
class FakeToolManager:
    """只记录 exclude，不真的返回工具清单"""

    def __init__(self):
        self.seen = []

    async def get_tools_description(self, exclude=None, allow=None):
        self.seen.append((exclude, allow))
        return []


def test_subagent_gates():
    # 用 object.__new__ 绕过 Conversation 的单例 __new__ 和会连 MCP 的 __init__
    conversation = object.__new__(Conversation)
    conversation._agent_stack = [AgentSpec(name="explore", description="只读", body="", tools=("file_read",), model=None)]
    conversation._task_depth = 1
    conversation._tool_manager = FakeToolManager()

    check("执行层：子 agent 拿不到 skill", conversation._is_tool_allowed("skill") is False)
    check("执行层：子 agent 也拿不到 task", conversation._is_tool_allowed("task") is False)
    check("执行层：白名单里的工具照常可用", conversation._is_tool_allowed("file_read") is True)

    asyncio.run(conversation._get_available_tools())
    exclude = conversation._tool_manager.seen[-1][0]
    check("清单层：exclude 里同时有 task 和 skill", exclude == {"task", "skill"}, str(exclude))

    # 主 agent（深度 0、没有 agent 定义）不该受限制
    conversation._task_depth = 0
    conversation._agent_stack = []
    asyncio.run(conversation._get_available_tools())
    check("清单层：主 agent 不排除任何工具", conversation._tool_manager.seen[-1][0] is None)


def test_repeatable_flag():
    check("is_repeatable 必须是 False（防止被 L3 折叠）", Skill.is_repeatable is False)


# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------
def main():
    try:
        test_two_levels()
        print()
        test_shadowing()
        print()
        test_bad_names()
        print()
        test_builtin_conflict()
        print()
        test_no_frontmatter()
        print()
        test_broken_frontmatter()
        print()
        test_missing_description()
        print()
        test_arguments()
        print()
        test_skill_dir_variable()
        print()
        test_long_body()
        print()
        test_unknown_fields_are_ignored()
        print()
        test_not_user_invocable()
        print()
        test_unknown_skill()
        print()
        test_slash_invocation()
        print()
        test_reload_drops_command()
        print()
        test_subagent_gates()
        print()
        test_repeatable_flag()
        print()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    # 失败必须用非 0 退出码：第 17.1 轮就是这么发现两个用例早已失效的
    # （"全绿"其实是 rsync 出来的假象，脚本失败仍返回 0）。
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)


if __name__ == "__main__":
    main()
