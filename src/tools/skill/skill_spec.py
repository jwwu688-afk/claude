# =============================================================================
# 文件：src/tools/skill/skill_spec.py
# 作用：定义「一份技能长什么样」，以及它的正文怎么渲染成给模型看的文字（第 21 轮新增）。
#
# 【技能的三层结构，这个文件负责第一层和第三层】
#   1. 元数据（name / description / 两个布尔）—— 启动时读进内存，进工具说明书；
#   2. 正文（SKILL.md 里 frontmatter 之后的部分）—— **调用时才读**；
#   3. 正文里提到的 reference.md / 脚本 —— 模型自己用 file_read / cmd_runner 去取。
#
#   第 2 层"调用时才读"不是偷懒，它带来两个直接好处：
#     · 改正文不用重启、不用 reload——每次都从磁盘现读；
#     · 启动时只读每个文件的前 4KB（frontmatter 就在开头），
#       几十个技能也不会把启动拖慢。
#
# 【为什么渲染要单独成一个函数（render）而不是塞进管理器】
#   两条调用链——模型自动调 skill 工具、用户敲 `/pdf a.pdf`——最终都要
#   "现读正文 + 变量替换 + 截断"。这段逻辑只有一份，两处调同一个函数，
#   行为就永远是同一套。第 14 轮斜杠命令、第 13 轮恢复会话都吃过
#   "两处各写一遍，下次改动只补在一处"的亏。
# =============================================================================

import os  # 取 PROJECT_DIR（启动时的 cwd）
import re  # 校验技能名
from dataclasses import dataclass  # 只装字段的小类
from pathlib import Path
from typing import List, Optional, Tuple

from core.limits import MAX_SKILL_BODY_CHARS  # 正文截断上限（见 limits.py 的说明）
from tools.skill.frontmatter import (  # noqa: E501  假 YAML 解析器
    as_bool,
    looks_like_frontmatter,
    parse_kv,
    split_frontmatter,
    strip_bom,
)

# 技能目录形态下，正文件必须叫这个名字（和 Claude Code 一致）。
#   为什么固定：同一个目录里可能还有 reference.md、scripts/，
#   不固定文件名的话就得猜"哪一份才是入口"，而猜错的表现是"技能加载了但正文是空的"。
SKILL_FILENAME = "SKILL.md"

# 技能名的合法形状：小写字母 / 数字，用单个连字符连接。
#   这不是审美问题——技能名同时是**斜杠命令名**和**模型要填的参数值**：
#     · 大写：/PDF 和 /pdf 在 parse() 里会被转小写，用户写大写反而困惑；
#     · 下划线：命令行里比连字符难敲（要按 Shift）；
#     · 空格：parse() 只按空格切第一刀，`/My Skill` 会被拆成命令 My + 参数 Skill；
#     · 中文：能敲出来，但一旦出错，报错信息里中英混排极难扫读，
#       而且函数名参数在部分接口上对非 ASCII 支持不一致。
#   所以干脆只收 ASCII 小写 + 连字符，规则简单到一句话能说清。
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# 技能名长度上限。64 是 Claude Code 的口径，也差不多是"人能记住"的上限。
MAX_NAME_CHARS = 64

# 【2026-11-10 第 21.2 轮】这里原来有一张 UNSUPPORTED_FIELDS 表：
#   把 allowed-tools / model / hooks / disable-model-invocation 这些本版不支持的字段
#   解析出来、在 /skills 里标一句"未生效"。现在整张表删掉了。
#
#   为什么删：标注本身不改变任何行为，只是多一句解释文案；而它要求
#   「识别字段 → 存进 SkillSpec → 在 /skills 拼标记 → 写测试」四处跟着维护。
#   真正在意 allowed-tools 为什么不生效的人，会去看文档和代码；
#   不看的用户，那行字也只是噪音。
#
#   现在的规则最简单：**认得的字段读进 SkillSpec，不认得的字段解析完就丢掉**。
#   frontmatter 里多写几行（从 Claude Code 拷技能过来很常见）不会报错、也不会加载失败。



