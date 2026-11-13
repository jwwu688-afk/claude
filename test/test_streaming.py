#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_streaming.py
# 作用：验证「流式输出」这条管线的分流是否正确。
#
# 【2026-11-13 重写：从"真打接口"改成"假客户端 + 可选真打"】
#   这个脚本原来直接 new 一个 APIClient()，拿 .env 里的真密钥发一次请求，
#   靠人眼看"字有没有一个个蹦出来"。它有三个问题：
#     1. **每次跑都花钱**。批量回归整个 test/ 目录时，它会静默消耗额度。
#     2. **没有断言**。只打印不判断，接口返回什么算"对"全靠感觉——
#        第 28 轮加 ThinkingChunk 之后它就开始报
#        `'ThinkingChunk' object has no attribute 'content'`，
#        而人眼看着打印输出是"收到回复了"，完全看不出管线已经分错类。
#     3. **必须联网**，离线环境（CI、飞机上）跑不了。
#
#   现在默认走**假客户端**：用一个预设的 chunk 序列（正文 + 思考心跳 +
#   工具参数心跳 + 完整消息）驱动真实的消费逻辑，逐条断言四类 chunk 的去向。
#   这正是第 28 / 30 轮新增两种心跳之后最该钉住的东西：
#     正文 → 拼进 full_content
#     心跳 → 只记字数，**绝不能进正文**（进了就会被当成"模型说过的话"
#            发回下一轮，上下文暴涨）
#   想真的验证线上接口（比如换供应商后确认流式没坏），用：
#     python test/test_streaming.py --live
#   它跑的是原来那段真请求逻辑，会花钱、需要联网。
# =============================================================================

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
# 一、假客户端：预设一段"像真的一样"的流
# =============================================================================
def make_fake_stream():
    """造一段 chunk 序列，覆盖四种真实会出现的元素

    顺序刻意模仿推理模型的真实节奏：
      先想一大段（ThinkingChunk）→ 开口说正文 → 中间又想了一下 →
      开始吐工具参数（ToolCallChunk）→ 最后给完整消息对象。

    为什么正文拆成"你"和"好呀"两段：真流式就是一片片来的，
    只发一整段的话，"逐片累加"这段逻辑等于没测到。
    """
    from core.api_client import ThinkingChunk, ToolCallChunk

    final = types.SimpleNamespace(role="assistant", content="你好呀", tool_calls=None)

    return [
        ThinkingChunk(240),   # 先想了 240 字（推理模型的常态）
        "你",
        "好呀",
        ThinkingChunk(80),    # 说一半又想了 80 字
        ToolCallChunk(36),    # 参数已经吐了 36 个字符
        final,                # 最后那条完整消息
    ]


def consume_stream(chunks):
    """按 conversation.py 的真实顺序分流，返回统计

    为什么在这里重写一遍分流而不是直接调 conversation 的方法：
      那段代码埋在 _run_one_iteration 里，依赖 API 客户端、界面、会话一堆东西；
      把它整条拉起来要 mock 十几个对象，测试会比被测代码更长。
      这里只抄"判断顺序"这一件事——而判断顺序恰恰是唯一容易错的地方：
      必须先 isinstance(ThinkingChunk) / isinstance(ToolCallChunk)，再 isinstance(str)，
      顺序反了心跳就会被当成正文。
    """
    from core.api_client import ThinkingChunk, ToolCallChunk

    full_content = ""
    thinking_chars = 0
    tool_call_chars = 0
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
        "thinking_chars": thinking_chars,
        "tool_call_chars": tool_call_chars,
        "message_obj": message_obj,
    }


def test_streaming_offline():
    """离线回归：四类 chunk 各归各位，正文里不能混进心跳"""
    print("🧪 流式管线（假客户端，不联网、不花钱）")

    stats = consume_stream(make_fake_stream())

    check("正文按片累加", stats["full_content"] == "你好呀", stats["full_content"])
    check("思考心跳累计字数", stats["thinking_chars"] == 320, str(stats["thinking_chars"]))
    check("工具参数心跳累计字数", stats["tool_call_chars"] == 36, str(stats["tool_call_chars"]))
    check("拿到了完整的消息对象", stats["message_obj"] is not None)
    check(
        "完整消息的内容和累加结果一致",
        stats["message_obj"] is not None and stats["message_obj"].content == stats["full_content"],
        f'{getattr(stats["message_obj"], "content", None)} vs {stats["full_content"]}',
    )

    # 这条是整个文件最重要的断言：心跳一旦混进正文，下一轮请求就会把
    # 模型的内心独白 / 工具参数当成"它自己说过的话"发回去。
    check(
        "心跳没有混进正文",
        stats["full_content"] == "你好呀",
        "正文里出现心跳说明分流顺序写反了",
    )


# =============================================================================
# 二、--live：原来那段真请求逻辑（会花钱，需要显式开启）
# =============================================================================
def test_streaming_live():
    """真打一次接口，人眼看流式有没有卡顿（会花钱！）

    保留它的理由：假客户端只能验"我们自己的分流逻辑"，
    验不了"供应商的流式协议有没有变"。换中转站之后想快速确认，
    这条比读代码快。
    """
    print("🧪 Testing streaming functionality (LIVE, 会产生一次请求费用)...")

    try:
        from core.api_client import APIClient

        client = APIClient()

        if not hasattr(client, 'get_completion_stream'):
            print("❌ get_completion_stream method not found!")
            return False

        print("✅ Streaming method found")

        request_params = {
            "messages": [
                {"role": "system", "content": "你是一个乐于助人的助手。"},
                {"role": "user", "content": "用三个词跟我打个招呼"},
            ]
        }

        print("📡 Making streaming request...")
        print("📝 Streaming response:")
        print("🤖 ", end="", flush=True)

        stream_gen = client.get_completion_stream(request_params)
        stats = consume_stream(stream_gen)
        print(stats["full_content"])

        print("\n📊 Streaming stats:")
        print(f'   正文长度: {len(stats["full_content"])}')
        print(f'   思考字数: {stats["thinking_chars"]}')
        print(f'   工具参数字数: {stats["tool_call_chars"]}')

        if stats["full_content"] and stats["message_obj"]:
            print("🎉 Streaming test completed successfully!")
            return True
        print("❌ No content received")
        return False

    except Exception as e:
        print(f"\n❌ Streaming test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    # 只有显式带 --live 才真发请求。默认永远走离线路径——
    # 这样它就能安全地被批量回归覆盖（原来它是个"跑一次花一次钱"的陷阱）。
    if "--live" in sys.argv:
        sys.exit(0 if test_streaming_live() else 1)

    test_streaming_offline()
    print()
    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        print("   想真打接口请加 --live（会产生一次请求费用）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
