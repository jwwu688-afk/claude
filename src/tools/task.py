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
#
# 【2026-11-06 第 3 轮】说明书和所有返回给模型的文字已改成中文。
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
            return "任务说明（prompt）为空，请把要交给子 agent 的详细说明写进 prompt 参数。"
        if len(subagent_type) == 0:  # 如果没指定类型
            return "没有指定子 agent 类型（subagent_type），目前可用的类型是 general-purpose。"

        from core.conversation import Conversation
        # 注意：这行 import 写在函数内部而不是文件开头，是为了避免"循环导入"。
        # 因为 conversation.py 会导入 tool_manager.py，tool_manager.py 又导入本文件，
        # 如果本文件在开头就导入 conversation，两边会互相等着对方，卡死。
        conversation = Conversation()  # 拿到那个唯一的"对话总管"

        # Second gate: a sub-agent must not delegate further.
        # First gate: Conversation._get_available_tools() hides `task` from its schema.
        # 翻译：这是第二道闸门。第一道闸门是"清单里不给它这个选项"，这里是"就算它硬要调也拦住"。
        if conversation.is_subagent:  # 如果我现在本身就是个子 agent
            return ("task 工具只对主 agent 开放。"
                    "子 agent 必须自己完成分配给它的任务并汇报结果，不要再往下派活。")
            # 返回一句拒绝的话，让模型知道"你不能再往下派活了，自己干"

        # Set only when we are really going to work: keeps the flag False on the
        # refusal path above.
        # 翻译：只有确实要开工了才把这盏灯点亮，上面被拒绝的那条路上它还保持 False
        self._has_initialized_task = True  # 标记"正在跑子任务"
        self._ui_manager.print_info(f"正在把任务交给 {subagent_type} 子 agent：{description}\n 任务说明：{prompt}")
        # 在界面上提示一下"我把活派给谁了、活是什么"
        subagent_system_prompt = self._subagent_manager.get_subagent_prompt(subagent_type)
        # 取出这类子 agent 的"人设设定"（比如：你是专门查代码的，要详尽、要给绝对路径）
        response = await conversation.start_task(task_system_prompt=subagent_system_prompt, user_input=prompt)
        # 让对话总管新开一页，跑完这个任务，把最终答复拿回来

        self._has_initialized_task = False  # 灯灭掉，表示子任务结束了
        return f"任务已结束，子 agent 的汇报如下：\n{response}"  # 把结果包一层，交回给主 agent

    def json_schema(self):
        # 【2026-11-06 第 3 轮】参数说明改成中文
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
                            "description": "用 3-5 个词简要描述这个任务"  # 三五个词的简短描述
                        },
                        "prompt": {
                            "type": "string",
                            "description": "要交给子 agent 执行的详细任务说明"  # 详细的任务说明
                        },
                        "subagent_type": {
                            "type": "string",
                            "description": "要使用的子 agent 类型，目前只有 general-purpose"  # 用哪种子 agent
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
        # 【2026-11-06 第 3 轮】整段翻译成中文，示例也换成了中文。
        return """
启动一个新的 agent 来自主处理复杂的多步骤任务。

可用的 agent 类型及其可用的工具：
- general-purpose：通用型 agent，用于调研复杂问题、搜索代码、执行多步骤任务。当你搜索某个关键词或文件、前几次尝试没有把握能找到时，用这个 agent 帮你搜。（可用工具：全部）

使用 task 工具时，必须指定 subagent_type 参数来选择用哪种 agent。

以下情况不要用这个工具：
- 你只是想读某个具体文件
- 你只是在找某个类定义，比如 "class Foo"
- 你只是在某一个文件或 2-3 个文件里搜代码
- 其他与上面 agent 描述无关的任务

使用说明：
1. 能并行就并行：需要同时做多件事时，在一条消息里发出多个 task 调用，性能最好
2. agent 干完只会返回一条消息给你。它的结果用户是看不到的。要把结果展示给用户，你得自己再用文字总结一遍
3. 每次调用都是独立的、无状态的。你没法再追问它，它也没法在汇报之外联系你。所以你的 prompt 里必须写清楚要做什么、要返回什么信息，越详细越好
4. agent 的输出一般可以信任
5. 明确告诉它你是要它写代码还是只做调研（搜索、读文件等），因为它不知道用户的意图
6. 如果某个 agent 的描述里写了"应当主动使用"，那就尽量在用户没要求时也主动用上，这点靠你自己判断

使用示例：

<example_agent_descriptions>
"code-reviewer"：写完一段比较重要的代码后，用它来审查
"greeting-responder"：用户打招呼时，用它来回一句友好的玩笑
</example_agent_description>

<example>
用户："请写一个判断数字是否为质数的函数"
助手：好的，我来写一个判断质数的函数
助手：先用写文件的工具把函数写出来
助手：我要写入的代码如下：
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
因为刚写完了一段比较重要的代码且任务已完成，现在用 code-reviewer agent 来审查
</commentary>
助手：现在用 code-reviewer agent 审查这段代码
助手：通过 task 工具启动 code-reviewer agent
</example>

<example>
用户："你好"
<commentary>
用户在打招呼，用 greeting-responder agent 回一句友好的玩笑
</commentary>
助手："我用 task 工具启动 greeting-responder agent"
</example>
        """.strip()
