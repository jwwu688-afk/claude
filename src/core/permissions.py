# =============================================================================
# 文件：src/core/permissions.py
# 作用：判断"一次工具调用要怎么处理"。所有权限判断都集中在这里。
#
# 【为什么必须集中、而且不能听模型的】（2026-11-06 第 9 轮）
#   第 8 轮的做法是：模型在参数里写 need_user_approve，写 false 就直接执行。
#   这个设计有个致命问题——**被判断的人自己当裁判**。
#   模型完全可以（也经常）为了"少打扰用户"，给一条 `del /s /q` 也标上 false，
#   于是那条命令一声不响地跑了。这不是模型坏，是设计给错了权力。
#
# 【2026-11-12 第 33 轮：从"问/不问"两态改成 allow / ask / deny 三态】
#   加工作模式之后，"不问"分成两种完全不同的情况：
#     · allow：放行，直接执行（默认模式下的只读工具、接受编辑模式下的 file_write）
#     · deny ：挡回，不执行也不打扰用户（计划模式下的写工具）
#   用一个 need_approval=False 表示不了"拒绝"——那样调用方会当成"可以执行"，
#   这是把安全判断做成了布尔值挤在一起的典型错误。所以 Decision 现在带 action。
#
#   need_approval 保留成 property（== action=="ask"），是为了不改调用方：
#   conversation.py 里三处判断和 _needs_user_approval 都还在用它。
#
# 【判定分两层：先算基础判定，再按模式过滤】
#   _classify_base() 是第 9 轮那套规则，一个字没改——危险命令、只读工具、
#   记忆目录、命令白名单、其余一律问。
#   _apply_mode() 只在上面加一层滤镜：
#     default → 原样
#     plan    → 只放行只读/查询，其余 deny
#     accept  → 本来要问的写文件改成放行
#     auto    → 全部放行
#   分成两层的理由：模式是"策略"，危险规则是"底线"。混在一起写，
#   以后加第五档就得在二十条正则中间找位置，改错一条就是安全事故。
#   现在加一档只需要在 _apply_mode 里加一个 if 分支，底线逻辑碰都不碰。
#
# ⚠️ 唯一的底线例外：auto 模式下连危险规则都不参与判定。
#   这是用户明确要求的"全放开，不做判断"（等价 Claude Code 的
#   bypassPermissions）。风险由使用者承担，但调用方仍会拿到 risk="high"，
#   用来在界面上打一行提示——不阻断，只是别让人事后完全不知道发生过什么。
#
# 【第 15.2 轮注】这里原本还列出"工作区路径边界"待做。
#   后来（第 8 轮）它做在了 file_tools 的 _resolve_path 里，
#   第 15.2 轮又整条删掉了——因为只拦文件工具、拦不住 cmd_runner，
#   结果是逼模型去绕，而不是真的防住。所以它现在既不是"已完成"也不是"待做"：
#   真要重新引入，位置应该在这个文件（_classify_base）里，和命令白名单一起判。
# =============================================================================

