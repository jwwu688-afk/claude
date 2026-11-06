# =============================================================================
# 文件：src/tools/subagent/subagent_manager.py
# 作用：管理"子 agent 的人设"。目前只登记了一种：general-purpose（通用型）。
#
# "人设"就是一段系统提示词，决定这个子 agent 的性格和工作方式。
# 比如通用型的要求是：查得细、要给绝对路径、不要主动创建文件、不要说表情包。
#
# 如果你想加一个"代码审查员"类型的子 agent，就在这里再登记一条，
# 然后在 Task 工具的说明里告诉模型"有这么个类型可用"就行。
#
# 【2026-11-06 第 3 轮】这段人设提示词已从英文翻译成中文。
# =============================================================================

# adapt from Claude Code
general_purpose_prompt = """
你是 Quick Star（Anthropic 官方的 Claude 命令行工具）的一个 agent。
拿到用户的消息后，你应该使用手头可用的工具把任务完成。
只做被要求做的事，不多做也不少做。
任务完成后，写一份详细的说明作为回复。

你的强项：
- 在大型代码库里搜索代码、配置和代码模式
- 同时分析多个文件，理解系统架构
- 排查需要翻很多文件才能搞清的复杂问题
- 执行多步骤的调研任务

工作准则：
- 搜索文件：大范围搜索时用 Grep 或 Glob；已经知道具体路径时用 Read。
- 分析问题：先看全局再逐步收窄。一种办法没有结果，就换一种策略。
- 要细致：查多个位置，考虑不同的命名习惯，找相关文件。
- 除非为了实现目标绝对必要，否则绝不要创建文件。优先修改已有文件，而不是新建。
- 不要主动创建文档（*.md）或 README，只有用户明确要求时才创建。
- 最终回复里要给出相关文件名和代码片段。回复中出现的一切文件路径都必须是绝对路径，不要用相对路径。
- 为了沟通清楚，不要使用表情符号。

注意：
- 除非为了实现目标绝对必要，否则绝不要创建文件。优先修改已有文件，而不是新建。
- 不要主动创建文档（*.md）或 README，只有用户明确要求时才创建。
- 最终回复里要给出相关文件名和代码片段。回复中出现的一切文件路径都必须是绝对路径，不要用相对路径。
- 为了和用户沟通清楚，绝对不要使用表情符号。
"""


class SubagentManager:
    # 单例：全程序共用一个登记表
    _instance = None  # 唯一实例
    _initialized = False  # 是否已初始化
    _system_prompt_map = {}  # 字典：类型名 → 对应的提示词

    def __new__(cls):
        if cls._instance is None:  # 第一次创建
            cls._instance = super(SubagentManager, cls).__new__(cls)
            cls._instance.register_subagent_prompt("general-purpose", general_purpose_prompt)
            # 登记第一种（也是目前唯一一种）子 agent：通用型
        return cls._instance

    def get_subagent_prompt(self, prompt_type):
        # 按类型名取提示词
        if prompt_type not in self._system_prompt_map:  # 如果模型编了个不存在的类型名
            raise ValueError(f"子 agent 类型 '{prompt_type}' 不存在")  # 报错
        return self._system_prompt_map[prompt_type]  # 返回对应的提示词

    def register_subagent_prompt(self, prompt_type, prompt):
        # 登记（新增）一种类型
        self._system_prompt_map[prompt_type] = prompt  # 塞进字典
