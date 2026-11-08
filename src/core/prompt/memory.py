# =============================================================================
# 文件：src/core/prompt/memory.py
# 作用：加载"项目记忆"——放在 THINKER.md 里、每次启动都会读进系统提示词的项目约定。
#
# 【一、为什么需要这个文件】
#   模型每轮对话都是"第一次来"。它不知道这个项目的规矩，于是会：
#     · 在中文注释的项目里写英文注释
#     · 改完代码不跑测试
#     · 随手引入一个新依赖（而这项目故意保持轻量）
#   这些事以前只能靠用户在对话里反复说。写进 THINKER.md 之后，
#   每次请求都自动带在系统提示词里，模型就"记得"了。
#
# 【二、对齐 Claude Code 的哪一部分】
#   Claude Code 有两套记忆，别搞混：
#     ① CLAUDE.md  —— 人手写的项目约定，放在仓库里，全文注入系统提示词
#                     ← 本文件实现的就是这一套
#     ② MEMORY.md  —— 模型自己写的"自动记忆"，放在 ~/.claude/projects/<项目>/memory/，
#                     注入的是索引而不是全文
#                     ← 那是另一套机制，本项目还没做
#
# 【三、查找顺序（照搬 CLAUDE.md 的规则）】
#   ~/.thinker/THINKER.md      全局，最先加载（优先级最低）
#   D:\THINKER.md              父目录……
#   D:\code\THINKER.md         ……
#   D:\code\thinker\THINKER.md 当前目录，最后加载（优先级最高）
#
#   越靠内层的越晚出现，越晚出现的越"贴近现场"。模型读到两条冲突的规则时，
#   会以后面那条为准——和 CSS 的层叠是一个道理，不需要额外写"谁覆盖谁"的逻辑。
#
# 【四、一个完整例子】
#   在 D:\code\thinker 下启动 thinker：
#     1. discover() 返回 [~/.thinker/THINKER.md, D:\code\thinker\THINKER.md]
#        （中间的 D:\THINKER.md、D:\code\THINKER.md 不存在，直接跳过）
#     2. load() 把两个文件按顺序拼成一段文字，总长度控制在 24000 字符内
#     3. prompt_manager 把它插在"行为规矩"和"环境信息"之间
#     4. 之后每轮请求都带着这段内容（所以改 THINKER.md 需要重启才生效）
# =============================================================================

import os  # 取当前工作目录
import re  # 解析 @import 语法
from pathlib import Path  # 路径操作：拼接、判断文件、取父目录
from typing import List, Optional, Set  # 类型标注（Python 3.8 不支持 list[str] 这种写法）

# =============================================================================
# 一、可调参数
#
# 这些数字都不是随便定的，每一个都对应一个具体的失败场景，用到时再解释。
# 单独提出来放顶部，是因为调参时不用翻到文件中间去找。
# =============================================================================

MEMORY_FILENAME = "THINKER.md"
# 记忆文件的名字。为什么不用 MEMORY.md：
#   Claude Code 的 CLAUDE.md 用的是"产品名.md"的惯例，一眼能看出这是谁的配置。
#   而且 MEMORY.md 在 Claude Code 里已经有专门含义（模型自动写的记忆），撞名会混淆。

GLOBAL_MEMORY_PATH = Path("~/.thinker") / MEMORY_FILENAME
# 全局记忆的位置：~/.thinker/THINKER.md
# 这个文件通常**不存在**——程序不会自动创建它，得用户自己建。
# 它装的是跨项目的个人偏好（"回答简洁点""别加没用的注释"），
# 和项目里的 THINKER.md（"这个项目不要用 pytest"）分工不同。

MAX_UPWARD_LEVELS = 6
# 从当前目录向上最多找几层父目录。
# 为什么要有上限：不限的话，从 D:\code\thinker 会一路找到 D:\，
# 万一那里躺着一个 THINKER.md（可能是别的项目误放的），会莫名其妙地生效。
# 6 层足够覆盖"项目藏在很深的子目录里"这种常见情况。

