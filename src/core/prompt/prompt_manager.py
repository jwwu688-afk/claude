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

from core.prompt.enviroment import get_enviroment_info  # 导入函数：生成环境信息文字
from core.prompt.memory import load as load_project_memory  # 导入函数：加载 THINKER.md
from core.prompt.system_rule import get_system_rule  # 导入函数：生成行为规矩文字


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
        sections = [get_system_rule()]  # 第一段：行为规矩

        memory = load_project_memory()  # 第二段：项目记忆（找不到就是空字符串）
        if memory:
            sections.append(memory)
            # 没有记忆文件时整段跳过——免得系统提示词里出现"以下是项目记忆：（空）"

        sections.append(get_enviroment_info())  # 第三段：当前环境信息
        return "\n\n".join(sections).strip()
