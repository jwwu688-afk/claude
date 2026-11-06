# =============================================================================
# 文件：src/main.py
# 作用：整个程序的「大门」。你在命令行敲 quickstar 时，最先跑的就是这个文件。
#
# 【整个程序是怎么转起来的】
#   1. main.py 画欢迎页，然后唤醒 Conversation（对话总管）
#   2. Conversation 把「系统提示词」塞进消息列表，再等用户打字
#   3. Conversation 把消息发给大模型（api_client.py 负责真正的网络请求）
#   4. 如果模型说"我要用某个工具"，Conversation 就去找 ToolManager 执行
#   5. 工具跑完的结果再塞回消息列表，再发给大模型，循环往复，直到模型不再用工具
#   6. UI 那一层（src/ui/）只负责"显示"和"要输入"，不参与逻辑判断
#
# 想看懂代码，建议的阅读顺序是：
#   main.py → core/conversation.py → core/api_client.py → tools/tool_manager.py
#   → tools/base_tool.py → 其他具体工具 → ui/
#
# 【2026-11-06 第 5 轮】欢迎页升级：
#   - 用方块字符拼出 5 行高的 "QUICK STAR" 标志，每个字符按列取渐变颜色
#   - 启动时有一道白色高光从左扫到右（约 0.4 秒），扫完停在彩色状态
#   - 上下各一条渐变分隔线，两端带星形符号
#   - 不是终端（比如输出被重定向）或设了 QUICKSTAR_NO_ANIM=1 时自动跳过动画
# =============================================================================

import asyncio  # 导入 asyncio 工具箱：让程序在"等网络回复"的时候还能去干别的事（异步）
import os  # 用来读环境变量（拿模型名、压缩阈值）和当前目录
import time  # 用来控制动画每一帧之间的间隔
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui.theme import console, ACCENT, DIM, WIDTH, gradient_color  # 统一的控制台、配色、渐变取色
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色
from rich.live import Live  # 实时刷新一块区域，动画就靠它


