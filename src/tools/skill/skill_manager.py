# =============================================================================
# 文件：src/tools/skill/skill_manager.py
# 作用：技能登记表。扫两个目录、处理同名覆盖、给模型/用户生成可用清单（第 21 轮新增）。
#
# 【两个目录，一个方向】
#   ~/.thinker/skills/<名字>/SKILL.md        全局（跟人走，跨项目）
#   <启动时的 cwd>/.thinker/skills/<名字>/SKILL.md  项目（进仓库，跟项目走）
#
#   为什么"项目 > 全局"（和 Claude Code 的企业 > 个人 > 项目正好相反）：
#     1. 和 THINKER.md 的层叠方向一致——那里也是"全局先加载、项目后加载，
#        越靠内层越优先"。两套配置给出相反的优先级，用户迟早会撞上
#        "记忆按项目走、技能却按全局走"这种怪事。
#     2. 用户的直觉就是"我在这个项目里改的技能应该赢"。Claude Code 那条规则
#        是给组织管控用的（企业要能压住个人），本项目没有企业层。
#   实现上正好利用"后写覆盖先写"：按 [全局, 项目] 顺序扫，后面覆盖前面，
#   和 core/prompt/memory.py 的 load() 一个套路。被覆盖的记一条 shadowed，
#   在 /skills 里标出来——不静默。
#
# 【两种文件形态】
#   <名字>/SKILL.md   目录形态，能放 reference.md / scripts/ 等支撑文件
#   <名字>.md         单文件形态，没有支撑文件时一个 md 就够了
#
#   为什么允许单文件：技能和"子 agent 定义"的最大区别，就是技能天生需要目录
#   （核心价值是"正文 + 一堆按需加载的支撑文件"）。但如果一份技能真的只有正文，
#   逼用户建一个只装一个文件的目录纯属添麻烦。
#
#   ⚠️ 别把这里的目录扫描和第 18 轮删掉的 `.thinker/agents` 混为一谈：
#   那次删的是"子 agent 从 md 定义"，理由是"四个内置类型够用，不需要每人维护一份"。
#   技能不一样——技能是**给用户自己写操作手册**的地方，没有目录就没有这个功能。
#   两件事长得像，目的不同。
#
# 【一次启动的时间线（这就是整个机制）】
#   1. main.cli() → 进入对话。第 24 轮起启动期**不扫技能目录**（技能不再占用命令名）
#   2. main() → Conversation() → ToolManager.__init__()
#   3.   → refresh_skill_tool() → count() → _ensure_scanned()：第一次触发懒扫描
#   4.   → SkillManager().reload()：扫两个目录，每个文件只读前 4KB
#   5. 模型要工具清单 → skill_tool.json_schema() → available_block()
#   6.   → 只把 name + description 放进 tools 字段（每轮都发，所以要卡上限）
#   7. 模型调 skill("pdf") → load_body() → render()：这时才读正文
#
#   注意第 4 步和第 6 步之间隔了一次"快照"：扫描结果是存在 self._specs 里的。
#   所以改 frontmatter 要 reload，改正文不用（第 7 步每次都读盘）。
# =============================================================================

import os  # 取启动时的 cwd（项目级技能目录的基准）
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.limits import (  # 四处上限集中定义，理由见 limits.py
    MAX_SKILLS_BLOCK_CHARS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILLS_IN_BLOCK,
)
from tools.skill.skill_spec import (  # 元数据解析 + 正文渲染
    SKILL_FILENAME,
    SkillSpec,
    build_spec,
    render,
)


# -----------------------------------------------------------------------------
# 目录位置。写成函数而不是模块级常量，是为了给测试留接缝：
#   测试要在一个临时目录里造技能，又不能真的改 Path.home()（会影响同进程其它用例），
#   把这个函数换掉就行。和 commands._cwd() 是同一个思路。
# -----------------------------------------------------------------------------
def global_skills_dir() -> Path:
    """全局技能目录：~/.thinker/skills"""
    return Path.home() / ".thinker" / "skills"


