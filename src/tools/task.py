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
# 【2026-11-06 第 8.1 轮】修掉一个现网可复现的误触发：
#   说明书里残留着从 Claude Code 抄来的示例"用户：你好 → 用 greeting-responder agent"，
#   但本项目只登记了 general-purpose。结果用户只说一句"你好"，
#   模型就照着示例派活（把类型硬凑成 general-purpose），
#   一次寒暄白开一份上下文、白等一轮审批。
#   现在：示例删除、类型表动态生成、类型不对时返回可用清单。
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
        # subagent_type：要哪种类型的子 agent（用 list_types() 取当前登记了哪些）
        if len(prompt) == 0:  # 如果说明书是空的
            return "任务说明（prompt）为空，请把要交给子 agent 的详细说明写进 prompt 参数。"
        if len(subagent_type) == 0:  # 如果没指定类型
            return self._unknown_type_message("")  # 统一走"类型不对"的提示

        # 【2026-11-06 第 8.1 轮新增】先校验类型是否存在。
        #   原来的做法是让 get_subagent_prompt() 抛 ValueError，
        #   但这个异常会被 tool_manager.run_tool 吞成一句
        #   "运行工具 'task' 时出错：子 agent 类型 'xxx' 不存在"，
        #   模型看不到"那到底有哪些可选"，多半会再猜一个类型继续撞。
        #   现在直接把可用清单交回去，它能立刻改对。
        if subagent_type not in self._subagent_manager.list_types():
            return self._unknown_type_message(subagent_type)

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

    def _unknown_type_message(self, subagent_type: str) -> str:
        """类型名不对时，把可用清单直接交回给模型（2026-11-06 第 8.1 轮新增）

        为什么不抛异常：
          异常会被 tool_manager.run_tool 接住，转成一句干巴巴的
          "运行工具 'task' 时出错：……"。模型知道"错了"，但不知道"对的是什么"，
          下一条消息十有八九再猜一个类型撞一次。
          把清单写进返回值，它同一轮就能改对，省一次往返。
        """
        available = self._subagent_manager.list_types()
        if not available:  # 一个类型都没登记（正常不会发生，除非有人清空了登记表）
            return "当前没有登记任何子 agent 类型，请不要使用这个工具，直接自己完成。"
        names = "、".join(available)  # 拼成"general-purpose"这种可读形式
        if subagent_type:  # 传了一个不认识的名字
            return (
                f"子 agent 类型 '{subagent_type}' 不存在。当前可用的类型：{names}。\n"
                "如果你原本只是要打招呼、闲聊或回答一个简单问题，请直接回答，不要派活。"
            )
        # 压根没传类型
        return f"没有指定子 agent 类型（subagent_type）。当前可用的类型：{names}。"

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
                            # 【第 8.1 轮】候选值从登记表取，不再手写，避免"说明里有、代码里没有"
                            "description": "要使用的子 agent 类型，可选：" + "、".join(self._subagent_manager.list_types())
                        }
                    },
                    "required": ["description", "prompt", "subagent_type"],  # 三个都必须给
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _types_block(self):
        """拼出"可用 agent 类型"那一段（2026-11-06 第 8.1 轮新增）

        为什么必须动态生成：
          原来这段是手写的，只写了 general-purpose；但下面的示例里又出现了
          code-reviewer 和 greeting-responder 两个根本没登记的类型。
          模型看到"用户：你好 → 启动 greeting-responder"这种字面命中的示例，
          就真的会对一句寒暄派活，白开一份上下文、白花两轮往返。
          现在登记表里有什么就写什么，说明和代码不会再对不上。
        """
        lines = []  # 每个类型一行
        for prompt_type in self._subagent_manager.list_types():  # 遍历登记表
            purpose, tools = self._subagent_manager.describe(prompt_type)  # 取用途和可用工具
            lines.append(f"- {prompt_type}：{purpose}（可用工具：{tools}）")
        if not lines:  # 一个类型都没有（理论上不会发生）
            return "- （当前没有登记任何类型，请不要使用这个工具）"
        return "\n".join(lines)

    def _tool_description(self):
        # 这段是告诉模型"什么时候该派活、什么时候不该派"的说明书。
        # 【2026-11-06 第 3 轮】整段翻译成中文，示例也换成了中文。
        # 【2026-11-06 第 8.1 轮】删掉了照抄 Claude Code 的两段 few-shot 示例：
        #   那两个示例里的 agent（code-reviewer / greeting-responder）在本项目不存在，
        #   而且"用户：你好"这种字面命中会直接把模型带偏——实测一句寒暄就触发派活。
        #   同时把类型表改成动态生成，并补上"不要派活"的具体场景。
        return f"""
启动一个新的 agent 来自主处理复杂的多步骤任务。

代价：每派一次活都会新开一份上下文，并至少多花两轮往返。能自己一句话答完的事，不要派活。

可用的 agent 类型及其可用的工具：
{self._types_block()}

使用 task 工具时，必须指定 subagent_type 参数来选择用哪种 agent。

以下情况适合用：
- 需要在大范围代码库里找一个你几次尝试都没定位到的东西
- 需要同时查多个位置、多种命名习惯，才能拼出一个结论
- 一个任务要跑很多步、会产生大量中间输出，而你只想要最后的结论

以下情况不要用这个工具：
- 打招呼、寒暄、闲聊、简单附和（比如道谢或应答），直接回答
- 单轮就能答完的问题：知识问答、解释概念、给建议、写一小段代码
- 你只是想读某个具体文件
- 你只是在找某个类定义，比如 "class Foo"
- 你只是在某一个文件或 2-3 个文件里搜代码
- 其他与上面 agent 描述无关的任务

使用说明：
1. 一次只能跑一个子 agent：同一条消息里发出多个 task 调用会依次执行，不要指望它们并行
2. agent 干完只会返回一条消息给你。它的结果用户是看不到的。要把结果展示给用户，你得自己再用文字总结一遍
3. 每次调用都是独立的、无状态的。你没法再追问它，它也没法在汇报之外联系你。所以你的 prompt 里必须写清楚要做什么、要返回什么信息，越详细越好
4. agent 的输出一般可以信任
5. 明确告诉它你是要它写代码还是只做调研（搜索、读文件等），因为它不知道用户的意图
        """.strip()

