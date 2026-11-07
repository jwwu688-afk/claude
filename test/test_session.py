# =============================================================================
# 文件：test/test_session.py
# 作用：验证会话持久化——写进去、读回来、按项目找最近一次、坏行不崩。
#
# 跑法：python test/test_session.py
#
# 这套东西的核心承诺只有一条：**进程被杀，对话还在**。
# 所以测试重点不是"格式好看"，而是：
#   · 每条消息真的落了盘（不是攒在内存里）
#   · 复杂结构（tool_calls、内容块数组）能原样还原
#   · 文件被截断/写坏时，能救回前面的部分而不是整体报废
# =============================================================================

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import session as session_mod  # noqa: E402
from core.session import SessionStore, load_messages_for_resume  # noqa: E402

_results = {"pass": 0, "fail": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _results["pass"] += 1
        print(f"  ✓ {name}")
    else:
        _results["fail"] += 1
        print(f"  ✗ {name}  {detail}")


def use_tmp_sessions(tmp: Path) -> Path:
    """把会话目录指到临时位置，返回旧值"""
    original = session_mod.SESSIONS_DIR
    session_mod.SESSIONS_DIR = tmp / "sessions"
    return original


def test_create_and_read(tmp: Path) -> None:
    print("测试: 新建会话并读回")
    original = use_tmp_sessions(tmp)
    try:
        store = SessionStore.create(cwd=str(tmp), model="spark-x2.6")
        check("文件已创建", store.path.is_file(), str(store.path))
        check("文件名是 jsonl", store.path.suffix == ".jsonl")

        store.append_message({"role": "user", "content": [{"type": "text", "text": "你好"}]})
        store.append_message({"role": "assistant", "content": [{"type": "text", "text": "在的"}]})

        messages, header = SessionStore.load(store.path)
        check("读回两条消息", len(messages) == 2, str(len(messages)))
        check("第一条是用户", messages[0]["role"] == "user")
        check("内容完整", messages[0]["content"][0]["text"] == "你好")
        check("header 有 cwd", header.get("cwd") == str(tmp), str(header.get("cwd")))
        check("header 有版本", header.get("v") == 1)
        check("header 有模型", header.get("model") == "spark-x2.6")
    finally:
        session_mod.SESSIONS_DIR = original


def test_complex_message(tmp: Path) -> None:
    print("测试: 复杂结构保真（工具调用）")
    original = use_tmp_sessions(tmp)
    try:
        store = SessionStore.create(cwd=str(tmp))
        complex_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "我看看"}],
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "file_read", "arguments": '{"path": "a.py"}'},
            }],
        }
        store.append_message(complex_msg)
        messages, _ = SessionStore.load(store.path)
        restored = messages[0]
        check("tool_calls 还在", "tool_calls" in restored)
        check("工具名正确", restored["tool_calls"][0]["function"]["name"] == "file_read")
        check("参数没被转义坏", "a.py" in restored["tool_calls"][0]["function"]["arguments"])

        tool_msg = {"role": "tool", "tool_call_id": "call_1", "name": "file_read",
                    "content": [{"type": "text", "text": "print(1)"}]}
        store.append_message(tool_msg)
        messages, _ = SessionStore.load(store.path)
        check("tool 消息的 tool_call_id 保住了", messages[1]["tool_call_id"] == "call_1")
    finally:
        session_mod.SESSIONS_DIR = original


def test_append_is_immediate(tmp: Path) -> None:
    print("测试: 写完立刻落盘（模拟崩溃）")
    original = use_tmp_sessions(tmp)
    try:
        store = SessionStore.create(cwd=str(tmp))
        store.append_message({"role": "user", "content": [{"type": "text", "text": "第一句"}]})
        store.append_message({"role": "assistant", "content": [{"type": "text", "text": "第二句"}]})

        # 不调用任何 close/flush，直接去读文件——模拟"进程被强杀"
        raw = store.path.read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip()]
        check("header + 2 条消息都在盘上", len(lines) == 3, str(len(lines)))
        check("是合法 JSONL", all(json.loads(l) for l in lines))
    finally:
        session_mod.SESSIONS_DIR = original


