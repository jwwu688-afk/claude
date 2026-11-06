# =============================================================================
# 文件：test/test_approval_and_cache.py
# 作用：验证第 8.2 轮的两件事
#   1. 状态行里的"缓存命中"从绝对 token 数改成百分比，且口径不会算出 >100%
#   2. 授权提问从"必须打 yes/no"改成"回车即同意，其他内容当拒绝理由"
#
# 为什么这两件事要写测试？
#   百分比是个"算错了也看不出来"的东西：分母取错，屏幕上照样显示 78%，
#   但那个 78% 是假的，你会基于它判断"缓存生效了"，然后继续按原价付费。
#   授权解析同理：判断错一次就是一条命令被静默执行，或者用户明明同意了却没跑。
#
# 全程离线，不调模型 API、不读键盘。
# 运行方式：在项目根目录执行 python test/test_approval_and_cache.py
#
# 【2026-11-06 第 8.2 轮】新增本文件。
# =============================================================================

import os  # 构造 APIClient 之前要保证三个环境变量存在
import sys  # 把 ../src 塞进搜索路径
import traceback  # 失败时打印完整现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# 构造 APIClient 会检查这三项，缺一个就抛 ValueError。
# 用 setdefault 是为了不覆盖 .env 里已有的真实配置；这里不会发任何网络请求。
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from core.api_client import APIClient  # 被测对象一：用量记账
from ui.input_handler import InputHandler  # 被测对象二：授权输入的解释
from ui.ui_manager import UIManager  # 被测对象三：工具调用摘要


# -----------------------------------------------------------------------------
# 一、缓存命中率
# -----------------------------------------------------------------------------

class FakeDetails:
    """模仿 OpenAI 的 prompt_tokens_details 对象"""

    def __init__(self, cached):
        self.cached_tokens = cached


class FakeUsage:
    """模仿接口返回的 usage 对象

    为什么不用真的 SDK 类型？
      usage 是"接口返回什么就是什么"的松散结构，不同中转站字段还不一样。
      这里故意只给需要的字段，正好验证 _record_usage 里的 getattr 兜底。
    """

    def __init__(self, prompt_tokens, cached=0, cache_read=0, cache_creation=0, openai_style=True):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = 10
        self.total_tokens = prompt_tokens + 10
        if openai_style:  # OpenAI 一定会带这个对象
            self.prompt_tokens_details = FakeDetails(cached)
        if cache_read:
            self.cache_read_input_tokens = cache_read  # Anthropic 口径
        if cache_creation:
            self.cache_creation_input_tokens = cache_creation


def fresh_client():
    """拿一个"账本清零"的 APIClient

    为什么要清类属性：它是单例，三个计数器挂在类上，
    上一个用例跑出来的数字会带到下一个用例里，断言全部会飘。
    """
    APIClient._instance = None
    APIClient._initialized = False
    APIClient._cached_tokens = 0
    APIClient._prompt_tokens = 0
    APIClient._total_cost = 0
    return APIClient()


def test_openai_style_rate():
    """① OpenAI 口径：prompt_tokens 已经包含缓存部分，分母就是它"""
    client = fresh_client()
    client._record_usage(FakeUsage(prompt_tokens=10000, cached=8000))
    assert client.cached_tokens == 8000
    assert client.prompt_tokens == 10000, f"分母应等于 prompt_tokens，实际 {client.prompt_tokens}"
    assert abs(client.cache_hit_rate - 80.0) < 0.01, f"命中率应为 80%，实际 {client.cache_hit_rate}"
    print("  ✓ OpenAI 口径：8000/10000 = 80%")


def test_anthropic_style_rate():
    """② Anthropic 口径：input_tokens 不含缓存读写，分母要把三块加起来

    这是最容易算错的地方。如果直接拿 prompt_tokens 当分母，
    input=2000、cache_read=8000 会算出 400%，屏幕上就会显示"缓存命中 400%"。
    """
    client = fresh_client()
    client._record_usage(FakeUsage(prompt_tokens=2000, cache_read=8000,
                                   cache_creation=0, openai_style=False))
    assert client.cached_tokens == 8000
    assert client.prompt_tokens == 10000, f"分母应为 2000+8000，实际 {client.prompt_tokens}"
    assert abs(client.cache_hit_rate - 80.0) < 0.01, f"命中率应为 80%，实际 {client.cache_hit_rate}"
    print("  ✓ Anthropic 口径：8000/(2000+8000) = 80%，没有算出 400%")


def test_no_cache_fields():
    """③ 接口完全不返回缓存字段时，命中率是 0，不能除零"""
    client = fresh_client()
    client._record_usage(FakeUsage(prompt_tokens=5000, openai_style=False))
    assert client.cached_tokens == 0
    assert client.cache_hit_rate == 0.0, f"应为 0，实际 {client.cache_hit_rate}"
    print("  ✓ 没有缓存字段：命中率 0%，不抛除零错误")


def test_rate_accumulates_across_calls():
    """④ 多次请求要按"总量"算，不是取最后一次

    如果取最后一次，用户只要中间跑一次不带缓存的小请求，
    状态行就会突然从 80% 掉到 0%，看起来像"缓存失效了"。
    """
    client = fresh_client()
    client._record_usage(FakeUsage(prompt_tokens=10000, cached=8000))  # 80%
    client._record_usage(FakeUsage(prompt_tokens=10000, cached=2000))  # 20%
    assert client.cached_tokens == 10000
    assert client.prompt_tokens == 20000
    assert abs(client.cache_hit_rate - 50.0) < 0.01, f"累计应为 50%，实际 {client.cache_hit_rate}"
    print("  ✓ 两次请求累计：(8000+2000)/(10000+10000) = 50%")


