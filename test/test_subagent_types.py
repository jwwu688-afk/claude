# =============================================================================
# 文件：test/test_subagent_types.py
# 作用：测试「子 agent 类型化」（第 18 轮）——内置清单、三层白名单、模型覆盖。
#
# 【2026-11-09 简化后】这个文件原来有 18 条用例，一大半在测 frontmatter 解析
#   和三级目录加载（脏数据、覆盖顺序、坏文件隔离……）。那两样功能已经删掉了，
#   对应的用例也一起删——**测一个不存在的入口比没有测试更糟**：
#   它会让下一个人以为"这个功能还在"，然后去找一个永远不走的分支。
#
#   现在只测三件事：
#     1. 内置清单本身对不对（数量、工具白名单、描述非空）；
#     2. 三层白名单是不是真的接上了（清单层 / 执行层 / 说明书层）；
#     3. 模型覆盖能不能走到请求里。
#   外加两条"防止退化"的用例：项目里的 .thinker/agents 不生效、
#   老接口 register_subagent_prompt 仍能造临时类型。
#
# 全程离线：不构造 ToolManager（会连 MCP）、不发请求、不调模型。
# 运行方式（项目根目录）：
#   python test/test_subagent_types.py
# =============================================================================

import asyncio  # get_tools_description 是 async
import os  # 拼路径
import sys  # 改模块搜索路径 + 失败时非 0 退出
import tempfile  # 造临时目录，验证"目录不再被扫描"
import traceback  # 失败时打印现场

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.api_client import APIClient  # 只测 model 优先级那段（用假 client，不发请求）
from core.conversation import Conversation  # 执行层白名单
from tools.subagent.agent_spec import (  # 被测的数据结构
    MAX_DESCRIPTION_CHARS,
    AgentSpec,
)
from tools.subagent.subagent_manager import SubagentManager  # 登记表
from tools.task import Task  # 说明书层
from tools.tool_manager import ToolManager  # 清单层（用裸对象，不触发 MCP）


# -----------------------------------------------------------------------------
# 一、造数据
# -----------------------------------------------------------------------------

READ_ONLY_TYPES = ("explore", "plan", "code-reviewer")
READ_ONLY_TOOLS = ("file_glob", "file_grep", "file_read")


def reset_manager():
    """丢掉单例并重新造

    为什么必须重置：SubagentManager 是单例。下面有用例会临时 register 一个假类型，
    不重置的话它会留到后面的用例里，让"内置只有四个"这类断言假失败。
    """
    SubagentManager._instance = None
    SubagentManager._initialized = False
    return SubagentManager()


class FakeTool:
    """最小可用的假工具：只需要 json_schema()"""

    def __init__(self, name):
        self.name = name

    def json_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}


def bare_tool_manager(names):
    """造一个不连 MCP 的 ToolManager 裸对象

    为什么用 __new__：构造真对象会给 MCPClient 起后台任务去连服务器，
    然后在 asyncio.run 收尾时卡住（THINKER.md 记过）。这里只测 allow 过滤，
    除 tools 字典之外都可以是空壳。
    """
    manager = object.__new__(ToolManager)
    manager.tools = {name: FakeTool(name) for name in names}
    manager.register_mcp_task = None
    manager._ensure_mcp_registration = lambda: None
    return manager


# -----------------------------------------------------------------------------
# 二、内置清单
# -----------------------------------------------------------------------------

def test_builtin_agents_complete():
    """内置就是四个：一个兜底能写，三个只读

    为什么这几条断言值得逐条写：
      · 数量：说明书长度和这里直接相关，多一个就多一段每轮都要发的文字；
      · general-purpose 的 tools 必须是 None：被收窄了，"派个 agent 去改文件"这条路就断了；
      · 三个只读类型的白名单必须一致：不一致说明有人只改了一个，
        那种情况最难查——同样的活派给 plan 能跑、派给 explore 就报工具不存在。
    """
    print("测试 内置: 清单完整")

    manager = reset_manager()

    assert manager.list_types() == ["general-purpose", "explore", "plan", "code-reviewer"]
    assert manager.get_spec("general-purpose").tools is None
    for name in READ_ONLY_TYPES:
        assert manager.get_spec(name).tools == READ_ONLY_TOOLS

    # 描述和人设都不能为空：主 agent 全靠描述判断"何时派活"，空描述等于这个类型不存在
    for spec in manager.list_specs():
        assert spec.description.strip(), f"{spec.name} 缺少 description"
        assert spec.body.strip(), f"{spec.name} 缺少人设正文"

    print("  ✓ 通过")


