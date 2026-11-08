#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_parallel_tools.py
# 作用：验证"连续的只读工具并发执行"（2026-11-08 第 15 轮）
#
# 【这一轮真正难的地方不是"把 await 换成 gather"】
#   而是三件容易被忽略的事，这个脚本逐条盯着：
#     1. **真的变快了**。如果工具内部是同步阻塞（没有 await 点），
#        gather 会退化成串行——结果全对，只是没快。这种"没生效的优化"最难发现。
#     2. **历史顺序必须按申请顺序**。并发完成顺序是不确定的；
#        真按完成顺序写历史，同一个会话每次重放顺序都不同，排查时没法对照。
#     3. **一条失败不能拖垮整批**。第 14.2 轮起 run_tool 会抛异常，
#        不兜住的话"一个文件读不到，其余四个也不读了"，比串行还糟。
#
# 不联网、不花钱：用带 sleep 的假工具模拟慢 IO。
# =============================================================================

import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import core.conversation as conversation_module  # noqa: E402
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


def make_call(call_id, name, arguments):
    """造一个和模型返回同形状的工具申请（不用真 SDK 对象，这里只用到三个字段）"""
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


class FakeTM:
    """假的工具管理器：按 path 决定睡多久、要不要抛错

    为什么用 sleep 而不是真读文件：这一轮要验证的是"调度"，
    真读小文件只要几十微秒，根本测不出并发的差别。
    """

    def __init__(self, delays=None, boom=()):
        self.delays = delays or {}
        self.boom = set(boom)
        self.calls = []      # 开始执行的顺序
        self.completed = []  # 执行完成的顺序（并发时和开始顺序可能不同）

    async def run_tool(self, name, **kwargs):
        target = kwargs.get("path", "")
        self.calls.append(target)
        await asyncio.sleep(self.delays.get(target, 0))
        if target in self.boom:
            raise RuntimeError("磁盘满了")
        self.completed.append(target)
        return f"内容:{target}"


class FakeUI:
    """假界面：只记录调用，不画东西"""

    def __init__(self):
        self.headers = []
        self.results = []
        self.errors = []
        self.approvals = 0

    def show_preparing_tool(self, name, args):
        self.headers.append(name)

    def show_tool_execution(self, name, args, success=True, result=""):
        self.results.append((name, success, result))

    def print_error(self, text, emoji="❌"):
        self.errors.append(text)

    def print_warning(self, text):
        self.errors.append(text)

    def show_tool_denied(self, reason=""):
        self.errors.append(f"denied:{reason}")

    async def wait_for_user_approval(self, default_yes=True):
        self.approvals += 1
        return True, ""


def fresh_history_manager():
    HistoryManager._instance = None
    HistoryManager._initialized = False
    return HistoryManager()


def build_conversation(ui, tm):
    conv = conversation_module.Conversation.__new__(conversation_module.Conversation)
    conv._ui_manager = ui
    conv._tool_manager = tm
    conv._history_manager = fresh_history_manager()
    conv._task_depth = 0
    conv._session = None
    return conv


def tool_entries(conv):
    """取出历史里最后几条 tool 消息的 tool_call_id，用来核对顺序"""
    return [
        m.get("tool_call_id") for m in conv._history_manager.messages_history[-1]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]


def test_parallel_is_faster():
    print("测试: 真的并发了吗（不是只有 await gather）")
    ui = FakeUI()
    # 两个各睡 0.3 秒：串行 ≥ 0.6，并发 ≈ 0.3
    tm = FakeTM(delays={"a.py": 0.3, "b.py": 0.3})
    conv = build_conversation(ui, tm)

    calls = [
        make_call("c1", "file_read", '{"path": "a.py"}'),
        make_call("c2", "file_read", '{"path": "b.py"}'),
    ]
    started = time.perf_counter()
    asyncio.run(conv._handle_tool_calls(calls))
    elapsed = time.perf_counter() - started

    check("两个工具都执行了", len(tm.calls) == 2, str(tm.calls))
    check("总耗时接近单个（并发生效）", elapsed < 0.55, f"实测 {elapsed:.2f}s，串行应 ≥0.6s")


def test_history_order_is_request_order():
    print("测试: 历史顺序 = 申请顺序（与完成顺序无关）")
    # 第一个慢、第二个快：完成顺序会反过来，历史必须仍按申请顺序
    ui = FakeUI()
    tm = FakeTM(delays={"slow.py": 0.25, "fast.py": 0.01})
    conv = build_conversation(ui, tm)

    calls = [
        make_call("c_slow", "file_read", '{"path": "slow.py"}'),
        make_call("c_fast", "file_read", '{"path": "fast.py"}'),
    ]
    asyncio.run(conv._handle_tool_calls(calls))

    check("完成顺序确实是反的", tm.completed[0] == "fast.py", str(tm.completed))
    check("历史仍按申请顺序", tool_entries(conv) == ["c_slow", "c_fast"], str(tool_entries(conv)))
    check("两条都写进历史", len(tool_entries(conv)) == 2)
    check("界面头部按申请顺序", ui.headers == ["file_read", "file_read"])


