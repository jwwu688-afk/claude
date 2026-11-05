# =============================================================================
# 文件：src/tools/task.py
# 作用：子任务工具（名字叫 task）。它让"主 agent"能把一件复杂的活分包出去。
#
# 打个比方：你（主 agent）在写一篇文章，需要有人先去查资料。
# 你可以喊一个实习生（子 agent）去查，他查完只把结论交给你，
# 中间翻了多少网页、走了多少弯路，你都不用管——这就是"节省上下文"。
#
# 两个关键设计：
#   1. 只有主 agent 能用这个工具（子 agent 的清单里根本没这个选项）
#   2. 子 agent 干活的聊天记录会单独存，干完就丢，只把最后一段话带回来
# =============================================================================

from tools.base_tool import BaseTool  # 导入父类
from tools.subagent.subagent_manager import SubagentManager  # 导入"子 agent 人设管理器"
from ui.ui_manager import UIManager  # 导入界面管理器，用来提示"正在派活"


class Task(BaseTool):
    def __init__(self):
        super().__init__()  # 父类初始化
        self.todos = []  # 这个字段其实没用到（复制过来的残留），可以忽略
        self._ui_manager = UIManager()  # 拿界面管理器
        self._has_initialized_task = False  # 标记"是不是正在跑子任务"
        self._subagent_manager = SubagentManager()  # 拿人设管理器（决定子 agent 的性格设定）

    @staticmethod
    def get_tool_name():
        return "task"  # 在模型眼里的名字

    async def act(self, description, prompt, subagent_type):
        # description：三五句话说明"这是个什么活"（给界面显示用）
        # prompt：详细的活儿说明书（子 agent 真正看的是这个）
        # subagent_type：要哪种类型的子 agent（目前只有 general-purpose 一种）
        if len(prompt) == 0:  # 如果说明书是空的
            return "Prompt is empty"  # 拒绝执行
        if len(subagent_type) == 0:  # 如果没指定类型
            return "Subagent type is empty"

        from core.conversation import Conversation
        # 注意：这行 import 写在函数内部而不是文件开头，是为了避免"循环导入"。
        # 因为 conversation.py 会导入 tool_manager.py，tool_manager.py 又导入本文件，
        # 如果本文件在开头就导入 conversation，两边会互相等着对方，卡死。
        conversation = Conversation()  # 拿到那个唯一的"对话总管"

        # Second gate: a sub-agent must not delegate further.
        # First gate: Conversation._get_available_tools() hides `task` from its schema.
        # 翻译：这是第二道闸门。第一道闸门是"清单里不给它这个选项"，这里是"就算它硬要调也拦住"。
        if conversation.is_subagent:  # 如果我现在本身就是个子 agent
            return ("Task tool is only available to the main agent. "
                    "Sub-agents must finish the assigned task themselves and report the result; "
                    "do not delegate further.")
            # 返回一句拒绝的话，让模型知道"你不能再往下派活了，自己干"

        # Set only when we are really going to work: keeps the flag False on the
        # refusal path above.
        # 翻译：只有确实要开工了才把这盏灯点亮，上面被拒绝的那条路上它还保持 False
        self._has_initialized_task = True  # 标记"正在跑子任务"
        self._ui_manager.print_info(f"Submitting task to {subagent_type} sub-agent: {description}\n Prompt: {prompt}")
        # 在界面上提示一下"我把活派给谁了、活是什么"
        subagent_system_prompt = self._subagent_manager.get_subagent_prompt(subagent_type)
        # 取出这类子 agent 的"人设设定"（比如：你是专门查代码的，要详尽、要给绝对路径）
        response = await conversation.start_task(task_system_prompt=subagent_system_prompt, user_input=prompt)
        # 让对话总管新开一页，跑完这个任务，把最终答复拿回来

        self._has_initialized_task = False  # 灯灭掉，表示子任务结束了
        return f"Task Finished with response: {response}"  # 把结果包一层，交回给主 agent

    def json_schema(self):
        return {
            "type": "function",  # 固定写法
            "function": {
                "name": self.get_tool_name(),  # 工具名 task
                "description": self._tool_description(),  # 说明书（文件末尾那一大段）
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "A short (3-5 word) description of the task"  # 三五个词的简短描述
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The task for the agent to perform"  # 详细的任务说明
                        },
                        "subagent_type": {
                            "type": "string",
                            "description": "The type of specialized agent to use for this task"  # 用哪种子 agent
                        }
                    },
                    "required": ["description", "prompt", "subagent_type"],  # 三个都必须给
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        # 这段是告诉模型"什么时候该派活、什么时候不该派"的说明书。
        # 【中文大意】
        #   1. 这个工具能启动一个能自己干活的 agent，用来处理复杂的多步骤任务
        #   2. 目前只有一种类型：general-purpose（通用型，什么工具都能用）
        #   3. 什么时候【不该】用：只是读某个文件、只是找某个类定义、只在 2-3 个文件里搜东西
        #   4. 使用要点：
        #      - 能并行就并行（一次发多个 task 申请）
        #      - 子 agent 是"一次性"的，它干完只回一段话，之后就不能再追问了
        #        所以你的说明书必须写得非常详细，明确要它返回什么
        #      - 要明确告诉它"是写代码还是只做调研"，因为它不知道你的意图
        #   5. 后面跟了几个示例，演示"写完代码后叫 code-reviewer 来审查"这种用法
        return """
Launch a new agent to handle complex, multi-step tasks autonomously. 

Available agent types and the tools they have access to:
- general-purpose: General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. (Tools: *)

When using the Task tool, you must specify a subagent_type parameter to select which agent type to use.

When NOT to use the Agent tool:
- When you want to read a specific file path
- When you are searching for a specific class definition like "class Foo"
- When you are searching for code within a specific file or set of 2-3 files
- Other tasks that are not related to the agent descriptions above

Usage notes:
1. Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses
2. When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result.
3. Each agent invocation is stateless. You will not be able to send additional messages to the agent, nor will the agent be able to communicate with you outside of its final report. Therefore, your prompt should contain a highly detailed task description for the agent to perform autonomously and you should specify exactly what information the agent should return back to you in its final and only message to you.
4. The agent's outputs should generally be trusted
5. Clearly tell the agent whether you expect it to write code or just to do research (search, file reads, etc.), since it is not aware of the user's intent
6. If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement.

Example usage:

<example_agent_descriptions>
"code-reviewer": use this agent after you are done writing a signficant piece of code
"greeting-responder": use this agent when to respond to user greetings with a friendly joke
</example_agent_description>

<example>
user: "Please write a function that checks if a number is prime"
assistant: Sure let me write a function that checks if a number is prime
assistant: First let me use the Write tool to write a function that checks if a number is prime
assistant: I'm going to use the Write tool to write the following code:
<code>
function isPrime(n) {
  if (n <= 1) return false
  for (let i = 2; i * i <= n; i++) {
    if (n % i === 0) return false
  }
  return true
}
</code>
<commentary>
Since a signficant piece of code was written and the task was completed, now use the code-reviewer agent to review the code
</commentary>
assistant: Now let me use the code-reviewer agent to review the code
assistant: Uses the Task tool to launch the with the code-reviewer agent 
</example>

<example>
user: "Hello"
<commentary>
Since the user is greeting, use the greeting-responder agent to respond with a friendly joke
</commentary>
assistant: "I'm going to use the Task tool to launch the with the greeting-responder agent"
</example>
        """.strip()
