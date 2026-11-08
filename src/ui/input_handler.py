# =============================================================================
# 文件：src/ui/input_handler.py
# 作用：管"从键盘拿输入"。程序里所有"等你打字"的地方，最终都走到这里。
#
# 有两件事：
#   1. get_user_input  —— 等你输入一句话
#   2. wait_for_user_approval —— 等用户按一次回车（或说一句不同意见）
#
# 为什么输入要用异步写法？
#   因为 input() 这个函数会一直卡着等人打字。
#   如果直接调用，整个程序（包括正在播的动画、正在收的数据）都会冻住。
#   所以这里把它丢到一个"线程池"里去等，主流程可以继续干别的。
# =============================================================================

"""
Input handling functionality for user interactions.（处理用户交互中的输入）
"""

import asyncio  # 用来做异步操作
import os  # 【第 10 轮】读压缩阈值，底部状态栏要显示
from typing import Optional  # 类型标注工具（表示"可能是这个值，也可能没有"）

from rich.text import Text  # 富文本：给横线染色

from . import theme  # 【2026-11-06 第 4 轮】输入提示符的样式从这里取
from .line_editor import LineEditor  # 【第 16 轮】Tab 补全用的自管行编辑
# 注：EXTENDED_PREFIX / KEY_CTRL_C 原本在这里被用来做"首字符分流"，
# 16.2 轮把分流整个删掉（改成整行自管）后，按键常量只在 line_editor 内部使用，
# 这里不再需要导入。


