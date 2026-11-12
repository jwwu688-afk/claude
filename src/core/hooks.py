# =============================================================================
# 文件：src/core/hooks.py
# 作用：在会话生命周期的几个固定点上挂 shell 命令（第 32 轮新增）
#
# 【它是干什么的】
#   模型的行为是概率性的：你写"别动 .env"，它大多数时候听话，但"大多数时候"不是保证。
#   hook 是确定性的：事件一到，命令就跑，跑完按退出码决定"继续 / 拦下"。
#   两类典型用法：
#     · 拦截：PreToolUse 跑一个脚本，发现命令里有 rm -rf 就 exit 2，工具不会执行；
#     · 注入：SessionStart 跑 `git status --short`，输出进入系统提示词，
#             模型开局就知道工作区脏不脏，不用你再贴一遍。
#
# 【一次 PreToolUse 的完整时间线】
#   12:00:01  模型要求调用 cmd_runner({"command": "rm -rf /"})
#   12:00:01  Conversation 调 run_event("PreToolUse", payload, tool_name="cmd_runner")
#   12:00:01  payload 以 JSON 写进子进程 stdin
#   12:00:02  脚本读 stdin、判断、往 stderr 写"这是删除根目录"、exit 2
#   12:00:02  run_event 返回 blocked=True, reason=那行 stderr
#   12:00:02  工具**没有执行**；reason 作为工具结果回到模型
#   12:00:03  模型读到失败原因，改成 `rm -rf ./build` 再试
#
# 【和 Claude Code 的 hooks 对齐到什么程度】
#   对齐的部分（这样现成脚本能直接搬）：settings.json 结构、事件名、stdin 字段名
#   （session_id / cwd / hook_event_name / tool_name / tool_input）、退出码语义
#   （0 继续、2 阻断、其它非阻断错误）、matcher 语法（精确 / `a|b` / 正则）。
#
#   刻意不同的地方：
#     1. **只读用户级配置**（~/.thinker/settings.json）。项目级 `.thinker/settings.json`
#        等于"克隆一个仓库就能在你机器上执行 shell"，这个风险不值得为了团队共享去担。
#        项目里有配置文件时，本模块**不静默忽略**，而是记一条提示（见 load()）。
#     2. **串行执行、遇阻断即停**。Claude Code 并行跑完再合并（deny 优先）；
#        thinker 是单线程交互，并行只省几十毫秒，却让日志交错、没法"前一个的结果影响后一个"。
#     3. 只支持 type=command，不支持 prompt / agent 型（那要再调一次模型，属于另一套东西）。
#     4. 超时默认 30s（Claude Code 60s）：这里是"敲完命令等它跑"的交互场景，
#        30 秒还没结果，用户已经在怀疑是不是卡死了。
#
# 【为什么不在 ToolManager 里做】
#   ToolManager 属于 tools 层，hooks 属于 core 层。在 tools 里 import core.hooks
#   会让依赖方向反掉（tools → core），以后 core 想引用工具就会成环。
#   所以 hook 的触发点放在 Conversation（它两边都能看见）。
# =============================================================================

import asyncio  # 起子进程、等超时
import json  # 读写 hook 的输入输出
import locale  # 解码兜底：Windows 的 cmd 内建命令按系统代码页（中文是 GBK）输出
import os  # 环境变量、cwd
import re  # matcher 的正则匹配
import sys  # 判断平台：Windows 的解码候选编码和别的系统不一样
from dataclasses import dataclass, field  # 两个"只装字段"的小类
from pathlib import Path  # 配置文件路径
from typing import Any, Dict, List, Optional

# 只支持用户级配置。路径写死在这里，没有第二个来源（理由见文件头第 1 条）。
DEFAULT_SETTINGS_PATH = Path("~/.thinker/settings.json").expanduser()

# P1 支持的四个事件。名字必须和 settings.json 里的键**完全一致**（大小写敏感）：
# 写成 "pretooluse" 不会报错、也不会生效——所以 load() 遇到不认识的事件名会记一条提示。
SUPPORTED_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")

