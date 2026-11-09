# =============================================================================
# 文件：test/test_task_description.py
# 作用：验证 task 工具的"说明书"跟"登记表"是一致的。
#
# 为什么专门为一段文字写测试？
#   2026-11-06 现场复现过一次：用户只输入"你好"，模型就去调 task 派活。
#   根因不是模型乱来，而是说明书里留着一句从 Claude Code 抄来的示例
#   "用户：你好 → 用 greeting-responder agent"，而项目里根本没登记这个 agent。
#   一段提示词写错，和一行代码写错一样，都会让功能真实失效——
#   所以它同样需要断言来兜住，不能靠"下次注意"。
#
# 这里检查四件事：
#   1. 登记表里有的类型，说明书里必须有（否则模型不知道该填什么）
#   2. 登记表里没有的类型，说明书里不能出现（否则模型会去够不存在的东西）
#   3. 说明书的"不要用"一节要覆盖寒暄/单轮问答（否则又会误触发）
#   4. 传了不存在的类型，要返回可用清单，而不是抛异常（否则模型瞎猜）
#
# 全程离线，不调模型 API、不碰真实文件。
# 运行方式：在项目根目录执行 python test/test_task_description.py
#
# 【2026-11-06 第 8.1 轮】新增本文件。
# =============================================================================

import asyncio  # act() 是 async，要用 asyncio.run 跑起来
import os  # 拼路径、改模块搜索路径
import sys  # 把 ../src 塞进搜索路径
import traceback  # 失败时打印完整现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# 和 test_file_tools.py 一样的做法：从 test/ 目录直接跑脚本时，
# Python 默认找不到 src/ 下的包，所以手动把 "../src" 放到搜索路径第一位

from tools.task import Task  # 被测对象：task 工具
from tools.subagent.subagent_manager import SubagentManager  # 登记表：类型清单的唯一来源


def run(coro):
    """同步地跑一次异步调用"""
    return asyncio.run(coro)


def fresh_task():
    """造一个干净的 Task 实例（每次都用同一个登记表，只重置实例状态）"""
    return Task()


def reset_registry():
    """把登记表恢复到"只有 general-purpose"的初始状态

    为什么要这样：下面有一个用例会临时登记一个假类型，
    如果不还原，后面的用例就会看到多出来的类型，断言跟着飘。
    """
    manager = SubagentManager()
    # 【第 18 轮】改前这里直接删 _system_prompt_map / _description_map / _tools_map
    #   三张表里的非 general-purpose 项。第 18 轮把三张表合并成了一个 _specs，
    #   且内置定义改成"构造即加载"，所以"还原"的正确做法是：
    #   把单例丢掉、重新造一个——__init__ 里会自动装回那四个内置类型。
    #   直接删 _specs 里的项也行，但那样会漏掉"内置到底应该有哪些"这个事实，
    #   以后内置从 4 个变 6 个，这个测试就会悄悄只还原一部分。
    SubagentManager._instance = None  # 丢掉旧实例
    SubagentManager._initialized = False  # 清初始化标记
    SubagentManager()  # 重新构造 = 重新装回内置定义


def test_description_lists_every_registered_type():
    """① 登记表里有的类型，说明书里必须有"""
    task = fresh_task()
    description = task._tool_description()
    for prompt_type in SubagentManager().list_types():
        assert prompt_type in description, f"说明书里缺少已登记的类型：{prompt_type}"
    print("  ✓ 已登记类型都出现在说明书里")


def test_description_has_no_unregistered_type():
    """② 登记表里没有的类型，说明书里不能出现

    这一条就是本次 bug 的回归测试：只要有人再把 Claude Code 的示例抄回来，
    这里立刻会红。
    """
    task = fresh_task()
    description = task._tool_description()
    registered = set(SubagentManager().list_types())
    # 【第 18 轮】code-reviewer 从"幽灵类型"变成了真实内置类型，
    #   所以它从这份名单里移除了。留下的三个仍是"没登记过"的名字：
    #   greeting-responder 是第 8.1 轮误触发的源头，两个下划线写法是模型常见的变体。
    for ghost in ("greeting-responder", "greeting_responder", "code_reviewer"):
        assert ghost not in registered, f"测试前提不成立：{ghost} 居然被登记了"
        assert ghost not in description, f"说明书里出现了未登记的类型：{ghost}"
    print("  ✓ 说明书里没有幽灵类型")


def test_description_has_no_literal_greeting_example():
    """③ 说明书里不能出现和用户输入字面重合的招呼语

    few-shot 示例和用户输入一字不差时，模型几乎一定会照着示例走。
    所以"你好"这类词不允许出现在说明书里（只能以否定规则的形式描述场景）。
    """
    task = fresh_task()
    description = task._tool_description()
    for greeting in ("你好", "谢谢", "您好"):
        assert greeting not in description, f"说明书里出现了会被字面命中的招呼语：{greeting}"
    print("  ✓ 说明书里没有字面命中的招呼语")


def test_description_forbids_trivial_delegation():
    """④ "不要用" 一节必须明确挡住寒暄和单轮问答"""
    task = fresh_task()
    description = task._tool_description()
    assert "打招呼" in description, "说明书没提「打招呼不要派活」"
    assert "闲聊" in description, "说明书没提「闲聊不要派活」"
    assert "单轮就能答完" in description, "说明书没提「单轮问答不要派活」"
    # "适合用"和"不要用"两张清单都要在，否则模型只有反例、没有正例
    assert "以下情况适合用" in description
    assert "以下情况不要用这个工具" in description
    print("  ✓ 正例/反例两节都在，寒暄与单轮问答已被明确挡住")


