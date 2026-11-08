# =============================================================================
# 文件：src/core/auto_memory.py
# 作用：管理"自动记忆"——模型自己在工作过程中记下的经验，跨会话保留。
#
# 【一、和 THINKER.md 的区别（这是最容易混的一点）】
#   THINKER.md（见 core/prompt/memory.py）
#     谁写：人
#     存哪：仓库里，跟着 git 走
#     内容：项目规矩（怎么跑测试、不要动哪个文件）
#     注入：全文，每轮都带
#
#   本文件管的 MEMORY.md / memory 目录
#     谁写：模型自己
#     存哪：~/.thinker/projects/<项目>/memory/，不进仓库，只在这台机器上
#     内容：经验（用户纠正过什么、踩过什么坑、用户的偏好）
#     注入：**只有索引**，细节等模型自己去读
#
#   为什么两套都要有：
#     项目规矩是"共识"，应该进仓库、团队共享、稳定不变；
#     经验是"个人积累"，随时会增加、只对这台机器有意义、还可能写错要改。
#     混在一起的话，模型每写一条经验都要改动仓库文件，既脏又容易冲突。
#
# 【二、目录结构】
#   ~/.thinker/projects/D--code-thinker/memory/
#   ├── MEMORY.md                          ← 索引，一行一条
#   ├── user-prefers-concise-replies.md    ← 一条记忆一个文件
#   └── project-tests-must-run-offline.md
#
#   目录名怎么来的：把项目绝对路径里的 : \ / 都换成 -。
#     D:\code\thinker  →  D--code-thinker
#   这样不同项目天然隔离，而且一眼能看出对应哪个目录。
#   （Claude Code 用的是同一套规则，C:\Users\Jarvis → C--Users-Jarvis）
#
# 【三、为什么只把索引注入提示词】
#   记忆会越攒越多。全量注入的话，攒到 50 条就把上下文吃掉了，
#   而其中 90% 和当前任务无关。
#   只给索引（每条一行标题 + 一句话说明），模型扫一眼就知道"有没有相关的"，
#   需要细节时再用 file_read 去读那一个文件——按需加载，成本可控。
#   这也是 Claude Code 的做法：启动时读索引的前 200 行 / 25KB。
#
# 【四、谁来写】
#   不提供专门的 memory 工具。模型用现有的 file_write / file_edit 直接操作，
#   前提是这两条路径要放行：
#     · tools/file_tools.py 的工作区边界要允许 memory 目录（它在 ~/.thinker 下，不在工作区）
#     · core/permissions.py 对 memory 目录的读写要免审批（否则每记一条弹一次窗）
#   复用已有工具而不是新造一个，是因为模型已经很会用 file_write 了，
#   新工具反而要多学一套参数。
# =============================================================================

import os  # 取当前工作目录
import re  # 文件名规范化（把标题转成 kebab-case 文件名时用）
from pathlib import Path  # 路径操作
from typing import List, Optional  # 类型标注（兼容 Python 3.8）

# --- 可调参数 ---------------------------------------------------------------

PROJECTS_DIR = Path("~/.thinker/projects")
# 所有项目的记忆都放在这个目录下，一个项目一个子目录

MEMORY_DIRNAME = "memory"
INDEX_FILENAME = "MEMORY.md"
# 索引文件名。和 THINKER.md 一样用大写，一眼能和普通记忆文件区分开。

MAX_INDEX_LINES = 200
MAX_INDEX_CHARS = 25000
# 索引的注入上限，和 Claude Code 的阈值一致。
# 超过就截断——索引是每轮都要带的东西，不能无限长。
# 200 行大约能放 200 条记忆，正常使用碰不到。

MAX_ENTRIES = 50
# 记忆条数上限。超过时在注入文本里提醒模型"该合并了"。
# 为什么不自动删：判断哪条还有用需要语义理解，机器删容易删错。
# 提示模型自己去合并/删除，比程序猜更靠谱。


# -----------------------------------------------------------------------------
# 一、路径计算
# -----------------------------------------------------------------------------

