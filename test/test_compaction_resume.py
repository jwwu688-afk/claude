# -*- coding: utf-8 -*-
"""压缩记录落盘 + resume 重建的测试（第 26 轮）

为什么单独一份：
  这次修的是"每次 resume 都要重压一次"——它不报错、不崩，只是每次悄悄花一次
  摘要调用、还把上下文从 10 条打回 796 条。这种"贵但不叫"的故障只能靠断言钉住。

跑法（项目根目录）：
  python test/test_compaction_resume.py
"""
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.history.history_manager import HistoryManager, estimate_messages_tokens  # noqa: E402
from core.session import (  # noqa: E402
    SessionStore,
    inspect_session,
    load_messages_for_resume,
    _read_session,
)

passed = 0
failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}" + (f"    → {detail}" if detail else ""))


class SilentUI:
    def __getattr__(self, name):
        return lambda *a, **k: None


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def asst(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def call(cid):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}}]}


def tool(cid, text):
    return {"role": "tool", "tool_call_id": cid, "name": "file_read",
            "content": [{"type": "text", "text": text}]}


def build_rounds(rounds=10, tools=2):
    """每轮 1 问 + N 次工具 + 1 答"""
    msgs = []
    for i in range(1, rounds + 1):
        msgs.append(user(f"第{i}轮：请处理第{i}件事"))
        for j in range(tools):
            msgs.append(call(f"c{i}_{j}"))
            msgs.append(tool(f"c{i}_{j}", f"第{i}轮读到文件{j}的内容"))
        msgs.append(asst(f"第{i}轮：已经做好"))
    return msgs


def make_store(directory, messages, with_header=True):
    """在临时目录里造一个会话文件（不走 SESSIONS_DIR，避免污染真实存档）"""
    path = pathlib.Path(directory) / "s.jsonl"
    store = SessionStore(path)
    if with_header:
        store._write_line({"type": "header", "v": 1, "id": "test", "cwd": str(directory)})
    for m in messages:
        store.append_message(m)
    return store, path


def fresh_hm():
    HistoryManager._instance = None
    HistoryManager._initialized = False
    hm = HistoryManager()
    hm._ui_manager = SilentUI()
    hm._model_max_tokens = 100000
    return hm


def fake_summary(text="【摘要】前面几件事已经完成。"):
    async def _fn(msgs):
        return text
    return _fn


# ---------------------------------------------------------------------------
# 1. 文件里能写能读
# ---------------------------------------------------------------------------
def test_append_and_read():
    print("测试 1：compaction 行能写进去、也能读回来")
    with tempfile.TemporaryDirectory() as d:
        store, path = make_store(d, build_rounds(rounds=3))
        store.append_compaction(keep_from=5, summary="摘要正文", before=18, after=6)

        messages, header, compaction = _read_session(path)
        check("消息读到了", len(messages) == 18, str(len(messages)))
        check("header 还在", header.get("id") == "test")
        check("压缩记录读到了", compaction is not None)
        check("keep_from 正确", compaction.get("keep_from") == 5, str(compaction))
        check("摘要正确", compaction.get("summary") == "摘要正文")
        check("条数统计正确", store.count_messages() == 18, str(store.count_messages()))


# ---------------------------------------------------------------------------
# 2. 没有记录时行为不变（老文件兼容）
# ---------------------------------------------------------------------------
def test_no_record_keeps_old_behavior():
    print("测试 2：老会话文件（没有压缩记录）行为完全不变")
    with tempfile.TemporaryDirectory() as d:
        messages = [{"role": "system", "content": [{"type": "text", "text": "旧规则"}]}] + build_rounds(rounds=2)
        _, path = make_store(d, messages)

        restored = load_messages_for_resume(path)
        check("条数 = 原消息去掉 system", len(restored) == len(messages) - 1, str(len(restored)))
        check("旧 system 被丢掉", all(m.get("role") != "system" for m in restored))
        check("内容原样", restored[0]["content"][0]["text"].startswith("第1轮"))


