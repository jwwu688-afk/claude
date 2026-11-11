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
import sys  # 【第 11 轮】读命令行参数（--version / --continue / -c）
from core.conversation import Conversation  # 导入对话总管 Conversation 类，后面靠它启动聊天
from ui.theme import console, ACCENT, DIM, WIDTH, STAR, WARN  # 配色与符号（WARN_SIGN 已随主目录警告一起移除）
# 注：OK 只在已删除的 run_init 里用过，随它一起去掉了
from rich import box  # rich 的边框样式集合（这里用圆角那个）
from rich.panel import Panel  # 面板：带边框的一块区域
from rich.text import Text  # 富文本：可以给一段文字的不同部分上不同颜色

# 【第 10 轮】版本号显示在欢迎页标题里。
# 为什么要放这里：改完代码后如果用户没重新启动进程，看到的还是旧界面，
# 容易反复"改了没效果"。标题带版本号，一眼就能确认当前跑的是哪一份代码。
# 改界面时顺手升一位（与 pyproject.toml 的 version 保持一致）。
# 【第 18.8 轮】0.9.3 → 0.9.4：输入层改成受控分帧重绘（修真机"跨行退格留下最后一个字符"），
#   底部留白默认 2 → 4（修"滚轮只能往下滚"）。18.6 轮的增量补丁在 conhost 上被真机证伪，
#   所以这次不是再打一个补丁，而是把多行输入的渲染方式整体换掉。
# 【第 18 轮】0.8.0 → 0.9.0：子 agent 类型化（.thinker/agents/*.md + 工具白名单 + 模型覆盖）。
# 【第 17 轮】0.7.2 → 0.8.0：五层压缩（L2/L3/L4 三个新层）+ 压缩补测。小版本号进位理由：
#   这是一次"行为可见"的功能级变更，压缩会真的删消息/换视图，不是补丁。
# 【2026-11-10 第 21 轮】0.9.4 → 0.10.0：技能机制（.thinker/skills + skill 工具 + /技能名）。
#   进位理由同第 17 轮：这是一次功能级变更，而且是**纯增量**——
#   没有技能的用户看到的行为一字不变（工具不登记、提示词不加段、REGISTRY 里不多命令）。
# 【2026-11-10 第 21.7 轮】0.10.0 → 0.10.1：会话文件懒创建——启动不再立刻落盘，
#   第一条真正发给模型的消息（含技能斜杠命令，不含本地斜杠命令）才建新 JSONL。
#   末位进位理由：不新增能力，只是把第 13 轮"何时建文件"的时机改对。
VERSION = "0.10.2"  # 【第 21.7 轮】会话懒创建；启动时 banner 会显示，便于确认真机跑的是新代码


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
    console.print()  # 最后空一行，让输入提示符不贴着提示文字



async def main(resume_path=None):
    # 定义主函数。前面带 async 表示它是"异步函数"，里面有等网络的操作也不会卡住界面
    welcome()  # 先画欢迎框
    _print_skill_hint()  # 【第 21 轮】有技能就提一行，否则用户根本不知道这个功能存在
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


def _print_skill_hint():
    """有技能时在欢迎区下面提一行（2026-11-10 第 21 轮新增）

    为什么值得单独打一行：
      技能是纯文件驱动的功能——用户不建 .thinker/skills/ 目录，就永远看不到它，
      也就不会想到去建。这和第 16 轮 Tab 补全的情况一样：功能做得再好，
      不写在用户会看的地方等于没做。
      刻意放在欢迎框下面而不是框里：框里只有三行（标题/模型/目录），
      加进去会把那个干净的版式撑乱。

    扫描失败为什么只打一行、不让程序退出：
      技能是**附加功能**。目录读不了（权限、编码）时，用户的核心诉求
      "跟模型对话"完全不受影响，为此拒绝启动属于本末倒置。
      但也绝不能静默——静默降级等于把 bug 藏起来（第 16.1 轮的教训），
      所以往 stderr 打一行，带上异常类型。
    """
    try:
        from tools.skill.skill_manager import SkillManager

        count = SkillManager().count()
    except Exception as error:  # 扫描期的任何意外都不该拦住启动
        print(f"[skills] 扫描技能目录失败（已跳过）：{type(error).__name__}: {error}")
        return

    if count:
        console.print(
            Text(
                f"  已加载 {count} 个技能 · /skills 查看 · 输入 / 后按 Tab 可补全",
                style=DIM,
            )
        )


def cli():
    # 定义命令行入口函数。pyproject.toml 里配置的 thinker 命令，指向的就是这个函数
    argv = sys.argv[1:]

    # 【第 11 轮】先处理不进入对话的参数
    # 【2026-11-08 清理】原来这里还有 `--init` 分支，现在已经删掉：
    #   生成项目记忆统一走会话内的 `/init`（core/commands.py）。
    #   留两条入口只会让守卫、提示文案、测试都得维护两份，
    #   而"必须退出对话才能建记忆"本身就是个没必要的限制。
    #
    #   但直接把分支删掉会带来一个副作用：`thinker --init` 变成"未知参数被忽略"，
    #   于是它不再报错，而是直接进入对话——老用户会以为参数没生效、或者以为卡住了。
    #   所以留一条明确的迁移提示，让旧习惯有个落点。
    if "--init" in argv:
        console.print(Text("  `thinker --init` 已移除，请先启动 thinker，再在会话内执行 /init。", style=WARN))
        return

    if "--version" in argv or "-V" in argv:
        console.print(f"thinker v{VERSION}")
        return

    # 【第 13 轮】--continue：接着本项目最近一次会话聊
    resume_path = None
    if "--continue" in argv or "-c" in argv:
        resume_path = run_continue()

    # 【第 24 轮】这里原来调 register_skill_commands()，把技能登记成 /名字 命令。
    #   现在技能只由模型按需调用，启动期不需要碰磁盘：
    #     · 命令表是静态的，进入对话前就已经完整（内置命令在 import 时就登记好了）；
    #     · 技能清单由 SkillManager._ensure_scanned() 懒加载，第一次被查询
    #       （模型要工具清单、用户敲 /skills）时才扫目录。
    #   顺带的好处：启动不再因为技能目录读不了而变慢或报错，
    #   坏掉的技能文件也只在真正用到时以一行告警出现，而不是挡在启动路径上。

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