def test_broken_line(tmp: Path) -> None:
    print("测试: 坏行被跳过，其余照常读")
    original = use_tmp_sessions(tmp)
    try:
        store = SessionStore.create(cwd=str(tmp))
        store.append_message({"role": "user", "content": [{"type": "text", "text": "好"}]})

        # 手动追加一行半截 JSON（模拟断电）
        with store.path.open("a", encoding="utf-8") as f:
            f.write('{"type": "message", "message": {"role": "assis\n')
        store.append_message({"role": "assistant", "content": [{"type": "text", "text": "在"}]})

        messages, _ = SessionStore.load(store.path)
        check("坏行被跳过，好行都读回来", len(messages) == 2, str(len(messages)))
        check("第一条对", messages[0]["content"][0]["text"] == "好")
        check("第三条对", messages[1]["content"][0]["text"] == "在")
    finally:
        session_mod.SESSIONS_DIR = original


def test_latest_by_cwd(tmp: Path) -> None:
    print("测试: 按项目找最近一次会话")
    original = use_tmp_sessions(tmp)
    try:
        proj_a = tmp / "projA"
        proj_b = tmp / "projB"
        proj_a.mkdir()
        proj_b.mkdir()

        a1 = SessionStore.create(cwd=str(proj_a))
        a1.append_message({"role": "user", "content": [{"type": "text", "text": "A 的会话"}]})
        b1 = SessionStore.create(cwd=str(proj_b))

        found_a = SessionStore.latest(str(proj_a))
        check("找到 A 的会话", found_a == a1.path, str(found_a))
        found_b = SessionStore.latest(str(proj_b))
        check("找到 B 的会话", found_b == b1.path, str(found_b))
        check("列出的都是会话文件", all(p.suffix == ".jsonl" for p in SessionStore.list_sessions()))
        check("未知目录返回 None", SessionStore.latest(str(tmp / "nope")) is None)
    finally:
        session_mod.SESSIONS_DIR = original


def test_resume_strips_system(tmp: Path) -> None:
    print("测试: 恢复时去掉旧 system")
    original = use_tmp_sessions(tmp)
    try:
        store = SessionStore.create(cwd=str(tmp))
        store.append_message({"role": "system", "content": [{"type": "text", "text": "旧提示词"}]})
        store.append_message({"role": "user", "content": [{"type": "text", "text": "问"}]})
        store.append_message({"role": "assistant", "content": [{"type": "text", "text": "答"}]})

        restored = load_messages_for_resume(store.path)
        check("system 被剔除", all(m["role"] != "system" for m in restored))
        check("对话保留", len(restored) == 2, str(len(restored)))
        check("顺序没乱", restored[0]["role"] == "user" and restored[1]["role"] == "assistant")

        kept = load_messages_for_resume(store.path, keep_system=True)
        check("keep_system=True 时保留 system", len(kept) == 3)
    finally:
        session_mod.SESSIONS_DIR = original


def test_empty_and_missing(tmp: Path) -> None:
    print("测试: 空文件与不存在的文件")
    original = use_tmp_sessions(tmp)
    try:
        empty = tmp / "sessions" / "empty.jsonl"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text("", encoding="utf-8")
        messages, header = SessionStore.load(empty)
        check("空文件返回空列表", messages == [] and header == {})

        messages, header = SessionStore.load(tmp / "nope.jsonl")
        check("不存在的文件不抛异常", messages == [])

        # 注意：前面几个用例已经在同一个临时 sessions 目录里建过会话（cwd 就是 tmp），
        # 所以这里要用一个从没被记录过的目录，才能真的测到"找不到"的情况。
        check("没记录过的目录 latest 返回 None", SessionStore.latest(str(tmp / "never_used")) is None)
    finally:
        session_mod.SESSIONS_DIR = original


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="thinker_session_") as raw:
        tmp = Path(raw)
        test_create_and_read(tmp)
        test_complex_message(tmp)
        test_append_is_immediate(tmp)
        test_broken_line(tmp)
        test_latest_by_cwd(tmp)
        test_resume_strips_system(tmp)
        test_empty_and_missing(tmp)

    total = _results["pass"] + _results["fail"]
    print()
    if _results["fail"] == 0:
        print(f"🎉 所有测试通过（{_results['pass']}/{total}）")
    else:
        print(f"❌ {_results['fail']} 项失败（通过 {_results['pass']}/{total}）")
        sys.exit(1)


if __name__ == "__main__":
    main()
