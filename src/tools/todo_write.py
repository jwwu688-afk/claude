# =============================================================================
# 文件：src/tools/todo_write.py
# 作用："待办清单"工具。模型用它写下一份任务清单，并在干的过程中不断打勾。
#
# 为什么这东西有用？
#   模型干长任务时容易"忘事"——做到第三步就忘了用户的原始要求。
#   让它先列出 1、2、3、4，每完成一步就更新一次清单，
#   相当于给它自己一个"进度条"，也让你能看见它在干什么。
#
# 这个文件里真正的代码只有几十行，剩下 200 多行全是写给模型看的使用说明
# （那些 <example> 就是在教模型"什么情况该用、什么情况不该用"）。
#
# 【2026-11-06 第 3 轮】说明书和所有返回给模型的文字已改成中文。
# =============================================================================

import json  # json 库：把待办清单转成格式化的文字，方便模型读
from tools.base_tool import BaseTool  # 导入父类
from ui.ui_manager import UIManager  # 导入界面管理器，用来把清单漂亮地打印出来


class TodoWrite(BaseTool):
    is_repeatable = True  # 待办清单是会话级状态，重读不产生副作用，L3 压缩可安全折叠

    def __init__(self):
        super().__init__()  # 父类初始化
        self.todos = []  # 存当前的待办清单，一开始是空的
        self._ui_manager = UIManager()  # 界面管理器

    @staticmethod
    def get_tool_name():
        return "todo_write"  # 在模型眼里的名字

    async def act(self, todos=None):
        # todos 是一个列表，每一项长这样：
        #   {"content": "写一个函数", "status": "pending", "id": "1"}
        if not todos:  # 如果没传清单（空列表或 None）
            return "没有提供任何待办事项（todos 为空）。"

        # Validate todos type
        if not isinstance(todos, list):  # isinstance 用来检查"这是不是某种类型"
            # 模型有时会把 list 写成字符串，这里要拦住并告诉它错在哪
            return f"参数类型错误：todos 应该是列表，实际收到的是 {type(todos).__name__}。收到的内容：{todos}"

        for i, todo in enumerate(todos):  # 逐项检查
            if not isinstance(todo, dict):  # 每一项必须是字典
                return f"第 {i} 项的类型错误：每一项应该是对象，实际收到的是 {type(todo).__name__}。收到的内容：{todos}"

            required_fields = ['content', 'status', 'id']  # 三个必填字段
            for field in required_fields:  # 逐个检查有没有漏
                if field not in todo:  # 如果缺了某个字段
                    return f"第 {i} 项缺少必填字段 '{field}'。收到的内容：{todos}"

            if todo['status'] not in ['pending', 'in_progress', 'completed']:  # 状态只能是这三种
                return (f"第 {i} 项的状态 '{todo['status']}' 无效。"
                        f"只能是 pending（未开始）、in_progress（进行中）、completed（已完成）之一。收到的内容：{todos}")

        self.todos = todos  # 校验通过，用新清单整个替换旧的（注意：是整体替换，不是增量修改）

        # Display the updated todos using UI manager
        self._ui_manager.display_todos(todos)  # 在界面上把清单画出来

        return f"待办清单已更新，共 {len(todos)} 项。"  # 告诉模型"更新成功了"

    def json_schema(self):
        # 【2026-11-06 第 3 轮】参数说明改成中文（枚举值 pending/in_progress/completed 保持不变，
        # 因为它们是程序内部逻辑依赖的数据格式，改了就坏）
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # todo_write
                "description": self._tool_description(),  # 说明书（文件末尾那一大段）
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",  # 数组（列表）
                            "items": {  # items 描述"列表里每一项长什么样"
                                "type": "object",
                                "properties": {
                                    "content": {
                                        "type": "string",
                                        "minLength": 1  # 内容至少 1 个字，不能是空的
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": [  # 只能是这三个值之一
                                            "pending",  # 待办（还没开始）
                                            "in_progress",  # 进行中（同一时间只该有一个）
                                            "completed"  # 已完成
                                        ]
                                    },
                                    "id": {
                                        "type": "string"  # 编号，用字符串形式（比如 "1"）
                                    }
                                },
                                "required": ["content", "status", "id"],  # 三项必填
                                "additionalProperties": False
                                # 不允许出现别的字段（防止模型乱加东西）
                            },
                            "description": "更新后的完整待办清单（注意是整体替换，不是增量修改）"
                        }
                    },
                    "required": ["todos"],
                    "additionalProperties": False
                }
            }
        }

    def get_status(self):
        # 这个方法会在每次工具执行完后被调用，用来生成"提醒模型看清单"的那段文字
        if not self.todos:  # 清单是空的
            return "当前没有任何待办事项，还没有添加过。"
        return json.dumps({"todos": self.todos}, indent=2, ensure_ascii=False)  # 转成带缩进的 JSON 文字，方便模型读
        # indent=2 表示每层缩进 2 个空格，纯粹为了好看
        # ensure_ascii=False 让中文原样输出，而不是变成 \u4e2d 这种转义码

    # adapt from claude code
    def _tool_description(self):
        # 这段说明书特别长，因为它要"教会"模型什么时候该用这个工具。
        # 【2026-11-06 第 3 轮】整段翻译成中文，结构和示例一一对应：
        #   一、什么时候该用（6 种情况）
        #   二、什么时候不该用（4 种情况）
        #   三、四个"该用"的例子
        #   四、四个"不该用"的例子
        #   五、状态管理规则
        return """
用这个工具为当前编码会话创建和管理一份结构化的任务清单。它能帮你跟踪进度、整理复杂任务，也让用户看到你的工作是有条理的。
它还能让用户了解当前任务的进度，以及整体请求的完成情况。

#### 什么时候该用这个工具
在以下场景中主动使用这个工具：

1. 复杂的多步骤任务——任务需要 3 个以上不同的步骤或动作
2. 需要仔细规划、涉及多次操作的非平凡任务
3. 用户明确要求使用待办清单
4. 用户一次给出多项任务（编号列表，或用逗号分隔）时
5. 刚收到新指令时——立刻把用户的要求转成待办事项
6. 开始做某项任务时——**动工之前**先把它标记为 in_progress。理想情况下同一时间只应有一项是 in_progress
7. 完成一项任务后——立刻标记为 completed，并把过程中发现的新任务补进清单

#### 什么时候不该用这个工具

以下情况请跳过这个工具：
1. 只有一个简单的任务
2. 任务太琐碎，跟踪它没有任何组织上的收益
3. 三步以内就能完成
4. 纯粹是闲聊或询问信息

注意：如果只有一个琐碎的任务，不要用这个工具，直接做就行。

#### 该用待办清单的例子

<example>
用户：我想在应用设置里加一个暗黑模式开关。做完记得跑测试和构建！
助手：我来帮你在设置里加暗黑模式开关。先建一个待办清单来跟踪这次实现。
*创建包含以下项的清单：*
1. 在设置页创建暗黑模式开关组件
2. 添加暗黑模式的状态管理（context/store）
3. 实现暗黑主题的 CSS-in-JS 样式
4. 更新现有组件以支持主题切换
5. 运行测试和构建，并修掉所有失败或报错
*开始做第一项*

<reasoning>
助手使用待办清单的原因：
1. 加暗黑模式是多步骤功能，涉及 UI、状态管理和样式
2. 用户明确要求做完后跑测试和构建
3. 助手据此把"确保测试和构建通过"作为最后一项
</reasoning>
</example>

<example>
用户：帮我把项目里所有 getCwd 函数重命名为 getCurrentWorkingDirectory
助手：我先搜一下代码库里所有出现 'getCwd' 的地方。
*用 grep 或搜索工具定位所有出现位置*
助手：我在 8 个文件里找到了 15 处 getCwd。现在建一个待办清单来跟踪这些修改。
*创建清单，每个需要改的文件对应一项*

<reasoning>
助手使用待办清单的原因：
1. 先搜索，弄清任务范围
2. 发现涉及多个文件、多处出现，判定这是复杂多步骤任务
3. 清单能确保每一处都被系统地跟踪和更新，避免漏改、保持代码一致
</reasoning>
</example>

<example>
用户：我要给电商网站实现这些功能：用户注册、商品目录、购物车、结算流程。
助手：我来实现这些功能。先把所有功能加到待办清单里。
*根据项目架构把每个功能拆成具体任务*
助手：先从用户注册开始。这涉及创建数据库模型、API 端点，以及和现有认证系统对接的前端表单。

<reasoning>
助手使用待办清单的原因：
1. 用户一次给了多个复杂功能
2. 清单能把大功能拆成可管理的任务
3. 便于跟踪整个实现过程的进度
</reasoning>
</example>

<example>
用户：能帮我优化一下 React 应用吗？渲染很慢，有性能问题。</user>
助手：我来帮你优化。先检查代码库，找出性能瓶颈。
*检查组件结构、渲染模式、状态管理、数据获取方式*
助手：分析后发现几个性能问题。建一个待办清单来跟踪优化工作。
*创建包含以下项的清单：1) 对 ProductList 里开销大的计算做 memo 化，2) 给 Dashboard 的长列表加虚拟滚动，3) 优化 Gallery 组件的图片加载，4) 修掉 ShoppingCart 里的状态更新死循环，5) 检查打包体积并做代码分割*
先从 ProductList 里开销大的计算做 memo 化开始。</assistant>

<reasoning>
助手使用待办清单的原因：
1. 先检查代码库，找出具体性能问题
2. 基于分析确定了多个优化点
3. 性能优化是非平凡任务，需要多步骤执行
4. 清单能系统地跟踪各个组件的改进，确保瓶颈都被处理
</reasoning>
</example>

#### 不该用待办清单的例子

<example>
用户：Python 里怎么打印 'Hello World'？
助手：在 Python 里可以这样打印 "Hello World"：

python
print("Hello World")

运行后会在控制台输出 Hello World。</assistant>

<reasoning>
助手没有使用待办清单，因为这是一个一步就能完成的简单任务，没必要跟踪多个步骤。
</reasoning>
</example>

<example>
用户：git status 命令是干什么的？
助手：git status 命令显示你工作目录和暂存区的当前状态。它会告诉你哪些改动已暂存、哪些还没暂存、哪些文件还没被 Git 跟踪。这是提交前了解仓库状态的基础命令。

<reasoning>
助手没有使用待办清单，因为这是纯信息类问题，没有实际的编码任务要完成。用户只是想要一个解释，并不需要助手执行多步操作。
</reasoning>
</example>

<example>
用户：你能给 calculateTotal 函数加一段注释说明它做什么吗？
助手：好的，我来给 calculateTotal 函数加一段注释。
* 用编辑工具给 calculateTotal 函数加注释 *

<reasoning>
助手没有使用待办清单，因为这是局限在代码某一处的简单任务，加一段注释不需要跟踪多个步骤或系统化组织。
</reasoning>
</example>

<example>
用户：帮我运行 npm install，告诉我结果。
助手：好的，我来运行 npm install。

*执行：npm install*

命令执行成功，输出如下：
[npm install 的输出]

根据你的 package.json，所有依赖都已安装完成。

<reasoning>
助手没有使用待办清单，因为这是一条命令、结果立刻可得，没有多个步骤需要跟踪。
</reasoning>
</example>

#### 任务状态与管理

1. **任务状态**：用这三种状态跟踪进度：
   - pending：尚未开始
   - in_progress：正在处理（同一时间只允许一个）
   - completed：已成功完成

2. **任务管理**：
   - 边做边实时更新状态
   - 完成一项就**立刻**标记，不要攒着一起改
   - 任何时刻只允许有一个 in_progress
   - 先完成当前任务，再开始新的
   - 已经无关的任务直接从清单里删掉

3. **完成标准**：
   - 只有**完全**做完才能标记为 completed
   - 遇到错误、被阻塞、做不完时，保持 in_progress
   - 被阻塞时，新建一个任务描述"需要解决什么"
   - 以下情况绝不能标记为 completed：
     - 测试还在失败
     - 只实现了一部分
     - 还有未解决的错误
     - 找不到必要的文件或依赖

4. **任务拆分**：
   - 每一项都要具体、可执行
   - 把复杂任务拆成更小、更容易处理的步骤
   - 用清晰、有描述性的任务名

拿不准的时候，就用这个工具。主动管理任务能体现你的细致，也能确保所有要求都被完成。
        """.strip()