def project_slug(project_dir: Optional[Path] = None) -> str:
    """把一个项目目录变成安全的目录名（2026-11-07 第 12 轮）

    规则：把冒号、反斜杠、正斜杠统统换成连字符。

        D:\\code\\thinker   →  D--code-thinker
        C:\\Users\\Jarvis   →  C--Users-Jarvis

    为什么不用哈希：
      哈希（md5 之类）能保证唯一，但目录名变成一串乱码，用户想手动翻记忆时
      根本分不清哪个文件夹对应哪个项目。可读性在这里比唯一性更重要——
      路径本身已经足够唯一了，撞车只可能发生在两个极端的路径上。

    为什么不合并连续的连字符（D--code 而不是 D-code）：
      合并后 D:\\code\\x 和 D:/code/x 会得到同一个名字，
      在 Windows 上这俩确实是同一个目录，影响不大；
      但保留原样能让人一眼看出"这里原本是个分隔符"。
    """
    target = (project_dir or Path(os.getcwd())).resolve()
    text = str(target)
    text = text.replace(":", "-").replace("\\", "-").replace("/", "-")
    return text.strip("-") or "root"
    # strip("-") 去掉首尾多余的连字符（比如 /home/x 开头的 /）
    # 万一整个路径都是分隔符（不可能），退回 "root" 兜底


def memory_root(project_dir: Optional[Path] = None) -> Path:
    """这个项目的记忆目录（2026-11-07 第 12 轮）

    ~/.thinker/projects/<slug>/memory
    注意 expanduser()：不展开的话 ~ 在文件系统里不是魔法字符，
    Path("~/x").exists() 永远是 False。
    """
    return (PROJECTS_DIR / project_slug(project_dir) / MEMORY_DIRNAME).expanduser()


def index_path(project_dir: Optional[Path] = None) -> Path:
    """索引文件的完整路径（2026-11-07 第 12 轮）"""
    return memory_root(project_dir) / INDEX_FILENAME


