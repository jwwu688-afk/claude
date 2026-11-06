# =============================================================================
# 文件：src/core/prompt/prompt_manager.py
# 作用：把系统提示词的各个零件拼起来，交给对话流程使用。
#
# 现在它只做一件很简单的事：把"规矩"和"环境信息"两段拼在一起。
# 看起来有点小题大做，但这是留好的扩展位——
# 以后要加"项目说明文件（比如 QUICKSTAR.md）"、"当前时间"、"可用工具摘要"，
# 都在这个文件里加一行就行，不用去改 conversation.py。
# =============================================================================

from abc import ABC, abstractmethod  # 抽象基类工具

from core.prompt.enviroment import get_enviroment_info  # 导入函数：生成环境信息文字
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
        rule = get_system_rule()  # 行为规矩
        environment = get_enviroment_info()  # 当前环境信息
        return f"{rule}\n\n{environment}".strip()
        # 中间空一行，让"规则"和"环境"在视觉上分开，模型读起来层次更清楚
