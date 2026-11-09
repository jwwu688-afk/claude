# =============================================================================
# 文件：test/test_spinner_frame.py
# 作用：测试等待动画的「帧内容计算」——宽度、动效节奏、纯文本与带色版本的一致性。
#
# 为什么单独为"一帧长什么样"写测试：
#   等待动画是纯视觉的，跑在后台线程里直接写 stdout，几乎没法端到端断言。
#   但它有几个**能算出来的硬约束**，而且每一条错了都会立刻看出来：
#     · 每帧宽度必须 <= 补齐宽度（28 列），否则会换行、把后面所有输出顶乱；
#     · 秒数从 9s 变 10s 会多一列，如果实现按"文字长度"而不是"去 ANSI 后的
#       显示宽度"算 pad，就会在 10 秒那一刻开始留残影；
#     · 动词每 3 秒换一个、省略号每 0.5 秒一格——这些是"看起来在动"的来源，
#       周期写错会变成"偶尔卡住"，肉眼很难第一时间发现。
#   把 `build_spinner_frame()` 抽成纯函数就是为了这一组断言（第 19 轮）。
#
# 全程离线：不启线程、不写 stdout、不调模型。
# 运行方式（项目根目录）：
#   python test/test_spinner_frame.py
# =============================================================================

import os  # 拼路径
import sys  # 改模块搜索路径 + 失败时非 0 退出
import traceback  # 失败时打印现场

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from rich.cells import cell_len  # 算"显示占几列"（中文 2 列），和分析 pad 时同一把尺子

from ui import theme  # 帧字符、动词表、ANSI 码都从这里来
from ui.display_manager import DisplayManager, build_spinner_frame  # 被测对象


def test_frame_chars_come_from_theme():
    """转轮字符必须来自 theme.SPIN_FRAMES（不能是硬编码的某一帧）"""
    print("测试 帧: 字符来源")

    for elapsed in (0.0, 0.13, 0.5, 1.7, 2.4):
        plain, _ = build_spinner_frame(elapsed)
        assert plain[0] in theme.SPIN_FRAMES, f"{elapsed}s 的帧字符不在 SPIN_FRAMES 里：{plain[0]}"

    print("  ✓ 通过")


def test_verb_rotates_every_three_seconds():
    """动词每 3 秒换一个，15 秒一轮回

    为什么 3 秒：太快会让人分神（文字一直在变），太慢又回到"是不是卡住了"。
    3 秒大约是一次呼吸的长度，正好。
    """
    print("测试 帧: 动词轮换")

    verb_at = {}
    for seconds in (0, 1, 2, 3, 4, 5, 6):
        plain, _ = build_spinner_frame(float(seconds))
        verb_at[seconds] = next(v for v in theme.WAIT_VERBS if v in plain)

    assert verb_at[0] == verb_at[1] == verb_at[2], "前 3 秒动词不该变"
    assert verb_at[3] != verb_at[2], "第 3 秒动词应该换"
    assert verb_at[6] != verb_at[3], "第 6 秒动词应该再换"

    # 5 个动词 × 3 秒 = 15 秒一轮回
    first, _ = build_spinner_frame(0.0)
    later, _ = build_spinner_frame(15.0)
    assert first.split("(")[0].split(")")[0].replace(".", "") == later.split("(")[0].split(")")[0].replace(".", ""), \
        "15 秒后动词应该回到第一个"

    print("  ✓ 通过")


def test_dots_cycle():
    """省略号 0→1→2→3→0，每 0.5 秒一格

    直接数点号个数，不靠"看起来对不对"。
    """
    print("测试 帧: 省略号循环")

    counts = []
    for half_second in range(8):  # 0.0 ~ 3.5s，覆盖两个完整循环
        plain, _ = build_spinner_frame(half_second * 0.5)
        # 只数动词后面的点，避免把 "(0s)" 里的点算进来
        tail = plain.split("(")[0]
        counts.append(tail.count("."))

    assert counts[:4] == [0, 1, 2, 3], f"前两秒点号应该是 0,1,2,3，实际 {counts[:4]}"
    assert counts[4:8] == [0, 1, 2, 3], f"第二轮应该重新从 0 开始，实际 {counts[4:8]}"

    print("  ✓ 通过")


def test_frame_width_never_exceeds_cells():
    """任何时刻的帧都不能超过补齐宽度（28 列）

    这是最硬的一条：超了就会自动换行，后面所有输出全被顶乱。
    采样从 0 秒到 1 小时，覆盖秒数 1→2→3→4 位的所有进位点。
    """
    print("测试 帧: 宽度上限")

    limit = DisplayManager._SPINNER_CELLS
    samples = [0, 0.5, 1, 9, 9.9, 10, 59, 60, 99, 599, 600, 999, 3599, 3600]
    for seconds in samples:
        plain, _ = build_spinner_frame(float(seconds))
        width = cell_len(plain)
        assert width <= limit, f"{seconds}s 的帧宽 {width} 列，超过上限 {limit}：{plain!r}"

    print("  ✓ 通过")


def test_plain_has_no_ansi():
    """纯文本版本里不能有转义码

    为什么重要：pad 是用 cell_len(plain) 算的。如果 plain 里混进了 ANSI，
    cell_len 会把 "\\x1b[38;5;209m" 这些字符也算进宽度（虽然 rich 的 cell_len
    对 ANSI 的处理不如 strip_ansi 干净），pad 就会偏小，残影重新出现。
    """
    print("测试 帧: 纯文本无转义")

    plain, styled = build_spinner_frame(12.3)
    assert "\x1b" not in plain, f"plain 里混进了转义码：{plain!r}"
    assert "\x1b" in styled, "styled 里应该有转义码"
    assert theme.ACCENT_ANSI in styled  # 转轮用品牌橙
    assert theme.DIM_ANSI in styled  # 说明文字用暗灰

    print("  ✓ 通过")


def test_same_elapsed_is_stable():
    """同一个时刻算两次，结果必须一样（纯函数，不能有随机/状态）

    为什么必须纯：后台线程每 100ms 算一次，如果帧依赖"上次算到哪"，
    那么两次刷新间隔抖动就会让动画跳帧或倒转。
    """
    print("测试 帧: 纯函数")

    for elapsed in (0.0, 3.7, 42.0):
        assert build_spinner_frame(elapsed) == build_spinner_frame(elapsed)

    print("  ✓ 通过")


def run_all_tests():
    """逐个跑；有失败就用非 0 退出码（约定见 THINKER.md 的「已知的坑」）"""
    print("开始运行等待动画帧测试（全程离线）...\n")

    cases = [
        test_frame_chars_come_from_theme,
        test_verb_rotates_every_three_seconds,
        test_dots_cycle,
        test_frame_width_never_exceeds_cells,
        test_plain_has_no_ansi,
        test_same_elapsed_is_stable,
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