def ensure_dir(project_dir: Optional[Path] = None) -> Path:
    """确保记忆目录存在，返回它（2026-11-07 第 12 轮）

    启动时调用一次。为什么要提前建：
      file_write 工具在建父目录这件事上不一定可靠（不同实现不一样），
      与其让模型第一次写记忆时踩坑，不如程序先把这个空壳建好。
      exist_ok=True 保证重复调用不报错。
    """
    root = memory_root(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_memory_path(raw_path, project_dir: Optional[Path] = None) -> bool:
    """判断一个路径是不是在记忆目录里（2026-11-07 第 12 轮）

    参数 project_dir 默认 None 表示"当前工作目录对应的项目"——生产代码一律走默认值。
    留这个参数是为了测试能指定一个临时项目，否则测试要么去污染真实的 ~/.thinker，
    要么没法验证"别的项目算不算越界"。

    用途原本有两个，现在剩一个：
      1. ~~file_tools 的工作区边界检查~~：记忆目录在 ~/.thinker 下，不在工作区里，
         不特批的话模型根本写不进去。【第 15.2 轮】工作区边界整条删掉了，
         这个用途随之消失——现在五个文件工具都能直接操作 ~/.thinker 下的路径。
         记录在此是因为它曾经是个真问题：当年不加特批，模型写了半天记忆却一个字
         都没落盘，而错误提示还是"路径超出工作区"，看着像用户在配置上犯了错。
      2. 权限判定：这个目录下的读写免审批，否则每记一条弹一次窗（**仍然生效**，
         见 core/permissions.py 的 classify）。这一条必须留着：取消路径限制只是
         "允许访问"，不等于"允许不问就写"。

    只认"指定项目（默认当前项目）"的记忆目录，不认所有项目的——
    跨项目读记忆属于越权，没有理由支持。
    """
    if not raw_path:
        return False
    try:
        candidate = Path(str(raw_path).replace("\\", "/")).resolve()
        root = memory_root(project_dir).resolve()
    except (OSError, ValueError, RuntimeError):
        # 路径里有非法字符、盘符不存在之类，一律当作"不是"
        return False
    try:
        candidate.relative_to(root)  # 不在里面会抛 ValueError
        return True
    except AttributeError:
        # 某些路径实现的边界行为差异，保守返回 False（宁可多问一次审批）
        return False
    except ValueError:
        return False


# -----------------------------------------------------------------------------
# 二、读取索引
# -----------------------------------------------------------------------------

def load_index(project_dir: Optional[Path] = None) -> str:
    """读索引文件的内容，超长截断（2026-11-07 第 12 轮）

    返回空字符串表示"还没有任何记忆"——调用方据此决定要不要注入这一节。
    为什么不存在时返回空串而不是报错：
      第一次用的人本来就没有记忆，这是正常状态，不是错误。
    """
    path = index_path(project_dir)
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # 权限、占用、路径过长……记忆读不出来不该影响对话，静默跳过
        return ""

    lines = text.splitlines()
    truncated = False
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        truncated = True

    body = "\n".join(lines)
    if len(body) > MAX_INDEX_CHARS:
        body = body[:MAX_INDEX_CHARS]
        truncated = True

    if truncated:
        # 明确告诉模型"索引不完整"，否则它会以为记忆就这么多，
        # 重复记录已经存在的内容。
        body += f"\n\n... [索引过长已截断，完整内容请读取 {path}] ..."
    return body.strip()


def count_entries(project_dir: Optional[Path] = None) -> int:
    """数一数记忆目录里有多少条记忆（不含索引本身）

    用来判断要不要提醒模型"该合并了"。
    只数 .md 文件，其他文件（临时文件、备份）不算。
    """
    root = memory_root(project_dir)
    if not root.is_dir():
        return 0
    try:
        return sum(
            1 for item in root.iterdir()
            if item.is_file() and item.suffix.lower() == ".md" and item.name != INDEX_FILENAME
        )
    except OSError:
        return 0


# -----------------------------------------------------------------------------
# 三、拼成给模型看的一段话
# -----------------------------------------------------------------------------

def render_for_prompt(project_dir: Optional[Path] = None) -> str:
    """生成注入系统提示词的"记忆"一节（2026-11-07 第 12 轮）

    返回空字符串表示"没有记忆目录也没有记忆"，调用方整段跳过。
    为什么没有记忆时也要注入（只要目录存在）：
      模型需要知道"你可以往这里记东西、路径是什么"。如果只在有记忆时才告诉它，
      它永远没机会写下第一条——这是个先有鸡还是先有蛋的问题。
      所以判断条件是"目录存在"，而不是"索引非空"。
    """
    root = memory_root(project_dir)
    if not root.is_dir():
        return ""  # 连目录都没有（说明 ensure_dir 没跑过），那就别硬指一条路径给它

    index = load_index(project_dir)
    entries = count_entries(project_dir)

    parts = [
        "## 你的记忆\n",
        f"这个项目有一份属于你的记忆，放在：`{root}`\n",
        "里面是你在**之前会话**里记下的经验。索引文件是 MEMORY.md，"
        "每条记忆是一个独立的 .md 文件。\n",
        "**使用方式**：\n"
        "- 需要细节时，用 file_read 读索引里指向的那个文件\n"
        "- 写新记忆：用 file_write 建一个新文件，再用 file_edit 往 MEMORY.md 里加一行\n"
        "- 写之前先读索引，避免重复记录同一件事\n",
    ]

    if index:
        parts.append("\n### 当前索引\n\n" + index + "\n")
    else:
        parts.append("\n### 当前索引\n\n（还没有任何记忆。遇到值得记的事再写第一条。）\n")

    if entries >= MAX_ENTRIES:
        # 到上限时不阻止写入，但明确提示合并——
        # 硬拦会让模型在"该记"的时候记不下来，反而丢信息。
        parts.append(
            f"\n⚠️ 记忆已达 {entries} 条（上限 {MAX_ENTRIES}）。"
            "优先合并或删除过时的条目，而不是继续追加。\n"
        )

    return "".join(parts)


def slugify(title: str, max_length: int = 48) -> str:
    """把一句中文标题变成可用作文件名的片段（2026-11-07 第 12 轮）

    给模型一个参考实现，让它在写记忆文件时文件名风格统一。
    中文没法转拼音（需要额外依赖），所以保留中文字符——
    Windows 和 Linux 都支持中文文件名，没必要为了"好看"牺牲可读性。

    只做三件事：
      1. 空白和标点换成连字符
      2. 连续连字符合并成一个
      3. 截断到 max_length，避免文件名过长（Windows 单段上限 255）
    """
    text = str(title or "").strip().lower()
    text = re.sub(r"[\s/\\:*?\"<>|]+", "-", text)  # 空白与 Windows 非法字符
    text = re.sub(r"-{2,}", "-", text)  # 连续连字符合并
    text = text.strip("-")
    return text[:max_length] or "memory"


def list_entries(project_dir: Optional[Path] = None) -> List[str]:
    """列出所有记忆文件名（不含索引），按名字排序（2026-11-07 第 12 轮）

    给排查问题用：想知道模型到底记了些什么，直接调这个比翻目录快。
    """
    root = memory_root(project_dir)
    if not root.is_dir():
        return []
    try:
        return sorted(
            item.name for item in root.iterdir()
            if item.is_file() and item.suffix.lower() == ".md" and item.name != INDEX_FILENAME
        )
    except OSError:
        return []
