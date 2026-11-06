# =============================================================================
# 文件：src/tools/__init__.py
# 作用：把 tools 文件夹标记成包，并把 ToolManager 暴露出来。
#
# 【2026-11-06 优化】这里原来写的是 from .tool_manager import ToolManager，
#   也就是"一导入 tools 包，就立刻把工具管理器加载进来"。
#   而 tool_manager 又会去导入各个具体工具，其中有的会绕回去导入 core 包，
#   core 包又急着加载 conversation……绕成一圈，最后就报
#   "cannot import name 'ToolManager' from partially initialized module"。
#   现在改成惰性导入：只有真正访问 tools.ToolManager 时才加载。
# =============================================================================

__all__ = ['ToolManager']  # 对外只提供这一个名字


def __getattr__(name):
    """访问 tools.ToolManager 时才真正加载模块（2026-11-06 新增，原理同 core/__init__.py）"""
    if name == 'ToolManager':  # 有人要 ToolManager
        from .tool_manager import ToolManager  # 现在才加载
        return ToolManager
    raise AttributeError(f"module 'tools' has no attribute '{name}'")