def test_one_failure_does_not_kill_batch():
    print("测试: 一条失败不影响同批其他")
    ui = FakeUI()
    tm = FakeTM(boom={"boom.py"})  # boom.py 抛错，good.py 正常
    conv = build_conversation(ui, tm)

    calls = [
        make_call("c_bad", "file_read", '{"path": "boom.py"}'),
        make_call("c_ok", "file_read", '{"path": "good.py"}'),
    ]
    asyncio.run(conv._handle_tool_calls(calls))

    entries = conv._history_manager.messages_history[-1]
    tool_msgs = [m for m in entries if isinstance(m, dict) and m.get("role") == "tool"]
    check("两条结果都写进历史", len(tool_msgs) == 2, str(len(tool_msgs)))

    bad = next(m for m in tool_msgs if m.get("tool_call_id") == "c_bad")
    good = next(m for m in tool_msgs if m.get("tool_call_id") == "c_ok")
    bad_text = str(bad["content"])
    good_text = str(good["content"])
    check("失败那条写明原因", "工具调用失败" in bad_text and "磁盘满了" in bad_text)
    check("成功那条不受影响", "内容:good.py" in good_text)
    check("界面标了失败", any(not ok for _, ok, _ in ui.results), str(ui.results))


def test_batch_split():
    print("测试: 批次划分（只并连续的只读调用）")
    conv = build_conversation(FakeUI(), FakeTM())

    calls = [
        make_call("1", "file_read", '{"path": "a"}'),
        make_call("2", "file_grep", '{"pattern": "x"}'),
        make_call("3", "file_write", '{"path": "b", "content": "x"}'),  # 写：断在这里
        make_call("4", "file_read", '{"path": "c"}'),
    ]
    # _collect_parallel_batch 传进来的已经是解析好的 dict（和真实路径一致），
    # 不能塞 JSON 字符串——classify() 会调 args.get(...)，字符串会直接炸
    parsed = [{"path": "a"}, {"pattern": "x"}, {}, {"path": "c"}]
    batch, next_index = conv._collect_parallel_batch(calls, parsed, 0)
    check("只收集到前两条只读", [c.id for c, _ in batch] == ["1", "2"], str([c.id for c, _ in batch]))
    check("停在写操作上", next_index == 2, str(next_index))

    # file_write 需要审批 → 收集立刻停下，返回空批（不是"长度为 1 的批"）：
    # 这样调用方会走单条路径，把审批流程走完整。
    batch2, next2 = conv._collect_parallel_batch(calls, parsed, 2)
    check("写操作不并入批次（返回空批）", len(batch2) == 0 and next2 == 2, f"{len(batch2)}, {next2}")


def test_approval_tool_not_batched():
    print("测试: 需要审批的工具不进批次")
    ui = FakeUI()
    tm = FakeTM()
    conv = build_conversation(ui, tm)

    # file_write 在权限表里属于"要问"，所以它自己走单条路径
    calls = [make_call("w1", "file_write", '{"path": "b.py", "content": "x"}')]
    asyncio.run(conv._handle_tool_calls(calls))
    check("确实停下来问了用户", ui.approvals == 1, str(ui.approvals))


def test_bad_json_not_batched():
    print("测试: 参数非法 JSON 时不进批次")
    ui = FakeUI()
    tm = FakeTM()
    conv = build_conversation(ui, tm)

    calls = [
        make_call("bad", "file_read", "{not json"),
        make_call("ok", "file_read", '{"path": "a.py"}'),
    ]
    asyncio.run(conv._handle_tool_calls(calls))

    entries = conv._history_manager.messages_history[-1]
    tool_msgs = [m for m in entries if isinstance(m, dict) and m.get("role") == "tool"]
    check("坏参数也回了一条失败结果", any("不是合法的 JSON" in str(m["content"]) for m in tool_msgs))
    check("好参数照常执行", "内容:a.py" in "".join(str(m["content"]) for m in tool_msgs))
    check("打印了解析错误", any("parsing failed" in e for e in ui.errors))


if __name__ == "__main__":
    print("🧪 只读工具并发\n")
    # get_reminder 会去构造 ToolManager 单例（连 MCP 服务器），测试里换成空串
    conversation_module.get_reminder = lambda: ""

    test_parallel_is_faster()
    print()
    test_history_order_is_request_order()
    print()
    test_one_failure_does_not_kill_batch()
    print()
    test_batch_split()
    print()
    test_approval_tool_not_batched()
    print()
    test_bad_json_not_batched()
    print()

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
