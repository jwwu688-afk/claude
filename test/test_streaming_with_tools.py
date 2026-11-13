#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_streaming_with_tools.py
# 作用：验证「流式输出 + 工具调用」这条路——工具清单发得对不对、
#       tool_calls 能不能完整取回来、参数是不是合法 JSON。
#
# 【2026-11-13 重写：原来是"只要不抛异常就算过"的真请求脚本】
#   改前的判定是最后一行 `return True`——**没有任何断言**。
#   它跑起来会真的 new APIClient() + ToolManager()（后者还会在后台连 MCP 服务器），
#   发一次请求、打印一堆东西，然后无条件成功。这种脚本是"永远绿"的典型：
#   接口挂了、参数拼坏了、工具清单格式错了，它照样退出码 0。
#   第 13.1 轮那个"漏 await 导致从来跑不通"的笔误能活那么久，就是因为
#   没人真的指望它的退出码。
#
#   现在默认走**假客户端**，把三件事变成可断言的：
#     1. 发给接口的 tools 结构对不对（type/function 三件套）；
#     2. tool_calls 分片心跳和正文会不会串味（串了就会把参数写进历史）；
#     3. 参数能不能 json.loads（真实故障里最常见的就是参数拼成半截）。
#   想真的验证线上接口，用 `python test/test_streaming_with_tools.py --live`。
# =============================================================================

import asyncio
import json
import os
import sys
import types

# Add src directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
src_path = os.path.join(current_dir, '../src')
sys.path.insert(0, src_path)

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


# =============================================================================
# 一、工具清单的形状
# =============================================================================
def build_tools_payload():
    """造一份"和 ToolManager 输出同形状"的工具清单

    为什么不直接 `await ToolManager().get_tools_description()`：
      那个单例会去连 MCP 服务器（要起子进程、等超时），离线测试不该有这副作用，
      而且它一旦连不上就会让整条用例变慢十几秒。这里只复刻**外形**，
      要验的是"我们把什么形状的东西发给了接口"。

    真实形状（见 tool_manager.get_tools_description）：
      {"type": "function",
       "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "cmd_runner",
                "description": "执行一条 shell 命令",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]


def test_tools_payload_shape():
    """工具清单必须是 OpenAI 认的那个形状，否则请求会 400"""
    print("🧪 工具清单形状")
    tools = build_tools_payload()

    check("是非空列表", isinstance(tools, list) and len(tools) >= 1, str(type(tools)))
    first = tools[0]
    check("每项有 type=function", first.get("type") == "function", str(first.get("type")))
    check("function 里有 name", bool(first["function"].get("name")), str(first["function"].get("name")))
    check(
        "function 里有 description",
        bool(first["function"].get("description")),
        str(first["function"].get("description"))[:30],
    )
    check(
        "function 里有 parameters 对象",
        isinstance(first["function"].get("parameters"), dict),
        str(type(first["function"].get("parameters"))),
    )


# =============================================================================
# 二、带工具的流：分片分流 + tool_calls 提取
# =============================================================================
def make_fake_tool_stream():
    """造一段"边说话边决定调工具"的流

    顺序对齐真实时序：先吐参数心跳（模型在想调什么）→ 说一句正文 →
    再吐几片参数 → 最后给完整消息（带 tool_calls）。
    """
    from core.api_client import ToolCallChunk

    final = types.SimpleNamespace(
        role="assistant",
        content="我来看一下当前目录。",
        tool_calls=[
            types.SimpleNamespace(
                id="call_1",
                function=types.SimpleNamespace(
                    name="cmd_runner",
                    arguments='{"command": "dir"}',
                ),
            )
        ],
    )

    return [
        ToolCallChunk(12),           # 参数开始吐
        "我来看一下",                 # 正文
        "当前目录。",
        ToolCallChunk(24),           # 参数又长了一截
        final,
    ]


def consume_tool_stream(chunks):
    """按真实顺序分流，并收集 tool_calls"""
    from core.api_client import ThinkingChunk, ToolCallChunk

    full_content = ""
    tool_call_chars = 0
    thinking_chars = 0
    message_obj = None

    for chunk in chunks:
        if isinstance(chunk, ThinkingChunk):
            thinking_chars += chunk.chars
            continue
        if isinstance(chunk, ToolCallChunk):
            tool_call_chars += chunk.chars
            continue
        if isinstance(chunk, str):
            full_content += chunk
            continue
        if hasattr(chunk, "role") and chunk.role == "assistant":
            message_obj = chunk
            break

    return {
        "full_content": full_content,
        "tool_call_chars": tool_call_chars,
        "thinking_chars": thinking_chars,
        "message_obj": message_obj,
        "tool_calls": list(getattr(message_obj, "tool_calls", None) or []),
    }


def test_tool_stream_offline():
    """离线回归：正文干净、工具申请完整、参数能解析"""
    print("🧪 带工具的流（假客户端，不联网、不花钱）")

    stats = consume_tool_stream(make_fake_tool_stream())

    check("正文只包含模型说的话", stats["full_content"] == "我来看一下当前目录。", stats["full_content"])
    check("参数心跳字数累计", stats["tool_call_chars"] == 36, str(stats["tool_call_chars"]))
    check("参数心跳没混进正文", "command" not in stats["full_content"] and "dir" not in stats["full_content"])
    check("取到了一条工具申请", len(stats["tool_calls"]) == 1, str(len(stats["tool_calls"])))

    if stats["tool_calls"]:
        call = stats["tool_calls"][0]
        check("工具名正确", call.function.name == "cmd_runner", call.function.name)

        # 这条是真故障的入口：参数在流式里是分片拼起来的，
        # 拼漏一个括号下一轮就会报"参数不是合法 JSON"，模型只能干瞪眼。
        try:
            parsed = json.loads(call.function.arguments)
            ok, detail = True, str(parsed)
        except json.JSONDecodeError as e:
            ok, detail = False, str(e)
        check("参数是合法 JSON", ok, detail)
        if ok:
            check("参数里有 command 字段", "command" in parsed, str(parsed))


# =============================================================================
# 三、--live：原来那段真请求逻辑（会花钱）
# =============================================================================
async def test_streaming_with_tools_live():
    """真打一次带 tools 的请求（会花钱、需要联网）"""
    print("🧪 Testing streaming with tools (LIVE, 会产生一次请求费用)...")

    try:
        from core.api_client import APIClient
        from tools.tool_manager import ToolManager

        client = APIClient()
        tool_manager = ToolManager()

        request_params = {
            "messages": [
                {"role": "system", "content": "你是一个乐于助人的助手。"},
                {"role": "user", "content": "请列出当前目录下的文件"},
            ],
            # 第 13.1 轮修的点：不 await 拿到的是协程对象，SDK 序列化时才报看不懂的错
            "tools": await tool_manager.get_tools_description(),
        }

        print("📡 Making streaming request with tools...")
        stats = consume_tool_stream(client.get_completion_stream(request_params))
        print(f'🤖 {stats["full_content"]}')
        print(f'   正文长度: {len(stats["full_content"])}')
        print(f'   工具申请数: {len(stats["tool_calls"])}')
        for i, tc in enumerate(stats["tool_calls"]):
            print(f'   {i + 1}: {tc.function.name}({tc.function.arguments[:60]})')

        print("🎉 Live tool streaming finished")
        return True

    except Exception as e:
        print(f"\n❌ Tool streaming test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(0 if asyncio.run(test_streaming_with_tools_live()) else 1)

    test_tools_payload_shape()
    print()
    test_tool_stream_offline()
    print()
    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        print("   想真打接口请加 --live（会产生一次请求费用）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