MAX_FILE_CHARS = 12000
# 单个记忆文件最多读多少字符，超了截断。
# 粗略换算：中文 1 字符 ≈ 1 token，12000 字符大约占 6% 的上下文窗口。
# 一个记忆文件写到 1 万字已经非常夸张，超过多半是往里贴了整份文档。

MAX_TOTAL_CHARS = 24000
# 所有记忆文件加起来的上限。
# 为什么正好是单文件的两倍：正常情况下"全局 + 项目"两个文件就够了。

MAX_IMPORT_DEPTH = 3
# @import 最多套几层。
# A 引 B、B 引 C 是合理的组织方式；套到第 4 层基本说明结构乱了，
# 而且每套一层都会让最终文本膨胀，不如在这里截断。

_IMPORT_RE = re.compile(r"^\s*@([^\s#]+)\s*$")
# 匹配"整行只有一个 @路径"的写法。
# 为什么要求"整行"：正文里出现的 @ 太多了——邮箱（a@b.com）、
# Python 装饰器（@dataclass）、社交媒体的 @某人，不能全当 import。
# 只有"这一行除了 @路径 什么都没有"才认为是导入指令。
# [^\s#]+ 表示路径里不能有空格和 #（# 是注释符号，允许行尾写 # 说明）。


# =============================================================================
# 二、查找：哪些文件该被加载
# =============================================================================

def _cwd(start: Optional[Path] = None) -> Path:
    """当前工作目录，支持调用方指定一个（2026-11-07 第 11 轮）

    为什么要能指定：测试需要在一个临时目录里造 THINKER.md 并验证加载结果。
    如果只能读真实的 cwd，测试就得 os.chdir 切来切去，既慢又容易互相干扰
    （一个测试忘了切回来，后面的全错）。

    .resolve() 的作用：把相对路径、含 .. 的路径、甚至软链接统一成绝对路径。
    后面判断"某个文件是不是在当前目录下面"时，两边必须是同样规格的路径才比得准。
    """
    return (start or Path(os.getcwd())).resolve()


def is_user_home(start: Optional[Path] = None) -> bool:
    """当前目录是不是用户主目录（2026-11-08 第 14.1 轮新增）

    【为什么需要判断这个】
      整个项目把"启动时的 cwd"当成项目根：项目记忆按 cwd 向上找、
      自动记忆按 cwd 算目录名、会话 header 记的是 cwd、文件工具的工作区也是 cwd。
      这套设计本身没错——但它在主目录里启动时，全都会指向一个"不是项目"的地方。

      实际踩到的例子：
        用户在 `PS C:/Users/Jarvis>` 下敲 thinker，再执行 /init，
        于是生成了 `C:/Users/Jarvis/THINKER.md`。这份文件会成为
        **所有从主目录启动的会话**的项目记忆，里面还写着
        "（没检测到常见的项目标志文件）"——它描述的不是任何项目。
      同一个原因下，那次会话的文件工具工作区是 C:/Users/Jarvis，
      也就改不到 D:/code/thinker 里的任何文件。

    【为什么只判主目录，不判"看起来不像项目"】
      后者会误伤：新建的空目录、只有一个脚本的目录、刻意在某个子目录里
      局部工作的场景，都"不像项目"但完全合理。
      主目录是唯一一个"在这儿建 THINKER.md 几乎肯定错了"的地方，
      误报率最低。宁可少提醒，也不要让用户学会无视警告。

    start 参数是给测试用的（同 _cwd），生产代码一律走默认值。
    异常一律返回 False：路径算不出来时不该拦着用户干活。
    """
    try:
        return _cwd(start) == Path.home().resolve()
    except (OSError, RuntimeError):
        # Path.home() 在极端环境下会抛（没有 HOME 变量、家目录不存在）。
        # 判断不出来就当"不是"，让程序继续跑——这只是一条提示，不值得为它失败。
        return False


