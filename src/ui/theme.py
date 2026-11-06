# =============================================================================
# 文件：src/ui/theme.py
# 作用：整个界面的"配色和符号"都集中在这里（2026-11-06 第 4 轮新增）
#
# 为什么单独搞一个文件？
#   之前颜色散落在各个文件里：有的用 print 打 emoji，有的用 rich 打 markdown，
#   想统一风格就得改七八个地方。现在所有颜色、符号、缩进都从这一份定义里取，
#   以后想换个主题，只改这一个文件。
#
# 风格参考 Claude Code：暗色、克制、少用花哨颜色，只用一个强调色（青色）。
# =============================================================================

from rich.console import Console  # rich 的控制台对象
from rich.theme import Theme  # 主题：给"样式名"起别名，后面用名字引用颜色

# --- 颜色 -------------------------------------------------------------------
ACCENT = "cyan"  # 强调色：标题、提示符
DIM = "bright_black"  # 次要信息：状态栏、参数、辅助说明
OK = "green"  # 成功
ERR = "red"  # 失败
WARN = "yellow"  # 警告

# --- 符号 -------------------------------------------------------------------
# 这些符号是 Claude Code 那种"极简科技感"的关键：
# 用一个圆点表示"模型开始说话"，用 ⎿ 表示"这是上面的结果"
BULLET = "⏺"  # 模型发言 / 工具调用的起始标记
RESULT = "⎿"  # 结果的转折标记
PROMPT = "❯"  # 输入提示符
CHECK_ON = "✔"  # 已完成
CHECK_OFF = "◻"  # 未完成
CHECK_RUN = "◼"  # 进行中
CROSS = "✗"  # 失败
DOT = "·"  # 分隔点

# 全局唯一的一个控制台对象。
# 之前每个类都 new 一个 Console，导致样式各写各的；现在大家共用这一个。
console = Console(
    theme=Theme({
        "qs.accent": ACCENT,  # 用法：console.print("文字", style="qs.accent")
        "qs.dim": DIM,
        "qs.ok": OK,
        "qs.err": ERR,
        "qs.warn": WARN,
    }),
    highlight=False,  # 关掉自动高亮，避免把普通文字里的数字、路径染成奇怪的颜色
)

# 终端宽度：面板、分隔线要按它来对齐
WIDTH = 66


def prompt_text() -> str:
    """生成输入提示符（带颜色）。

    为什么用原始颜色码而不是 rich？
      因为 input() 是 Python 内置函数，不接受 rich 对象，只能给它一段字符串。
      现代终端（Windows Terminal、iTerm、VS Code 终端）都认这种颜色码。
    """
    return f"\x1b[36m{PROMPT}\x1b[0m "