@dataclass(frozen=True)
class SkillSpec:
    """一份技能的元数据（元数据 = 启动时就要知道的那部分）

    frozen=True 的理由和 AgentSpec 一样：这份数据会被工具说明书和 /skills
    同时引用。谁都能改的话，某处顺手改掉 description，模型看到的说明
    和用户看到的清单就对不上了，而这种 bug 运行时很难看出来。

    字段：
      name            技能名。合法性在 build_spec 里校验。
      description     一句话说明（进工具说明书，模型靠它决定用不用）。
      path            SKILL.md 或 <名字>.md 的绝对路径。正文每次现读它。
      base_dir        ${SKILL_DIR} 要替换成的目录（单文件形态时是它的父目录）。
      source          "global" 还是 "project"，/skills 里要标来源。
      user_invocable  【第 24 轮起完全不起作用，仅为兼容保留】原本控制"是否登记成
                      /名字 命令"。技能不再占命令名之后，这个字段没有任何行为；
                      留着只为一件事：从别处拷来的技能文件里写了
                      `user-invocable: false` 时不要报错。
                      /skills 列表也不再为它打标记——斜杠命令都已经不存在了，
                      再标一句"不进斜杠命令"只会让人去找一条根本用不了的路。
      argument_hint   【第 24 轮起不再进 /help，但仍然有用】原本是 /help 里的用法提示。
                      现在唯一的去处是 skill_manager.available_block()：模型看到的
                      技能清单里会拼成「（参数：[文件路径]）」。展示对象从用户变成了模型，
                      所以这条**不能删**——删了模型就不知道这个技能要不要带参数。
    """

    name: str
    description: str
    path: Path
    base_dir: Path
    source: str
    user_invocable: bool = True
    argument_hint: str = ""

    # ---- 给界面看的小工具（写法对齐 AgentSpec.tools_text / model_text）----

    def source_text(self) -> str:
        """来源的中文写法。/skills 直接打印它。"""
        return "项目" if self.source == "project" else "全局"

    # 【2026-11-10 第 21.1 轮】这里原来有 visible_to_model()：没写描述、
    #   或者标了 disable-model-invocation 的技能不进工具清单。
    #   现在整条规则删掉了——**所有技能一律对模型可见**。
    #   为什么删：这个维度带来的是"同一个技能有时在清单里、有时不在"的心智负担，
    #   而它的收益（省一两行 token）远小于"为什么模型不知道我有这个技能"的困惑。
    #   没写描述的技能仍然可见，只是清单里显示占位文字（见 skill_manager.available_block）。


def _hint_text(value) -> str:
    """把 argument-hint 归一成一句显示用的文字

    【为什么需要这个函数（实测踩到的坑）】
      argument-hint 的惯用写法是 `[文件路径]`，而假 YAML 解析器看到以 `[` 开头、
      以 `]` 结尾的值，会把它当成**内联列表**，于是我们拿到的不是字符串
      而是 ["文件路径"]，直接 str() 就变成 `/pdf ['文件路径']`——
      尖括号、引号全冒出来，/help 里那一行看着像程序出错。

      要"从根上"修，得让解析器知道"哪些键永远不是列表"，那是给解析器开特例，
      以后每加一个字段就要记得加一条。这里在**取字段时**还原更省事：
      列表就按原来的方括号写法拼回去（[a, b]），单元素就还原成 [a]。
    """
    if isinstance(value, list):
        inner = ", ".join(str(item) for item in value if str(item).strip())
        return f"[{inner}]" if inner else ""
    return str(value or "").strip()


def is_valid_name(name: str) -> Tuple[bool, str]:
    """校验技能名；不合法时返回 (False, 中文原因)

    为什么把原因也返回：启动时的告警、/skills 的提示都要说"为什么这个名字不行"。
    只回一个 False，用户只能靠猜——"UPPER 不行？那我改成 upper 试试"，
    试错成本比直接告诉他"不能有大写字母"高得多。
    """
    if not name:
        return False, "名字为空"
    if len(name) > MAX_NAME_CHARS:
        return False, f"超过 {MAX_NAME_CHARS} 个字符"
    if not _NAME_PATTERN.match(name):
        return False, "只能用「小写字母 / 数字 / 单个连字符」，例如 pdf、git-commit"
    return True, ""