def test_description_drops_fake_parallel_promise():
    """⑤ 说明书不能承诺并行（当前实现是串行的）

    task.py 的说明书原来写着"能并行就并行，性能最好"，但
    Conversation 是单例 + 一页消息栈，一次只能跑一个子 agent。
    承诺做不到的事，会让模型一次发好几个 task，然后干等着。
    """
    task = fresh_task()
    description = task._tool_description()
    assert "能并行就并行" not in description, "说明书还在承诺并行"
    assert "依次执行" in description, "说明书没说明多个 task 是依次执行的"
    print("  ✓ 并行承诺已改为「依次执行」")


def test_unknown_type_returns_available_list():
    """⑥ 类型不存在时返回清单，而不是抛异常被吞成「运行工具时出错」"""
    task = fresh_task()
    result = run(task.act(description="测试", prompt="干点活", subagent_type="greeting-responder"))
    assert isinstance(result, str), "返回值应该是字符串（工具结果），不是异常"
    assert "不存在" in result, f"没有说明类型不存在：{result}"
    for prompt_type in SubagentManager().list_types():
        assert prompt_type in result, f"返回的可用清单里缺少 {prompt_type}"
    print("  ✓ 未知类型返回可用清单，不抛异常")


def test_empty_type_returns_available_list():
    """⑦ 没传类型时也要给出清单"""
    task = fresh_task()
    result = run(task.act(description="测试", prompt="干点活", subagent_type=""))
    assert "没有指定" in result, f"提示不对：{result}"
    assert "general-purpose" in result, "没给出可用类型"
    print("  ✓ 空类型提示里带了可用清单")


def test_empty_prompt_is_rejected():
    """⑧ 空的 prompt 要拦住（否则子 agent 收到一片空白，白跑一轮）"""
    task = fresh_task()
    result = run(task.act(description="测试", prompt="", subagent_type="general-purpose"))
    assert "prompt" in result and "为空" in result, f"提示不对：{result}"
    print("  ✓ 空 prompt 被拦住")


def test_new_type_shows_up_automatically():
    """⑨ 登记一个新类型，说明书和参数说明要自动跟着变"""
    reset_registry()  # 先确保只有 general-purpose
    manager = SubagentManager()
    manager.register_subagent_prompt(
        "code-reviewer-测试用",  # 用一个不可能撞上的名字
        "你是一个代码审查员。",
        description="写完重要代码后，用它来审查。",
        tools="file_read、file_grep",
    )
    try:
        task = fresh_task()
        description = task._tool_description()
        assert "code-reviewer-测试用" in description, "新类型没出现在说明书里"
        assert "写完重要代码后，用它来审查。" in description, "新类型的用途说明没出现"
        assert "file_read、file_grep" in description, "新类型的可用工具没出现"
        schema = task.json_schema()
        param_desc = schema["function"]["parameters"]["properties"]["subagent_type"]["description"]
        assert "code-reviewer-测试用" in param_desc, "参数说明里的候选值没跟着更新"
    finally:
        reset_registry()  # 不管成功失败都要还原，别污染其他用例
    print("  ✓ 新登记的类型会自动出现在说明书和参数说明里")


def test_schema_candidates_match_registry():
    """⑩ 参数说明里的候选值必须和登记表完全一致"""
    task = fresh_task()
    schema = task.json_schema()
    param_desc = schema["function"]["parameters"]["properties"]["subagent_type"]["description"]
    for prompt_type in SubagentManager().list_types():
        assert prompt_type in param_desc, f"参数说明里缺少 {prompt_type}"
    assert "greeting-responder" not in param_desc
    print("  ✓ 参数候选值与登记表一致")


def test_description_is_leaner():
    """⑪ 说明书变小了（原来那两段示例约 900 字符，纯粹是负担）"""
    task = fresh_task()
    description = task._tool_description()
    assert len(description) < 1400, f"说明书又变长了（{len(description)} 字符），检查是不是又抄了示例回来"
    assert "<example>" not in description, "说明书里还有 few-shot 示例块"
    print(f"  ✓ 说明书长度 {len(description)} 字符，无 few-shot 示例块")


def main():
    tests = [
        test_description_lists_every_registered_type,
        test_description_has_no_unregistered_type,
        test_description_has_no_literal_greeting_example,
        test_description_forbids_trivial_delegation,
        test_description_drops_fake_parallel_promise,
        test_unknown_type_returns_available_list,
        test_empty_type_returns_available_list,
        test_empty_prompt_is_rejected,
        test_new_type_shows_up_automatically,
        test_schema_candidates_match_registry,
        test_description_is_leaner,
    ]
    passed = 0
    failed = 0
    for test in tests:
        print(f"- {test.__name__}")
        try:
            test()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ 断言失败：{e}")
        except Exception:
            failed += 1
            print("  ✗ 用例异常：")
            traceback.print_exc()

    print(f"\n结果：{passed}/{len(tests)} 通过")
    if failed:
        print(f"未通过：{failed} 项")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
