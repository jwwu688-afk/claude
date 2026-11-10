# =============================================================================
# 文件：src/core/prompt/prompt_manager.py
# 作用：把系统提示词的各个零件拼起来，交给对话流程使用。
#
# 【2026-11-07 第 11 轮】加了第三段：项目记忆（THINKER.md）。
#   顺序是：规矩 → 项目记忆 → 环境信息。
#
#   为什么记忆放在中间、环境放最后？
#     模型的注意力对"靠后"和"靠前"都敏感，中间反而最弱。所以：
#       · 规矩放最前面（身份、安全底线，必须最先立住）
#       · 环境放最后（"今天几号、在哪个目录"，模型每轮都要看，放尾部更容易被引用）
#       · 项目记忆放中间：它是一整块"项目特有的约定"，和前后都不冲突
#   这三段之间用空行隔开，模型读起来的层次更清楚。
# =============================================================================

from abc import ABC, abstractmethod  # 抽象基类工具

from core.auto_memory import render_for_prompt as render_auto_memory  # 【第 12 轮】自动记忆
from core.prompt.enviroment import get_enviroment_info  # 导入函数：生成环境信息文字
from core.prompt.memory import load as load_project_memory  # 导入函数：加载 THINKER.md
from core.prompt.system_rule import get_system_rule  # 导入函数：生成行为规矩文字

# 【2026-11-10 第 21 轮】技能那一小段（有技能时才拼进去）。
#
# 为什么工具说明书里已经写了、这里还要再说一遍：
#   第 8 轮的"工具选择"一节就是这个先例——光在 file_read 的说明书里写"请用我"，
#   模型有时仍会凭习惯去敲 dir；把规则放进系统提示词（每轮都带、优先级最高）之后就稳了。
#   技能是同一个问题：说明书在 tools 字段里，模型可能一扫而过，
#   而"先加载再动手"是它最容易跳过的步骤（跳过之后它会按自己脑补的流程干，
#   看起来还挺像样，但和手册里的约定完全不同）。
#
# 为什么只写三句、不复述技能清单：
#   清单在 skill 工具的说明书里，是**动态生成**的。在这里再列一遍，
#   等于同一件事维护两处：新建一个技能文件，这里的清单不会跟着变，
#   模型就会拿着过期清单去调不存在的技能。所以这里只给规则，不给数据。
SKILLS_SECTION = """## 技能

这个项目里可能有若干「技能」——写在 .thinker/skills/ 下的操作手册（怎么做 PDF、怎么发版、怎么 review）。
任务和某个技能的描述对得上时，先用 skill 工具把它的正文加载进来，再按正文里的步骤做。
不要凭技能名猜正文内容，也不要跳过技能自己编一套流程。""".strip()


class BasePromptManager(ABC):
    # 抽象基类，相当于"提示词管理器上岗证"
    @abstractmethod
    def get_system_prompt(self) -> None:
        pass  # 谁继承我，就必须自己实现这个方法


class PromptManager(BasePromptManager):
    # 真正的实现。单例，全程序一个
    _instance = None  # 唯一实例
    _initialized = False  # 是否已初始化

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._initialized = True  # 这里没什么要初始化的，只是打个标记

    def get_system_prompt(self) -> str:
        # 每次调用都会重新拼一遍（所以日期永远是当天的）
        # 【2026-11-06 第 3 轮】原来是三引号模板里嵌两段内容，会导致第二段（环境信息）
        #   前面多出 8 个空格缩进，看着很别扭。改成直接拼接，顺便去掉多余空白。
        #
        # 【2026-11-07 第 12 轮】四段式：规矩 → 项目记忆 → 自动记忆 → 环境信息。
        #   项目记忆（THINKER.md）和自动记忆（MEMORY.md）是两回事：
        #     前者是人写的项目规矩，全文注入；
        #     后者是模型自己积累的经验，只注入索引，细节靠 file_read 按需取。
        #   两者都放在中间，因为"规矩"要最先立住、"环境"要在最后便于引用。
        # 【2026-11-10 第 21 轮】变成五段：在自动记忆和环境信息之间插了"技能"。
        #   插在这里而不是末尾，是为了保住"环境永远在最后"这条第 11 轮的判断——
        #   日期/目录这类每轮都要引用的东西放尾部最容易被模型读到。
        sections = [get_system_rule()]  # 第一段：行为规矩

        memory = load_project_memory()  # 第二段：项目记忆（找不到就是空字符串）
        if memory:
            sections.append(memory)
            # 没有记忆文件时整段跳过——免得系统提示词里出现"以下是项目记忆：（空）"

        auto_memory = render_auto_memory()  # 第三段：模型自己的记忆（索引 + 使用说明）
        if auto_memory:
            sections.append(auto_memory)
            # 目录不存在时返回空串，同样整段跳过

        # 【2026-11-10 第 21 轮】第四段：技能规则（只在真的加载到技能时才拼）
        #   局部 import 的理由：core 层不该在导入期就依赖 tools 层。
        #   tools/skill 现在只读 core.limits，但以后它可能想引用提示词相关的东西，
        #   那时模块顶部的 import 就会成环——留一个函数内 import，这个坑就提前避掉了。
        #   代价是每次拼提示词多一次 dict 查找（模块已在 sys.modules 里），可以忽略。
        from tools.skill.skill_manager import SkillManager

        if SkillManager().count() > 0:
            sections.append(SKILLS_SECTION)

        sections.append(get_enviroment_info())  # 第五段：当前环境信息
        return "\n\n".join(sections).strip()
