#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_tool_call_persist.py
# 作用：验证"带工具调用的消息能落盘"（2026-11-08 第 14.2 轮）
#
# 【要复现的那个 bug】
#   assistant 消息里的 tool_calls 来自 openai SDK，是 pydantic 对象。
#   add_message 会把它立刻写进 JSONL，而 json.dumps 不认识 pydantic 对象：
#     TypeError: Object of type ChatCompletionMessageFunctionToolCall is not JSON serializable
#   结果是**这个会话第一次真正用上工具就中断**，而且断在"模型已经答完、
#   正要去执行工具"的那一刻——用户看到半截回答加一段红色堆栈。
#
# 【这个脚本验四件事】
#   1. 对象能被转成字典，字段（id / type / function.name / function.arguments）都在
#   2. 转换不修改调用方手里的原字典（不原地改）
#   3. 端到端：add_message → JSONL → 读回来，tool_calls 仍是可用的字典
#   4. 兜底：真遇到不可序列化的东西，不崩、有告警、不写坏文件
#
# 不联网、不花钱：用的都是本地对象和临时目录。
# =============================================================================

import asyncio  # 不需要，但保留给将来可能的异步用例
import contextlib  # 捕获 stderr
import io  # 捕获 stderr 用
import json  # 验证落盘的每一行是否合法
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 构造 Conversation 会顺带建 APIClient，它要求三个环境变量齐全。
# setdefault 不覆盖真实配置，且本测试不发任何请求。
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

import core.session as session_module  # noqa: E402
from core.conversation import Conversation, normalize_message  # noqa: E402
from core.history.history_manager import HistoryManager  # noqa: E402
from core.session import SessionStore  # noqa: E402
from openai.types.chat import ChatCompletionMessageFunctionToolCall  # noqa: E402
from openai.types.chat.chat_completion_message_function_tool_call import Function  # noqa: E402

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


def make_sdk_call(name="file_read", arguments='{"path": "a.py"}', call_id="call_abc"):
    """造一个和真实接口返回一模一样的 tool_call 对象

    为什么不用字典伪装：这个 bug 的根因正是"对象 vs 字典"的区别，
    用字典测等于没测。这里直接构造 SDK 的 pydantic 类型。
    """
    return ChatCompletionMessageFunctionToolCall(
        id=call_id,
        type="function",
        function=Function(name=name, arguments=arguments),
    )


def test_normalize_shape():
    print("测试: 对象 → 字典")
    call = make_sdk_call()
    message = {"role": "assistant", "content": None, "tool_calls": [call]}

    result = normalize_message(message)

    check("返回的是新字典", result is not message)
    check("role 保留", result["role"] == "assistant")
    check("content 保留为 None", result["content"] is None)

    calls = result["tool_calls"]
    check("tool_calls 变成列表", isinstance(calls, list) and len(calls) == 1)
    check("元素是字典", isinstance(calls[0], dict))
    check("id 正确", calls[0].get("id") == "call_abc")
    check("type 正确", calls[0].get("type") == "function")
    check("工具名正确", calls[0]["function"]["name"] == "file_read")
    check("参数字符串正确", calls[0]["function"]["arguments"] == '{"path": "a.py"}')

    # 最关键的断言：原来的报错就是这一行抛的
    try:
        json.dumps(result, ensure_ascii=False)
        check("能被 json.dumps 序列化", True)
    except TypeError as e:
        check("能被 json.dumps 序列化", False, str(e))


def test_no_mutation():
    print("测试: 不原地修改调用方的字典")
    call = make_sdk_call()
    message = {"role": "assistant", "content": None, "tool_calls": [call]}

    normalize_message(message)

    check("调用方手里的仍是对原对象", message["tool_calls"][0] is call,
          "如果这里变了，说明函数偷偷改了入参")


def test_dict_passthrough():
    print("测试: 已经是字典就原样返回")
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "type": "function", "function": {"name": "n", "arguments": "{}"}}],
    }
    check("返回同一个对象（不复制）", normalize_message(message) is message)


def test_no_tool_calls():
    print("测试: 没有 tool_calls 的消息不受影响")
    message = {"role": "user", "content": [{"type": "text", "text": "你好"}]}
    check("原样返回", normalize_message(message) is message)
    check("None 也原样返回",
          normalize_message({"role": "assistant", "content": "hi", "tool_calls": None})["tool_calls"] is None)


