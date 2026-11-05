# =============================================================================
# 文件：src/core/history/__init__.py
# 作用：把 history 文件夹标记成包，并把 HistoryManager 暴露出来。
# =============================================================================

from .history_manager import HistoryManager  # 从同目录的 history_manager.py 里导入这个类

__all__ = ['HistoryManager']  # 对外只提供这一个名字
