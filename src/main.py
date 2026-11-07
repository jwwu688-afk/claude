# =============================================================================
# 文件：src/main.py
# 作用：整个程序的「大门」。你在命令行敲 thinker 时，最先跑的就是这个文件。
#
# 【整个程序是怎么转起来的】
#   1. main.py 画欢迎框，然后唤醒 Conversation（对话总管）
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
# 【2026-11-06 第 7 轮】欢迎页按 Claude Code 2.1 的实际样式重做：
#   - 圆角细边框 + 居中标题「✻ Welcome to Thinker」，用 Claude 品牌橙
#   - 框内只放三样：标题、模型+版本、当前目录
#   - 框下是一行暗灰提示，不再是上一轮那套彩色大字和扫光动画
#   （上一轮的做法可以从 git 历史里找回来，见第 5 轮提交）
# =============================================================================

import asyncio  # 导入 asyncio 工具箱：让程序在"等网络回复"的时候还能去干别的事（异步）
import os  # 用来读环境变量（拿模型名、压缩阈值）和当前目录
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui import theme  # 配色、符号、logo 都从这里取
from ui.theme import console, ACCENT, DIM  # 统一的控制台、Claude 橙、暗灰
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色


def _readable_dir(path: str) -> str:
    """把路径显示得短一点

    目录太长会把欢迎区撑得很宽，只保留最后两级就够了。
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")  # 统一成 / 再切开
    if len(parts) <= 2:  # 短路径就原样显示
        return path
    return "…/" + "/".join(parts[-2:])  # 只留最后两段，前面用省略号


def welcome():
    """画欢迎区（2026-11-07 第 10 轮按 Claude Code 2.1.296 实拍重做）

    改前的样子是一个居中的圆角框，标题在框里居中。
    实拍截图显示真正的 Claude Code 是"**没有框**"的：左边一个橙色方块 logo，
    右边紧挨着三行左对齐的文字（产品+版本 / 模型+计费 / 当前目录）。

    为什么不再用 Panel：
      框会把视觉重心拉到中心的标题上，而 Claude Code 的重心是"Logo + 环境信息"
      这一块整体。去掉框之后，下面所有输出都能从第 0 列开始，排版也更整齐。

    布局（三行文字对齐 logo 的前三行，第四行只有 logo）：
        ▄▄▄▄▄▄▄  ▄▄▄▄▄▄   Thinker v0.1.0
        ███████  ██████   spark-x2.6 · API Usage Billing
        ███████  ██████   …/code/thinker
        ▀▀▀▀▀▀▀  ▀▀▀▀▀▀
    """
    console.print()  # 顶部先空一行，别贴着终端边界

    model = os.getenv("OPENAI_MODEL", "未配置")  # 当前用的模型
    version = "0.1.0"  # 与 pyproject.toml 的 version 保持一致
    info_lines = [
        f"Thinker v{version}",  # 第一行：产品名 + 版本
        f"{model} · API Usage Billing",  # 第二行：模型 + 计费方式
        _readable_dir(os.getcwd()),  # 第三行：当前目录
    ]

    # 逐行拼：logo 第 i 行 + 间隔 + 第 i 行文字。
    # logo 有 4 行、文字只有 3 行，所以最后一行只画 logo。
    gap = " " * (theme.LOGO_GAP - 1)  # logo 和文字之间的空隙（少算 1 格，因为要补一个分隔空格）
    for index, logo_line in enumerate(theme.LOGO_LINES):
        line = Text()
        line.append(logo_line, style=f"bold {theme.ACCENT}")  # logo 整块用品牌橙
        if index < len(info_lines):  # 这一行还有文字要配
            line.append(gap)
            # 第一、二行是主体信息用默认色，第三行是路径用暗灰
            style = DIM if index == 2 else ""
            line.append(info_lines[index], style=style)
        console.print(line)

    console.print()  # logo 和下面的内容之间留一行
    # 底部状态栏不在这里画。截图里它出现在"输入框横线之下"，
    # 所以交给 InputHandler 在第一次输入完成后补上（见 input_handler._draw_status_bar）。



async def main():
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎框
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
    # 定义命令行入口函数。pyproject.toml 里配置的 thinker 命令，指向的就是这个函数
    try:
        asyncio.run(main())
        # asyncio.run 是异步世界的"启动器"：它负责建立一个事件循环，把 main() 跑起来，跑完再收尾
    except KeyboardInterrupt:
        # 【2026-11-06 第 2 轮新增】用户按 Ctrl+C 时，别再打一大段红色堆栈吓人，说句话就好
        console.print()
        console.print(Text("  已退出 Thinker。", style=DIM))


if __name__ == "__main__":
    # 这一行的意思是：如果这个文件是被"直接运行"的（而不是被别人 import 导入），才执行下面这行
    cli()  # 那么就直接启动程序
