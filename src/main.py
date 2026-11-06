# =============================================================================
# 文件：src/main.py
# 作用：整个程序的「大门」。你在命令行敲 quickstar 时，最先跑的就是这个文件。
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
#   - 圆角细边框 + 居中标题「✻ Welcome to QuickStar」，用 Claude 品牌橙
#   - 框内只放三样：标题、模型+版本、当前目录
#   - 框下是一行暗灰提示，不再是上一轮那套彩色大字和扫光动画
#   （上一轮的做法可以从 git 历史里找回来，见第 5 轮提交）
# =============================================================================

import asyncio  # 导入 asyncio 工具箱：让程序在"等网络回复"的时候还能去干别的事（异步）
import os  # 用来读环境变量（拿模型名、压缩阈值）和当前目录
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui.theme import console, ACCENT, DIM, WIDTH, STAR  # 统一的控制台、Claude 橙、暗灰、框宽、星号
from rich import box  # rich 的边框样式集合（这里用圆角那个）
from rich.panel import Panel  # 面板：带边框的一块区域
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色


def _readable_dir(path: str) -> str:
    """把路径显示得短一点

    目录太长会把框撑破，只保留最后两级就够了，比如 D:\\code\\claude → …/code/claude
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")  # 统一成 / 再切开
    if len(parts) <= 2:  # 短路径就原样显示
        return path
    return "…/" + "/".join(parts[-2:])  # 只留最后两段，前面用省略号


def welcome():
    """画欢迎框（2026-11-06 第 7 轮按 Claude Code 样式重写）

    整体结构和 Claude Code 一致：
        框里是对齐居中的标题和一两行环境信息，框外是一行暗灰的操作提示。
    刻意做得"空"一点——留白本身就是 Claude Code 的观感来源。
    """
    console.print()  # 顶部先空一行，别贴着终端边界

    model = os.getenv("OPENAI_MODEL", "未配置")  # 当前用的模型
    body = Text(justify="center")  # 全部内容居中
    body.append(f"{STAR} ", style=f"bold {ACCENT}")  # 星号用品牌橙
    body.append("Welcome to QuickStar", style=f"bold {ACCENT}")  # 标题也用一个颜色，不花
    body.append("\n\n")  # 标题和下面的信息之间留一行空白
    body.append(f"{model} · MCP 客户端版", style="")  # 第二行：模型 + 版本性质
    body.append("\n")
    body.append(_readable_dir(os.getcwd()), style=DIM)  # 第三行：当前目录（暗灰）

    box_width = min(WIDTH, max(40, console.width - 4))  # 框宽：不超过 WIDTH，也不超出终端
    console.print(
        Panel(
            body,
            box=box.ROUNDED,  # 圆角边框，就是 Claude Code 那个框的样子
            border_style=DIM,  # 边框用暗灰，不抢内容
            padding=(1, 4),  # 上下各空 1 行、左右各空 4 格
            width=box_width,
        )
    )

    # 框下面一行操作提示。Claude Code 在这里放 tips，我们只保留最必要的三条。
    threshold = os.getenv("COMPRESS_THRESHOLD", "0.8")  # 压缩阈值，默认 80%
    try:
        threshold_pct = f"{float(threshold) * 100:.0f}%"
    except ValueError:  # 环境变量写错了也不能让欢迎页崩
        threshold_pct = "80%"

    hint = Text()
    hint.append("  ")
    hint.append("输入问题开始", style=DIM)
    hint.append(" · ", style=DIM)
    hint.append("Ctrl+C 退出", style=DIM)
    hint.append(" · ", style=DIM)
    hint.append(f"上下文 {threshold_pct} 自动压缩", style=DIM)
    console.print(hint)
    console.print()  # 最后空一行，让输入提示符不贴着提示文字


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