# 这套权限系统分两层：
# _classify_base 判断"这条操作本身危不危险"，
# _apply_mode 再根据当前工作模式决定"这个判定要不要调"。
# 两层解耦，模式只能收紧或有限放宽，底线始终不动。
#
# 第一层里，危险命令一票否决（rm -rf / 直接 ASK(high)，模型写 need_user_approve: False 也没用）；
# 只读工具默认放行，但模型主动要求确认时会尊重它改成询问；
# 自动记忆目录是唯一主动放宽的口子，路径必须真落在 ~/.thinker/projects/<项目>/memory/ 里，.. 穿越会被挡掉；
# 命令执行只信白名单不信模型，python train.py 这种看着人畜无害的也得问，因为"判断命令安不安全"正是模型最容易出错、最容易被绕过的环节；
# 其余一切（文件写、task、MCP 工具）默认都要问。
#
# 第二层四档模式态度分明：auto 全放行但保留 risk=high 供 UI 提示（"不问，但我知道它高危"）；
# plan 最严，高危命令直接从 ASK 收紧成 DENY，命令类改用更宽的只读口径（node --check、git log 能过，npm install 不行），
# MCP 工具声明了只读才放行、没声明就弹一次确认，内置写工具一律拒；
# accept 只放宽"文件写入"这一类，把 ASK 降成 ALLOW，但模型自己要求确认时就不放；
# default 及任何未知模式原样返回，未知值按"问得最多"的那档处理，是安全方向。
#
# 贯穿始终的原则就三条：hint 只能收紧不能放宽（need_user_approve 最多把 ALLOW 变 ASK，在危险命令和命令执行上完全无效）；
# 模式不能突破底线（高危命令在 plan 下更严、在 auto 下放行也保留 risk）；
# 宁可多问不留洞（命令只信白名单、MCP 未声明就 ASK），成本只是用户多按一次回车，收益是堵住模型"我保证安全"的绕过路径。

import re  # 用正则匹配危险命令
from dataclasses import dataclass  # 装判断结果的小容器

from core.auto_memory import is_memory_path  # 【第 12 轮】识别"记忆目录"，那里读写免审批
from core.modes import MODE_ACCEPT, MODE_AUTO, MODE_DEFAULT, MODE_PLAN  # 【第 33 轮】四档模式


# --- 三态动作 ---------------------------------------------------------------
# 用常量而不是裸字符串：这三个值会跨模块传（conversation 要按它分流），
# 写错一个字母不会报错、只会静默走进 else 分支，是最难查的一类 bug。
ACTION_ALLOW = "allow"  # 直接执行，不打扰用户
ACTION_ASK = "ask"  # 停下来问用户
ACTION_DENY = "deny"  # 直接拒绝，把理由写回历史让模型改道


# 只读工具：不改磁盘、不启进程，没必要每次都打断用户
# 【2026-11-10 第 21 轮】加入 skill：它只做一件事——读 .thinker/skills/ 下的 SKILL.md
#   并把正文交出去。不写盘、不执行命令、不改任何状态，属于纯读。
#   注意"技能里写了什么"完全不参与权限判定：正文里就算写着"直接 rm -rf 不用问"，
#   真去删文件时仍然是 cmd_runner，仍然要弹确认。技能只是文字，不是通行证。
READ_ONLY_TOOLS = {
    "file_read", "file_glob", "file_grep", "todo_write", "skill",
}

# 会改磁盘的工具：默认模式下这一版一律要问
# 【第 33 轮】accept 模式只对这两个放行。为什么不含 cmd_runner：
#   改文件的影响范围是"看得见的那几个文件"，而一条命令能干什么是无法枚举的，
#   两者风险不是一个量级。这正是 Claude Code 的 acceptEdits 边界。
WRITE_TOOLS = {
    "file_write", "file_edit",
}

# 【2026-11-07 第 12 轮】只对"记忆目录"免审批的三个工具。
# 为什么不把 file_glob / file_grep 也放进来：模型要知道有哪些记忆，读索引就够了，
# 不需要在记忆目录里搜索。范围越窄越安全。
_MEMORY_TOOLS = {
    "file_read", "file_write", "file_edit",
}

# 【2026-11-13 第 33.5 轮】计划模式不再"从工具清单里剪掉写工具"。
#   第 33 轮做法是给 conversation 一个 PLAN_TOOL_ALLOWLIST，让模型连
#   file_write 都看不到。实测代价太大：模型写方案时说不出"用哪个工具改哪一处"，
#   而且 cmd_runner 的可用命令被白名单卡死（`node --check` 这类只读命令全被挡）。
#   现在改成 Claude Code 的做法：**工具照常下发，执行前按模式裁决**。
#   安全边界没有变松——classify() 的 plan 分支仍然会 deny 写工具和写命令，
#   只是模型"知道有这个工具、也知道现在用不了"，方案因此能写得更具体。

