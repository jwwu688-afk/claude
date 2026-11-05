# =============================================================================
# 文件：src/tools/base_tool.py
# 作用：所有工具的"共同父类"，相当于一张"工具上岗证"。
#
# 想自己加一个新工具？只要写一个类继承 BaseTool，并实现这三个方法：
#   act()        —— 真正干活的地方（模型调用工具时执行的就是它）
#   json_schema()—— 自我介绍：我叫什么、能干什么、需要哪些参数
#   get_status() —— 汇报自己的状态（大部分工具返回空字符串就行）
#
# 为什么要统一？因为 ToolManager 只会用这三种方式对待工具，
# 不统一的话就得为每个工具写一套特殊逻辑，没法维护。
# =============================================================================

from abc import abstractmethod  # 导入"抽象方法"装饰器：用来标记"这个方法必须由子类实现"


class BaseTool:
    def __init__(self):
        pass  # 父类不需要初始化任何东西，pass 就是"什么都不做"

    @staticmethod
    def get_tool_name():
        # @staticmethod 表示这是"静态方法"：不需要创建对象就能调用，比如 CmdRunner.get_tool_name()
        return "base_agent"  # 默认名字（子类都会覆盖它，改成自己的名字）

    @abstractmethod
    async def act(self, **kwargs):
        # 干活的方法。async 表示异步（因为工具可能要等网络、等命令跑完）
        # **kwargs 表示"可以接任意多个有名参数"
        pass

    @abstractmethod
    def json_schema(self):
        # 返回一个字典，描述这个工具。大模型就是靠这份描述决定"要不要用它"的
        pass

    @abstractmethod
    def get_status(self):
        # 返回一段文字，说明工具当前状态
        pass