# ---------------------------------------------------------------------------
# 3. 有记录时按记录重建
# ---------------------------------------------------------------------------
def test_resume_applies_record():
    print("测试 3：有压缩记录时，恢复出的是「公告 + 保留区」")
    with tempfile.TemporaryDirectory() as d:
        messages = build_rounds(rounds=10)          # 每轮 6 条，共 60 条
        store, path = make_store(d, messages)
        # 模拟"压缩到第 30 条之前都被压掉，保留最后 10 条"
        store.append_compaction(keep_from=30, summary="前面 15 轮做完了 A、B、C。")

        restored = load_messages_for_resume(path)
        check("第一条是压缩公告", restored[0]["content"][0]["text"].startswith("[为节省上下文空间"),
              restored[0]["content"][0]["text"][:40])
        check("公告里带着摘要", "做完了 A、B、C" in restored[0]["content"][0]["text"])
        check("保留区 = 原消息数 - 30，再加一条公告",
              len(restored) == len(messages) - 30 + 1, str(len(restored)))
        check("保留区从第 30 条开始",
              restored[1]["content"][0]["text"] == messages[30]["content"][0]["text"],
              restored[1]["content"][0]["text"][:30])
        check("没有 system 混进来", all(m.get("role") != "system" for m in restored))


# ---------------------------------------------------------------------------
# 4. 压缩之后又聊了几句 —— 那几句必须还在
# ---------------------------------------------------------------------------
def test_messages_after_compaction_survive():
    print("测试 4：压缩后新聊的消息，resume 时必须保留")
    with tempfile.TemporaryDirectory() as d:
        messages = build_rounds(rounds=10)
        store, path = make_store(d, messages)
        store.append_compaction(keep_from=30, summary="前面的做完了。")
        # 压缩之后又追加 4 条（这 4 条是文件里 40~43）
        for m in [user("新问题"), asst("新回答"), user("再问一个"), asst("再答一个")]:
            store.append_message(m)

        restored = load_messages_for_resume(path)
        texts = ["".join(b.get("text", "") for b in (m.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text") for m in restored]
        check("公告在", texts[0].startswith("[为节省上下文空间"))
        check("保留区 + 新增 4 条 + 公告",
              len(restored) == len(messages) - 30 + 4 + 1, str(len(restored)))
        check("新聊的那句还在", "再问一个" in texts, str(texts[-4:]))


# ---------------------------------------------------------------------------
# 5. 坏记录要能安全退化
# ---------------------------------------------------------------------------
def test_bad_record_falls_back():
    print("测试 5：记录坏了（缺字段/负数/非数字）就退回完整历史，不能丢对话")
    cases = [
        ("缺 keep_from", {"type": "compaction", "summary": "x"}),
        ("keep_from 是负数", {"type": "compaction", "keep_from": -5, "summary": "x"}),
        ("keep_from 不是数字", {"type": "compaction", "keep_from": "abc", "summary": "x"}),
    ]
    for label, record in cases:
        with tempfile.TemporaryDirectory() as d:
            messages = build_rounds(rounds=2)
            store, path = make_store(d, messages)
            store._write_line(record)
            restored = load_messages_for_resume(path)
            check(f"{label} → 退回完整历史", len(restored) == len(messages),
                  f"{len(restored)} vs {len(messages)}")


# ---------------------------------------------------------------------------
# 6. 多次压缩只认最后一条
# ---------------------------------------------------------------------------
def test_last_record_wins():
    print("测试 6：多次压缩时，只有最后一条记录生效")
    with tempfile.TemporaryDirectory() as d:
        messages = build_rounds(rounds=10)
        store, path = make_store(d, messages)
        store.append_compaction(keep_from=10, summary="第一次的摘要")
        store.append_compaction(keep_from=30, summary="第二次的摘要")

        restored = load_messages_for_resume(path)
        text = restored[0]["content"][0]["text"]
        check("用的是第二次的摘要", "第二次的摘要" in text and "第一次的摘要" not in text, text[:60])
        check("保留区按 30 算", len(restored) == len(messages) - 30 + 1, str(len(restored)))


# ---------------------------------------------------------------------------
# 7. 端到端：压缩 → resume → 不再超标（本次修复的核心）
# ---------------------------------------------------------------------------
def test_roundtrip_no_recompress():
    print("测试 7：压缩落盘后 resume，账本必须回到低位、不再触发压缩")
    with tempfile.TemporaryDirectory() as d:
        rounds = build_rounds(rounds=12)                 # 60 条
        store, path = make_store(d, rounds)

        hm = fresh_hm()
        system = {"role": "system", "content": [{"type": "text", "text": "规则"}]}
        hm.messages_history[-1] = [system] + rounds
        hm.set_summarizer(fake_summary())

        # 模拟 Conversation._write_compaction 的坐标换算
        def write_compaction(keep_count, summary, stats):
            total = store.count_messages()
            store.append_compaction(max(0, total - keep_count), summary,
                                    stats["before_count"], stats["after_count"])

        hm.set_compaction_writer(write_compaction)

        before = estimate_messages_tokens(hm.messages_history[-1])
        stats = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
        check("压缩成功", stats["compressed"], stats["reason"])

        # 文件里必须有记录
        _, _, record = _read_session(path)
        check("文件里写进了压缩记录", record is not None)
        check("keep_from 等于文件条数减保留条数",
              record["keep_from"] == len(rounds) - stats["kept_count"],
              f"{record['keep_from']} vs {len(rounds)}-{stats['kept_count']}")

        # 现在模拟 resume
        restored = load_messages_for_resume(path)
        rebuilt = [system] + restored
        after = estimate_messages_tokens(rebuilt)

        check("恢复后条数远少于原文", len(rebuilt) < 15, str(len(rebuilt)))
        check("恢复后 token 大幅下降", after < before / 2, f"{before} → {after}")

        # 关键：恢复后不该再触发 L5
        hm2 = fresh_hm()
        hm2.messages_history[-1] = rebuilt
        hm2.seed_usage_from_messages(rebuilt)
        ratio = hm2._current_usage_ratio()
        check("恢复后占用远低于 0.95（不会再压）", ratio < 0.95, f"{ratio:.1%}")
        check("连 0.70 总闸都没到", ratio < 0.70, f"{ratio:.1%}")


# ---------------------------------------------------------------------------
# 8. 没有 writer 时压缩照常工作（不能因为缺回调就压不动）
# ---------------------------------------------------------------------------
def test_compress_without_writer():
    print("测试 8：没注入落盘回调时，压缩仍然正常（只是不落盘）")
    hm = fresh_hm()
    hm.messages_history[-1] = [{"role": "system", "content": [{"type": "text", "text": "规则"}]}] + build_rounds(rounds=10)
    hm.seed_usage_from_messages(hm.messages_history[-1])
    hm.set_summarizer(fake_summary())
    # 故意不注入 writer

    stats = asyncio.run(hm._compress_by_boundary(hm.messages_history[-1]))
    check("压缩成功", stats["compressed"], stats["reason"])
    check("条数下降", stats["after_count"] < stats["before_count"])




# ---------------------------------------------------------------------------
# 9. 列表展示：条数用压缩后的，预览用原始的
# ---------------------------------------------------------------------------
def test_inspect_session():
    print("测试 9：/resume 列表要显示压缩后的条数，预览仍取原始消息")
    with tempfile.TemporaryDirectory() as d:
        messages = build_rounds(rounds=10)
        store, path = make_store(d, messages)
        store.append_compaction(keep_from=30, summary="前面的做完了。")

        info = inspect_session(path)
        check("raw 保留原始条数", len(info["raw"]) == len(messages), str(len(info["raw"])))
        check("effective 是压缩后的",
              len(info["effective"]) == len(messages) - 30 + 1, str(len(info["effective"])))
        check("标了已压缩", info["compacted"] is True)
        # 预览必须来自 raw：effective 第一条是公告，拿它做预览会显示成
        # 「[为节省上下文空间…」，用户完全看不出这个会话是干嘛的。
        check("raw 第一条是用户原话",
              info["raw"][0]["content"][0]["text"].startswith("第1轮"))
        check("effective 第一条是公告",
              info["effective"][0]["content"][0]["text"].startswith("[为节省上下文空间"))

    # 没有压缩记录：条数只比 raw 少 system，且不能标成"已压缩"
    with tempfile.TemporaryDirectory() as d:
        messages = [{"role": "system", "content": [{"type": "text", "text": "规则"}]}] + build_rounds(rounds=2)
        _, path = make_store(d, messages)
        info = inspect_session(path)
        check("没有记录时不算已压缩", info["compacted"] is False)
        check("没有记录时只是去掉 system",
              len(info["effective"]) == len(messages) - 1, str(len(info["effective"])))


def main():
    tests = [
        test_append_and_read,
        test_no_record_keeps_old_behavior,
        test_resume_applies_record,
        test_messages_after_compaction_survive,
        test_bad_record_falls_back,
        test_last_record_wins,
        test_roundtrip_no_recompress,
        test_compress_without_writer,
        test_inspect_session,
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
