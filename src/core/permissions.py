# =============================================================================
# 文件：src/core/permissions.py
# 作用：判断"一次工具调用要不要先问用户"。所有权限判断都集中在这里。
#
# 【为什么必须集中、而且不能听模型的】（2026-11-06 第 9 轮）
#   第 8 轮的做法是：模型在参数里写 need_user_approve，写 false 就直接执行。
#   这个设计有个致命问题——**被判断的人自己当裁判**。
#   模型完全可以（也经常）为了"少打扰用户"，给一条 `del /s /q` 也标上 false，
#   于是那条命令一声不响地跑了。这不是模型坏，是设计给错了权力。
#
# 【现在三条规则，按顺序判】
#   1. 命中危险模式           → 必须问，而且默认回车是"拒绝"
#                              （模型写 false 也不行，这条不可被降级）
#   2. 只读工具 / 只读命令    → 不问，直接执行
#   3. 其余一切               → 必须问
#
#   模型仍然可以"加严"：对只读工具显式写 true，我们照样弹确认。
#   这叫"只能收紧，不能放松"——权限系统的基本要求。
#
#   ⚠️ 唯一例外：执行命令（cmd_runner）时，模型的 need_user_approve **完全不生效**。
#      只有"命令在白名单里"才免确认，否则一律要问。理由见下面 classify() 里的注释。
#      代价是会多问几下；换来的是"模型不能给自己的命令发通行证"这条底线。
#
# 【这一轮只做"最小可用"】
#   完整的权限引擎（用户自定义规则文件、工作区路径边界、"本次会话总是允许"
#   的记忆、按工具粒度的持久化白名单）留给第 10 轮。但"模型不能自己给自己
#   放行"这条底线，必须先落地。
# =============================================================================

import re  # 用正则匹配危险命令
from dataclasses import dataclass  # 装判断结果的小容器


# 只读工具：不改磁盘、不启进程，没必要每次都打断用户
READ_ONLY_TOOLS = {
    "file_read", "file_glob", "file_grep", "todo_write",
}

# 会改磁盘的工具：这一版一律要问（第 10 轮再做"工作区内的小改动免确认"）
WRITE_TOOLS = {
    "file_write", "file_edit",
}


