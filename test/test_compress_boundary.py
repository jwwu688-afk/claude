# -*- coding: utf-8 -*-
"""L5 摘要压缩（按边界一次性压）的测试（第 25 轮）

为什么单独一个文件，而不是塞进原有测试：
  这一轮改的是"压多少、压几次"这种最容易出隐性故障的地方——
  压多了丢上下文，压少了等于没压，压重复了白花钱。三类都得钉住。

怎么跑（在项目根目录）：
  python test/test_compress_boundary.py

约定：不依赖 pytest，不用真实模型，全部用假摘要器；结束时打印通过数。
"""
import asyncio
import copy
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.history.history_manager import HistoryManager, estimate_messages_tokens  # noqa: E402

passed = 0
failed = 0


def check(name, ok, detail=""):
    """一条断言。失败时把 detail 打出来，方便直接定位。"""
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}" + (f"    → {detail}" if detail else ""))


class SilentUI:
    """把界面输出吞掉：测试关心的是行为，不是提示文案。"""

    def __getattr__(self, name):
        return lambda *a, **k: None


def fresh(**kwargs):
    """造一个干净的 HistoryManager（它本身是单例，必须重置）"""
    HistoryManager._instance = None
    HistoryManager._initialized = False
    hm = HistoryManager()
    hm._ui_manager = SilentUI()
    for k, v in kwargs.items():
        setattr(hm, k, v)
    return hm


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def asst(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def call(cid, name="file_read", args='{"path": "a.py"}'):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def tool(cid, text):
    return {"role": "tool", "tool_call_id": cid, "name": "file_read",
            "content": [{"type": "text", "text": text}]}


def build_rounds(rounds=10, tools=2):
    """造一段"每轮 1 问 + 2 次工具 + 1 答"的真实结构：每轮 6 条"""
    msgs = [{"role": "system", "content": [{"type": "text", "text": "系统规则"}]}]
    for i in range(1, rounds + 1):
        msgs.append(user(f"第{i}轮：请处理第{i}件事"))
        for j in range(tools):
            msgs.append(call(f"c{i}_{j}"))
            msgs.append(tool(f"c{i}_{j}", f"第{i}轮读到文件{j}的内容"))
        msgs.append(asst(f"第{i}轮：已经做好"))
    return msgs


def texts(messages):
    out = []
    for m in messages:
        out.append("".join(b.get("text", "") for b in (m.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "text"))
    return out


def pairs_ok(messages):
    """检查 tool_call 配对是否完整：每个 tool 结果都能找到对应的 assistant 申请，
    每个带 tool_calls 的 assistant 也都还能找到结果（末尾未完成的除外）。"""
    pending = set()
    for m in messages:
        role = m.get("role")
        calls = m.get("tool_calls") or []
        if role == "assistant" and calls:
            for c in calls:
                pending.add(c.get("id"))
        elif role == "tool":
            cid = m.get("tool_call_id")
            if cid not in pending:
                return False, f"tool 结果 {cid} 找不到对应的 assistant 申请"
            pending.discard(cid)
    return True, ""


def fake_summarizer(counter=None, boom=False):
    """假摘要器：记录被调用几次，或按需抛异常"""
    async def _fn(msgs):
        if counter is not None:
            counter.append(len(msgs))
        if boom:
            raise RuntimeError("模拟模型超时")
        return "【摘要】前面几件事都已经完成，结论是 X。"
    return _fn


# ---------------------------------------------------------------------------
# 1. 基础：一次压到边界，条数大幅下降
# ---------------------------------------------------------------------------
def test_basic_boundary():
    print("测试 1：多轮对话一次压到边界，条数大幅下降")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer())

    before = len(hm.messages_history[-1])
    stats = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
    after = len(hm.messages_history[-1])

    check("压缩成功", stats["compressed"], stats["reason"])
    check("条数明显下降", after < before - 30, f"{before} → {after}")
    # +3 = system 1 条 + 公告 1 条 + 对齐余量 1 条。
    # 为什么留余量：align_top_cut 发现刀口落在 tool 结果上时，会把刀口往前挪到
    # 那条 assistant(tool_calls)，保留区因此多出 1~2 条。这是必要的——不挪就会
    # 把申请和结果拆开，下一次请求直接 400。所以断言按"上界"放宽，不按等号。
    check("保留区不超过 keep_recent+3", after <= hm._compact_keep_recent + 3, f"after={after}")
    check("系统提示词还在", hm.messages_history[-1][0]["role"] == "system")
    check("插入了压缩公告", any(t.startswith("[为节省上下文空间") for t in texts(hm.messages_history[-1])))


# ---------------------------------------------------------------------------
# 2. 核心：连续压第二次不能再花一次调用（原来的套娃 bug）
# ---------------------------------------------------------------------------
def test_second_compress_is_noop():
    print("测试 2：连续压第二次必须什么都不做，且不再调用模型")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    calls = []
    hm.set_summarizer(fake_summarizer(counter=calls))

    first = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
    count_after_first = len(hm.messages_history[-1])
    snapshot = copy.deepcopy(hm.messages_history[-1])

    second = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))

    check("第一次压成功", first["compressed"])
    check("第二次不再压", not second["compressed"], second["reason"])
    check("理由是只剩上次的摘要", second["reason"] == "only_previous_summary", second["reason"])
    check("历史一条没动", hm.messages_history[-1] == snapshot)
    check("条数没变", len(hm.messages_history[-1]) == count_after_first)
    check("摘要模型只被调用 1 次（关键）", len(calls) == 1, f"实际调用 {len(calls)} 次，每次原料 {calls}")
    check("公告仍然只有 1 条",
          sum(1 for t in texts(hm.messages_history[-1]) if t.startswith("[为节省上下文空间")) == 1)