def build_conversation(tmp_root):
    """造一个"能落盘"的 Conversation

    为什么用 __new__ 而不是 Conversation()：
      正常初始化会建 ToolManager（连 MCP 服务器，起子进程）、读 .env 里的真实密钥。
      这里只测"消息怎么落盘"，不需要那些东西。
    """
    old_sessions_dir = session_module.SESSIONS_DIR
    session_module.SESSIONS_DIR = Path(tmp_root) / "sessions"

    HistoryManager._instance = None  # 重置单例，拿到干净账本
    HistoryManager._initialized = False

    conv = Conversation.__new__(Conversation)
    conv._history_manager = HistoryManager()
    conv._task_depth = 0
    conv._session = SessionStore.create(cwd=str(tmp_root), model="test-model")
    return conv, old_sessions_dir


def test_roundtrip(tmp_root):
    print("测试: 端到端落盘 + 读回")
    conv, old_sessions_dir = build_conversation(tmp_root)
    try:
        call = make_sdk_call()
        conv.add_message({"role": "user", "content": [{"type": "text", "text": "读一下 a.py"}]})
        conv.add_message({"role": "assistant", "content": None, "tool_calls": [call]})
        conv.add_message({
            "role": "tool",
            "tool_call_id": "call_abc",
            "name": "file_read",
            "content": [{"type": "text", "text": "print(1)"}],
        })

        path = conv._session.path
        check("会话文件已生成", path.is_file())

        # 每一行都必须是合法 JSON——这就是改前崩掉的地方
        lines = path.read_text(encoding="utf-8").splitlines()
        valid = True
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                valid = False
                break
        check("每一行都是合法 JSON", valid, f"共 {len(lines)} 行")

        messages, _header = SessionStore.load(path)
        check("读回 3 条消息", len(messages) == 3, f"实际 {len(messages)}")

        assistant = [m for m in messages if m.get("role") == "assistant"][0]
        calls = assistant.get("tool_calls") or []
        check("读回后 tool_calls 仍是字典", bool(calls) and isinstance(calls[0], dict))
        if calls:
            check("读回后工具名还在", calls[0]["function"]["name"] == "file_read")
            check("读回后参数还在", calls[0]["function"]["arguments"] == '{"path": "a.py"}')

        # 内存里的那份也应该是字典（对象和字典混用是这次 bug 的第二个原因）
        in_memory = [m for m in conv.messages if m.get("role") == "assistant"][0]
        check("内存里存的也是字典", isinstance(in_memory["tool_calls"][0], dict))
    finally:
        session_module.SESSIONS_DIR = old_sessions_dir


def test_unserializable_fallback(tmp_root):
    print("测试: 真遇到不可序列化的字段，不崩、有告警")
    conv, old_sessions_dir = build_conversation(tmp_root)
    try:
        conv.add_message({"role": "user", "content": [{"type": "text", "text": "第一条"}]})
        before = len(conv._session.path.read_text(encoding="utf-8").splitlines())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            # set 不是 JSON 类型。normalize_message 不管这个字段，所以会走到兜底分支
            conv.add_message({"role": "assistant", "content": "hi", "weird": {1, 2}})
        after = len(conv._session.path.read_text(encoding="utf-8").splitlines())

        check("没有抛异常打断对话", True)
        check("坏消息没有被写进文件", after == before, f"{before} → {after}")
        check("stderr 有告警", "[会话存档]" in stderr.getvalue(), stderr.getvalue().strip()[:50])

        # 后面的消息还能正常写——说明兜底只影响那一条
        conv.add_message({"role": "user", "content": [{"type": "text", "text": "第二条"}]})
        final = len(conv._session.path.read_text(encoding="utf-8").splitlines())
        check("后续消息仍可写入", final == before + 1, f"{before} → {final}")
    finally:
        session_module.SESSIONS_DIR = old_sessions_dir


if __name__ == "__main__":
    print("🧪 工具调用消息的落盘\n")
    tmp_root = tempfile.mkdtemp(prefix="thinker_persist_test_")
    try:
        test_normalize_shape()
        print()
        test_no_mutation()
        print()
        test_dict_passthrough()
        print()
        test_no_tool_calls()
        print()
        test_roundtrip(tmp_root)
        print()
        test_unserializable_fallback(tmp_root)
        print()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
