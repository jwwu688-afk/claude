# =============================================================================
# 文件：src/core/__init__.py
# 作用：把一个文件夹标记成"包"（Python 里叫 package）。
#
# 有了这个文件，别的代码才能写 from core.xxx import yyy。
# 里面的内容是可选的——这里写的是"有人写 from core import Conversation 时，
# 直接把 conversation.py 里的那个类给他"，省得他写一长串路径。
# =============================================================================

from .conversation import Conversation  # 把 Conversation 类暴露到包的最外层

__all__ = ['Conversation']  # __all__ 声明"这个包对外提供哪些名字"（配合 from core import * 使用）