def test_unknown_type_raises():
    """取一个不存在的类型要抛 ValueError（调用方会先校验，走到这里就是 bug）"""
    print("测试 内置: 未知类型")

    manager = reset_manager()
    try:
        manager.get_spec("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("取不存在的类型居然没抛异常")

    # 但 describe 不能抛：它只用于拼说明书，拼不出来顶多难看
    assert manager.describe("nope") == ("", "未说明")

    print("  ✓ 通过")


def test_register_compat():
    """老接口 register_subagent_prompt 仍能造临时类型（tools 字符串要能解析）

    保留这个接口的理由：测试和临时脚本用它造假类型最方便。
    它要是悄悄坏了，受影响的是"以后想加变体测试"的人，不会有人当场发现。
    """
    print("测试 内置: 兼容接口")

    manager = reset_manager()
    manager.register_subagent_prompt(
        "tmp-reviewer",
        "你是一个临时审查员。",
        description="临时类型",
        tools="file_read、file_grep",  # 中文顿号也要认
    )

    spec = manager.get_spec("tmp-reviewer")
    assert spec.tools == ("file_read", "file_grep")
    assert "tmp-reviewer" in manager.list_types()

    # tools="全部" 要归一化成 None（继承全部），不能变成 ("全部",)
    manager.register_subagent_prompt("tmp-all", "你是兜底。", description="x", tools="全部")
    assert manager.get_spec("tmp-all").tools is None

    print("  ✓ 通过")


def test_system_prompt_has_env_and_boundary():
    """子 agent 的 system prompt = 人设 + 环境 + 边界

    为什么单独测这一段：它是"子 agent 只看自己的提示词"这条设计的落地。
    少了环境，子 agent 不知道自己在哪里干活；少了边界，它会以为自己也能派活。
    """
    print("测试 内置: system prompt 拼装")

    manager = reset_manager()
    prompt = manager.get_subagent_prompt("explore")

    assert "只读的代码探索 agent" in prompt  # 人设正文在
    assert "当前工作目录" in prompt  # 环境三行在
    assert "【边界】" in prompt
    assert "不能把任务再派给别的 agent" in prompt

    print("  ✓ 通过")


def test_project_dir_is_ignored():
    """项目里的 .thinker/agents 不再被加载（防止目录机制被悄悄加回来）

    为什么留这条：简化时删掉了目录扫描。以后如果有人"顺手"把它加回来，
    这条会红——提示他先想清楚要不要恢复那三个概念（优先级、覆盖、来源）。
    """
    print("测试 内置: 目录不生效")

    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = os.path.join(tmp, ".thinker", "agents")
        os.makedirs(agents_dir)
        with open(os.path.join(agents_dir, "ghost.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: ghost\ndescription: 不该被加载\n---\n你是幽灵。\n")

        os.chdir(tmp)  # 在"项目根目录"下重新造一次登记表
        try:
            manager = reset_manager()
            types = manager.list_types()
        finally:
            os.chdir(old_cwd)

    assert "ghost" not in types
    assert len(types) == 4

    print("  ✓ 通过")


# -----------------------------------------------------------------------------
# 三、三层白名单
# -----------------------------------------------------------------------------

def test_allow_filters_schema_list():
    """清单层：allow 给了三个工具，就只能看到三个（exclude 仍生效）"""
    print("测试 白名单: 清单层")

    manager = bare_tool_manager(["file_read", "file_grep", "file_glob", "cmd_runner", "task"])
    schemas = asyncio.run(
        manager.get_tools_description(exclude={"task"}, allow={"file_read", "file_grep", "file_glob"})
    )
    names = [item["function"]["name"] for item in schemas]
    assert names == ["file_read", "file_grep", "file_glob"]

    # allow=None 表示不限制（主 agent 的路径），应该全给
    all_names = [
        item["function"]["name"]
        for item in asyncio.run(manager.get_tools_description(exclude={"task"}))
    ]
    assert "cmd_runner" in all_names

    print("  ✓ 通过")


def test_execute_layer_blocks_out_of_scope_tools():
    """执行层：白名单外的工具（含 task）一律拒绝

    用裸 Conversation 只测这一个方法，不碰 API、不碰历史。
    """
    print("测试 白名单: 执行层")

    explore = AgentSpec(
        name="explore",
        description="只读",
        body="只读",
        tools=READ_ONLY_TOOLS,
        model=None,
    )

    conv = object.__new__(Conversation)

    # 主 agent：栈为空，全部允许
    conv._agent_stack = []
    assert conv._is_tool_allowed("cmd_runner") is True
    assert conv._is_tool_allowed("task") is True

    # explore 子 agent：白名单内允许，白名单外拒绝，task 永远拒绝
    conv._agent_stack = [explore]
    assert conv._is_tool_allowed("file_read") is True
    assert conv._is_tool_allowed("cmd_runner") is False
    assert conv._is_tool_allowed("task") is False

    # tools=None（继承全部）：除了 task，其它都允许
    inherit = AgentSpec(
        name="general-purpose", description="兜底", body="兜底", tools=None, model=None
    )
    conv._agent_stack = [inherit]
    assert conv._is_tool_allowed("cmd_runner") is True
    assert conv._is_tool_allowed("task") is False

    print("  ✓ 通过")


def test_description_lists_tools_and_model():
    """说明书层：每个类型写出工具与模型；未登记的类型不能出现

    第 8.1 轮的教训：说明书里出现一个没登记的类型，模型就会照着去派活，
    然后收到"类型不存在"，白烧两轮。
    """
    print("测试 白名单: 说明书层")

    reset_manager()
    description = Task()._tool_description()

    for name in ("general-purpose", "explore", "plan", "code-reviewer"):
        assert name in description
    assert "file_glob、file_grep、file_read" in description  # 工具白名单可见
    assert "模型：继承" in description
    assert "nonexistent-agent" not in description

    print("  ✓ 通过")


def test_description_truncates_long_text():
    """过长的 description 要被截断——它会进每次请求的 tools 字段"""
    print("测试 白名单: 描述截断")

    manager = reset_manager()
    long_text = "很长的描述" * 100
    manager._specs["verbose"] = AgentSpec(
        name="verbose", description=long_text, body="x", tools=None, model=None
    )

    description = Task()._tool_description()
    # 断言"可观察到的上限"，而不是"我以为会切成什么样"：
    # 160 能被"很长的描述"整除，所以前 32 组会原样保留，
    # 写"某个固定片段不存在"会挂——那不是 bug，是断言的写法有问题。
    line = next(l for l in description.splitlines() if l.startswith("- verbose"))
    assert "…" in line
    assert len(line) <= MAX_DESCRIPTION_CHARS + 40  # 上限 = 描述上限 + 前缀/工具/模型那几十字

    print("  ✓ 通过")


# -----------------------------------------------------------------------------
# 四、模型覆盖
# -----------------------------------------------------------------------------

def test_model_override_reaches_request():
    """子 agent 指定的模型要真的进到请求里（用假 client，不发网络请求）

    为什么测到这一层：改前 `get_completion_stream` 是无条件
    `request_params["model"] = self.model`，不管上层怎么传都会被抹掉。
    子 agent 的 model 字段会"看起来配置了、实际没生效"，而且没有任何报错。
    """
    print("测试 模型: 覆盖进请求")

    class FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return iter([])  # 空流：不发请求，也不产生任何 chunk

    api = object.__new__(APIClient)  # 绕开 __init__，避免读 .env / 建真实客户端
    fake = FakeCompletions()
    api.client = type("FakeClient", (), {"chat": type("Chat", (), {"completions": fake})()})()
    api.model = "global-model"

    # ① 指定了模型 → 用指定的
    list(api.get_completion_stream({"messages": [], "model": "agent-model"}))
    assert fake.calls[-1]["model"] == "agent-model"

    # ② 没指定 → 退回全局
    list(api.get_completion_stream({"messages": []}))
    assert fake.calls[-1]["model"] == "global-model"

    print("  ✓ 通过")


# -----------------------------------------------------------------------------
# 五、跑全部
# -----------------------------------------------------------------------------

def run_all_tests():
    """逐个跑；有失败就用非 0 退出码（约定见 THINKER.md 的「已知的坑」）"""
    print("开始运行子 agent 类型化测试（全程离线）...\n")

    cases = [
        test_builtin_agents_complete,
        test_unknown_type_raises,
        test_register_compat,
        test_system_prompt_has_env_and_boundary,
        test_project_dir_is_ignored,
        test_allow_filters_schema_list,
        test_execute_layer_blocks_out_of_scope_tools,
        test_description_lists_tools_and_model,
        test_description_truncates_long_text,
        test_model_override_reaches_request,
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