def test_rate_never_exceeds_100():
    """⑤ 中转站乱报（只给 cached、不给 prompt_tokens）时，命中率也不能超过 100%"""
    client = fresh_client()
    client._record_usage(FakeUsage(prompt_tokens=0, cached=640, openai_style=False))
    assert 0 <= client.cache_hit_rate <= 100, f"命中率越界：{client.cache_hit_rate}"
    print(f"  ✓ 异常用量下命中率仍为 {client.cache_hit_rate:.0f}%，没有越界")


def test_empty_usage_is_safe():
    """⑥ 拿不到用量（很多流式接口最后一帧才给）时不能崩"""
    client = fresh_client()
    client._record_usage(None)
    assert client.cache_hit_rate == 0.0
    print("  ✓ usage 为 None 时安全返回 0%")


# -----------------------------------------------------------------------------
# 二、授权输入的解释
# -----------------------------------------------------------------------------

def test_enter_means_yes():
    """⑦ 直接回车 = 同意（这是第 8.2 轮的核心交互）"""
    assert InputHandler.interpret_approval("", True) == (True, "")
    assert InputHandler.interpret_approval("   ", True) == (True, ""), "只有空格也算回车"
    print("  ✓ 回车 / 空格 = 同意")


def test_enter_defaults_to_no_when_asked():
    """⑧ 默认值可以翻转（留给第 9 轮的高危操作：回车 = 拒绝）"""
    assert InputHandler.interpret_approval("", False) == (False, "")
    print("  ✓ default_yes=False 时，回车 = 拒绝（接口已留好）")


def test_word_lists():
    """⑨ 词表:y / n 以及中文都能认，且大小写无关"""
    for word in ("y", "Y", "yes", "YES", "是", "确认", "ok"):
        assert InputHandler.interpret_approval(word, True)[0] is True, f"'{word}' 应判为同意"
    for word in ("n", "N", "no", "NO", "否", "取消", "算了"):
        assert InputHandler.interpret_approval(word, True)[0] is False, f"'{word}' 应判为拒绝"
    print("  ✓ 中英文词表都生效，大小写无关")


def test_other_text_is_rejection_with_reason():
    """⑩ 输入别的内容 = 拒绝 + 那句话就是"我想怎么改"

    这是最有用的一条：用户想说"别删这个文件，用 file_edit"时，
    一句话说完就完事，不用先选 no、再等系统问一遍理由。
    """
    ok, reason = InputHandler.interpret_approval("别删这个文件，改用 file_edit", True)
    assert ok is False
    assert reason == "别删这个文件，改用 file_edit", f"理由没带出来：{reason!r}"
    print("  ✓ 其他内容 → 拒绝 + 原话作为说明")


def test_substring_no_longer_tricks_yes():
    """⑪ 回归：子串匹配的坑

    改前用的是 "yes" in text。输入 "yesterday" 里含 "yes"，
    会被判成同意——也就是一条命令在用户完全没同意的情况下被执行。
    同理 "I don't know" 含 "no"，会被判成拒绝（这个方向上只是丢理由，还算安全）。
    现在改成精确匹配词表，两种情况都不会再误判。
    """
    ok, reason = InputHandler.interpret_approval("yesterday", True)
    assert ok is False, "旧写法会把 yesterday 判成同意，这是严重的误放行"
    assert reason == "yesterday"
    ok2, _ = InputHandler.interpret_approval("I don't know", True)
    assert ok2 is False, "含 no 的句子仍是拒绝（行为对，但理由要保留）"
    print("  ✓ 'yesterday' 不再被当成 yes；'I don't know' 仍是拒绝")


# -----------------------------------------------------------------------------
# 三、工具调用摘要（审批那一行显示什么）
# -----------------------------------------------------------------------------

def test_format_tool_call():
    """⑫ 常用的几类工具都能压成一行，且不再铺开整个参数字典"""
    ui = UIManager()
    assert ui.format_tool_call("cmd_runner", {"command": "dir"}) == "cmd_runner(dir)"
    assert ui.format_tool_call("file_read", {"path": "src/main.py"}) == "file_read(src/main.py)"
    assert ui.format_tool_call("file_grep", {"pattern": "TODO"}) == "file_grep(TODO)"
    # 改文件时最容易把整屏撑爆的场景：new_string 可能有几百行
    preview = ui.format_tool_call("file_edit", {
        "path": "a.py", "old_string": "hi", "new_string": "bye\n" * 200,
    })
    assert preview == "file_edit(a.py)", f"应只显示路径，实际 {preview[:60]}"
    todos_preview = ui.format_tool_call("todo_write", {"todos": [{"content": "a"}]})
    assert todos_preview == "todo_write(1 项)", todos_preview
    print("  ✓ 命令/路径/模式/待办都能压成一行；file_edit 只显示路径")


def test_format_tool_call_without_args():
    """⑬ 没有参数时不要出现空括号"""
    ui = UIManager()
    assert ui.format_tool_call("task", {}) == "task"
    print("  ✓ 无参数时只显示工具名")


def main():
    tests = [
        test_openai_style_rate,
        test_anthropic_style_rate,
        test_no_cache_fields,
        test_rate_accumulates_across_calls,
        test_rate_never_exceeds_100,
        test_empty_usage_is_safe,
        test_enter_means_yes,
        test_enter_defaults_to_no_when_asked,
        test_word_lists,
        test_other_text_is_rejection_with_reason,
        test_substring_no_longer_tricks_yes,
        test_format_tool_call,
        test_format_tool_call_without_args,
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