def _iter_upward(start: Path) -> List[Path]:
    """从 start 一路往上收集目录，最多 MAX_UPWARD_LEVELS 层（2026-11-07 第 11 轮）

    返回的是"从内到外"的顺序：[当前目录, 父目录, 祖父目录, ...]。
    注意：这不是最终加载顺序，加载前会反过来（见 discover）。

    为什么用 for 循环加计数器，而不是 `while current != current.parent`：
      Path.parent 到根目录之后会返回自己，那种写法也能停，
      但多一层判断不如直接数层数直观，而且层数上限本来就是需求的一部分。
    """
    dirs = []
    current = start
    for _ in range(MAX_UPWARD_LEVELS):
        dirs.append(current)
        if current.parent == current:  # 到根了（C:\ 的 parent 还是 C:\）
            break
        current = current.parent
    return dirs


def discover(start: Optional[Path] = None) -> List[Path]:
    """列出要加载的记忆文件，顺序 = 优先级从低到高（2026-11-07 第 11 轮）

    只返回**真实存在**的文件。不存在的直接跳过，不会在结果里留空位——
    调用方拿到的列表会直接拼进系统提示词，留个空位只会多出空标题。

    返回顺序（重点）：
        1. ~/.thinker/THINKER.md        全局，最先 → 优先级最低
        2. 最外层目录的 THINKER.md
        3. ……逐层往里……
        4. 当前目录的 THINKER.md        最后 → 优先级最高

    为什么全局放最前面：全局装的是"我这个人的习惯"，项目装的是"这个项目的规矩"。
    项目规矩通常更具体、更贴近当下任务，应该能覆盖个人习惯，
    所以让它排在后面、后加载。

    为什么用 reversed()：_iter_upward 给的是"从内到外"，
    而加载要"从外到内"，所以反过来。
    """
    found: List[Path] = []

    # 第一步：全局记忆
    global_path = GLOBAL_MEMORY_PATH.expanduser()
    # expanduser() 把路径开头的 ~ 换成真实家目录（C:\Users\23889）。
    # 不展开的话 Path("~/x").is_file() 永远为 False——~ 在文件系统里不是魔法字符。
    if global_path.is_file():
        found.append(global_path)

    # 第二步：从最外层目录往里，逐层找
    upward = _iter_upward(_cwd(start))
    for directory in reversed(upward):
        candidate = directory / MEMORY_FILENAME
        if candidate.is_file():
            found.append(candidate)

    return found


# =============================================================================
# 三、读取：单个文件、@import 展开、大小控制
# =============================================================================