class InputHandler:
    """Handles all user input operations.（处理所有用户输入操作）"""

    def __init__(self):
        """Initialize the input handler.（初始化，这里没什么要做的）"""
        self._console = theme.console  # 【第 10 轮】画输入框横线要用
        self._status_shown = False  # 【第 10 轮】底部状态栏只画一次
        # 【第 16 轮】Tab 补全用的行编辑器。
        #   在这里就建好而不是用到时再建：它的 available 判断要看 msvcrt 和 isatty，
        #   启动时算一次就够了，不用每次输入都重新 import 一遍。
        self._editor = LineEditor()

    def _draw_rule(self) -> None:
        """画输入框下方那条横线（2026-11-07 第 10 轮新增）

        截图里每轮输入下面都有一条贯穿窗口的暗灰细线，作用是"把这一轮输入和
        后面的输出分开"。没有它，用户输入和模型回复之间只有空行，长对话里
        很难一眼看出"我上一次说的是哪句"。

        宽度取终端当前宽度；取不到就退化成 60 列，绝不因为宽度异常抛错。

        【16.1 轮补了 flush】这条横线和下面的状态栏画完必须立刻冲出缓冲区。
          原因：紧接着就是下一次 input()，而 input() 是直接往 sys.stdout 写的。
          如果 rich 的内容还压在缓冲里，用户看到的顺序会变成
          "输入行 → 横线 → 状态栏"，看起来就像状态栏（那个橙色 ▸▸）不见了
          或者跑到了输入框上面。这类错乱只在特定时序下出现，极难复现，
          所以不靠"rich 大概会自己 flush"，直接显式刷。
        """
        try:
            width = max(20, self._console.width - 1)
        except Exception:  # 某些非交互环境拿不到宽度
            width = 60
        self._console.print(Text("─" * width, style="qs.dim"))
        if not self._status_shown:  # 第一次输入完成才画状态栏，位置正好在横线下面
            self._status_shown = True
            self._draw_status_bar()
        # 显式刷出缓冲区，理由见本方法文档字符串里【16.1 轮补了 flush】那段。
        # 用 _console.file 而不是 sys.stdout：rich 可能把输出接到别处（比如测试里
        # 换成 StringIO），刷错对象等于没刷。拿不到 file 也不报错。
        try:
            self._console.file.flush()
        except Exception:
            pass

    def _draw_status_bar(self) -> None:
        """画输入框下面那行黄色状态提示（2026-11-07 第 10 轮新增）

        截图原文是 `▸▸ auto mode on (shift+tab to cycle) · ← for agents`。
        那两项对应"权限模式切换"和"agent 面板"，本项目都还没有，
        照抄会变成按了没反应的假按钮——所以保留字形和配色，
        内容换成本项目真实存在的操作提示。

        只画一次：它是"底栏"，不是每轮都要重复的日志。
        """
        threshold = os.getenv("COMPRESS_THRESHOLD", "0.8")
        try:
            threshold_pct = f"{float(threshold) * 100:.0f}%"
        except ValueError:  # 环境变量写错也不能让界面崩
            threshold_pct = "80%"

        line = Text()
        line.append(f"{theme.ARROWS} ", style=f"bold {theme.WARN}")
        line.append("默认模式", style=theme.WARN)
        line.append(f" {theme.DOT} ", style="qs.dim")
        # 【第 18.3 轮】补上输入层的真实能力提示：↑↓ 翻历史、←→ 改句子中间，
        #   这些是第 18 / 18.1 轮加的功能，不写出来用户根本不知道能用。
        #   只写真实存在的，不照抄截图里"auto mode / for agents"那些没实现的假按钮。
        line.append("↑↓ 历史", style="qs.dim")
        line.append(f" {theme.DOT} ", style="qs.dim")
        line.append("←→ 编辑", style="qs.dim")
        line.append(f" {theme.DOT} ", style="qs.dim")
        line.append("ctrl+c 退出", style="qs.dim")
        line.append(f" {theme.DOT} ", style="qs.dim")
        line.append(f"上下文 {threshold_pct} 自动压缩", style="qs.dim")
        self._console.print(line)

    async def get_user_input(self, prompt: str = None, completer=None) -> str:
        """
        Get user input asynchronously.（异步获取用户输入）

        Args:
            prompt: The prompt to display to the user（显示给用户看的提示文字）
            completer: 候选来源，给 Tab 补全用（2026-11-08 第 16 轮新增）。
                       None 表示不要补全，走原来的 input()，行为和以前完全一样。

        Returns:
            User input as string（用户输入的字符串）
        """
        # 【2026-11-06 第 4 轮】改成一个橙色的 > 提示符，并去掉原来单独一行的 👤
        #   原来每次输入前都打一个 emoji，屏幕上会堆一排人头，很乱。
        # 【2026-11-07 第 10 轮】用户输入完补一条横线，对齐 Claude Code 的输入框。
        #   只在"用默认提示符"时画——授权提问那种缩进的小提示不画线。
        draw_rule = prompt is None
        if prompt is None:  # 调用方没指定提示符
            prompt = theme.prompt_text()  # 用统一的 >
        loop = asyncio.get_event_loop()  # 拿到当前的"事件循环"（异步任务的总调度台）
        # 【第 16 轮】原来这里直接 run_in_executor(None, input, prompt)。
        #   现在改走 _read_line：它内部会先探一个字符，只有以 "/" 开头才进自管模式，
        #   其余情况仍然调 input()——也就是说普通对话（尤其是中文输入）
        #   走的是完全没变的那条老路。
        answer = await loop.run_in_executor(None, self._read_line, prompt, completer)
        if draw_rule:
            self._draw_rule()
        return answer

    def _read_line(self, prompt: str, completer) -> str:
        """在线程池里执行的"读一行"（2026-11-08 第 16 轮新增，16.2 轮重写）

        为什么这个函数是同步的、且必须在线程池里跑：
          它内部会阻塞等键盘。放在事件循环里会冻住整个程序（动画、流式输出全停），
          这正是原来那行 run_in_executor 要解决的问题。

        【16.2 轮：把"先探一个字符"整个删掉了】
          16 轮的做法是：先读一个字符看是不是 "/"，不是就交回 input()。
          为了让那个字符"看起来还在屏幕上"，只能拼进提示符：

              input(prompt + first)      # 第一个字变成提示符的一部分

          后果是删不掉（prompt 不属于可编辑区），而且丢字（返回值不含它）。
          16.1 轮想用 msvcrt.ungetch 把字符塞回缓冲区，实测也不行：
            · ungetch 要 bytes（b"a"），传 str 直接 TypeError；
            · 宽字符版 ungetwch 对中文抛 OSError。
          异常被 except 吞掉后静默退回有 bug 的老写法，所以症状一点没变。

          现在改成：**只要需要补全，整行都由 LineEditor 自己读**。
          没有"交给 input() 一部分"这回事，也就没有"字符要不要还回去"的问题。
          提示符也由 LineEditor 自己写，屏幕上所有字符出自同一处，
          rich 和 input() 抢输出顺序、把 `>` 盖掉的情况也不会再发生。
        """
        # 三种情况仍然走原来的 input()：
        #   1. 调用方不需要补全（授权提问、选会话编号）——没必要为它担自管的风险；
        #   2. 用户显式要求用普通输入（逃生开关，见下）；
        #   3. 环境不支持（非 Windows / 不是终端 / 输出被重定向）。
        if (
            completer is None
            or os.getenv("THINKER_PLAIN_INPUT") == "1"
            or not self._editor.available
        ):
            return input(prompt)
        # THINKER_PLAIN_INPUT=1 的用途：万一自管模式在某个终端/输入法下表现异常
        # （比如中文打不出来），用户设这个变量就能立刻回到原生 input()，
        # 代价只是没有 Tab 补全。留这个口子，是因为真机行为我在测试环境验证不了。
        return self._editor.readline(prompt, completer)

    # 【2026-11-06 第 8.2 轮】"同意"和"拒绝"的精确词表。
    #   为什么不用原来那种 "yes" in text 的子串匹配？
    #     输入 "I don't know" 里有 "no"，会被当成拒绝；
    #     输入 "not sure" 也一样；反过来 "yes but..." 会被当成干脆同意。
    #     这个坑第 1 轮文档里就记过，本轮随授权交互重做一起修掉。
    YES_WORDS = {"y", "yes", "是", "确认", "确定", "好", "可以", "行", "ok", "okay", "允许", "同意"}
    NO_WORDS = {"n", "no", "否", "不", "不要", "不用", "取消", "cancel", "拒绝", "算了"}

    @classmethod
    def interpret_approval(cls, raw: str, default_yes: bool = True) -> tuple[bool, str]:
        """把用户那一行输入解释成（是否执行, 拒绝理由）（2026-11-06 第 8.2 轮新增）

        规则只有三条，谁都能一眼记住：
          1. 空输入（直接回车）  → 按默认值走。默认同意，所以回车就是"执行"
          2. 命中同意/拒绝词表   → 照词表来，没有理由
          3. 其他任何内容        → 拒绝，并把这段内容当作"我想怎么改"交给模型

        第 3 条是最有用的一条：用户想说"别删这个文件，用 file_edit"时，
        不用先选 no、再等系统问一遍理由，一句话说完就完事。
        这也是 Claude Code 的做法——除了同意，剩下的输入都是反馈。

        单独抽成纯函数是为了能测：它不读键盘、不打印，传字符串就能验。
        """
        text = (raw or "").strip()  # 去掉首尾空格，避免"手滑多打一个空格"被判成有内容
        if not text:  # 直接回车
            return default_yes, ""
        low = text.lower()  # 统一小写，让 YES / Yes / yes 都能认
        if low in cls.YES_WORDS:
            return True, ""
        if low in cls.NO_WORDS:
            return False, ""
        return False, text  # 其他内容：拒绝 + 当作说明

    async def wait_for_user_approval(self, default_yes: bool = True) -> tuple[bool, str]:
        """等用户对某个操作点头（2026-11-06 第 8.2 轮重写）

        改前：必须打字回 yes 或 no，判断用子串匹配，误判率高。
        改后：只读一行，回车即默认，其余交给 interpret_approval 解释。
              提问本身由 DisplayManager.approval_question 画（见那里的说明）。

        参数 default_yes：这次提问"什么都不填"时算同意还是拒绝。
          目前调用方一律传 True（回车即执行）。将来权限引擎按风险分级时，
          高危操作会传 False，那样回车就变成"拒绝"，必须打 y 才放行——
          接口留在这里，第 9 轮不用再改签名。
        """
        answer = await self.get_user_input("  " + theme.prompt_text())  # 缩进两格，和 ⏺ 头部对齐
        return self.interpret_approval(answer, default_yes)

    async def get_choice_input(self, prompt: str, choices: list, case_sensitive: bool = False) -> Optional[str]:
        """
        Get user input with predefined choices.（让用户从几个固定选项里选一个）

        Args:
            prompt: The prompt to display（提示文字）
            choices: List of valid choices（可选的选项列表）
            case_sensitive: Whether the choice matching is case sensitive（是否区分大小写）

        Returns:
            The chosen option or None if invalid（选中的项，或者 None 表示没选对）
        """
        print(prompt)  # 打印提示
        print(f"Options: {', '.join(choices)}")  # 把选项用逗号连起来显示，比如 "Options: a, b, c"

        user_input = await self.get_user_input()  # 等输入

        if not case_sensitive:  # 如果不区分大小写
            user_input = user_input.lower()  # 把输入转小写
            choices = [choice.lower() for choice in choices]  # 把选项也都转小写（列表推导式）

        if user_input in choices:  # 如果输入在选项里
            return user_input  # 返回这个选项
        return None  # 不在就返回 None

    async def get_numbered_choice(self, title: str, items: list, hint: str = "输入编号，直接回车取消") -> Optional[int]:
        """列一个带编号的清单，让用户输数字选一项（2026-11-07 第 14 轮新增）

        为什么不复用上面的 get_choice_input：
          那个的语义是"选项本身就是答案"（a / b / c），要求输入必须命中选项；
          而 /resume 的选项是"一整行会话摘要"，不可能让人照着敲一遍。
          这里只负责把行号翻译成下标，调用方自己知道下标对应哪个对象。

        返回 0 开始的下标；回车、非数字、超出范围一律返回 None（当作取消）。
        为什么不抛异常：取消是完全正常的操作，不该逼调用方写 try/except。
        """
        print(title)  # 标题
        for i, item in enumerate(items, 1):  # 从 1 开始编号，更符合人的习惯
            print(f"  {i}. {item}")

        raw = await self.get_user_input(hint)  # 等一行输入（提示文字走统一的 ❯）
        text = (raw or "").strip()
        if not text.isdigit():  # 回车、字母、带空格的数字都算取消
            return None

        index = int(text) - 1  # 界面上是 1 开始，内部下标是 0 开始
        if 0 <= index < len(items):  # 越界也当取消，不报错
            return index
        return None
