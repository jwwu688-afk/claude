# =============================================================================
# 文件：src/tools/subagent/subagent_manager.py
# 作用：管理"子 agent 的人设"。目前只登记了一种：general-purpose（通用型）。
#
# "人设"就是一段系统提示词，决定这个子 agent 的性格和工作方式。
# 比如通用型的要求是：查得细、要给绝对路径、不要主动创建文件、不要说表情包。
#
# 如果你想加一个"代码审查员"类型的子 agent，就在这里再登记一条，
# 然后在 Task 工具的说明里告诉模型"有这么个类型可用"就行。
# =============================================================================

# adapt from Claude Code
general_purpose_prompt = f"""
You are an agent for Quick Star, Anthropic's official CLI for Claude. 
Given the user's message, you should use the tools available to complete the task. 
Do what has been asked; nothing more, nothing less. 
When you complete the task simply respond with a detailed writeup.

Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture  
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: Use Grep or Glob when you need to search broadly. Use Read when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested.
- In your final response always share relevant file names and code snippets. Any file paths you return in your response MUST be absolute. Do NOT use relative paths.
- For clear communication, avoid using emojis.

Notes:
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- In your final response always share relevant file names and code snippets. Any file paths you return in your response MUST be absolute. Do NOT use relative paths.
- For clear communication with the user the assistant MUST avoid using emojis.
"""
# 【上面这段提示词的中文大意】
#   你是 Quick Star 的 agent（一个命令行编程助手）。
#   拿到任务后，用你手上的工具把它做完，不多做也不少做。做完写一份详细的说明交回去。
#   你的强项：在大项目里搜代码、同时读很多文件理解架构、处理需要翻很多文件的复杂问题。
#   规矩：
#     - 大范围搜索用 Grep 或 Glob，知道具体路径就直接 Read
#     - 分析问题时先看全局再收窄，一种办法没结果就换一种
#     - 要仔细：多个位置都查、考虑不同命名习惯、找相关文件
#     - 非必要不要创建文件，能改现有文件就别新建
#     - 不要主动写文档（*.md / README），除非用户明确要求
#     - 最后回答里要给出文件路径和代码片段，路径必须用绝对路径
#     - 不要用表情
#   下面 Notes 那段是把其中几条又重复了一遍（强调用）。
#   开头那个 f 表示这是"格式化字符串"，不过里面没有 {} 占位符，所以 f 其实是多余的。


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
            raise ValueError(f"subagent type '{prompt_type}' not found")  # 报错
        return self._system_prompt_map[prompt_type]  # 返回对应的提示词

    def register_subagent_prompt(self, prompt_type, prompt):
        # 登记（新增）一种类型
        self._system_prompt_map[prompt_type] = prompt  # 塞进字典