# 内置工具全集（2026-11-13 第 33.5 轮新增）。
#
# 为什么需要它：计划模式下要把"我们自己写的工具"和"MCP 外部工具"分开处理。
#   内置工具能静态判断读写（就是下面这几个集合）；MCP 工具做不到——
#   它可能是"查天气"，也可能是"提交工单"，同一个名字下什么都可能发生。
#   计划模式对前者的策略是 deny，对后者只能交给用户当场决定。
#
# 维护约定：新增内置工具时必须加进这个集合，否则它会被当成 MCP 工具、
# 在计划模式下变成"要用户点确认"。漏加的后果不严重（多问一次），
# 但会让"内置工具不用问"这个预期悄悄失效。
_BUILTIN_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS | {
    "cmd_runner", "task", "skill", "todo_write", "smart_context_cropper",
}


# 危险命令：命中即"高危"，默认模式下模型的 need_user_approve 无法把它降级成免确认。
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


# 只读命令白名单：命中的命令在默认模式下也不弹确认。
# 注意必须"整条命令都是只读"，所以 _is_safe_command 会先排除管道和重定向。
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


# -----------------------------------------------------------------------------
# 计划模式的命令判定：黑名单（2026-11-12 第 33 轮新增，11-13 第 33.5 轮改成黑名单）
#
# 【为什么从白名单翻成黑名单】
#   第 33 轮用的是"只认列出来的查询命令"。实测不够用：`node --check`、
#   `Get-ChildItem`、`tsc --noEmit`、`git remote -v` 全是只读，但都不在白名单里，
#   计划模式下用户几乎什么都跑不了——这是"安全的代价"压过了实用价值。
#   Claude Code 的做法相反：**工具照常可用，只拦明确的写操作**。
#   白名单永远列不全，黑名单只需要列"会造成改动的动作"。
#
# 【这条判定的边界必须说清楚】
#   它拦不住蓄意的写入。`python -c "open('x','w')"`、`node -e "..."` 都能绕过
#   任何基于文本的规则——所以解释器单独设了规则（默认视为"可执行任意代码"）。
#   它是"尽力而为"的只读过滤，不是沙箱。这一点和 is_safe_command 的定位不同：
#   那个是"确认无害才免问"，这个是"确认有害才拒绝"。
#
# 【为什么仍然拒绝重定向】
#   `>` 一行就能写文件，而且写到哪里无法从命令文本判断。
#   命令替换 `$()` 和反引号同理（可能执行任意东西）。
# -----------------------------------------------------------------------------

# 一元写命令：出现在**命令位置**（每段的第一个词）就是写操作。
# 为什么按命令位置判、而不是整段做正则匹配：
#   `cat copy.py` 里也有 "copy"，`vim rm.md` 里也有 "rm"——整段匹配会把
#   纯读命令按文件名误杀。只看第一个词，这个坑就不存在。
_WRITE_COMMANDS = frozenset({
    "rm", "del", "erase", "rmdir", "rd", "mkdir", "move", "mv", "copy", "cp",
    "ren", "rename", "touch", "tee", "ln", "mklink", "attrib", "icacls", "cacls",
    "chmod", "chown", "chgrp", "takeown", "setx",
    "new-item", "remove-item", "set-content", "add-content", "clear-content",
    "out-file", "copy-item", "move-item", "rename-item", "new-psdrive",
    "schtasks", "npx",
})

# git 的写子命令（第一个词是 git 时看第二个词）。
_WRITE_GIT_SUBCMDS = frozenset({
    "add", "commit", "push", "pull", "merge", "rebase", "reset", "checkout",
    "switch", "restore", "stash", "apply", "am", "cherry-pick", "revert",
    "clean", "init", "clone", "config", "submodule", "worktree", "rm", "mv",
})

