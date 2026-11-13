# =============================================================================
# 文件：test/test_stream_pump.py
# 作用：测试「边读流边检查 Esc」——重点是"模型还没吐第一个字时也能打断"。
#
# 为什么这条 bug 必须专门测：
#   Esc 的检查点原来在 `for chunk in iterator` 的循环体里。模型思考时
#   （首字节还没到）循环体一次都不执行，标志位设了也没人看——用户实测
#   "按 Esc 没反应"就是这个原因。这类 bug **纯看代码很难发现**：
#   `if esc.triggered: break` 明明写在那里，但它根本执行不到。
#
#   所以核心用例是"给一个 5 秒才吐第一个字的流，按 Esc 必须立刻停"。
#   如果哪天有人把实现改回同步 for 循环，这条会超时失败。
#
# 全程离线：假迭代器 + 假 Esc，不连网络、不调模型。
# 运行方式（项目根目录）：
#   python test/test_stream_pump.py
# =============================================================================

import os  # 拼路径
import sys  # 改模块搜索路径 + 失败时非 0 退出
import time  # 计时：验证"立刻"到底有多快
import traceback  # 失败时打印现场

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.stream_pump import StreamInterrupted, iter_stream_with_esc  # 被测对象


class FakeEsc:
    """最小可用的 Esc 监听器替身：只有一个 triggered 标志"""

    def __init__(self):
        self.triggered = False

    def press(self):
        self.triggered = True


def test_normal_stream_in_order():
    """正常流：产出顺序、数量都要和源一致"""
    print("测试 流泵: 正常顺序")

    esc = FakeEsc()
    assert list(iter_stream_with_esc(iter(["a", "b", "c"]), esc)) == ["a", "b", "c"]

    print("  ✓ 通过")


def test_none_esc_is_passthrough():
    """esc 为 None：退化成直通（非 Windows / 非终端 / 管道场景）"""
    print("测试 流泵: 无监听器直通")

    assert list(iter_stream_with_esc(iter([1, 2, 3]), None)) == [1, 2, 3]

    print("  ✓ 通过")


def test_esc_interrupts_before_first_chunk():
    """核心：模型还在思考（5 秒内一个字节都没有）时，Esc 必须立刻生效

    这是原来那个 bug 的回归测试。如果实现退回到"收到数据才检查"，
    这里会等满 5 秒然后超时（断言里的 0.5 秒兜住）。
    """
    print("测试 流泵: 首字节到达前打断")

    esc = FakeEsc()

    def blocked():
        time.sleep(5)  # 模拟模型思考：5 秒不给数据
        yield "never"

    generator = iter_stream_with_esc(blocked(), esc, poll_interval=0.02)
    esc.press()  # 用户在这 5 秒里按了 Esc

    started = time.time()
    try:
        next(generator)
    except StreamInterrupted:
        elapsed = time.time() - started
        assert elapsed < 0.5, f"打断用了 {elapsed:.2f}s，说明还在同步等数据"
    else:
        raise AssertionError("按了 Esc 却没有打断")

    print("  ✓ 通过")


def test_esc_interrupts_between_chunks():
    """吐字过程中按 Esc：也要立刻停，并且泵线程不能再继续吞数据"""
    print("测试 流泵: 吐字过程中打断")

    esc = FakeEsc()
    consumed = []

    def slow():
        for i in range(200):
            consumed.append(i)
            time.sleep(0.02)
            yield i

    generator = iter_stream_with_esc(slow(), esc, poll_interval=0.02)
    assert next(generator) == 0  # 先正常收一块

    esc.press()
    try:
        next(generator)
    except StreamInterrupted:
        pass
    else:
        raise AssertionError("吐字过程中按 Esc 没有打断")

    # 打断之后再等一会儿，泵线程应该已经停了（最多多消费 1 个：它可能正卡在 next 上）
    count_after_interrupt = len(consumed)
    time.sleep(0.2)
    assert len(consumed) - count_after_interrupt <= 1, (
        f"打断后泵线程还在消费：多了 {len(consumed) - count_after_interrupt} 个"
    )

    print("  ✓ 通过")


def test_source_error_propagates():
    """源流的异常要原样抛到主线程（调用方的非流式 fallback 依赖它）"""
    print("测试 流泵: 异常透传")

    esc = FakeEsc()

    def broken():
        yield "a"
        raise ValueError("boom")

    generator = iter_stream_with_esc(broken(), esc)
    assert next(generator) == "a"
    try:
        next(generator)
    except ValueError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("源流异常没有传到主线程")

    print("  ✓ 通过")


def test_empty_stream():
    """空流：立刻结束，不报错、不卡住"""
    print("测试 流泵: 空流")

    esc = FakeEsc()
    assert list(iter_stream_with_esc(iter([]), esc)) == []

    print("  ✓ 通过")


def run_all_tests():
    """逐个跑；有失败就用非 0 退出码（约定见 THINKER.md 的「已知的坑」）"""
    print("开始运行流泵测试（全程离线）...\n")

    cases = [
        test_normal_stream_in_order,
        test_none_esc_is_passthrough,
        test_esc_interrupts_before_first_chunk,
        test_esc_interrupts_between_chunks,
        test_source_error_propagates,
        test_empty_stream,
    ]

    failures = []
    for case in cases:
        try:
            case()
        except Exception as e:
            failures.append((case.__name__, e))
            print(f"❌ {case.__name__} 失败：{e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {len(failures)} 个用例失败（通过 {len(cases) - len(failures)}/{len(cases)}）")
        for name, _ in failures:
            print(f"   - {name}")
        sys.exit(1)

    print(f"🎉 所有测试通过（{len(cases)}/{len(cases)}）")


if __name__ == "__main__":
    run_all_tests()
