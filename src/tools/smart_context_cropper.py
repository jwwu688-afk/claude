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
#
# 【2026-11-06 第 3 轮】说明书和所有返回给模型的文字已改成中文。
# =============================================================================

from typing import Optional  # 类型标注：表示"可能是这个值，也可能是 None"
from core.history.history_manager import Crop_Direction, HistoryManager  # 导入"裁剪方向"枚举和历史管理器
from tools.base_tool import BaseTool  # 导入父类


class SmartContextCropper(BaseTool):
    is_repeatable = True  # 只裁剪上下文、不产生外部副作用，结果可安全折叠

    def __init__(self):
        super().__init__()  # 父类初始化
        self._history_manager = HistoryManager()  # 拿到历史管理器（单例，跟对话用的是同一个）

    @staticmethod
    def get_tool_name():
        return "smart_context_cropper"  # 在模型眼里的名字

    @staticmethod
    def _normalize_direction(raw) -> Optional[Crop_Direction]:
        """把模型给的裁剪方向统一成枚举（2026-11-06 第 9 轮新增）

        为什么需要这一步？
          这个工具的说明书示例里写的是 "TOP" / "BOTTOM"（大写），
          而枚举值是 "top" / "bottom"。原来的代码直接拿它去和 Crop_Direction.TOP 比，
          大写的 "TOP" 比不上小写的 "top"，于是一路落到 else 分支——
          模型想从顶部删，实际却从底部删了。这种"静默走错分支"最难查。
          现在先归一化（去空格、转小写），再转成枚举；转不了就明确报错。
        """
        if isinstance(raw, Crop_Direction):  # 已经是枚举（测试里会这么传）
            return raw
        text = str(raw or "").strip().lower()  # 去空格 + 转小写
        try:
            return Crop_Direction(text)
        except ValueError:  # 既不是 top 也不是 bottom
            return None

    async def act(self, crop_direction: Crop_Direction, crop_amount: int, deleted_messages_summary: str = ""):
        # crop_direction：从头删还是从尾删
        # crop_amount：删几条
        # deleted_messages_summary：被删内容的摘要
        #
        # 【2026-11-06 第 9 轮修复】这个参数原来是"必填但没人用"：
        #   模型认真写了摘要，工具拿到手直接扔掉，于是删掉的信息真的消失了。
        #   现在把它作为一条消息插回被删位置（见 HistoryManager.crop_message 的 note 参数）。
        try:  # 试
            try:
                amount = int(crop_amount)  # 模型有时会把数字写成字符串
            except (TypeError, ValueError):
                return "裁剪数量无效：crop_amount 必须是整数。"
            if amount <= 0:  # 删除数量必须是正数
                return "裁剪数量无效：crop_amount 必须大于 0。"

            direction = self._normalize_direction(crop_direction)  # 归一化方向
            if direction is None:
                return "裁剪方向无效：只能是 top（从开头删）或 bottom（从结尾删）。"

            note = deleted_messages_summary.strip() if isinstance(deleted_messages_summary, str) else ""
            result = self._history_manager.crop_message(direction, amount, note=note)  # 让历史管理器去删

            if result.startswith("裁剪成功") and note:
                # 明确告诉模型"你写的摘要没白发"，它下次才会继续认真写
                return result + " 已把 deleted_messages_summary 作为一条消息保留在对话里。"
            return result  # 把"删成功"或"删不了"的结果返回给模型
        except Exception as e:  # 出错就捕获
            return f"smart_context_cropper 执行出错：{e}"  # 返回错误信息

    def json_schema(self):
        # 【2026-11-06 第 3 轮】参数说明改成中文（枚举值 top/bottom 保持不变，程序逻辑依赖它）
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
                            "description": "裁剪聊天记录前是否需要用户先明确同意。",
                            # 删聊天记录算是有风险的操作，所以问模型"要不要先让用户确认"
                            "default": True
                        },
                        "crop_direction": {
                            "type": "string",
                            "enum": ["top", "bottom"],  # enum 表示只能从这两个值里挑一个
                            "description": "裁剪方向。top 表示从开头删（系统提示词之后的部分），bottom 表示从结尾删。"
                        },
                        "crop_amount": {
                            "type": "integer",
                            "minimum": 1,  # 最少删 1 条
                            "description": "要删除的消息条数。不能超过「保留最新用户消息」所允许的上限。"
                        },
                        "deleted_messages_summary": {
                            "type": "string",
                            "description": "对被删内容的摘要，说明删掉了哪些信息、留下了什么结论。"
                        }
                    },
                    "required": ["need_user_approve", "crop_direction", "crop_amount", "deleted_messages_summary"]
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        # 【2026-11-06 第 3 轮】整段翻译成中文，两个示例也换成了中文。
        return """
从聊天记录的开头或结尾裁剪消息，同时保证"用户最新说的那句"永远不会被删掉。

执行 smart_context_cropper 之前，请遵循以下规则：

1. 裁剪规则
    - 永远保留最新一条用户消息。
    - 从顶部裁剪 → 删掉系统提示词之后最老的那些消息。
    - 从底部裁剪 → 优先删最近的那些消息。

2. 是否需要用户同意
    - 始终结合用户当前的任务来判断。
    - 如果你确定要删的内容与当前任务无关，可以不问直接删。
    - 只要有一点不确定，就先征求用户确认。

3. 如何处理被删内容
    - 如果被删的消息里有有用信息，删除前先用一句话把结论留下来。
    - 摘要要保留可能仍然与用户当前任务相关的上下文。
    - 如果被删内容确实没有任何有用信息，deleted_messages_summary 可以留空。

<example>
系统提示词：你是一个专业的编程助手。
用户消息：写一个计算斐波那契数列的 Python 函数。
助手：这是基础版本……
用户消息：请优化一下性能。
助手：这是优化后的版本……
</example>
<smart_context_cropper>
smart_context_cropper(
    need_user_approve = true,
    crop_direction = "top",
    crop_amount = 2,
)
</smart_context_cropper>

<example>
系统提示词：你是一个专业的排障助手。
用户消息：诊断一下我们 API 每天午夜出现的延迟尖峰。
助手：我先验证几个假设……
助手：尝试 1：开启详细日志后观察到长时间的 GC 停顿；调整了 GC 参数，但延迟没有下降。
助手：尝试 2：怀疑是数据库写锁；给热点表加了索引；没有明显改善。
助手：尝试 3：排查是否与批处理任务相关；关掉了一个实验，但尖峰依然存在。
# 把三次失败总结完之后，这些详细的尝试记录就可以从底部删掉，让上下文更聚焦。
</example>
<smart_context_cropper>
smart_context_cropper(
    need_user_approve = false,
    crop_direction = "bottom",
    crop_amount = 3,
    deleted_messages_summary = "失败总结：(1) 调整 GC 参数无效，虽然确实观察到了长停顿；(2) 数据库写锁的假设不成立——加索引并没有降低延迟；(3) 批处理任务的相关性无法确认——关掉相关实验后没有变化。结论：尖峰很可能是因为定时任务和数据库维护时段重叠。下一步：检查 00:00 的定时任务和数据库清理/备份窗口，在那段时间加临时限流或排队。"
)
</smart_context_cropper>

</examples>
    """.strip()