def project_skills_dir(cwd: Optional[str] = None) -> Path:
    """项目技能目录：<cwd>/.thinker/skills

    参数 cwd 默认取 os.getcwd()，而不是向上找"项目根"：
      本项目所有东西（THINKER.md 的查找、自动记忆目录、会话归属、文件工具的
      默认起点）都按"启动时的 cwd"算口径一致。技能要是自己去找根目录，
      就会出现"在主目录启动、技能跑到家目录去"这种解释不通的现象。
    """
    return Path(cwd or os.getcwd()) / ".thinker" / "skills"


# 【第 24 轮】这里原来有个 _reserved_command_names()，用来拦"技能和内置命令重名"
# （比如一个叫 cost 的技能会把 /cost 顶掉）。
# 技能不再登记成斜杠命令之后，这个冲突面直接消失了：技能名和命令名是两个独立命名空间，
# 同时存在也不会互相影响。所以整个函数删掉——留着它反而会让一个叫 cost 的技能被静默拒载，
# 而用户完全看不出为什么要跳过它。


class SkillManager:
    """技能登记表（单例）

    为什么单例：ToolManager / Conversation / HistoryManager 全是单例，
    而技能清单要同时被三处读（工具说明书、斜杠命令表、/skills）。
    多份实例意味着"刚 reload 完，工具说明书里还是旧清单"。
    """

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SkillManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._specs: Dict[str, SkillSpec] = {}  # 名字 → 技能（项目覆盖全局后的结果）
        self._shadowed: List[Tuple[str, Path]] = []  # 被覆盖的 (名字, 被覆盖的文件)
        self._warnings: List[str] = []  # 扫描期的问题（拒载原因、解析失败……）
        self._scanned = False  # 是否扫过。第一次查清单时才扫，import 期不碰磁盘
        self._initialized = True

    # -------------------------------------------------------------------------
    # 扫描
    # -------------------------------------------------------------------------
    def _ensure_scanned(self) -> None:
        """第一次被查询时才扫目录

        为什么不在 __init__ 里扫：
          SkillManager 会随着"模型要工具清单"被创建/引用。如果在 import 期
          或对象构造期就扫磁盘，读盘失败会变成"启动就崩"，而那时用户连界面都看不到。
          延迟到第一次查询，问题可以变成一行告警，而不是一段堆栈。
        """
        if not self._scanned:
            self.reload()

    def reload(self) -> None:
        """重新扫两个目录（/skills reload 走这里）
        扫描规则（三个刻意的限制）：
          · **只扫一层**，不递归：Claude Code 支持 apps/web/.claude/skills 变成
            /apps/web:deploy，那是因为它要做 monorepo。本项目没这个需求，
            而"命名空间"会带来前缀、冲突、展示三个概念，维护的人只有一个。
          · **只读前 4KB**：frontmatter 就在开头，正文调用时才读（见 build_spec）。
          · **iterdir 排序**：不排序的话顺序取决于文件系统，
            /skills 列表和工具说明书每台机器都不一样，排查时对不上。
        """
        specs: Dict[str, SkillSpec] = {}
        shadowed: List[Tuple[str, Path]] = []
        warnings: List[str] = []

        # 全局在前、项目在后：后面的同名项覆盖前面的。
        #   这个顺序就是"项目赢"的全部实现——没有额外的优先级比较逻辑，
        #   改顺序 = 改优先级。所以不要在中间插入第三个目录而不想清楚它该排哪儿。
        for root, source in ((global_skills_dir(), "global"), (project_skills_dir(), "project")):
            if not root.is_dir():
                continue  # 目录不存在 = 正常状态（新用户本来就没有），不告警

            try:
                children = sorted(root.iterdir())
            except OSError as error:  # 目录权限不够、盘符被拔掉
                warnings.append(f"扫描 {root} 失败：{type(error).__name__}: {error}")
                continue

            for child in children:
                target = self._pick_entry_file(child)
                if target is None:
                    # 目录里放别的东西不算错（用户可能放 README、备份文件）
                    continue

                spec, problems = build_spec(target, source)
                warnings.extend(problems)
                if spec is None:
                    continue  # 名字不合法等原因被拒载，原因已经在 problems 里

                previous = specs.get(spec.name)
                if previous is not None:
                    # 同名覆盖：记下被覆盖的那份，/skills 里要能看见
                    # "你改的是全局那份，但项目里的版本赢了"——否则用户会奇怪
                    # "我明明改了描述，怎么没生效"。
                    shadowed.append((spec.name, previous.path))
                # 直接赋值就是"覆盖"：dict 的键不变、值换掉，
                # 所以列表顺序仍然保持"全局那份第一次出现的位置"。
                # 这不是巧合而是依赖：available_block 的稳定性靠它，
                # 换成 pop + 重新插入就会让同名技能的清单顺序在不同机器上跳。
                specs[spec.name] = spec

        # 整批替换而不是"先清空再逐个塞"：
        #   reload 过程中如果抛了异常（比如某个目录权限突然变化），
        #   清空过的那种写法会留下一个空清单——用户看到的是"技能全没了"。
        #   先攒在局部变量里、最后一次性赋值，异常时旧清单还在，最多是"这次没刷新成功"。
        self._specs = specs
        self._shadowed = shadowed
        self._warnings = warnings
        self._scanned = True

    @staticmethod
    def _pick_entry_file(child: Path) -> Optional[Path]:
        """判断一个目录项是不是技能，是就返回它的入口文件

        三种情况：
          目录 + 里面有 SKILL.md  → 目录形态
          以 .md 结尾的文件       → 单文件形态
          其他                    → 不是技能，跳过（不告警）
        """
        if child.is_dir():
            candidate = child / SKILL_FILENAME
            return candidate if candidate.is_file() else None
        # suffix.lower()：Windows 上用户可能把文件命名成 FOO.MD。
        #   不转小写的话它会被当成"不是技能"，而界面上什么提示都没有。
        if child.suffix.lower() == ".md":
            return child if child.is_file() else None
        return None

    # -------------------------------------------------------------------------
    # 查询
    # -------------------------------------------------------------------------
    def list_specs(self) -> List[SkillSpec]:
        """所有已加载的技能（顺序 = 扫描顺序，稳定）"""
        self._ensure_scanned()
        return list(self._specs.values())

    def get(self, name: str) -> Optional[SkillSpec]:
        """按名字取技能；没有就返回 None

        为什么返回 None 而不是抛异常：
          调用方是工具（模型可能传错名字）和斜杠命令（技能可能刚被删）。
          这两种情况都该"把正确清单交回去"，让模型/用户下一轮改对，
          而不是抛一个被上层吞成"运行工具时出错"的异常（第 8.1 轮的教训）。
        """
        self._ensure_scanned()
        return self._specs.get(name)

    def count(self) -> int:
        """一共加载了多少个技能（ToolManager 用它决定要不要登记 skill 工具）

        注意它是"当前快照"的数量，不是磁盘上的实时数量：
          新建技能文件之后，要 reload 才会变。这正是我们要的口径——
          tool 的登记状态不该在任意一次 count() 调用里悄悄变。
        """
        self._ensure_scanned()
        return len(self._specs)

    @property
    def warnings(self) -> List[str]:
        """扫描期的问题列表（/skills 展示；启动时也可以用）"""
        self._ensure_scanned()
        return list(self._warnings)

    @property
    def shadowed(self) -> List[Tuple[str, Path]]:
        """被项目版本覆盖掉的全局技能 [(名字, 被覆盖的文件路径)]"""
        self._ensure_scanned()
        return list(self._shadowed)

    def available_block(self) -> str:
        """拼出「可用技能」那一段，塞进 skill 工具的说明书

        【第 21.1 轮】规则简化成两条：**所有技能都列**，只是要卡长度。
          1. 每条描述截到 MAX_SKILL_DESCRIPTION_CHARS（默认 300）；
          2. 最多 MAX_SKILLS_IN_BLOCK 条、整块不超过 MAX_SKILLS_BLOCK_CHARS 字符，
             超了就截断并注明"还有 N 个没列出"。

        为什么砍掉原来那条"没描述的不列"：
          它制造了一个解释成本很高的状态——"我明明写了技能文件，模型却说不知道"。
          省下的只是几十个字符，换来的是一次次排查。现在没写描述也能出现在清单里，
          只是描述位置显示一句占位文字，模型至少知道"有这么个技能，要看得调它"。

        为什么长度上限必须留着：工具的 description 字段**每轮请求都发**。
        10 个技能 × 300 字符 ≈ 每轮 1400 token；50 个就是 6000+。
        这和"让模型知道全部技能"不矛盾——超长时给的是"还有 N 个，用 /skills 查看"，
        信息没丢，只是不占每轮预算。
        """
        self._ensure_scanned()

        specs = list(self._specs.values())
        if not specs:
            # 这里返回的是一句"没有技能"而不是空字符串：
            #   空字符串会让工具说明书里出现一对空的 <可用技能></可用技能>，
            #   模型看到标签却看不到内容，会以为工具坏了。写一句话成本更低。
            return "（当前没有加载到任何技能）"

        lines: List[str] = []
        used = 0  # 已用字符数（含换行），用来卡整块上限
        # 为什么要自己记 used、不直接 join 完再截：
        #   整块截断会把最后一行切成半句话（"描述到一半就没了"），模型看到的是坏数据。
        #   逐行累加、超了就整行丢弃，剩下的才是每一条都完整可读的清单。
        for spec in specs:
            if len(lines) >= MAX_SKILLS_IN_BLOCK:
                break  # 条数到顶
            if spec.description.strip():
                description = _shorten(spec.description, MAX_SKILL_DESCRIPTION_CHARS)
            else:
                # 没写描述时不编造、也不隐藏，给一句占位：
                # 模型能判断"这个技能存在，但要知道它干什么得先加载正文"。
                description = "（未写描述，调用后见正文）"
            line = f"- {spec.name}：{description}"
            if spec.argument_hint:
                # 只在技能自己声明了 argument-hint 时才显示参数提示。
                # 硬给每个技能补一句"参数：..."，等于告诉模型"它们都需要参数"，
                # 而大部分技能是零参数的。
                line += f"（参数：{spec.argument_hint}）"
            if lines and used + len(line) > MAX_SKILLS_BLOCK_CHARS:
                break  # 总长到顶
            lines.append(line)
            used += len(line) + 1

        omitted = len(specs) - len(lines)
        if omitted > 0:
            # 这条不算进技能清单，所以不受条数上限约束——它本身就是"还有更多"的提示。
            # 数量必须算准（用真实总数减已列数），写死个"等等"会让模型以为清单是完整的，
            # 从而不去 /skills 里找剩下的那些。
            lines.append(f"- （另有 {omitted} 个技能未列出，用 /skills 查看）")
        return "\n".join(lines)

    def load_body(self, spec: SkillSpec, args: str = "") -> str:
        """读技能正文并渲染（变量替换 + 截断）

        为什么这一层只是转手：真正的渲染在 skill_spec.render()。
        两条入口（skill 工具、斜杠命令）都必须走同一个函数，行为才一致；
        管理器只负责"提供这次要用的那个 spec"。
        """
        return render(spec, args or "")


def _shorten(text: str, limit: int) -> str:
    """超长就截断并加省略号（保留原样时不做任何加工）

    为什么先 rstrip 再拼省略号：截断点常常刚好落在空格上，
    不 rstrip 会显示成"读取 PDF 的正文与表格 …"，中间空一格很难看。
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
