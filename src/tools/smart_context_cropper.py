# =============================================================================
# 文件：src/tools/smart_context_cropper.py
# 作用："上下文裁剪"工具。让模型自己动手删掉聊天记录里没用的部分。
#
# 什么时候用得上？
#   比如模型试了三种方案都失败了，这三种方案的详细过程很占篇幅。
#   但结论"三条路都不通"还有用。于是模型可以先写一句结论摘要，
#   再把那三段废话删掉——这样上下文就腾出空间了。
#
# 它和 history_manager 里的"自动压缩"有什么区别？
#   自动压缩是"快满了必须删"，是粗暴的强制删除；
#   这个工具是"模型自己判断哪些没用"，更聪明，但需要模型主动想起用它。
# =============================================================================

import subprocess  # 导入了但没用到（残留代码，可以删）
from core.history.history_manager import Crop_Direction, HistoryManager  # 导入"裁剪方向"枚举和历史管理器
from tools.base_tool import BaseTool  # 导入父类


class SmartContextCropper(BaseTool):
    def __init__(self):
        super().__init__()  # 父类初始化
        self._history_manager = HistoryManager()  # 拿到历史管理器（单例，跟对话用的是同一个）

    @staticmethod
    def get_tool_name():
        return "smart_context_cropper"  # 在模型眼里的名字

    async def act(self, crop_direction: Crop_Direction, crop_amount: int, deleted_messages_summary: str):
        # crop_direction：从头删还是从尾删
        # crop_amount：删几条
        # deleted_messages_summary：被删内容的摘要（这个参数其实没被使用，见下面）
        try:  # 试
            if (crop_amount <= 0):  # 删除数量必须是正数
                return "Invalid crop amount. It must be positive."
            result = self._history_manager.crop_message(crop_direction, crop_amount)  # 让历史管理器去删
            return result  # 把"删成功"或"删不了"的结果返回给模型
            # ⚠️ 这里没有处理 deleted_messages_summary。
            #    也就是说：模型辛苦写的摘要，其实根本没被存进对话里，
            #    删掉的内容是真的消失了。这是个明显可以改进的点。
        except Exception as e:  # 出错就捕获
            return f"smart_context_cropper run Error: {e}"  # 返回错误信息

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),
                "description": self._tool_description(),  # 说明书
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": {
                            "type": "boolean",
                            "description": "Whether the crop messages action requires explicit user approval before execution",
                            # 删聊天记录算是有风险的操作，所以问模型"要不要先让用户确认"
                            "default": True
                        },
                        "crop_direction": {
                            "type": "string",
                            "enum": ["top", "bottom"],  # enum 表示只能从这两个值里挑一个
                            "description": "Direction to crop messages. 'top' removes messages from the start (after system message), 'bottom' removes from the end."
                            # top = 从开头删（系统提示词之后），bottom = 从结尾删
                        },
                        "crop_amount": {
                            "type": "integer",
                            "minimum": 1,  # 最少删 1 条
                            "description": "Number of messages to remove. Must not exceed the allowed limit for preserving the latest user message."
                        },
                        "deleted_messages_summary": {
                            "type": "string",
                            "description": "Summary of the deleted messages."  # 被删内容的摘要
                        }
                    },
                    "required": ["need_user_approve", "crop_direction", "crop_amount", "deleted_messages_summary"]
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        # 【中文大意】
        #   这个工具能从顶部或底部裁剪聊天记录，但永远不会删掉"用户最新说的那句"。
        #
        #   1. 裁剪规则
        #      - 必须保留最新一条用户消息
        #      - 从顶部删 → 删系统提示词之后最老的那些
        #      - 从底部删 → 优先删最近的那些（比如刚才失败的尝试过程）
        #
        #   2. 要不要问用户
        #      - 如果你很确定删的内容跟当前任务无关，可以直接删，不用问
        #      - 只要有一点不确定，就先问
        #
        #   3. 删除前先摘要
        #      - 如果被删的内容里有有用信息，先用一句话把结论留下来
        #      - 如果确实什么都没用，摘要就留空
        #
        #   后面跟了两个例子：一个是删顶部（简单的），一个是删底部（调试失败过程，
        #   配了一段很详细的失败总结，演示"删之前先留结论"该怎么写）。
        return """
Crop conversation history from either the top (after system messages) or the bottom, while ensuring the latest user message is never removed.

Before executing smart_context_cropper, follow these steps:
1. Crop Rules
    - Always preserve the latest user message.
    - Top crop → remove the oldest non-system messages that appear after the system message.
    - Bottom crop → remove the most recent messages first.

2. Approval Requirements
    - Always consider the user's current task.
    - If you are certain that the messages to be cropped are unrelated to the user's current task, you may proceed without explicit approval.
    - If there is any uncertainty, request confirmation before cropping.

3. Handling Removed Content
    - If cropped messages contain useful information, provide a concise summary before deletion.
    - Ensure summaries retain any context that might still be relevant to the user's ongoing task.
    - If Nothing useful related in deleted message, than deleted_messages_summary will be empty.

<example>
system_prompt: You are a professional programming assistant.
user_message: Write a Python function to compute the Fibonacci sequence.
assistant: Here's’s the basic version...
user_message: Please optimize the performance.
assistant: Here’s the optimized version...
</example>
<smart_context_cropper>
smart_context_cropper(
    need_user_approve = true,
    crop_direction = "TOP",
    crop_amount = 2,
)
</smart_context_cropper>

<example>
system_prompt: You are a professional debugging assistant.
user_message: Diagnose the midnight latency spikes in our API.
assistant: I will explore a few hypotheses...
assistant: Attempt 1: Enabled verbose logging and observed long GC pauses; tuned GC, but latency stayed high.
assistant: Attempt 2: Investigated DB write locks; added an index to the hot table; no measurable improvement.
assistant: Attempt 3: Correlated spikes with a batch job; disabled an experiment, but the spikes persisted.
# Once the failures are summarized, the detailed attempt logs can be removed from the bottom to keep context focused.
</example>
<smart_context_cropper>
smart_context_cropper(
    need_user_approve = false,
    crop_direction = "BOTTOM",
    crop_amount = 3,
    deleted_messages_summary = "Failure summary: (1) GC tuning ineffective despite observed long pauses; (2) DB write-lock hypothesis invalid—adding index did not reduce latency; (3) Batch-job correlation inconclusive—disabling related experiment had no effect. Conclusion: Spikes likely due to overlap of scheduled tasks with DB maintenance. Next steps: inspect cron jobs at 00:00 and DB vacuum/backup windows; add temporary rate-limiting or queueing around that window."
)
</smart_context_cropper>

</examples>
    """.strip()
