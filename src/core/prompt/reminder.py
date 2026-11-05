# =============================================================================
# 文件：src/core/prompt/reminder.py
# 作用：生成一小段"提醒文字"，塞在工具执行结果后面，提醒模型去看看待办清单。
#
# 为什么需要提醒？
#   模型干着干着就容易忘了"我还有个清单要更新"。
#   每次工具执行完就附一句"顺便看看你的 todo 吧"，
#   相当于给它一个小闹钟，能明显提高它主动更新清单的概率。
#
# 这是一种很实用的小技巧：与其在系统提示词里说十遍，
# 不如在实际使用的地方反复提一句——因为最近的内容模型记得最牢。
# =============================================================================

from tools.todo_write import TodoWrite  # 导入待办工具，为了拿到它当前的状态
from tools.tool_manager import ToolManager  # 导入工具管理器，为了按名字找到那个待办工具


def get_reminder() -> str:
    # 定义函数，返回一段提醒文字
    tool_manager = ToolManager()  # 拿到工具管理器（单例，就是对话在用的那个）
    todo_status = tool_manager.get_tool_status(TodoWrite.get_tool_name())  # 查询待办工具现在装了些什么
    # 上面这行的效果：如果清单是空的，会返回"No todos in memory..."；
    # 如果有内容，会返回格式化好的 JSON 文字

    return f"""
<reminder>
## Current Todo Status
{todo_status}
Remember to check and update your todos using tool todo_write regularly to stay organized and productive.
</reminder>
""".strip()
    # 上面把状态嵌进一段固定模板里。
    # 中文大意：这是你的当前待办状态……记得经常用 todo_write 工具更新它，保持条理。
    # 用 <reminder> 这种标签包起来，是让模型能清楚区分"这是系统提醒"和"这是工具结果"
