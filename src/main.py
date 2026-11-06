# =============================================================================
# 文件：src/main.py
# 作用：整个程序的「大门」。你在命令行敲 quickstar 时，最先跑的就是这个文件。
#
# 【整个程序是怎么转起来的】
#   1. main.py 画欢迎面板，然后唤醒 Conversation（对话总管）
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
# 【2026-11-06 第 4 轮】欢迎页从"字符画星星"换成了 rich 面板，
#   配色和符号统一由 ui/theme.py 提供，风格向 Claude Code 靠。
# =============================================================================

import asyncio  # 导入 asyncio 工具箱：让程序在"等网络回复"的时候还能去干别的事（异步）
import os  # 用来读环境变量（拿模型名、压缩阈值）和当前目录
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui.theme import console, ACCENT, DIM, WIDTH  # 统一的控制台、配色常量、面板宽度
from rich.panel import Panel  # rich 的面板：带圆角边框的一块区域
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色


def _readable_dir(path: str) -> str:
    """把路径显示得短一点（2026-11-06 第 4 轮新增）

    目录太长会把面板撑破，只保留最后两级就够了，比如 D:\\code\\claude → …/code/claude
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")  # 统一成 / 再切开
    if len(parts) <= 2:  # 短路径就原样显示
        return path
    return "…/" + "/".join(parts[-2:])  # 只留最后两段，前面用省略号


def welcome():
    """画启动面板：名字、版本、模型、目录、快捷键（2026-11-06 第 4 轮重写）"""
    body = Text()  # 面板里的正文，一段一段往上拼

    # 第一行：品牌名。✻ 是 Claude 系的标志性符号，用强调色加粗
    body.append("✻ ", style=f"bold {ACCENT}")
    body.append("QuickStar", style=f"bold {ACCENT}")
    body.append("  CLI", style=f"{DIM}")
    body.append("\n")

    # 第二行：一句话说明这是什么
    body.append("Claude Code 复刻 · MCP 客户端版", style=DIM)
    body.append("\n\n")

    # 第三、四行：当前运行的模型和目录，方便确认"我在哪儿跑、用的什么模型"
    body.append("模型  ", style=DIM)
    body.append(os.getenv("OPENAI_MODEL", "未配置"), style="")
    body.append("\n")
    body.append("目录  ", style=DIM)
    body.append(_readable_dir(os.getcwd()), style="")

    console.print(
        Panel(
            body,
            border_style=DIM,  # 边框用暗灰，不抢内容
            padding=(1, 2),  # 上下各 1 行、左右各 2 格内边距
            width=WIDTH,
        )
    )

    # 面板下面一行小字：告诉用户怎么开始、怎么退出
    threshold = os.getenv("COMPRESS_THRESHOLD", "0.8")  # 压缩阈值，默认 80%
    try:
        threshold_pct = f"{float(threshold) * 100:.0f}%"
    except ValueError:  # 环境变量写错了也不能让欢迎页崩
        threshold_pct = "80%"
    hint = Text()
    hint.append("  回车发送", style=DIM)
    hint.append(" · ", style=DIM)
    hint.append("Ctrl+C 退出", style=DIM)
    hint.append(" · ", style=DIM)
    hint.append(f"上下文 {threshold_pct} 自动压缩", style=DIM)
    console.print(hint)
    console.print()  # 最后空一行，让输入提示符不贴着面板


async def main():
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎面板
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
