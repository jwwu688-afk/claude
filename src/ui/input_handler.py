# =============================================================================
# 文件：src/ui/input_handler.py
# 作用：管"从键盘拿输入"。程序里所有"等你打字"的地方，最终都走到这里。
#
# 有两件事：
#   1. get_user_input  —— 等你输入一句话
#   2. wait_for_user_approval —— 等你回答"要不要执行这个危险操作"
#
# 为什么输入要用异步写法？
#   因为 input() 这个函数会一直卡着等人打字。
#   如果直接调用，整个程序（包括正在播的动画、正在收的数据）都会冻住。
#   所以这里把它丢到一个"线程池"里去等，主流程可以继续干别的。
# =============================================================================

"""
Input handling functionality for user interactions.（处理用户交互中的输入）
"""

import asyncio  # 用来做异步操作
from typing import Optional  # 类型标注工具（表示"可能是这个值，也可能没有"）

from . import theme  # 【2026-11-06 第 4 轮】输入提示符的样式从这里取


class InputHandler:
    """Handles all user input operations.（处理所有用户输入操作）"""

    def __init__(self):
        """Initialize the input handler.（初始化，这里没什么要做的）"""
        pass  # 什么都不做

    async def get_user_input(self, prompt: str = None) -> str:
        """
        Get user input asynchronously.（异步获取用户输入）

        Args:
            prompt: The prompt to display to the user（显示给用户看的提示文字）

        Returns:
            User input as string（用户输入的字符串）
        """
        # 【2026-11-06 第 4 轮】改成一个青色的 ❯ 提示符，并去掉原来单独一行的 👤
        #   原来每次输入前都打一个 emoji，屏幕上会堆一排人头，很乱。
        if prompt is None:  # 调用方没指定提示符
            prompt = theme.prompt_text()  # 用统一的 ❯
        loop = asyncio.get_event_loop()  # 拿到当前的"事件循环"（异步任务的总调度台）
        return await loop.run_in_executor(None, input, prompt)
        # 上一行是关键：把 input 函数丢到"执行器"里去跑
        #   None 表示用默认的线程池
        #   input 是要执行的函数，prompt 是传给它的参数
        # await 表示"在这儿等结果，但等着的时候不占着 CPU"
        # 这样 input() 在等键盘时，主程序还能继续处理别的事情

    async def wait_for_user_approval(self, content: str, emoji: str = "🤖") -> tuple[bool, str]:
        """
        Wait for user approval (yes/no) for a specific action.（等用户对某个操作点头或拒绝）

        Args:
            content: Content to display for approval（要展示给用户看的内容，比如"我要执行 rm -rf 了"）
            emoji: Emoji to display before the approval request（图标）

        Returns:
            Tuple of (approval_status, message)（一个元组：要不要执行 + 拒绝理由）
        """
        if emoji:  # 如果有图标
            print(emoji)  # 先打印出来
        print("Please confirm if you want to execute the tool call: ", content)  # 告诉用户"我要干这个，你同意吗"
        print("Answer yes or no, if refusing please provide a reason")  # 提示：同意就答 yes，拒绝请说明理由

        while True:  # 一直问，直到得到有效回答（这就是"死循环"的正当用法）
            user_input = await self.get_user_input()  # 等用户打字

            if "yes" in user_input.lower():  # 如果输入里包含 "yes"（不区分大小写）
                return True, ""  # 同意，理由为空
            elif "no" in user_input.lower():  # 如果输入里包含 "no"
                return False, user_input  # 拒绝，理由就是用户输入的原话
            else:  # 既没有 yes 也没有 no
                print("Invalid input, please answer yes or no")  # 让他重说

            # ⚠️ 这里用的是"包含"判断，会误判：
            #    输入 "I don't know" 里有 "no"，会被当成拒绝；
            #    输入 "cannot do that" 里也有 "no"，同样误判。
            #    更好的写法是：user_input.strip().lower() == "yes" 这样精确匹配。

    async def get_choice_input(self, prompt: str, choices: list, case_sensitive: bool = False) -> Optional[str]:
        """
        Get user input with predefined choices.（让用户从几个固定选项里选一个）

        Args:
            prompt: The prompt to display（提示文字）
            choices: List of valid choices（可选的选项列表）
            case_sensitive: Whether the choice matching is case sensitive（是否区分大小写）

        Returns:
            The chosen option or None if invalid（选中的项，或者 None 表示没选对）
        """
        print(prompt)  # 打印提示
        print(f"Options: {', '.join(choices)}")  # 把选项用逗号连起来显示，比如 "Options: a, b, c"

        user_input = await self.get_user_input()  # 等输入

        if not case_sensitive:  # 如果不区分大小写
            user_input = user_input.lower()  # 把输入转小写
            choices = [choice.lower() for choice in choices]  # 把选项也都转小写（列表推导式）

        if user_input in choices:  # 如果输入在选项里
            return user_input  # 返回这个选项
        return None  # 不在就返回 None