def build_spec(path: Path, source: str) -> Tuple[Optional[SkillSpec], List[str]]:
    """从一个文件读出技能元数据；读不出合法技能时返回 (None, problems)

    参数 path：SKILL.md 或 <名字>.md 的路径（已经确认文件存在）。
    参数 source：来源标记，只用于展示与优先级说明。

    返回 (spec 或 None, problems)：
      problems 是**给人看**的中文说明列表，交给 /skills 和启动告警。
      为什么要一路传出来而不是就地 print：启动时可能一次扫出 10 个问题，
      混在欢迎页里会被刷掉；集中到 /skills 里，用户想看的时候一次看全。

    **只读前 4096 字符**：frontmatter 就在开头。启动时不把几十个技能全文吞进内存，
    正文在调用时才读（顺带好处：改正文不用 reload）。
    """
    problems: List[str] = []

    try:
        # errors="replace"：文件不是 UTF-8 时用 � 代替非法字节，
        # 而不是抛 UnicodeDecodeError 把整个扫描打断——一个坏文件不该拖垮所有技能。
        # 4096 这个数字的来历：frontmatter 只有几行，加上注释和空行通常 <500 字节；
        #   取 4096 是给"头部写了很长的 description + 一堆字段"留足余量，
        #   同时保证 50 个技能全部扫一遍也只有 200KB 的读量。
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError as error:  # 权限不足、文件被占用、扫描后被删
        return None, [f"{path}：读取失败（{type(error).__name__}: {error}）"]

    head = strip_bom(head)
    raw, _body = split_frontmatter(head)

    if raw is None and looks_like_frontmatter(head):
        # 有开头没结尾。这时 split_frontmatter 会退回"整份当正文"，
        # 于是字段全空、name 取文件名——技能仍能加载，但必须让用户知道原因。
        problems.append(f"{path.name}：frontmatter 缺少闭合的 `---`，字段没有生效")

    fields, parse_problems = parse_kv(raw) if raw is not None else ({}, [])
    for problem in parse_problems:
        problems.append(f"{path.name}：{problem}")

    # ---- name ----
    # 目录形态取目录名，单文件形态取文件名（去掉 .md）：
    # 这就是"没有 frontmatter 的技能照样能用"的落地方式（对齐 Claude Code）。
    #   为什么目录形态不能用 path.stem：SKILL.md 的 stem 是 "SKILL"，
    #   所有目录形态的技能会全部叫 SKILL、互相覆盖——这个 bug 只在"多个技能同名"时暴露，
    #   而且表现为"只剩一个"，很容易被当成扫描没生效。
    default_name = path.parent.name if path.name == SKILL_FILENAME else path.stem
    raw_name = fields.get("name")
    name = str(raw_name).strip() if raw_name not in (None, "") else default_name

    valid, reason = is_valid_name(name)
    if not valid:
        # 名字不合法是**拒载**，不是"加载后再说"：
        # 它同时是斜杠命令名，一个敲不出来的名字等于"加载了也没法用"。
        return None, problems + [f"{path.name}：技能名 '{name}' 不合法（{reason}），已跳过"]

    # ---- description ----
    # 允许写成块列表（手写时偶尔会发生），拼成一句话，而不是让类型错误崩在后面。
    raw_description = fields.get("description", "")
    if isinstance(raw_description, list):
        description = "，".join(str(item) for item in raw_description if str(item).strip())
    else:
        description = str(raw_description).strip()

    # 【2026-11-10 第 21.3 轮】把所有空白（含换行）压成单个空格。
    #   为什么必须压：description 的用途是"清单里的一行"，而块写法（`|`）会带真实换行——
    #   官方 `claude-api` 技能就是这样，压平前它会把 `- 名字：描述` 拆成两行，
    #   模型看到的是一条断掉的清单项。压平放在这里而不是展示层，是因为
    #   `/skills` 和工具说明书两处都要用，只改一处还是会漏。
    description = " ".join(description.split())

    # ---- 只剩一个布尔 ----
    # 【第 21.2 轮】disable-model-invocation 现在既不读也不标注：
    #   它和 allowed-tools / model / hooks 一样，解析完就丢。
    #   技能一律对模型可见，没有例外，也就不需要"这个字段没生效"的提示。
    user_invocable = as_bool(fields.get("user-invocable"), default=True)

    argument_hint = _hint_text(fields.get("argument-hint"))

    # 到这里 fields 里可能还剩一堆本版不认识的键（allowed-tools / hooks / metadata…）。
    #   什么都不做就是正确行为：**不认识的字段直接忽略**。
    #   为什么不做白名单校验：frontmatter 是给人手写的，写了个没用的字段顶多不生效，
    #   为它报错或警告只会让"拷个别人的技能"变成一件需要排错的事。

    # base_dir 用 path.parent 而不是 path：${SKILL_DIR} 要指向"技能所在的目录"，
    #   正文里写 `${SKILL_DIR}/scripts/extract.py` 才能拼对。
    #   目录形态下它正好是 <名字>/，单文件形态下是那个 .md 的父目录——两种都成立。
    spec = SkillSpec(
        name=name,
        description=description,
        path=path,
        base_dir=path.parent,
        source=source,
        user_invocable=user_invocable,
        argument_hint=argument_hint,
    )
    return spec, problems