def _truncate(text: str, limit: int, path: Path) -> str:
    """内容超长时截断，并在正文里写明"这里被截断了"（2026-11-07 第 11 轮）

    为什么不能只是切片了事：
      模型看到半截规则，会以为"就这些"。如果被切掉的后半段里写着
      "绝对不要动 xxx"，它完全不知道，照样去动。
      所以在截断处明确告诉它：被截了多少、完整内容在哪个文件——
      它至少知道自己看到的是残篇，必要时会主动去读原文件。

    参数 limit 由调用方算：既要受单文件上限约束，
    也要受"总量还剩多少"约束（见 load）。
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    return (
        f"{head}\n\n... [此文件过长，已截断 {len(text) - limit} 个字符。"
        f"需要完整内容请直接读取 {path}] ..."
    )


def _read_with_imports(path: Path, seen: Set[Path], depth: int = 0) -> str:
    """读一个记忆文件，把里面的 @import 展开成实际内容（2026-11-07 第 11 轮）

    为什么要支持 @import：
      一个项目的约定可能很长（编码规范、提交规范、架构说明），
      全塞进 THINKER.md 会变成一坨。拆成多个文件、主文件只留目录，
      既好维护，也能让不同文件被不同子目录复用。

    参数 seen：已经读过的文件路径集合，用来防死循环。
      典型死法：a.md 里写 @b.md，b.md 里又写 @a.md。
      没有这个集合会一直递归到 Python 的递归上限，抛 RecursionError。
      注意 seen 是**跨整个加载过程共享**的（load 里创建一次），
      这样 A 文件引入过的内容，B 文件再引一次也会被判为重复。

    参数 depth：当前套了几层。到 MAX_IMPORT_DEPTH 就不再展开，
      把 @xxx 原样留在文本里——而不是删掉。留着的好处是模型能看到
      "这里本该有内容"，必要时自己去读。

    读文件的细节：
      encoding="utf-8"   中文项目基本都是 UTF-8
      errors="replace"   万一文件是 GBK 存的，遇到非法字节换成 � 而不是抛异常。
                         宁可显示几个乱码，也不能让整个程序启动失败。
    """
    resolved = path.resolve()
    if resolved in seen:  # 循环引用
        return f"[循环引用，已跳过：{path}]"
    seen.add(resolved)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        # 权限不足、文件被别的程序独占、路径太长……都可能抛 OSError。
        # 记忆加载失败不该影响启动，所以退回一句话说明。
        return f"[无法读取 {path}：{e}]"

    if depth >= MAX_IMPORT_DEPTH:  # 到最深一层了，@import 原样留着
        return text

    output = []
    for line in text.splitlines():
        # splitlines() 会自动处理 \r\n 和 \n，比 split("\n") 稳妥：
        # Windows 上写的文件是 \r\n，直接 split("\n") 每行末尾会多一个 \r。
        match = _IMPORT_RE.match(line)
        if not match:  # 普通行，原样保留
            output.append(line)
            continue

        # 相对路径是相对于"当前这个记忆文件所在的目录"，
        # 不是相对于进程的 cwd——否则从不同目录启动会解析出不同的文件。
        target = (path.parent / match.group(1)).resolve()
        if not target.is_file():
            output.append(f"[@import 找不到文件：{match.group(1)}]")
            continue

        imported = _read_with_imports(target, seen, depth + 1)
        # 加首尾标记，让模型（和排查问题的人）能看出这一段是引入进来的，
        # 而不是主文件里原本就有的内容。
        output.append(f"<<< 来自 {match.group(1)} >>>")
        output.append(imported)
        output.append(f"<<< {match.group(1)} 结束 >>>")

    return "\n".join(output)


def load(start: Optional[Path] = None) -> str:
    """把能找到的项目记忆拼成一段文本，供系统提示词使用（2026-11-07 第 11 轮）

    返回空字符串表示"一个记忆文件都没有"。调用方据此决定要不要加这一节——
    否则系统提示词里会多出一个空标题"## 项目记忆"，白占 token 还打断阅读节奏。

    预算控制的两层：
      1. 单文件不超过 MAX_FILE_CHARS
      2. 累计不超过 MAX_TOTAL_CHARS，快用完时后面的文件只留标题

    为什么总量用完时还留个标题：
      让模型知道"还有别的记忆文件存在，只是内容没给"。这比完全隐藏好——
      它至少不会以为自己已经看全了全部约定。
    """
    files = discover(start)
    if not files:
        return ""

    seen: Set[Path] = set()  # 防 @import 循环，跨文件共享
    sections = []
    used = 0  # 已经用掉多少字符

    for path in files:
        # 显示路径时，如果文件在当前目录下面，显示相对路径（短、好读）；
        # 否则显示完整路径（全局记忆一般在家目录，显示全路径反而清楚）。
        try:
            shown = path
            if path.is_relative_to(_cwd(start)):
                shown = path.relative_to(_cwd(start))
        except (ValueError, AttributeError):
            # AttributeError：Python 3.8 没有 is_relative_to 这个方法
            # ValueError：路径不在给定的父目录下
            # 两种情况都退回绝对路径，不影响功能
            shown = path

        body = _read_with_imports(path, seen)
        body = body.strip()
        if not body:  # 空文件（或只有空行）不算一节
            continue

        remaining = MAX_TOTAL_CHARS - used
        if remaining <= 200:
            # 预算几乎用完。留 200 的余量是因为还要拼标题和分隔符，
            # 一点不留的话最后的文本会超过上限。
            sections.append(f"### {shown}\n[内容已省略：记忆总量超出上限]")
            continue

        body = _truncate(body, min(MAX_FILE_CHARS, remaining), path)
        used += len(body)
        sections.append(f"### {shown}\n{body}")

    if not sections:
        return ""

    # 这段引导语不是可有可无的装饰。
    # 模型拿到一堆"用户写的规则"时，需要知道三件事：
    #   1. 这是什么（项目约定，不是普通聊天内容）
    #   2. 优先级多高（高于默认习惯，低于用户当前这句话）
    #   3. 多条冲突时听谁的（靠后的更贴近当前目录）
    # 不写清楚的话，模型可能把记忆里的示例当成任务要求，或者反过来忽略它。
    header = (
        "## 项目记忆\n\n"
        "下面是用户预先写好的项目约定，来自 THINKER.md。\n"
        "**把它当作这个项目的既定要求**：与你的默认习惯冲突时以它为准；"
        "但用户当前这句话的优先级仍然最高。\n"
        "靠后的文件更贴近当前目录，冲突时以后者为准。\n"
    )
    return header + "\n" + "\n\n".join(sections)


# =============================================================================
# 四、/init：扫描项目，生成一份 THINKER.md 骨架
#
# 设计原则：**能自动填的都填好，需要人判断的留空并写清怎么填**。
# 生成出来的文件应当"立刻能用"（技术栈、目录树已经在了），
# 但真正有价值的三节（约定、禁忌、踩过的坑）必须人来写——
# 这些内容机器猜不出来，硬编只会产出一堆正确但无用的废话。
# =============================================================================

_SKIP_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", ".venv", "venv", "env",
    "node_modules", "dist", "build", ".pytest_cache", ".mypy_cache",
    "target", "out", ".next", "coverage",
}
# 画目录树时跳过的目录。全是产物或依赖——
# 列进记忆里除了浪费 token 没有任何价值，反而会淹没真正的源码目录。


def _detect_stack(cwd: Path) -> List[str]:
    """猜项目的技术栈（2026-11-07 第 11 轮）

    只看几个标志性文件，不做复杂解析。
    为什么不解析依赖清单：这份骨架是给人改的，猜错了用户自己会改；
    写一大堆探测逻辑（读 lock 文件、解析版本号、判断框架）投入产出比太低。
    """
    hints = []
    if (cwd / "pyproject.toml").is_file():
        # 优先看 pyproject：现代 Python 项目的标准配置
        hints.append("Python · 依赖与打包配置在 `pyproject.toml`")
    elif (cwd / "requirements.txt").is_file():
        # 老项目常见；用 elif 是因为两个都有时以 pyproject 为准
        hints.append("Python · 依赖在 `requirements.txt`")
    if (cwd / "package.json").is_file():
        hints.append("JavaScript/TypeScript · 依赖在 `package.json`")
    if (cwd / "go.mod").is_file():
        hints.append("Go · 模块定义在 `go.mod`")
    if (cwd / "Cargo.toml").is_file():
        hints.append("Rust · 依赖在 `Cargo.toml`")
    if (cwd / "pom.xml").is_file():
        hints.append("Java · Maven 项目（`pom.xml`）")
    return hints or ["（没检测到常见的项目标志文件，请手动补充）"]
    # 一个都没匹配到时也要给一句可执行的话，而不是留空——
    # 空着会让人以为工具坏了。


def _tree(cwd: Path, max_entries: int = 24, max_depth: int = 2) -> str:
    """画一棵浅层目录树（2026-11-07 第 11 轮）

    为什么限制得这么狠（两层、24 项）：
      记忆文件每一轮请求都会带上，里面塞 500 行目录树等于每轮白烧几千 token。
      模型真需要看细节时会自己去 list 目录——不需要我们提前喂全。
      两层足够表达"src/ 下面有 core、tools、ui 三大块"这种结构信息。

    排序规则：目录排在文件前面，同类按名字排。
    这样读起来像正常人的目录浏览习惯，而不是按文件系统的原始顺序乱跳。
    """
    lines: List[str] = []
    count = 0

    def walk(directory: Path, prefix: str, depth: int) -> None:
        # 用闭包递归，count / lines 通过 nonlocal 修改外层变量
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),  # False(目录) 排在 True(文件) 前
            )
        except OSError:
            # 目录没权限读、或者刚好被删掉，跳过即可，不能让整个 init 失败
            return
        for entry in entries:
            if count >= max_entries:
                lines.append(f"{prefix}…")  # 明说"还有但没列完"，避免误会
                return
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                # 点开头的一律跳过：.git / .env / .idea 都是配置或产物，
                # 不是项目结构的一部分
                continue
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                count += 1
                walk(entry, prefix + "  ", depth + 1)  # 每深一层缩进两格
            else:
                lines.append(f"{prefix}{entry.name}")
                count += 1

    walk(cwd, "", 1)
    return "\n".join(lines) if lines else "（空目录）"


def build_init_content(cwd: Optional[Path] = None) -> str:
    """生成 THINKER.md 的初始骨架文本（2026-11-07 第 11 轮）

    生成的内容分两类：
      自动填的：技术栈、目录结构、常用命令的位置
      留空的：这个项目是什么、代码约定、不要做的事、已知的坑

    留空的三节里都写了一句"示例"，而不是空着。原因：
      一个空的 `## 代码约定` 底下什么都没有，用户大概率会直接跳过；
      给两个具体例子（"注释用中文""不要引入新依赖"），人就知道该写什么粒度。

    为什么整个骨架用 f-string 而不是多个字符串拼接：
      里面嵌了 {stack} 和 {tree} 两段动态内容，用 f-string 最直观。
      注意模板里的普通大括号（如果有）必须写成 {{ }} 转义——
      目前模板里没有，Markdown 代码块用的是反引号，不受影响。
    """
    root = _cwd(cwd)
    # 把列表转成 markdown 的无序列表：每项前面加 "- "
    stack = "\n".join(f"- {item}" for item in _detect_stack(root))
    tree = _tree(root)

    return f"""# THINKER.md