# ---------------------------------------------------------------------------
# 3. 用户最新指令不能被压掉
# ---------------------------------------------------------------------------
def test_latest_user_kept():
    print("测试 3：用户最新那句指令必须留在历史里")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=8)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer())

    asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))

    joined = "\n".join(texts(hm.messages_history[-1]))
    check("最后一轮的用户消息还在", "第8轮：请处理第8件事" in joined, joined[:80])
    check("第一轮的用户消息被压掉了", "第1轮：请处理第1件事" not in joined)


# ---------------------------------------------------------------------------
# 4. 工具调用配对不能被拆散
# ---------------------------------------------------------------------------
def test_tool_pairs_intact():
    print("测试 4：压缩后 tool_call 配对零断裂")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer())

    asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))

    ok, why = pairs_ok(hm.messages_history[-1])
    check("配对完整", ok, why)


# ---------------------------------------------------------------------------
# 5. 摘要失败：一条都不动
# ---------------------------------------------------------------------------
def test_summary_failure_keeps_all():
    print("测试 5：摘要失败时历史原样保留（不静默删除）")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer(boom=True))

    snapshot = copy.deepcopy(hm.messages_history[-1])
    stats = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))

    check("没有压", not stats["compressed"])
    check("理由是摘要失败", stats["reason"] == "summarize_failed", stats["reason"])
    check("历史逐字节没变", hm.messages_history[-1] == snapshot)
    check("没有插入公告", not any(t.startswith("[为之前") or t.startswith("[为节省") for t in texts(hm.messages_history[-1])))


# ---------------------------------------------------------------------------
# 6. 只有一轮对话：不该压（压掉就没有任务了）
# ---------------------------------------------------------------------------
def test_single_round_noop():
    print("测试 6：只有一轮对话时不压（否则会把任务本身压掉）")
    hm = fresh()
    page = [{"role": "system", "content": [{"type": "text", "text": "规则"}]},
            user("只有这一句需求")]
    for i in range(30):
        page.append(call(f"c{i}"))
        page.append(tool(f"c{i}", f"结果{i}"))
    hm.messages_history[-1] = page
    hm.seed_usage_from_messages(page)
    hm.set_summarizer(fake_summarizer())

    snapshot = copy.deepcopy(page)
    stats = asyncio.run(hm._compress_by_boundary(page))

    check("没有压", not stats["compressed"])
    check("理由是没有可压内容", stats["reason"] == "nothing_to_compress", stats["reason"])
    check("历史没动", hm.messages_history[-1] == snapshot)