# 默认 30 秒、上限 300 秒。
# 上限不是拍脑袋：hook 卡住时用户唯一的出路是 Ctrl+C，不能让一条 `sleep 9999` 把会话拖死。
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0

# 【2026-11-12 第 32.1 轮】下面是三个"防事故"的上限，都是实测后补的。
#
# 输出上限：hook 的 stdout 会进上下文（SessionStart 进系统提示词，PreToolUse 进工具结果），
#   一个 `git log` 或 `npm ls` 能吐几千行。不截断的话，它会把你的上下文预算顶掉一大半，
#   而且每一轮都要为它重新付一次输入费用。4000 字符约等于 1500 token，够放下一屏摘要。
MAX_CONTEXT_CHARS = 4000
#
# 阻断原因的上限：它要显示给用户看，长了反而看不清。而且它是"失败信息"，
#   前面几十个字通常已经说明了问题（异常类型 + 第一行）。
MAX_REASON_CHARS = 1000
#
# 失败提示次数：一条写坏的 hook 会在**每次**工具调用前失败。不节流的话，
#   一次对话能刷出几十行"启动失败"，把真正有用的输出顶走。
#   3 次足够让用户注意到并有时间去看，又不会淹没界面。
MAX_ERROR_REPORTS = 3


class HookBlocked(Exception):
    """PreToolUse 拒绝执行时抛这个（第 32 轮新增）

    为什么用异常而不是返回标记值：
      并发路径（_execute_tools_parallel）里每条工具调用都在自己的协程里跑，
      抛异常能天然做到"只中断这一条、其他条照常完成"；
      返回标记值的话，调用方还得在每个地方额外判断一次返回值类型。
      异常最终会被现有的 `except Exception` 兜住，变成一条工具失败消息给模型——
      整条链路不用新增任何消息类型。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class HookSpec:
    """一条 hook 配置（settings.json 里 hooks 数组里的一个 handler）"""

    event: str  # 属于哪个事件
    command: str  # 要跑的 shell 命令
    matcher: str = ""  # 只对 Pre/PostToolUse 有意义；空 = 匹配全部
    timeout: float = DEFAULT_TIMEOUT  # 秒


@dataclass
class HookOutcome:
    """一次事件里所有 hook 跑完的汇总结果

    字段含义：
      blocked —— 有 hook exit 2（或返回 decision=block），调用方必须停手
      reason  —— 阻断原因，会作为工具结果/提示给模型看
      contexts—— 各 hook 的 stdout（非阻断），按执行顺序收好
      errors  —— 超时、非 0 非 2 的退出码等"不影响主流程但要说一声"的情况
    """

    blocked: bool = False
    reason: str = ""
    contexts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        """把所有非阻断输出拼成一段（没有就是空串）

        为什么用一个列表存、到这里才拼：
          一条事件可能匹配多个 hook（比如一个取 git status、一个取待办清单），
          它们的输出应该按配置顺序一起给模型，而不是后一个覆盖前一个。
          提前拼成字符串的话，"按条处理"变成"拼坏字符串"，调试时看不出是哪条 hook 说的。
        """
        return "\n".join(part for part in self.contexts if part).strip()


class HookRunner:
    """配置的读取者 + 命令的执行者

    一个进程一个实例，由 Conversation 在初始化时建好。配置文件只在建实例时读一次：
    改完 settings.json 要重启 thinker——和 mcp.json 一个规矩，不做热加载。
    （热加载会让"这一轮用的是旧 hook、下一轮是新 hook"，排查问题时说不清。）

    为什么不做成模块级单例（像 PromptManager 那样）：
      单例意味着"配置一旦读进来就全局固定"，测试要验证不同配置就得改全局状态。
      由 Conversation 持有、允许传 settings_path，测试可以给每个用例一份临时配置，
      互不干扰（test_hooks.py 就是这么跑的）。
    """

    def __init__(self, settings_path: Optional[str] = None, project_dir: Optional[str] = None):
        self.settings_path = Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS_PATH
        # project_dir 决定两件事：hook 子进程的 cwd、以及 THINKER_PROJECT_DIR 的值。
        # 用"启动 thinker 时的目录"而不是配置文件所在目录——hook 脚本里写的相对路径
        # （比如 `python .thinker/hooks/guard.py`）就是按这个目录找的。
        self.project_dir = Path(project_dir or os.getcwd())
        self.specs: List[HookSpec] = []
        self.notes: List[str] = []  # 加载期的问题（未知事件名、字段类型不对等），给启动时提示用
        # 【第 32.1 轮】每条 hook 已经报过几次错（键是 "事件:命令"）。
        #   为什么是实例级而不是模块级：换一份配置（测试里每个用例一份）就是个新的 runner，
        #   计数应该跟着配置走；进程重启也自然清零。
        self._error_counts: Dict[str, int] = {}
        self._load()

    # -------------------------------------------------------------------------
    # 加载

    def _load(self) -> None:
        """读 settings.json，把 hooks 解析成 HookSpec 列表

        容错的取舍：
          文件不存在 → 完全静默（hooks 是可选功能，没配的人不该每次启动都看到提示）；
          文件存在但读不懂 → 记一条 note，**不抛异常**（配置坏了不该让 CLI 起不来，
            这和 server_config 里"坏一条跳一条"是同一个原则）；
          单个 handler 写错 → 跳过这一条，其余照常加载。

        为什么整份读进来再解析，而不是边读边执行：
          执行发生在会话运行时（PreToolUse 要等工具调用），加载只发生一次。
          把"读配置"和"跑命令"彻底分开，才能做到"配置再烂也不会在启动期崩"。
        """
        if not self.settings_path.exists():
            return

        try:
            # utf-8-sig：Windows 记事本保存的带 BOM 文件，普通 utf-8 读出来首字符是 \ufeff
            text = self.settings_path.read_text(encoding="utf-8-sig")
        except OSError as error:
            self.notes.append(f"读不了 {self.settings_path}（{error}），本次不加载任何 hook")
            return

        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            # 带行号列号：手写 JSON 少个逗号时，只说"解析失败"等于让人自己数行
            self.notes.append(
                f"{self.settings_path.name} 不是合法 JSON（第 {error.lineno} 行第 {error.colno} 列），本次不加载任何 hook"
            )
            return

        if not isinstance(data, dict):
            self.notes.append(f"{self.settings_path.name} 最外层应该是 {{ }} 包裹的对象，本次不加载任何 hook")
            return

        hooks = data.get("hooks")
        if hooks is None:  # 文件存在但没有 hooks 键：可能是只用了别的设置，正常
            # thinker 暂时没有别的 settings 键，但保留这个分支是刻意的：
            # 以后加 "permissions" / "ui" 之类的配置时，用户可能只写那一段。
            # 那时这里会自然变成"只加载 hooks 部分"，不需要改判断。
            return
        if not isinstance(hooks, dict):
            self.notes.append("settings.json 里的 hooks 应该是 { } 包裹的对象，本次不加载任何 hook")
            return

        for event, groups in hooks.items():
            if event not in SUPPORTED_EVENTS:
                # 不认识的键十有八九是拼写错误（"PretoolUse"），也可能是抄了 Claude Code
                # 但我们没实现的事件（Stop / Notification）。两种都值得说一声。
                self.notes.append(f"不支持的事件 {event}（本版本只有 {' / '.join(SUPPORTED_EVENTS)}），已忽略")
                continue
            if not isinstance(groups, list):
                self.notes.append(f"{event} 应该是列表（用 [ ]），已忽略")
                continue
            for group in groups:
                self._load_group(event, group)

    def _load_group(self, event: str, group: Any) -> None:
        """解析一个 {matcher, hooks:[...]} 分组

        注意"分组"和"handler"是两层：matcher 写在分组上，命令写在 hooks 里。
        这个嵌套是 Claude Code 的结构，保留它才能让现成的 settings.json 直接可用，
        尽管对 thinker 来说（一个 matcher 只配一条命令时）确实多了一层括号。
        """
        if not isinstance(group, dict):
            self.notes.append(f"{event} 里的分组应该是 {{ }} 包裹的对象，已跳过")
            return

        matcher = group.get("matcher", "")
        if not isinstance(matcher, str):
            self.notes.append(f"{event} 的 matcher 应该是字符串，已跳过这一组")
            return

        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            self.notes.append(f"{event} 的分组里缺少 hooks 列表，已跳过")
            return

        for handler in handlers:
            if not isinstance(handler, dict):
                self.notes.append(f"{event} 里的 handler 应该是 {{ }} 包裹的对象，已跳过")
                continue

            # type 缺省按 command 处理：手写配置时少写一个字段就整条不生效，太苛刻了
            handler_type = handler.get("type", "command")
            if handler_type != "command":
                self.notes.append(f"{event} 的 handler 类型 {handler_type} 不支持（只有 command），已跳过")
                continue

            command = handler.get("command")
            if not isinstance(command, str) or not command.strip():
                self.notes.append(f"{event} 的 handler 缺少 command，已跳过")
                continue

            timeout = handler.get("timeout", DEFAULT_TIMEOUT)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                timeout = DEFAULT_TIMEOUT  # 写错就退回默认值，不因为一个数字废掉整条 hook
            timeout = min(float(timeout), MAX_TIMEOUT)

            self.specs.append(
                HookSpec(event=event, command=command.strip(), matcher=matcher.strip(), timeout=timeout)
            )

    # -------------------------------------------------------------------------
    # 匹配

    def specs_for(self, event: str, tool_name: Optional[str] = None) -> List[HookSpec]:
        """取出某个事件下、当前这次调用要跑的 hook（保持配置里的顺序）

        为什么保持顺序而不是按耗时/字母排：
          串行执行时，顺序决定"谁先看到这次调用"。日志类 hook 一般要放最前
          （即使后面的 hook 拦下了，日志里也该有记录），拦截类放后面。
          这个顺序只能由配置文件决定，代码替用户重排等于剥夺了这个控制权。
        """
        return [
            spec
            for spec in self.specs
            if spec.event == event and (tool_name is None or _matcher_matches(spec.matcher, tool_name))
        ]

    # -------------------------------------------------------------------------
    # 执行

    async def run_event(
        self,
        event: str,
        payload: Dict[str, Any],
        tool_name: Optional[str] = None,
    ) -> HookOutcome:
        """跑完这个事件下所有匹配的 hook，返回汇总结果

        payload 会**原样**以 JSON 写进每个子进程的 stdin（和 Claude Code 一致），
        所以一个脚本可以同时用于两边。这里不额外塞私有字段——脚本读不到预期字段时
        最容易的处理是"当空处理继续跑"，而私有字段会让它以为自己是给 thinker 专用的。
        """
        outcome = HookOutcome()

        for spec in self.specs_for(event, tool_name):
            await self._run_spec(spec, payload, outcome)
            if outcome.blocked:
                # 串行短路：已经有人喊停了，后面的 hook 不再执行。
                # 为什么不是"跑完再合并"（Claude Code 的做法）：见文件头"刻意不同的地方"第 2 条。
                break

        return outcome

    def _report_error(self, spec: HookSpec, outcome: HookOutcome, message: str) -> None:
        """记一条 hook 失败，并按次数节流（第 32.1 轮新增）

        为什么必须节流：
          一条写坏的 hook（语法错、路径写错）会在**每次**工具调用前失败。
          不节流的话，一次对话能刷出几十行完全相同的黄字，把真正有用的输出顶走，
          而且用户会逐渐学会无视这行提示——那比不提示更糟。
          每个"事件:命令"最多报 3 次，第 4 次改成一句"之后不再提示"，再往后彻底安静。
          注意：安静的是提示，失败本身照旧被吞掉（不阻断流程），行为不变。
        """
        key = f"{spec.event}:{spec.command}"
        count = self._error_counts.get(key, 0) + 1
        self._error_counts[key] = count

        if count <= MAX_ERROR_REPORTS:
            outcome.errors.append(message)
        elif count == MAX_ERROR_REPORTS + 1:
            outcome.errors.append(f"{message}\n（这条 hook 本次会话已失败 {count} 次，之后不再提示）")
        # 超过 MAX_ERROR_REPORTS + 1：什么都不加。计数继续涨，但不再打扰。

    async def _run_spec(self, spec: HookSpec, payload: Dict[str, Any], outcome: HookOutcome) -> None:
        """执行一条 hook，把结果**累积**进 outcome（不返回值，减少调用处的样板）

        这一步做四件事，顺序不能乱：
          1. 准备环境（cwd、THINKER_PROJECT_DIR、PYTHONIOENCODING）；
          2. 起子进程，把 payload 以 JSON 写进它的 stdin；
          3. 带超时地等它结束（超时就杀，不让一条 sleep 拖死会话）；
          4. 按退出码 + stdout 判定"放行 / 阻断 / 记个错误"。

        为什么用 shell 而不是直接 exec：
          配置里写的是"命令"（`python xxx.py`、`echo done`），需要 shell 解释。
          代价是命令必须按平台的 shell 语法写（Windows 是 cmd，不认 `$(...)`），
          这一点写进了 README 的注意事项里。
        """
        env = os.environ.copy()
        env["THINKER_PROJECT_DIR"] = str(self.project_dir)
        # 空 session_id 也照给：脚本里 `if session_id:` 就能判断"这次有没有会话"
        env["THINKER_SESSION_ID"] = str(payload.get("session_id") or "")
        # 让 Python 脚本统一按 UTF-8 输出。不设的话，Windows 上 print 中文走 GBK，
        # 而下面按 UTF-8 解码 → 输出变问号。cmd 内建命令（echo）不受这个变量影响，
        # 所以解码侧还有一套兜底（见 _decode_output）。
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            process = await asyncio.create_subprocess_shell(
                spec.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_dir),
                env=env,
            )
        except Exception as error:  # 命令本身不合法（引号没配对等）
            self._report_error(spec, outcome, f"{spec.event} hook 启动失败（{type(error).__name__}: {error}）：{spec.command}")
            return

        stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_data), timeout=spec.timeout)
        except asyncio.TimeoutError:
            # 超时不是"阻断"：一条记日志的 hook 卡住，不该把用户的工具调用也拦下来。
            # 但也不能静默——静默会让人以为 hook 生效了，实际根本没跑完。
            process.kill()
            await process.wait()
            self._report_error(spec, outcome, f"{spec.event} hook 超时（>{spec.timeout:g}s）：{spec.command}")
            return

        code = process.returncode
        stdout_text = _decode_output(stdout)
        stderr_text = _decode_output(stderr)

        # 先把 stdout 当 JSON 试一次：脚本可以用它做更细的控制（decision=block、additionalContext）
        decision, message = _interpret_stdout(stdout_text)

        if code == 2:
            # 退出码 2 = 阻断。原因优先用 stderr（错误信息该走错误通道），
            # 没有 stderr 就退回 stdout 里的话——总比给一句"被 hook 拦下了"却不说为什么强。
            outcome.blocked = True
            outcome.reason = _clamp_text(
                stderr_text or message or f"{spec.event} hook 拒绝了这次操作（exit 2）", MAX_REASON_CHARS
            )
            return

        if decision == "block":
            # 也支持 exit 0 + JSON {"decision": "block"}：Claude Code 的另一种写法。
            # 对齐它，是为了不让按官方示例写的脚本在 thinker 里变成"静默放行"。
            outcome.blocked = True
            outcome.reason = _clamp_text(message or f"{spec.event} hook 拒绝了这次操作", MAX_REASON_CHARS)
            return

        if code != 0:
            # 非 0 非 2：非阻断错误。记下来让 UI 提示一句，但流程继续。
            self._report_error(spec, outcome, f"{spec.event} hook 退出码 {code}：{stderr_text or spec.command}")
            return

        # 正常结束：把输出收进 contexts（SessionStart 会进系统提示词，
        # PreToolUse/PostToolUse 会附在工具结果里）
        if message:
            outcome.contexts.append(_clamp_text(message, MAX_CONTEXT_CHARS))
        if stderr_text:
            # exit 0 却写了 stderr：很多人用 stderr 打调试日志。不当错误，只当提示信息。
            outcome.contexts.append(_clamp_text(stderr_text, MAX_CONTEXT_CHARS))


def _clamp_text(text: str, limit: int) -> str:
    """把过长的 hook 输出截断，并在末尾说明截掉多少（第 32.1 轮新增）

    为什么是"加一句说明"而不是直接切片：
      截断而不告知，用户会以为 hook 就输出了这么多，排查时拿着一份缺尾的日志
      去对问题，永远对不上。多这十几个字，至少知道"后面还有东西"。

    为什么按字符而不是 token 算：
      这里不需要精确计费，只需要一个"不会失控"的上界；字符数零成本，
      token 要调分词器，而且不同模型口径不同。
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断 {len(text) - limit} 字符，完整内容请看 hook 自己的日志）"


