# =============================================================================
# 文件：src/ui/__init__.py
# 作用：把 ui 文件夹标记成包，并把几个界面组件都暴露出来。
#
# 这样别的代码可以写：
#   from ui import UIManager
# 而不用写：
#   from ui.ui_manager import UIManager
# 少打几个字，也方便以后挪文件位置时不用改所有引用。
# =============================================================================

"""
UI module for handling user interactions and display functionality.
（界面模块：负责用户交互和显示）

Main Components:（主要组件）
- InputHandler: Handles all user input operations（管输入）
- DisplayManager: Manages all display and output operations（管显示）
- UIManager: Unified interface that coordinates input and display（协调两者的总接口）
- Base classes: Abstract interfaces for extensibility（抽象基类，方便以后扩展）
"""

from .input_handler import InputHandler  # 输入处理器
from .display_manager import DisplayManager  # 显示管理器
from .ui_manager import UIManager  # 总前台
from .base import BaseInputHandler, BaseDisplayManager, UIConfig  # 抽象基类和小配置

__all__ = [
    # __all__ 是一个列表，声明"这个包对外提供哪些名字"
    'InputHandler',
    'DisplayManager',
    'UIManager',
    'BaseInputHandler',
    'BaseDisplayManager',
    'UIConfig'
]

# Version information
__version__ = '1.0.0'  # 版本号
__author__ = 'Thinker Team'  # 作者
