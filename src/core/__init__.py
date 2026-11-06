# =============================================================================
# 文件：src/core/__init__.py
# 作用：把一个文件夹标记成"包"（Python 里叫 package）。
#
# 有了这个文件，别的代码才能写 from core.xxx import yyy。
#
# 【2026-11-06 优化】这里原来是直接写 from .conversation import Conversation，
#   也就是"一导入 core 这个包，就立刻把整个对话模块加载进来"。
#   结果造成循环依赖：
#     tools.tool_manager → core.history.history_manager → 触发 core 包初始化
#     → 加载 conversation → 加载 reminder → 又要 tools.tool_manager（此时还没加载完）
#     → ImportError: cannot import name 'ToolManager' from partially initialized module
#   表现就是：单独 import tools.tool_manager 会直接报错，只有从 main.py 进才不报。
#   现在改成"用到的时候才加载"（惰性导入），循环就断开了。
# =============================================================================

__all__ = ['Conversation']  # __all__ 声明"这个包对外提供哪些名字"（配合 from core import * 使用）


def __getattr__(name):
    """当有人访问 core.Conversation 时，才真去加载那个模块（2026-11-06 新增）

    这是 Python 3.7 起支持的一个机制（PEP 562）：
      访问包里的某个属性时，如果包里没有，就会调用这个 __getattr__。
    我们利用它把"导入"推迟到真正用到的那一刻。
    """
    if name == 'Conversation':  # 有人要 Conversation
        from .conversation import Conversation  # 现在才真正去加载
        return Conversation  # 交给他
    raise AttributeError(f"module 'core' has no attribute '{name}'")  # 别的名字就没有了
