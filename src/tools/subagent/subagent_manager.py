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
# 【2026-11-06 第 8 轮】原来这里让子 agent "用 Grep / Glob / Read"，
#   但工具清单里根本没有这三个名字，派活时子 agent 只会反复收到"找不到这个工具"。
#   已改成实际登记的 file_grep / file_glob / file_read。
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
- 搜索文件：按文件名找用 file_glob，按内容搜用 file_grep，已经知道具体路径时用 file_read。不要用 cmd_runner 去拼 grep / find 命令（Windows 的 cmd.exe 里默认没有这些命令）。
- 修改文件：改文件里的一小段用 file_edit，整体重写或新建用 file_write；两者都要求先用 file_read 读过目标文件。
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
    _system_prompt_map = {}  # 字典：类型名 → 对应的提示词（真正发给子 agent 的那段）
    # 【2026-11-06 第 8.1 轮新增】下面两张表存的是"给主 agent 看的说明"，
    #   和上面的人设提示词是两回事：人设进子 agent 的 system prompt，
    #   这两张表进 task 工具的说明书（也就是每次请求都发给模型的 tools 字段）。
    #   分开存的原因：原来 task.py 里把这句说明手写在描述里，
    #   结果描述里写了 general-purpose，示例里又冒出 greeting-responder
    #   和 code-reviewer 两个根本没登记的类型，模型照着示例派活。
    #   现在统一从这里取，登记了什么就只说有什么。
    _description_map = {}  # 字典：类型名 → 一句话用途
    _tools_map = {}  # 字典：类型名 → 可用工具说明

    def __new__(cls):
        if cls._instance is None:  # 第一次创建
            cls._instance = super(SubagentManager, cls).__new__(cls)
            cls._instance.register_subagent_prompt(
                "general-purpose",  # 类型名
                general_purpose_prompt,  # 人设提示词
                description=(
                    "通用型 agent，用于调研复杂问题、搜索代码、执行多步骤任务。"
                    "当你搜索某个关键词或文件、前几次尝试没有把握能找到时，用这个 agent 帮你搜。"
                ),
                tools="全部",
            )
            # 登记第一种（也是目前唯一一种）子 agent：通用型
        return cls._instance

    def get_subagent_prompt(self, prompt_type):
        # 按类型名取提示词
        if prompt_type not in self._system_prompt_map:  # 如果模型编了个不存在的类型名
            raise ValueError(f"子 agent 类型 '{prompt_type}' 不存在")  # 报错
        return self._system_prompt_map[prompt_type]  # 返回对应的提示词

    def list_types(self):
        """列出所有已登记的类型名（2026-11-06 第 8.1 轮新增）

        给 task 工具拼说明书用，也用来校验模型传进来的类型是否存在。
        这样以后新增一个类型（比如 code-reviewer）只需要在 __new__ 里多登记一条，
        工具说明、参数校验、错误提示三处会自动跟着变，不用再改第二个文件。
        """
        return list(self._system_prompt_map.keys())

    def describe(self, prompt_type):
        """取某个类型的"用途 + 可用工具"（2026-11-06 第 8.1 轮新增）

        返回一个二元组 (用途说明, 可用工具)，没登记过就返回空串和"未说明"。
        只用于拼说明书，不参与执行，所以取不到也不该抛异常。
        """
        return (
            self._description_map.get(prompt_type, ""),
            self._tools_map.get(prompt_type, "未说明"),
        )

    def register_subagent_prompt(self, prompt_type, prompt, description="", tools="全部"):
        # 登记（新增）一种类型
        self._system_prompt_map[prompt_type] = prompt  # 塞进字典
        # 【2026-11-06 第 8.1 轮】后两个参数带默认值，是为了兼容旧写法：
        #   原来只有两个参数，老代码调用不会因为少传而报错。
        self._description_map[prompt_type] = description  # 用途说明（给主 agent 看）
        self._tools_map[prompt_type] = tools  # 可用工具（给主 agent 看）