> 这是这个项目的"长期记忆"。Thinker 每次启动都会读它，写在这里的约定对每一轮对话都生效。
> 下面带 `<!-- -->` 的地方需要你自己补；补完可以删掉注释。

## 这个项目是什么

<!-- 一两句话说明它解决什么问题。模型靠这句判断"该往哪个方向帮"。 -->

## 技术栈

{stack}

## 目录结构

```
{tree}
```

## 常用命令

```bash
# 按实际情况改成真的能跑的命令，模型会照着用
# python -m pytest          # 跑测试
# python src/main.py        # 启动
```

## 代码约定

<!-- 这里是最有价值的部分。写"必须遵守"的硬规则，不要写泛泛而谈的建议。例如：
- 注释用中文，解释"为什么"而不是"是什么"
- 公共函数要有类型标注
- 新增依赖必须先问，这个项目故意保持轻量
-->

## 不要做的事

<!-- 例如：
- 不要用 print 调试，项目统一用 rich 输出
- 不要动 src/core/permissions.py 里的判定顺序，那是有意排的
-->

## 已知的坑

<!-- 踩过一次的坑写在这里，避免下次重犯。例如：
- Windows 终端把 ⏺(U+23FA) 当 emoji 渲染，颜色不受控，圆点只能用 ●
-->
"""


def build_global_content() -> str:
    """生成全局记忆的骨架（2026-11-07 第 11 轮）

    和项目骨架的区别：**这里不放目录结构、技术栈、常用命令**。
    那些是"这个项目"的信息，写进全局记忆会污染所有项目——
    你在 D 盘的项目里写"用 pnpm"，到了 Python 项目里模型也会看到。

    全局记忆只装两类东西：
      1. 我这个人的沟通偏好（语言、详略、要不要客套）
      2. 跨项目都成立的红线（不要自动提交、破坏性操作先问）

    内容刻意写得短：这份文件每次请求都进上下文，
    几十行是合适的量级，超过 200 行模型会跳读，重要规则反而丢。
    """
    return """# 全局记忆