def _posix(path) -> str:
    """把路径转成正斜杠写法

    为什么必须转：正文是 markdown，`D:\\code\\x.py` 里的反斜杠在 markdown 里
    是转义符，渲染出来会少一个；而模型拿到 `D:/code/x.py` 反而更稳
    （和 file_tools._resolve_path 的口径一致，两边都认正斜杠）。
    """
    return str(path).replace("\\", "/")


def _support_note(spec: SkillSpec) -> str:
    """加载正文时加在最前面的「技能目录」提示（2026-11-10 第 21.4 轮新增）

    【为什么必须有这一行】
      技能目录里常带支撑文件（`reference.md`、`examples/*.md`、`scripts/*.py`），
      而正文里写的通常是**裸相对路径**（"读 examples/3p-updates.md"）。
      但本项目的文件工具把相对路径解析到**启动时的项目根**，不是技能目录——
      实测：模型照做会把 `examples/3p-updates.md` 读成
      `D:/code/thinker/examples/3p-updates.md`，那里当然没有这个文件。
      模型拿到的报错是"文件不存在"，它不知道这是路径基准错了，
      只会换个写法乱试，或者干脆放弃这个技能。

      一行提示就能把基准说清楚，成本约 80 字符，而且**只在加载时出现一次**
      （不像工具说明书那样每轮都发）。

    【什么时候不加】
      · 单文件形态（`<名字>.md`）：它的 base_dir 是父目录，里面可能是别的技能，
        指过去反而误导；
      · 目录里确实只有 SKILL.md：没有支撑文件，加了是噪音。
    """
    if spec.path.name != SKILL_FILENAME:
        return ""
    try:
        has_support = any(entry.name != SKILL_FILENAME for entry in spec.base_dir.iterdir())
    except OSError:  # 目录权限异常：宁可不提示，也不要让加载失败
        return ""
    if not has_support:
        return ""

    directory = _posix(spec.base_dir)
    return (
        f"【技能目录】{directory}\n"
        "正文里提到的相对路径（如 `examples/x.md`、`scripts/y.py`）都相对这个目录。\n"
        f"读取或执行它们时请用绝对路径（{directory}/...），或先 cd 到该目录再执行。"
    )


