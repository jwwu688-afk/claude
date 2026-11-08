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
import sys  # 【第 11 轮】读命令行参数（--init / --version）
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui.theme import console, ACCENT, DIM, WIDTH, STAR, OK, WARN, WARN_SIGN  # 配色与符号
from rich import box  # rich 的边框样式集合（这里用圆角那个）
from rich.panel import Panel  # 面板：带边框的一块区域
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色

# 【第 10 轮】版本号显示在欢迎页标题里。
# 为什么要放这里：改完代码后如果用户没重新启动进程，看到的还是旧界面，
# 容易反复"改了没效果"。标题带版本号，一眼就能确认当前跑的是哪一份代码。
# 改界面时顺手升一位（与 pyproject.toml 的 version 保持一致）。
VERSION = "0.3.2"


def _readable_dir(path: str) -> str:
    """把路径显示得短一点

    目录太长会把欢迎区撑得很宽，只保留最后两级就够了。
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")  # 统一成 / 再切开
    if len(parts) <= 2:  # 短路径就原样显示
        return path
    return "…/" + "/".join(parts[-2:])  # 只留最后两段，前面用省略号


def welcome():
    """画欢迎框（2026-11-07 第 10 轮改回第 7 轮的圆角框版）

    第 9 轮曾按 Claude Code 2.1.296 的实拍截图改成"橙色方块 logo + 三行左对齐"。
    但终端只能用 █▄▀ 拼形状，实际渲染出来就是两个橙色大方块，很难看
    （截图见 docs/优化记录/10-第10轮-等待动画与界面还原.md 的教训一节）。
    Claude Code 那个 logo 是矢量图，字符画还原不了，所以退回圆角框版：
    观感干净、信息密度合适，也不需要假装自己有个图形 logo。

    框里三行：✻ Welcome to Thinker / 模型 + 版本性质 / 当前目录。
    框外一行暗灰提示。
    """
    console.print()  # 顶部先空一行，别贴着终端边界

    model = os.getenv("OPENAI_MODEL", "未配置")  # 当前用的模型
    body = Text(justify="center")  # 全部内容居中
    body.append(f"{STAR} ", style=f"bold {ACCENT}")  # 星号用品牌橙
    body.append(f"Welcome to Thinker v{VERSION}", style=f"bold {ACCENT}")  # 标题带版本号，方便确认跑的是哪份代码
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
    _warn_if_home()
    console.print()  # 最后空一行，让输入提示符不贴着提示文字


def _warn_if_home():
    """在主目录启动时给一句提醒（2026-11-08 第 14.1 轮新增）

    【为什么要在启动时提醒，而不是等用户执行 /init 才说】
      这个错误一旦发生，代价不止一个文件：项目记忆、会话归属、
      文件工具的工作区全都会以主目录为根。用户往往要到
      "为什么它改不了我的项目文件"、"为什么记忆里没有我写的约定"时才发现，
      而那时候已经白聊了好几轮。
      在门口说一句，比在出事后解释便宜得多。

    【为什么放在框下面，而不是塞进欢迎框】
      框里固定三行（标题、模型、目录），加一行会把它撑高，
      标题就不再垂直居中了——第 10 轮调这个框调了很久，不破坏它。

    【为什么判据是"主目录"而不是"看起来不像项目"】
      后者会误伤空目录、单脚本目录、只在某个子目录里局部工作的场景。
      主目录是唯一一个"在这儿生成 THINKER.md 几乎肯定错了"的位置（见
      core/prompt/memory.py 的 is_user_home 注释）。
    """
    from core.prompt.memory import is_user_home  # 放函数内：只有需要时才导入

    if not is_user_home():
        return

    console.print(Text(f"  {WARN_SIGN} 当前在用户主目录启动。", style=f"bold {WARN}"))
    console.print(Text("    项目记忆、会话归属、文件工具的工作区都会以这里为根，", style=DIM))
    console.print(Text("    它改不到别的盘里的项目文件。要处理某个项目，先 cd 过去再启动。", style=DIM))



async def main(resume_path=None):
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎框
    conversation = Conversation()  # 先创建对话总管，后面收尾时还要用到它
    try:
        await conversation.start_conversation(resume_path)
        # start_conversation() 是启动聊天，await 的意思是"在这儿等着，但等待的时候不占着 CPU"
        # 【第 13 轮】多了个参数：传会话文件路径表示"接着上次聊"，传 None 表示全新开始
    finally:
        # 【2026-11-06 第 2 轮新增】不管正常结束还是被 Ctrl+C 打断，都要收尾
        #   不收尾的话：MCP 那些外部服务器进程会留在后台变成孤儿进程，
        #   退出时还会打一段 "cancel scope" 的报错。
        await conversation.shutdown()


def run_continue():
    """找到本项目最近的一次会话，返回它的文件路径（2026-11-07 第 13 轮）

    为什么限定"本项目"：在 A 项目里说 --continue，接下的是 A 上次的话题。
    如果跨项目恢复，模型会拿着一堆另一个项目的上下文，反而更糊涂。
    """
    from core.session import SessionStore

    path = SessionStore.latest(os.getcwd())
    if path is None:
        console.print(Text("  没有找到可恢复的会话，这次从头开始。", style=WARN))
    else:
        console.print(Text(f"  接着上次的会话：{path.name}", style=DIM))
    return path


def run_init(force: bool = False, scope: str = "project", here: bool = False):
    """生成 THINKER.md 骨架（2026-11-07 第 11 轮新增，同日补了 --global）

    用法：
        thinker --init             项目记忆（写到当前目录）
        thinker --init --global    全局记忆（写到 ~/.thinker/）
        thinker --init --here      允许写在用户主目录（默认拒绝）

    这是 Claude Code 里 `/init` 的等价物。Claude Code 的 `/init` 只生成项目级，
    全局那份要用户自己在 `/memory` 里建——这里直接给个参数，省掉那一步。

    挂成命令行参数而不是斜杠命令，是因为斜杠命令系统还没做，这一步不该等它。
    （第 14 轮已经有了 /init，两条路径共用下面的 write_init_file 和同一套守卫。）
    """
    from core.prompt.memory import GLOBAL_MEMORY_PATH, MEMORY_FILENAME, is_user_home, write_init_file

    # 【第 14.1 轮】和 /init 用同一个守卫：主目录里默认不生成"项目记忆"。
    #   两边规则必须一致，否则用户在 CLI 被拦、进到会话里又能写，会更糊涂。
    if scope == "project" and is_user_home() and not here:
        console.print(Text(f"  {WARN_SIGN} 当前目录是用户主目录：{os.getcwd()}", style=f"bold {WARN}"))
        console.print(Text("  在这里生成的 THINKER.md 会成为所有「从主目录启动」的会话的项目记忆。", style=DIM))
        console.print(Text("  请先 cd 到项目目录再执行 --init；确实要写在这里请加 --here。", style=DIM))
        return

    if scope == "global":
        target = str(GLOBAL_MEMORY_PATH.expanduser())
        what = "全局记忆"
        next_step = "按实际习惯改「说话方式」「动手习惯」，尤其是那几条红线（不要自动提交）。"
    else:
        target = os.path.join(os.getcwd(), MEMORY_FILENAME)
        what = "项目记忆"
        next_step = "补充「代码约定」「不要做的事」两节，模型每轮都会读到。"

    existed = os.path.exists(target)
    path = write_init_file(force=force, scope=scope)

    if existed and not force:
        console.print(Text(f"  {what}已存在，没有覆盖：{path}", style=WARN))
        console.print(Text("  想重新生成请加 --force", style=DIM))
        return

    console.print(Text(f"  已生成{what}：{path}", style=OK))
    console.print(Text(f"  {next_step}", style=DIM))


def cli():
    # 定义命令行入口函数。pyproject.toml 里配置的 thinker 命令，指向的就是这个函数
    argv = sys.argv[1:]

    # 【第 11 轮】先处理不进入对话的参数
    if "--init" in argv:
        run_init(
            force="--force" in argv,
            scope="global" if "--global" in argv else "project",
            here="--here" in argv,  # 【第 14.1 轮】主目录里要写，得显式给这个
        )
        return
    if "--version" in argv or "-v" in argv:
        console.print(f"thinker v{VERSION}")
        return

    # 【第 13 轮】--continue：接着本项目最近一次会话聊
    resume_path = None
    if "--continue" in argv or "-c" in argv:
        resume_path = run_continue()

    try:
        asyncio.run(main(resume_path))
        # asyncio.run 是异步世界的"启动器"：它负责建立一个事件循环，把 main() 跑起来，跑完再收尾
    except KeyboardInterrupt:
        # 【2026-11-06 第 2 轮新增】用户按 Ctrl+C 时，别再打一大段红色堆栈吓人，说句话就好
        console.print()
        console.print(Text("  已退出 Thinker。", style=DIM))


if __name__ == "__main__":
    # 这一行的意思是：如果这个文件是被"直接运行"的（而不是被别人 import 导入），才执行下面这行
    cli()  # 那么就直接启动程序