> 这份文件对**所有项目**生效。项目特有的约定写在各项目的 THINKER.md 里，不要写在这里。
> 每轮对话都会带上这份内容，所以尽量短——超过 200 行模型会跳读。

## 说话方式

- 中文回答，技术术语和代码标识符保持原样，不要翻译
- 先给结论再给理由，不要"好问题""我很乐意帮您"这类开场
- 能一句话说清就别写三段；需要展开时用表格或短列表
- 不确定就直说，不要编；信息可能过时先说明

## 动手习惯

- 改代码前先读相关文件，不要凭印象改
- 一次只改一处，改完说清楚「改了哪个文件、为什么」
- 不引入新依赖，除非我明确要求
- **不要自动 git commit / push**，等我说"提交"再动
- 破坏性操作（删文件、强推、改系统配置）先问

## 输出风格

- 报告结果给关键数字和前后对比，不要只写"已完成"
- 提到代码位置用 `路径:行号`
- 没要求时不要大段解释你刚写的东西

## 我的环境

<!-- 按实际情况补充，模型会据此决定用什么命令。例如：
- Windows 11 + Git Bash，命令用 POSIX 写法
- Python 用 conda 环境，激活后跑
-->
"""


def write_init_file(cwd: Optional[Path] = None, force: bool = False, scope: str = "project") -> Path:
    """把骨架写到磁盘，返回写入的路径（2026-11-07 第 11 轮）

    参数 scope 决定写哪一份（2026-11-07 补充）：
      "project" → <cwd>/THINKER.md     只对这个项目生效，可以提交进 git
      "global"  → ~/.thinker/THINKER.md 对所有项目生效，跟着这台机器走

    为什么两种用同一个函数：
      除了"目标路径"和"模板内容"，剩下的逻辑完全一样——
      判断文件存不存在、不覆盖、写 UTF-8。拆成两个函数会把这三段抄两遍。

    为什么默认不覆盖已存在的文件：
      用户可能已经在里面写了几百字的项目约定，重新生成一次就全没了。
      这类"一条命令毁掉手写内容"的默认行为，代价远大于便利——
      想重写的人加个 --force 就行，成本很低。

    这个判断放在这里而不是交给调用方：
      调用方（`core/commands.py` 的 /init）还要处理"开关解析"和"打印提示"，
      再让它记住"要先检查文件存在"容易漏。收在一处，只判断一次。
      （原来还有个 CLI 调用方 main.run_init，2026-11-08 清理后已删除。）
    """
    if scope == "global":
        target = GLOBAL_MEMORY_PATH.expanduser()
        content = build_global_content()
        target.parent.mkdir(parents=True, exist_ok=True)
        # ~/.thinker 目录可能还不存在（全新机器上只装了包、没跑过程序）。
        # parents=True 会连中间层一起建，exist_ok=True 保证目录已存在时不报错。
    else:
        target = _cwd(cwd) / MEMORY_FILENAME
        content = build_init_content(_cwd(cwd))

    if target.exists() and not force:
        return target  # 什么都不做，把现有路径返回去，调用方据此提示"已存在"

    target.write_text(content, encoding="utf-8")
    # encoding 必须显式指定：Windows 默认用 GBK 写文件，
    # 生成内容里有中文和 ⏺ ● 这类符号，不指定会乱码甚至抛 UnicodeEncodeError。
    return target