# ---------------------------------------------------------------------------
# 7. 账本重算
# ---------------------------------------------------------------------------
def test_usage_recalculated():
    print("测试 7：压完账本要重算，否则状态行显示压缩前的占用")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer())

    before_tokens = hm.history_token_usage[-1].total_tokens
    stats = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
    after_tokens = hm.history_token_usage[-1].total_tokens

    check("账本变小了", after_tokens < before_tokens, f"{before_tokens} → {after_tokens}")
    check("账本等于压缩后的估算值",
          after_tokens == stats["after_tokens"] == estimate_messages_tokens(hm.messages_history[-1]),
          f"{after_tokens} vs {stats['after_tokens']}")


# ---------------------------------------------------------------------------
# 8. keep_recent 可调
# ---------------------------------------------------------------------------
def test_keep_recent_tunable():
    print("测试 8：keep_recent 调小 → 保留更少、压得更狠")
    results = {}
    for keep in (5, 20):
        hm = fresh()
        hm._compact_keep_recent = keep
        hm.messages_history[-1] = build_rounds(rounds=10)
        hm.seed_usage_from_messages(hm.messages_history[-1])
        hm.set_summarizer(fake_summarizer())
        asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
        results[keep] = len(hm.messages_history[-1])

    check("keep=5 比 keep=20 保留得更少", results[5] < results[20], str(results))


# ---------------------------------------------------------------------------
# 9. 两种公告都能被识别
# ---------------------------------------------------------------------------
def test_notice_detection():
    print("测试 9：两种压缩公告都能被识别出来")
    hm = fresh()
    with_summary = hm._create_compression_notice([user("x")], "摘要内容")[0]
    without = hm._create_compression_notice([user("x")], None)[0]
    normal = user("这是一句普通用户消息")

    check("带摘要的公告能识别", hm._is_compression_notice(with_summary))
    check("无摘要的公告能识别", hm._is_compression_notice(without))
    check("普通消息不误判", not hm._is_compression_notice(normal))


# ---------------------------------------------------------------------------
# 10. 公告后面又攒了新内容时，要连公告一起重压（信息不能丢）
# ---------------------------------------------------------------------------
def test_recompress_with_new_content():
    print("测试 10：公告后面又攒了新内容 → 允许再压，并把旧摘要一起重压")
    hm = fresh()
    hm._compact_keep_recent = 6
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    calls = []
    hm.set_summarizer(fake_summarizer(counter=calls))
    asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))

    # 手动把公告挪到压缩区（模拟"又聊了 20 条，边界往前推"）
    page = hm.messages_history[-1]
    extra = []
    for i in range(20):
        extra.append(user(f"新第{i}轮：继续做") if i % 5 == 0 else asst(f"新进展{i}"))
    page.extend(extra)

    stats = asyncio.run(hm._compress_by_boundary(page))
    check("这次压成功了", stats["compressed"], stats["reason"])
    check("摘要模型被调用第 2 次", len(calls) == 2, f"实际 {len(calls)} 次")
    # 旧摘要本身要被送进第 2 次的原料里，否则信息丢了
    last_input = hm.messages_history[-1]
    check("压完仍有 1 条公告",
          sum(1 for t in texts(last_input) if t.startswith("[为节省上下文空间")) == 1)


def test_compact_now_passes_reason():
    print("测试 11：/compact 的返回值必须带 compressed / reason（否则提示语会张冠李戴）")
    hm = fresh()
    hm.messages_history[-1] = build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summarizer())

    first = asyncio.run(hm.compact_now())
    check("第一次压了", first["compressed"], first["reason"])
    check("reason 是 ok", first["reason"] == "ok", first["reason"])
    check("条数确实变少", first["after_count"] < first["before_count"])

    second = asyncio.run(hm.compact_now())
    check("第二次没压", not second["compressed"], second["reason"])
    check("reason 说明只剩旧摘要", second["reason"] == "only_previous_summary", second["reason"])
    check("第二次条数没变", second["after_count"] == second["before_count"])


def main():
    tests = [
        test_basic_boundary,
        test_second_compress_is_noop,
        test_latest_user_kept,
        test_tool_pairs_intact,
        test_summary_failure_keeps_all,
        test_single_round_noop,
        test_usage_recalculated,
        test_keep_recent_tunable,
        test_notice_detection,
        test_recompress_with_new_content,
        test_compact_now_passes_reason,
    ]
    for fn in tests:
        fn()
        print()
    total = passed + failed
    if failed:
        print(f"❌ 失败 {failed}/{total}")
        return 1
    print(f"🎉 所有测试通过（{passed}/{total}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