# 包管理器的写子命令（npm / pnpm / yarn）。
# 注意 run 也在里面：`npm run build` 会生成产物，计划模式下不该跑。
_WRITE_PKG_SUBCMDS = frozenset({
    "i", "install", "add", "remove", "uninstall", "publish", "link",
    "ci", "update", "upgrade", "run", "exec",
})

# pip / conda / poetry 的写子命令。
_WRITE_INSTALL_SUBCMDS = frozenset({
    "install", "uninstall", "update", "upgrade", "remove", "add",
})

# 参数里出现这几个，说明这条命令会写盘或执行外部程序。
# 为什么不用 `-o`：`grep -o` 是只输出匹配部分、不写文件，一刀切会误伤常用命令。
# 为什么不用 `-f`：`grep -f pattern.txt` 是从文件读模式，同样是只读。
_WRITE_FLAGS = frozenset({
    "--output", "-delete", "-exec", "-execdir", "-ok", "-okdir",
})

# 出现即拒绝：重定向写文件、命令替换可能执行任意内容。
_WRITE_MARKERS = (">", "$(", "`", "\n")


@dataclass(frozen=True)
class Decision:
    """一次工具调用的权限判定结果（第 33 轮改成三态）

    action：allow（直接执行）/ ask（问用户）/ deny（拒绝，不执行）
    risk  ：low（只读）/ normal（普通写入）/ high（高危）
            高危在 ask 时"回车默认拒绝"，在 auto 时只用于界面提示
    reason：高危原因，或者 plan 模式拒绝时给模型看的理由

    为什么 risk 在 deny 时也要带出来：计划模式下模型调 `del /s /q`，
    拒绝理由里必须说清"这是高危操作"，否则它会以为是模式问题、
    换个写法继续试，而不是意识到这条命令本身有问题。
    """

    action: str = ACTION_ASK
    risk: str = "normal"
    reason: str = ""

    @property
    def need_approval(self) -> bool:
        """兼容第 9 轮以来的调用方：只有 ask 才需要弹窗"""
        return self.action == ACTION_ASK

    @property
    def is_high_risk(self) -> bool:
        return self.risk == "high"

    @property
    def is_denied(self) -> bool:
        """计划模式下的拒绝。调用方据此把理由写回历史，不弹窗"""
        return self.action == ACTION_DENY


def match_dangerous(command: str) -> str:
    """命中危险规则就返回原因，没命中返回空串"""
    if not command:
        return ""
    for pattern, reason in _DANGER_PATTERNS:
        if pattern.search(command):
            return reason
    return ""