def _decode_output(data: bytes) -> str:
    """把子进程的输出字节解成文本（去掉首尾空白）

    为什么不能直接 utf-8：
      Windows 上 `echo 中文` 由 cmd.exe 执行，它按**控制台代码页**输出（简体中文是 cp936）。
      按 utf-8 硬解会得到一串替换字符（����），用户看到的 hook 输出全是问号。
      而用户自己的 Python 脚本走 PYTHONIOENCODING=utf-8（见 _run_spec），是 UTF-8。

      所以顺序是：先严格 UTF-8（能过就一定是 UTF-8，不会误判），
      失败再按"控制台代码页"解。

    为什么不用 locale.getpreferredencoding(False)：
      实测在 Anaconda 的 Python 3.12 里它返回 "utf-8"（环境开了 UTF-8 模式），
      而 cmd.exe 输出的是 GBK —— 用它会解出一堆乱码。Windows 必须用 "oem"
      （控制台代码页）或 "mbcs"（ANSI 代码页），这两个才是 cmd 实际用的。
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        pass

    candidates = []
    if sys.platform == "win32":
        candidates += ["oem", "mbcs"]  # 控制台代码页 → ANSI 代码页
    candidates.append(locale.getpreferredencoding(False) or "utf-8")

    for encoding in candidates:
        try:
            return data.decode(encoding).strip()
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace").strip()


def _matcher_matches(matcher: str, tool_name: str) -> bool:
    """判断这次工具调用是否命中 matcher

    规则对齐 Claude Code：
      · 空字符串或 "*" → 匹配全部；
      · 只含字母数字下划线横线空格和 | → 精确匹配，`|` 分隔多个（"file_write|file_edit"）；
      · 其它情况按正则整串匹配（"mcp__.*"）。
    大小写敏感——这是和 Claude Code 一致的坑，写 "bash" 不会命中 "Bash"。
    在 thinker 里工具名是全小写的（file_write、cmd_runner），所以基本不会踩到。
    """
    if not matcher or matcher == "*":
        return True

    parts = [part.strip() for part in matcher.split("|")]
    if all(re.fullmatch(r"[A-Za-z0-9_\- ]+", part) for part in parts):
        return tool_name in parts

    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error:
        # 正则写错了：当成"不匹配"，并让它静默——加载期没法验证正则，
        # 真写错了用户会发现 hook 不生效，比每次启动报一条看不懂的正则错误好排查。
        return False


def _interpret_stdout(text: str):
    """把 hook 的 stdout 解析成（decision, message）

    返回的 decision 只有三种：
      None     —— 不是 JSON，整段当普通文本（message 即原文）
      "block"  —— 明确要求阻断（message 是原因）
      "text"   —— 是 JSON，但只是携带上下文（message 是 additionalContext / systemMessage）
    """
    if not text:
        return None, ""

    stripped = text.strip()
    if not stripped.startswith("{"):  # 绝大多数 hook 直接 echo 一段文字，走这条快路
        return None, stripped

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None, stripped  # 看起来像 JSON 但其实是普通文本（比如以 { 开头的日志）

    if not isinstance(data, dict):
        return None, stripped

    if data.get("decision") == "block":
        return "block", str(data.get("reason") or "")

    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("additionalContext"):
        return "text", str(specific["additionalContext"])

    if data.get("systemMessage"):
        # systemMessage 在 Claude Code 里是"显示给用户的警告"，这里当成上下文处理：
        # thinker 没有独立的通知通道，塞进上下文至少模型能看到。
        return "text", str(data["systemMessage"])

    return None, stripped
