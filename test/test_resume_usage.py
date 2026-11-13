#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_resume_usage.py
# 作用：验证「恢复会话后的用量估算」（2026-11-07 第 13.1 轮新增）
#
# 【为什么这个测试必须有】
#   第 13 轮的 `--continue` 有个不显眼的洞：恢复时只换消息列表，不写用量账本。
#   而"要不要压缩"看的就是账本最后一条，空账本 → 直接判"不用压"。
#   于是恢复一个已经用到 80% 的长会话，第一次请求会把整份历史原样发出去，
#   大概率 400。这个洞不会在"恢复一个短会话"时暴露，只能靠测试盯着。
#
# 【这个脚本不联网、不花钱】
#   只算字符、只碰内存里的账本，不构造 APIClient。
# =============================================================================

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.history.history_manager import (  # noqa: E402
    HistoryManager,
    estimate_messages_tokens,
    estimate_tokens,
)

passed = 0
total = 0


def check(name, condition, detail=""):
    """记一次断言（和项目里其他测试脚本保持同样的输出风格）"""
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"  ✓ {name}" + (f"：{detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"：{detail}" if detail else ""))


def fresh_history_manager(max_tokens_k: str = "1") -> HistoryManager:
    """拿一个"账本干净"的 HistoryManager

    为什么要重置单例：`HistoryManager` 是单例，模块级共享。
    不重置的话，上一个测试留下的 history_token_usage 会让断言失效。
    这里通过改环境变量把窗口调到 1k，让"超没超 80%"能在很小的数据上验证。
    """
    os.environ["MODEL_MAX_TOKENS"] = max_tokens_k
    os.environ.pop("COMPRESS_THRESHOLD", None)  # 用默认 0.8
    HistoryManager._instance = None
    HistoryManager._initialized = False
    return HistoryManager()


def test_estimate_shape():
    """估算函数本身的量级对不对"""
    print("测试: 估算函数的量级")
    # 中文 1 字 ≈ 1 token
    zh = estimate_tokens("上下文压缩")
    check("5 个中文字约 5 token", 5 <= zh <= 7, f"估出 {zh}")

    # ASCII 约 3.5 字符 ≈ 1 token
    ascii_text = "a" * 350
    en = estimate_tokens(ascii_text)
    check("350 个 ASCII 约 100 token", 95 <= en <= 105, f"估出 {en}")

    check("空串估成 0", estimate_tokens("") == 0)
    check(
        "同样长度下中文比英文贵",
        estimate_tokens("中" * 100) > estimate_tokens("a" * 100),
        f"{estimate_tokens('中' * 100)} vs {estimate_tokens('a' * 100)}",
    )


def test_tool_call_arguments_counted():
    """tool_calls 的参数必须算进去（只看 content 会严重低估）"""
    print("测试: 工具参数有没有被算进去")
    big_args = '{"path": "a.py", "new_string": "' + "x" * 2000 + '"}'
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "file_edit", "arguments": big_args}}
        ],
    }
    estimated = estimate_messages_tokens([message])
    check(
        "2000 字符的 new_string 被计入",
        estimated >= 500,
        f"两条加起来估出 {estimated} token",
    )


def test_seed_triggers_compression():
    """恢复一份长历史后，压缩判断必须立刻变成 True

    这是本轮的修复目标：改前账本是空的，_requires_compression() 恒为 False。
    """
    print("测试: 恢复长历史后能触发压缩")
    hm = fresh_history_manager("1")  # 窗口 1024 token，阈值 0.8 → 约 819

    long_text = "这是一段很长的历史。" * 300  # 约 3000 字，必然越过 819
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "你是一个助手"}]},
        {"role": "user", "content": [{"type": "text", "text": long_text}]},
    ]

    before = hm._requires_compression()
    check("恢复前账本为空，判为不用压缩", before is False)

    estimated = hm.seed_usage_from_messages(messages)
    check("估算出了正数", estimated > 800, f"估出 {estimated} token")
    check("账本真的写了进去", len(hm.history_token_usage) == 1)
    check(
        "写的是整页估值（覆盖，不是累加）",
        hm.history_token_usage[-1].total_tokens == estimated,
    )
    check("恢复后立刻判为需要压缩", hm._requires_compression() is True)

    # 再 seed 一次：应当覆盖同一个位置，而不是多出一条账
    hm.seed_usage_from_messages(messages)
    check("重复 seed 不会多出账本条目", len(hm.history_token_usage) == 1)


def test_short_history_not_compressed():
    """短历史不该被误判成需要压缩（别为了修 bug 造成反向问题）"""
    print("测试: 短历史不误压")
    hm = fresh_history_manager("200")  # 窗口 204800，阈值约 163840
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "你是一个助手"}]},
        {"role": "user", "content": [{"type": "text", "text": "你好"}]},
    ]
    estimated = hm.seed_usage_from_messages(messages)
    check("短会话估得很小", estimated < 100, f"估出 {estimated} token")
    check("短会话不触发压缩", hm._requires_compression() is False)


def test_compression_flag_reset():
    """seed 之后必须清掉「已压过」标记，否则第一次判断会被跳过"""
    print("测试: 已压标记被重置")
    hm = fresh_history_manager("1")
    hm._compressed_for_current_usage = True  # 模拟"上次已经压过"
    hm.seed_usage_from_messages([{"role": "user", "content": [{"type": "text", "text": "很长" * 500}]}])
    check("seed 后标记为 False", hm._compressed_for_current_usage is False)


if __name__ == "__main__":
    print("🧪 恢复会话的用量估算\n")
    test_estimate_shape()
    print()
    test_tool_call_arguments_counted()
    print()
    test_seed_triggers_compression()
    print()
    test_short_history_not_compressed()
    print()
    test_compression_flag_reset()
    print()

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