def is_safe_command(command: str) -> bool:
    """判断一条 shell 命令是不是"纯只读"（默认模式下的免确认口径）

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


def is_readonly_command(command: str) -> bool:
    """判断一条命令在计划模式下能不能跑（2026-11-13 第 33.5 轮改成黑名单）

    与 is_safe_command 的分工：
      · is_safe_command 是白名单——"确认无害才免问"，用于默认模式的免确认；
      · 这里是黑名单——"确认有害才拒绝"，用于计划模式的放行判断。
    两者方向相反，因为要回答的问题不同：前者决定"要不要打扰用户"，
    后者决定"能不能在只读模式下跑"。

    拒绝条件（任一命中）：
      1. 命中危险规则（rm -rf / format / git push --force …）；
      2. 含重定向或命令替换（>、$()、反引号、换行）；
      3. 按 && / ; / | 拆开后，任何一段命中写操作黑名单。

    为什么拆段检查而不整条匹配：
      `git status && rm -rf build` 整条匹配也能命中 rm，但
      `git log | head` 里的 `|` 会让"整条"看起来很脏；拆段之后
      "每段都干净"这个判据既可读又可测。

    为什么不拦 `<`：读重定向不写盘，`sort < a.txt` 是合法的只读用法。
    """
    text = (command or "").strip()
    if not text:
        return False
    # `git log | ` 这种残缺写法在 shell 里本来就是语法错误，
    # 拆段后空段会被过滤掉、看起来像"只有一段干净的"，所以要单独挡一下。
    if text.endswith(("|", "&&", ";", "||")):
        return False
    if match_dangerous(text):
        return False
    if any(marker in text for marker in _WRITE_MARKERS):
        return False
    segments = [piece for piece in re.split(r"&&|;|\|", text) if piece.strip()]
    if not segments:
        return False
    return all(_segment_is_readonly(piece) for piece in segments)


def _command_name(token: str) -> str:
    """从一段的第一个词里取出"命令名"（2026-11-13 第 33.5 轮新增）

    要处理三种写法：带路径（`C:\\tools\\rm.exe`）、带引号（`"C:\\Program Files\\node.exe"`）、
    带 .exe 后缀。统一成小写纯名字之后再比对，用户怎么写都能被认出来。
    """
    name = token.strip().strip('"').strip("'").lower()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _segment_is_readonly(segment: str) -> bool:
    """一段命令里有没有写操作（2026-11-13 第 33.5 轮新增）

    只看**命令位置**（每段的第一个词），不看整段文本：
      `cat copy.py` 的第一个词是 cat，不算写；`copy a b` 的第一个词是 copy，算写。
      按整段做正则匹配会把前者误杀——这是把白名单翻成黑名单时最容易忽略的坑。

    解释器单独处理：python / node 能执行任意代码，只放行明确的版本查询与语法检查。
    这是整条判定里唯一的"默认拒绝"分支，也是所有文本级规则共同的盲区
    （`python -c "open('x','w')"`、`node -e "..."` 都能绕过命令名判断）。
    """
    tokens = segment.strip().split()
    if not tokens:
        return True
    # 去掉 `FOO=bar cmd` 这种环境变量前缀：bash 下很常见，不剥掉会把 FOO=bar 当成命令名
    while tokens and "=" in tokens[0]:
        tokens = tokens[1:]
    if not tokens:
        return True

    lowered = [token.lower() for token in tokens]
    if any(flag in lowered for flag in _WRITE_FLAGS):
        return False

    name = _command_name(tokens[0])
    sub = lowered[1] if len(tokens) > 1 else ""
    third = lowered[2] if len(tokens) > 2 else ""

    if name in _WRITE_COMMANDS:
        return False

    if name == "git":
        if sub in _WRITE_GIT_SUBCMDS:
            return False
        if sub == "remote" and third in {"add", "remove", "rename", "set-url", "prune"}:
            return False
        if sub == "branch" and third.startswith(("-d", "--delete", "-m", "--move", "--force")):
            return False
        # `git tag`（无参数）和 `git tag -l` 都是列出，`git tag v1.0` 才是创建
        if sub == "tag" and third and not third.startswith(("-l", "--list")):
            return False
        return True

    if name in {"npm", "pnpm", "yarn"}:
        return sub not in _WRITE_PKG_SUBCMDS

    if name in {"pip", "pip3", "conda", "poetry"}:
        return sub not in _WRITE_INSTALL_SUBCMDS

    # 解释器：只认版本查询 / 语法检查
    if name.startswith("python") or name == "py":
        return sub in {"--version", "-v", "-V"}
    if name == "node":
        return sub in {"--version", "-v", "--check"}

    return True


def _classify_base(tool_name: str, args: dict) -> Decision:
    """第 9 轮那套规则，一字未改（2026-11-12 第 33 轮原样抽出）

    参数 args 是模型给的工具参数（可能带 need_user_approve）。
    这里的判断与"当前是什么模式"无关——模式是外面那层滤镜的事。
    """
    hint = args.get("need_user_approve")  # 模型自己的意见：只能用来"加严"
    command = args.get("command") if isinstance(args.get("command"), str) else ""

    # 规则 1：危险命令。模型写 false 也不放行，回车默认拒绝。
    danger_reason = match_dangerous(command)
    if danger_reason:
        return Decision(ACTION_ASK, "high", danger_reason)

    # 规则 2a：只读工具。模型显式要求"问一下"时，尊重它（加严）。
    if tool_name in READ_ONLY_TOOLS:
        return Decision(ACTION_ASK if hint else ACTION_ALLOW, "low", "")

    # 规则 2a-2：【2026-11-07 第 12 轮】记忆目录的读写免确认。
    #   自动记忆（~/.thinker/projects/<项目>/memory/）是模型自己的笔记本：
    #     · 它只会往里写一两百字的小文件
    #     · 写错了用户随时能打开看、能改
    #     · 每记一条都弹确认的话，对话节奏会被打断得没法用
    #   这是唯一主动放宽的地方，所以做了一个很窄的例外：
    #   路径必须真的落在那个目录里（is_memory_path 会 resolve 后按路径段比较），
    #   换成工作区里任何一个别的文件都不生效。
    if tool_name in _MEMORY_TOOLS and is_memory_path(args.get("path")):
        return Decision(ACTION_ALLOW, "low", "")

    # 规则 2b / 3：执行命令。
    #   这里是第 9 轮最关键的一刀：cmd_runner 的 need_user_approve **完全不再参与判定**。
    #   只有两种情况——
    #     · 命令在白名单里（dir / git status / type …）→ 免确认
    #     · 其他一切命令（含 `python main.py`、`npm test` 这种看着人畜无害的）→ 必须问
    #   为什么不给模型留"我保证安全"的口子？
    #     因为"判断一条命令是否安全"正是模型最容易出错、也最容易被绕过的环节。
    #     上一版留了口子，模型只要写 false 就能让 `del /s /q` 静默执行。
    #     现在宁可多问几下（回车即同意，成本很低），也不留这个洞。
    if tool_name == "cmd_runner":
        if command and is_safe_command(command):
            return Decision(ACTION_ALLOW, "low", "")
        return Decision(ACTION_ASK, "normal", "")

    # 规则 3：其余一切都要问（含 file_write / file_edit / task / MCP 工具）
    return Decision(ACTION_ASK, "normal", "")


def _apply_mode(base: Decision, tool_name: str, args: dict, mode: str, readonly_hint: bool = False) -> Decision:
    """按当前工作模式过滤基础判定（2026-11-12 第 33 轮新增）

    四档的处理顺序是刻意的：先看 auto（全放行，连 base 都不用细看），
    再看 plan（最严），然后 accept（部分放宽），最后 default 原样返回。

    为什么 auto 要放在最前面、而不是"在 base 上把 ask 改成 allow"：
      base 对危险命令返回的是 risk="high"，如果走"把 ask 改成 allow"的路径，
      得到的 Decision 仍然带着 high；虽然 action 对了，但语义上是
      "高危且不问"，更准确的说法是"不问，但我知道它高危"。
      这里直接返回 allow + base.risk，正是这个意思，也让调用方能继续用
      risk 打提示。顺序上放最前只是让这条捷径更显眼。
    """
    if mode == MODE_AUTO:
        # 全放开：不做任何判断。risk 照原样带出去，调用方用它打一行提示。
        return Decision(ACTION_ALLOW, base.risk, base.reason)

    if mode == MODE_PLAN:
        # 计划模式：只放行"读"和"查"。
        # 高危命令即使看着像查询也必须拒——它是底线，不参与模式放宽。
        if base.risk == "high":
            return Decision(
                ACTION_DENY,
                "high",
                f"计划模式不执行高危操作（{base.reason}）。当前只能读取和查询。",
            )
        # 命令类：用黑名单口径单独判一次（第 33.5 轮）。
        # 为什么不直接用 base.action：base 用的是默认模式的窄白名单，
        # `node --check`、`Get-ChildItem`、`git remote -v` 这些只读命令会被判成 ask，
        # 而它们在 plan 模式下本来就该允许。
        if tool_name == "cmd_runner":
            command = args.get("command") if isinstance(args.get("command"), str) else ""
            if is_readonly_command(command):
                return Decision(ACTION_ALLOW, "low", "")
            return Decision(
                ACTION_DENY,
                base.risk,
                "计划模式下不执行可能写盘或改变状态的命令。"
                "只读命令（查看、搜索、git log/diff/status、类型检查）可以直接跑；"
                "需要改动就把它写进方案，由用户切换模式后再执行。",
            )
        # 读工具、task（派只读子 agent 调研）、skill（读技能手册）放行。
        if tool_name in READ_ONLY_TOOLS or tool_name == "task":
            return Decision(ACTION_ALLOW, "low", "")
        # 【第 33.5 轮】MCP 外部工具：无法静态判断它会不会改外部状态。
        #   第 33 轮这里是一律 deny，实测结果就是用户说的"MCP 工具用不了"。
        #   Claude Code 的做法是"没声明就请求权限"，这里照办：
        #     · 服务器用 annotations.readOnlyHint 声明了只读 → 直接放行；
        #     · 没声明 → ask，弹一次确认，用户回车即可执行。
        #   为什么不静默放行：MCP 里可能有"提交工单""发消息"这类动作，
        #   计划模式的价值就是"动手之前先看一眼"，静默放行等于把它取消。
        if tool_name not in _BUILTIN_TOOLS:
            if readonly_hint:
                return Decision(ACTION_ALLOW, "low", "")
            return Decision(ACTION_ASK, "normal", "")
        # 剩下的内置工具（file_write / file_edit / smart_context_cropper）一律拒绝。
        return Decision(
            ACTION_DENY,
            base.risk,
            f"计划模式不能使用 '{tool_name}'（它会修改文件或历史）。"
            "你可以把它写进方案里说明打算怎么改，由用户切换模式后再执行。",
        )

    if mode == MODE_ACCEPT:
        # 接受编辑：只把"改文件"这一类的 ask 降成 allow。
        # 三个条件缺一不可：
        #   1. base 要求确认（base 已经是 allow 的不必再处理）；
        #   2. 工具在 WRITE_TOOLS 里——命令、task、MCP 一律不放；
        #   3. 模型没有主动要求确认（need_user_approve=true 表示它自己觉得该问，
        #      这是"只能收紧"那条原则在模式层的延续）。
        if (
            base.action == ACTION_ASK
            and tool_name in WRITE_TOOLS
            and not args.get("need_user_approve")
        ):
            return Decision(ACTION_ALLOW, "normal", "")
        return base

    # default（以及任何未知值）：原样返回。
    # 未知值当默认处理是安全方向——默认模式是"问得最多"的那一档。
    return base


def classify(tool_name: str, args: dict, mode: str = MODE_DEFAULT, readonly_hint: bool = False) -> Decision:
    """判断这次工具调用要怎么处理（2026-11-12 第 33 轮加 mode 参数）

    参数 args 是模型给的工具参数（可能带 need_user_approve）。
    参数 mode 是当前工作模式，默认 default —— 这样老的调用方
    （测试、脚本、还没改的模块）不传它也能跑，行为和以前完全一致。
    参数 readonly_hint（第 33.5 轮新增）：这个工具是否**自声明为只读**。
      目前只有 MCP 工具能给（服务端 annotations.readOnlyHint）。计划模式用它
      决定"MCP 工具直接放行"还是"问用户一次"。内置工具不靠它——
      它们的读写属性由本文件里的集合判断。

    返回 Decision，调用方只需要看 action / risk / reason。
    """
    args = args or {}
    base = _classify_base(tool_name, args)
    return _apply_mode(base, tool_name, args, mode, readonly_hint)