# 危险命令：命中即"高危"，模型的 need_user_approve 无法把它降级成免确认。
# 每条是 (正则, 给人看的原因)。正则统一用 IGNORECASE 编译。
_DANGER_RULES = [
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", "递归强制删除文件"),
    (r"\brm\s+-[a-z]*r", "递归删除"),
    (r"\bdel\s+/[sfq]|\berase\s+/[sfq]", "Windows 强制删除"),
    (r"\b(rd|rmdir)\s+/s", "递归删除目录"),
    (r"\bformat\s+[a-z]:", "格式化磁盘分区"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\b[^|]*\bof=", "裸写设备或文件"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "直接写入块设备"),
    (r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:", "fork 炸弹"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机或重启"),
    (r"\b(bcdedit|reg\s+delete)\b", "修改启动项或注册表"),
    (r"\bchmod\s+(-[a-z]+\s+)*777\b", "把权限开放给所有人"),
    (r"\bchown\s+-r\b", "递归修改所有者"),
    (r"\bgit\s+push\b[^|]*(\s--force\b|\s-f\b)", "强制推送远程仓库"),
    (r"\bgit\s+reset\s+--hard\b", "丢弃本地未提交的改动"),
    (r"\bgit\s+clean\s+-[a-z]*[fd]", "删除未跟踪文件"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python)\b", "下载后直接执行"),
    (r"\btaskkill\b|\bkillall\b|\bpkill\b", "批量结束进程"),
    (r"\bpip\s+uninstall\b", "卸载 Python 依赖"),
    (r"\bnpm\s+(uninstall|publish)\b", "卸载依赖或发布包"),
    (r"\bRemove-Item\b[^|]*(-Recurse|-Force)", "递归强制删除（PowerShell）"),
]

_DANGER_PATTERNS = [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _DANGER_RULES]


# 只读命令白名单：命中的命令即使是 cmd_runner 也不弹确认。
# 注意必须"整条命令都是只读"，所以下面 _is_safe_command 会先排除管道和重定向。
_SAFE_COMMAND_PREFIXES = (
    "dir", "ls", "pwd", "cd", "echo", "type", "cat", "head", "tail",
    "where", "which", "tree", "find", "grep", "wc",
    "git status", "git diff", "git log", "git show", "git branch",
    "python --version", "python -v", "node --version", "node -v",
    "npm --version", "npm ls",
)

# 出现这些符号就说明命令可能"拼接了别的东西"，一律不算只读。
# 例如 `dir && del /s /q` 的前缀是 dir，但它绝不安全。
_CHAIN_MARKERS = ("|", "&&", ";", ">", "<", "$(", "`", "\n")


@dataclass(frozen=True)
class Decision:
    """一次工具调用的权限判定结果

    need_approval：要不要弹确认
    risk：low（只读）/ normal（普通写入）/ high（高危，回车默认拒绝）
    reason：高危时给用户看的原因，其他情况是空串
    """

    need_approval: bool
    risk: str = "normal"
    reason: str = ""

    @property
    def is_high_risk(self) -> bool:
        return self.risk == "high"


def match_dangerous(command: str) -> str:
    """命中危险规则就返回原因，没命中返回空串"""
    if not command:
        return ""
    for pattern, reason in _DANGER_PATTERNS:
        if pattern.search(command):
            return reason
    return ""


def is_safe_command(command: str) -> bool:
    """判断一条 shell 命令是不是"纯只读"

    两条都得满足：
      1. 没有任何串联/重定向符号（否则前缀再安全也可能带上危险动作）
      2. 以白名单里的某个命令开头（按词边界匹配，避免 sticky 这种脏匹配）
    """
    text = (command or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(marker in text for marker in _CHAIN_MARKERS):
        return False
    for prefix in _SAFE_COMMAND_PREFIXES:
        if low == prefix or low.startswith(prefix + " "):
            return True
    return False


def classify(tool_name: str, args: dict) -> Decision:
    """判断这次工具调用要不要问用户（2026-11-06 第 9 轮新增）

    参数 args 是模型给的工具参数（可能带 need_user_approve）。
    返回 Decision，调用方只需要看 need_approval / risk / reason。
    """
    args = args or {}
    hint = args.get("need_user_approve")  # 模型自己的意见：只能用来"加严"
    command = args.get("command") if isinstance(args.get("command"), str) else ""

    # 规则 1：危险命令。模型写 false 也不放行，回车默认拒绝。
    danger_reason = match_dangerous(command)
    if danger_reason:
        return Decision(True, "high", danger_reason)

    # 规则 2a：只读工具。模型显式要求"问一下"时，尊重它（加严）。
    if tool_name in READ_ONLY_TOOLS:
        return Decision(bool(hint), "low", "")

    # 规则 2b / 3：执行命令。
    #   这里是本轮最关键的一刀：cmd_runner 的 need_user_approve **完全不再参与判定**。
    #   只有两种情况——
    #     · 命令在白名单里（dir / git status / type …）→ 免确认
    #     · 其他一切命令（含 `python main.py`、`npm test` 这种看着人畜无害的）→ 必须问
    #   为什么不给模型留"我保证安全"的口子？
    #     因为"判断一条命令是否安全"正是模型最容易出错、也最容易被绕过的环节。
    #     上一版留了口子，模型只要写 false 就能让 `del /s /q` 静默执行。
    #     现在宁可多问几下（回车即同意，成本很低），也不留这个洞。
    #   什么时候能少问？第 10 轮做"本次会话总是允许"的记忆，让用户自己拍板。
    if tool_name == "cmd_runner":
        if command and is_safe_command(command):
            return Decision(False, "low", "")
        return Decision(True, "normal", "")

    # 规则 3：其余一切都要问（含 file_write / file_edit / task / MCP 工具）
    return Decision(True, "normal", "")
