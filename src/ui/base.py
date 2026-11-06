# =============================================================================
# 文件：src/ui/base.py
# 作用：给界面组件定"合同"。里面全是抽象方法，一行实际功能都没有。
#
# 为什么要这样？因为这样"换界面"很容易：
#   今天用命令行，明天想做个网页版，只要新写一个类实现这些方法，
#   上层代码完全不用改。这就是"面向接口编程"。
#
# 这个文件里的类：
#   BaseInputHandler   —— 输入组件的合同（要能拿用户输入、要能等用户确认）
#   BaseDisplayManager —— 显示组件的合同（要能打印消息、能流式显示）
#   UIConfig           —— 一些小配置（各种表情符号、刷新频率）
# =============================================================================

"""
Base classes and interfaces for UI components.
（界面组件的基类和接口）
"""

from abc import ABC, abstractmethod  # ABC 是抽象基类，abstractmethod 用来标记"必须实现"的方法
from typing import Optional  # 类型标注工具（这里没用到）


class BaseInputHandler(ABC):
    """Abstract base class for input handlers.（输入处理器的抽象基类）"""
    # 注意括号里写了 ABC，表示这是抽象基类，不能直接创建对象

    @abstractmethod
    async def get_user_input(self, prompt: str = "Enter: ") -> str:
        """Get user input with specified prompt.（获取用户输入，prompt 是提示文字）"""
        pass  # 只有声明没有实现

    @abstractmethod
    async def wait_for_user_approval(self, default_yes: bool = True) -> tuple[bool, str]:
        """Wait for user approval.（等用户确认：同意还是拒绝，外加可能的拒绝理由）

        【2026-11-06 第 8.2 轮】签名改了，顺手把原来"标注 bool、实际返回元组"
        的不一致也修掉：现在就是 Tuple[bool, str]，第一个是要不要执行，
        第二个是用户如果不执行时说的那句话（没有就是空串）。

        提问文字不再从参数传进来 —— 画问题归显示层（approval_question），
        输入层只负责读一行、解释一行，两件事分开才好替换。
        """
        pass


class BaseDisplayManager(ABC):
    """Abstract base class for display managers.（显示管理器的抽象基类）"""

    @abstractmethod
    def print_assistant_message(self, content: str, emoji: str = "🤖") -> None:
        """Print assistant message.（打印模型的回复，带 markdown 格式）"""
        pass

    @abstractmethod
    def start_stream_display(self, refresh_rate: int = 10) -> None:
        """Start streaming display.（开始流式显示模式）"""
        pass

    @abstractmethod
    def stop_stream_display(self) -> None:
        """Stop streaming display.（结束流式显示模式）"""
        pass

    @abstractmethod
    def print_streaming_content(self, chunk: str) -> None:
        """Print streaming content chunk.（显示一小段流式内容）"""
        pass


class UIConfig:
    """Configuration for UI components.（界面的一些小配置）"""

    def __init__(
        self,
        default_emoji_user: str = "👤",  # 用户发言前的图标
        default_emoji_assistant: str = "🤖",  # 模型发言前的图标
        default_emoji_error: str = "❌",  # 报错时的图标
        default_emoji_success: str = "✅",  # 成功时的图标
        default_emoji_info: str = "ℹ️",  # 提示信息的图标
        stream_refresh_rate: int = 10  # 流式显示每秒刷新几次
    ):
        self.default_emoji_user = default_emoji_user  # 存起来
        self.default_emoji_assistant = default_emoji_assistant
        self.default_emoji_error = default_emoji_error
        self.default_emoji_success = default_emoji_success
        self.default_emoji_info = default_emoji_info
        self.stream_refresh_rate = stream_refresh_rate