# =============================================================================
# 一、大号字体的"字模"
# 每个字母用 5 行、每行若干个方块字符拼成，画出来就是 5 行高的粗体标题。
# 用 █ 而不是 #，是因为方块字符在终端里是"满格"的，拼起来才连成一片。
# =============================================================================
_GLYPHS = {
    "Q": [" ███ ", "█   █", "█   █", "█  ██", " ████"],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "I": ["███", " █ ", " █ ", " █ ", "███"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "K": ["█   █", "█  █ ", "███  ", "█  █ ", "█   █"],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
}

_WORDS = ["QUICK", "STAR"]  # 标题的两个单词，中间会空开几格
_GAP = 3  # 两个单词之间空几格


def _build_logo_lines() -> list[str]:
    """把字模拼成 5 行文字（2026-11-06 第 5 轮新增）

    返回的每一行长度都一样，后面按"列号"上色时才能对齐。
    注意：字母之间必须留 1 个空格，否则方块的边缘会连在一起，字母就糊成一团了。
    """
    lines = [""] * 5  # 5 行，先都留空
    first = True  # 标记"是不是第一个字母"
    for word_index, word in enumerate(_WORDS):  # 逐个单词
        for char in word:  # 逐个字母
            if not first:  # 不是第一个字母，先补一列空格当间距
                for row in range(5):
                    lines[row] += " "
            first = False
            glyph = _GLYPHS[char]  # 取出这个字母的 5 行字模
            for row in range(5):
                lines[row] += glyph[row]  # 一行一行接上去
        if word_index < len(_WORDS) - 1:  # 不是最后一个词，补上更宽的词间距
            for row in range(5):
                lines[row] += " " * _GAP
    return lines


_LOGO_LINES = _build_logo_lines()  # 只在启动时算一次
_LOGO_WIDTH = max(len(line) for line in _LOGO_LINES)  # 标题总宽度，用来算居中和渐变


def _logo_text(highlight_start: int = None, band: int = 9):
    """生成带渐变的标题（2026-11-06 第 5 轮新增）

    highlight_start：白色高光的起始列。传 None 就是纯渐变（动画的最后一帧）。
    band：高光带多宽。
    """
    pad = max(0, (console.width - _LOGO_WIDTH) // 2)  # 左边留白，让标题在终端里居中
    block = Text()
    for row, line in enumerate(_LOGO_LINES):  # 逐行
        if row > 0:
            block.append("\n")  # 除第一行外，每行前面先换行
        block.append(" " * pad)  # 左边留白
        for col, char in enumerate(line):  # 逐列
            if char == " ":  # 空格不用上色，直接补上
                block.append(" ")
                continue
            if highlight_start is not None and highlight_start <= col < highlight_start + band:
                block.append(char, style="bold white")  # 高光带：纯白，最亮
            else:
                block.append(char, style=f"bold {gradient_color(col, _LOGO_WIDTH)}")  # 渐变彩色
    return block


def _gradient_rule() -> Text:
    """生成一条渐变分隔线，两端带星形（2026-11-06 第 5 轮新增）"""
    rule = Text()
    inner = max(20, console.width - 6)  # 左右各留 3 个位置给符号和空格，别让右端换行
    rule.append("  ")
    rule.append("✦ ", style=f"bold {gradient_color(0, inner)}")  # 左端星形
    for i in range(inner):
        rule.append("─", style=gradient_color(i, inner, dim=True))  # 渐变的横线（压暗，不抢主体）
    rule.append(" ✦", style=f"bold {gradient_color(inner, inner)}")  # 右端星形
    return rule


def _star_strip() -> Text:
    """画一条星星装饰带（2026-11-06 第 5 轮新增）

    固定图案 ✦ · ✧ ·，不是随机撒点——随机每次启动都不一样，看起来像脏点，
    固定图案反而更像"设计过的装饰"。
    """
    pattern = ["✦", "·", "✧", "·"]  # 四种符号循环
    count = 19  # 一共几个符号
    strip = Text()
    for i in range(count):
        symbol = pattern[i % len(pattern)]
        color = gradient_color(i, count)  # 每个符号按位置取渐变
        if symbol == "·":  # 小点不发光，用暗色
            strip.append(symbol + " ", style=gradient_color(i, count, dim=True))
        else:  # 星形加粗发光
            strip.append(symbol + " ", style=f"bold {color}")
    return strip


def _readable_dir(path: str) -> str:
    """把路径显示得短一点（2026-11-06 第 4 轮新增）

    目录太长会把版面撑破，只保留最后两级就够了，比如 D:\\code\\claude → …/code/claude
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")  # 统一成 / 再切开
    if len(parts) <= 2:  # 短路径就原样显示
        return path
    return "…/" + "/".join(parts[-2:])  # 只留最后两段，前面用省略号


def _should_animate() -> bool:
    """判断要不要播动画（2026-11-06 第 5 轮新增）

    两种情况不播：
      1. 输出不是终端（比如被重定向到文件、被程序捕获）——动画会变成一堆乱码
      2. 用户明确设了 QUICKSTAR_NO_ANIM=1
    """
    if os.getenv("QUICKSTAR_NO_ANIM"):  # 用户主动关掉
        return False
    return console.is_terminal  # 只有真终端才播


def welcome():
    """画启动页：扫光标题 + 渐变分隔线 + 信息行（2026-11-06 第 5 轮重写）"""
    console.print()  # 顶部先空一行，别贴着终端边界
    console.print(_star_strip(), justify="center")  # 顶部星星装饰带
    console.print()
    if _should_animate():  # 真终端：播一段 0.4 秒的扫光
        with Live(console=console, auto_refresh=False, transient=False) as live:
            for start in range(-9, _LOGO_WIDTH + 9, 3):  # 高光从左边屏幕外扫到右边
                live.update(_logo_text(highlight_start=start), refresh=True)  # 画一帧
                time.sleep(0.025)  # 每帧停 25 毫秒
            live.update(_logo_text(highlight_start=None), refresh=True)  # 最后一帧：纯渐变
            time.sleep(0.08)
    else:  # 非终端：直接画最终样子
        console.print(_logo_text(highlight_start=None))

    tagline = Text("Claude Code 复刻 · MCP 客户端版", style=DIM)  # 一句话说明这是什么
    console.print(tagline, justify="center")  # 居中显示

    console.print()  # 和下面的分隔线拉开一点
    console.print(_gradient_rule())  # 上分隔线
    console.print()

    # ---- 信息行：模型 + 目录 -------------------------------------------------
    model = os.getenv("OPENAI_MODEL", "未配置")  # 当前用的模型
    info = Text()
    info.append("   模型  ", style=DIM)
    info.append(model, style=f"bold {ACCENT}")  # 模型名用强调色，方便一眼看清
    info.append("        目录  ", style=DIM)
    info.append(_readable_dir(os.getcwd()))
    console.print(info)

    # ---- 提示行：怎么用、什么时候会自动压缩 -----------------------------------
    threshold = os.getenv("COMPRESS_THRESHOLD", "0.8")  # 压缩阈值，默认 80%
    try:
        threshold_pct = f"{float(threshold) * 100:.0f}%"
    except ValueError:  # 环境变量写错了也不能让欢迎页崩
        threshold_pct = "80%"

    tips = Text()
    tips.append("   回车发送", style=DIM)
    tips.append("  ·  ", style=DIM)
    tips.append("Ctrl+C 退出", style=DIM)
    tips.append("  ·  ", style=DIM)
    tips.append(f"上下文 {threshold_pct} 自动压缩", style=DIM)
    console.print(tips)
    console.print()


async def main():
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎页
    conversation = Conversation()  # 先创建对话总管，后面收尾时还要用到它
    try:
        await conversation.start_conversation()
        # start_conversation() 是启动聊天，await 的意思是"在这儿等着，但等待的时候不占着 CPU"
    finally:
        # 【2026-11-06 第 2 轮新增】不管正常结束还是被 Ctrl+C 打断，都要收尾
        #   不收尾的话：MCP 那些外部服务器进程会留在后台变成孤儿进程，
        #   退出时还会打一段 "cancel scope" 的报错。
        await conversation.shutdown()


def cli():
    # 定义命令行入口函数。pyproject.toml 里配置的 quickstar 命令，指向的就是这个函数
    try:
        asyncio.run(main())
        # asyncio.run 是异步世界的"启动器"：它负责建立一个事件循环，把 main() 跑起来，跑完再收尾
    except KeyboardInterrupt:
        # 【2026-11-06 第 2 轮新增】用户按 Ctrl+C 时，别再打一大段红色堆栈吓人，说句话就好
        console.print()
        console.print(Text("  已退出 QuickStar。", style=DIM))


if __name__ == "__main__":
    # 这一行的意思是：如果这个文件是被"直接运行"的（而不是被别人 import 导入），才执行下面这行
    cli()  # 那么就直接启动程序
