# =============================================================================
# 文件：src/main.py
# 作用：整个程序的「大门」。你在命令行敲 quickstar 时，最先跑的就是这个文件。
#
# 【整个程序是怎么转起来的】
#   1. main.py 调用 welcome() 画一个欢迎框，然后唤醒 Conversation（对话总管）
#   2. Conversation 把「系统提示词」塞进消息列表，再等用户打字
#   3. Conversation 把消息发给大模型（api_client.py 负责真正的网络请求）
#   4. 如果模型说"我要用某个工具"，Conversation 就去找 ToolManager 执行
#   5. 工具跑完的结果再塞回消息列表，再发给大模型，循环往复，直到模型不再用工具
#   6. UI 那一层（src/ui/）只负责"显示"和"要输入"，不参与逻辑判断
#
# 想看懂代码，建议的阅读顺序是：
#   main.py → core/conversation.py → core/api_client.py → tools/tool_manager.py
#   → tools/base_tool.py → 其他具体工具 → ui/
# =============================================================================

import asyncio  # 导入 asyncio 工具箱：让程序在"等网络回复"的时候还能去干别的事（异步）
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天


# 终端里有一些"看不见的指令"，靠它来改颜色、改光标位置，下面这两行是最常用的两个
CSI = "\x1b["  # 控制序列的开始标记（终端一看到这个就知道"后面是指令，不是要显示的字"）
RESET = CSI + "0m"  # 颜色重置指令：把字体颜色恢复成默认，拼起来就是 \x1b[0m


def fg_256(n):
    # 定义一个小工具函数：生成"256 色终端"里的某一个前景色（就是字的颜色）
    # n 是色号，0-255 之间，比如 196 是正红，226 是亮黄
    return f"{CSI}38;5;{n}m"  # 拼出形如 \x1b[38;5;196m 的字符串，终端收到后就会把字改成这个颜色


def gradient_text(text, start=196, end=226):
    # 定义函数：把一串文字染成"从 start 色慢慢过渡到 end 色"的渐变色
    # start / end 是色号，写在括号里带等号的叫"默认参数"——不传就用这个值
    res = []  # 准备一个空列表，等会儿一个字符一个字符地往里塞
    L = len(text)  # 先数清楚这段文字一共有几个字符，后面算颜色要用
    for i, ch in enumerate(text):
        # enumerate 会同时给你两样东西：i 是"第几个"（从 0 开始），ch 是"这个字符本身"
        color = int(start + (end - start) * i / max(1, L - 1))
        # 上面这行是渐变的数学核心：
        #   i / (L-1) 算出"当前走到百分之几"，越往后越接近 1
        #   (end - start) 是颜色要跨越的总跨度，乘上进度就是"该加多少"
        #   max(1, L-1) 是防止文字只有 1 个字符时除以 0 报错
        res.append(fg_256(color) + ch)  # 给这个字符套上颜色指令，然后放进入列表
    return "".join(res) + RESET  # 把列表里所有碎片拼成一个完整字符串，末尾补上"恢复默认颜色"


def welcome():
    # 定义函数：画出启动时那个带星星的欢迎框
    border_color = fg_256(33)  # 横线用的颜色（33 是一种蓝色）
    star1 = fg_256(226) + "✦" + RESET  # 一颗实心星星：黄色 + 星星符号 + 恢复默认色
    star2 = fg_256(220) + "✧" + RESET  # 一颗空心星星：稍暗一点的黄色

    border = border_color + "═" * 50 + RESET  # 把"═"这个字符重复 50 次，拼成一条横线
    print("\n " + border)  # 先空一行（\n 是换行符），再打印上边框
    print("  " + (star1 + " ") * 5 + (star2 + " ") * 5)  # 打印第一排星星：5 颗实心 + 5 颗空心
    # 注意："(star1 + " ") * 5" 的意思是把"一颗星星+一个空格"这个组合复制 5 遍

    text = gradient_text("★ Welcome to Quick Star ★", 196, 226)  # 生成一段从红到黄的渐变文字
    print("\n" + text.center(55))  # 把这段文字居中排到 55 个字符宽，再打印出来

    print("\n  " + (star2 + " ") * 5 + (star1 + " ") * 5)  # 打印第二排星星，这次是空心在前
    print(" " + border + "\n")  # 打印下边框，最后再空一行，让后面的输入不贴着框


async def main():
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎框
    conversation = Conversation()  # 先创建对话总管，后面收尾时还要用到它
    try:
        await conversation.start_conversation()
        # start_conversation() 是启动聊天，await 的意思是"在这儿等着，但等着的时候不占着 CPU"
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
        print("\n已退出 QuickStar。")


if __name__ == "__main__":
    # 这一行的意思是：如果这个文件是被"直接运行"的（而不是被别人 import 导入），才执行下面这行
    cli()  # 那么就直接启动程序