def render(spec: SkillSpec, args: str = "") -> str:
    """读出正文，做变量替换与截断，返回交给模型的整段文字

    参数 spec：已经扫描到的技能元数据。
    参数 args：用户（或模型）这次传进来的参数串，会替换到 $ARGUMENTS。

    变量只有三个（刻意不做 $0 / $1 / 命名参数）：
      $ARGUMENTS     调用时传的整串参数
      ${SKILL_DIR}   技能所在目录（单文件形态时是它的父目录）
      ${PROJECT_DIR} 启动时的 cwd
    另外为了兼容 Claude Code 的技能，也认 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}`
    （第 21.3 轮加，见下面的替换处）。

    为什么不做 $0 / $1：要做就得处理"引号怎么切、`$1.00` 里的 $1 要不要替换、
    参数缺失时占位符原样保留还是清空"这一整套，收益远小于成本——
    技能的参数几乎永远是一整个路径或一句话。

    失败处理：文件在扫描之后被删掉了 → 返回一段说明文字，不抛异常。
      理由和 task 工具"未知类型返回清单"一致：异常会被上层转成一句
      "运行工具时出错"，模型看不到有用信息；返回说明它至少知道发生了什么。
    """
    try:
        text = spec.path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return (
            f"技能 '{spec.name}' 的文件读不到了：{spec.path}\n"
            f"原因：{type(error).__name__}: {error}\n"
            "可能是扫描之后被删掉了，让用户先 /skills reload 再试。"
        )

    _raw, body = split_frontmatter(text)
    # strip() 去掉的是两条 `---` 之间那几个换行留下的空行。
    #   为什么不能把空行留着：技能正文会被当成工具结果/用户消息发出去，
    #   开头连续三四个空行既占 token，又会让界面上的回执看起来像"内容没加载出来"。
    #   只去首尾，不动中间——正文里的缩进和空行可能是有意义的（代码块、表格）。
    body = body.strip()

    # 先展开路径变量、再替换 $ARGUMENTS。
    # 顺序不能反：参数是用户写的内容，如果先替换它，用户碰巧写了个
    # `${SKILL_DIR}` 就会被二次展开——那种"我的参数被吃掉了"的问题极难查。
    # 【第 21.3 轮】同时认 Claude Code 的两个变量名，为了能直接导入公开技能：
    #   `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}` 是那边的写法，
    #   不认的话它们会以字面量留在正文里，模型会照着去执行一条带 `${...}` 的假路径。
    body = body.replace("${CLAUDE_SKILL_DIR}", _posix(spec.base_dir))
    body = body.replace("${CLAUDE_PROJECT_DIR}", _posix(os.getcwd()))
    body = body.replace("${SKILL_DIR}", _posix(spec.base_dir))
    body = body.replace("${PROJECT_DIR}", _posix(os.getcwd()))

    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args or "")
        # args 为空时替换成空串，而不是保留占位符：
        #   保留的话模型会看到字面的 "$ARGUMENTS"，以为要自己去填，
        #   于是反而来反问用户"参数是什么"——而那正是这次调用已经发生过的事。
    elif args:
        # 正文里没写占位符，但用户传了参数：追加到末尾。
        # 不追加的话参数会凭空消失——用户敲 `/pdf a.pdf`，模型却看不到 a.pdf，
        # 会去反问"你要处理哪个文件"，看起来就像命令没生效。
        body += f"\n\nARGUMENTS: {args}"

    # 【第 21.4 轮】技能目录提示放在最前面：模型读正文之前先知道"相对路径相对谁"。
    #   放在变量替换之后、截断之前：提示里没有占位符，但截断必须把它算进长度，
    #   否则"正文 20000 + 提示 80"会超过上限，兜底那一刀就白设了。
    note = _support_note(spec)
    if note:
        body = f"{note}\n\n{body}"

    if len(body) > MAX_SKILL_BODY_CHARS:
        # 截断并注明总长度：模型知道"后面还有"，可以让用户分段加载；
        # 不注明的话它会以为技能就这么多，然后按半截规矩干活。
        # 截在字符上而不是行上：markdown 的行长度差异极大（一行表格可能几千字符），
        #   按行截反而更难保证上限。代价是可能把一段代码块切成两半，
        #   所以提示里必须写清"已截断"，让模型知道那半截不是原文的结尾。
        original = len(body)
        body = (
            body[:MAX_SKILL_BODY_CHARS]
            + f"\n\n…（正文已截断：只加载了前 {MAX_SKILL_BODY_CHARS} 字符，"
            f"全文共 {original} 字符）"
        )

    # 【2026-11-10 第 21.8 轮】回复语言约束。
    #   为什么加在这里而不是只靠系统提示词：
    #     系统提示词（全局 THINKER.md）确实写了"中文回答"，但导入的技能正文是英文，
    #     而且常常自带英文的输出模板（"Here's what I understand…"这类）。
    #     对模型来说，**离内容最近的那条指令最有效**——正文说用英文模板，
    #     隔着几千字符的系统提示词说用中文，前者会赢。
    #     所以把约束贴在正文末尾，紧挨着内容本身。
    #   为什么放在 render()（两条入口共用的函数）：
    #     斜杠命令和 skill 工具最终都走这里，改一处两条路都覆盖；
    #     只改斜杠命令的前缀，模型自己调 skill 工具时就会漏。
    #   为什么放在截断之后：它是约束不是内容，被截断逻辑吃掉就白加了；
    #     也因此不计入 20000 的正文上限（差这几十字符不改变"该不该截"的判断）。
    #   为什么不翻译正文本身：翻译 13MB 的第三方技能既会走味，也会让上游更新没法合并；
    #     要的只是"产出是中文"，把约束贴在产出前一步就够了。
    body += (
        "\n\n【回复语言】用中文回复用户；代码、命令、专有名词、文件路径保持原文。"
        "技能正文是英文不代表要用英文回复。"
    )

    return body
