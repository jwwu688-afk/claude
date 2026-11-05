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
# =============================================================================

import json  # json 库：把待办清单转成格式化的文字，方便模型读
from tools.base_tool import BaseTool  # 导入父类
from ui.ui_manager import UIManager  # 导入界面管理器，用来把清单漂亮地打印出来


class TodoWrite(BaseTool):
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
            return "No todos provided"

        # Validate todos type
        if not isinstance(todos, list):  # isinstance 用来检查"这是不是某种类型"
            # 模型有时会把 list 写成字符串，这里要拦住并告诉它错在哪
            return f"Error: todos type is wrong. Expected list, got {type(todos).__name__}. todos = {todos}"

        for i, todo in enumerate(todos):  # 逐项检查
            if not isinstance(todo, dict):  # 每一项必须是字典
                return f"Error: todo item {i} type is wrong. Expected dict, got {type(todo).__name__}. todos = {todos}"

            required_fields = ['content', 'status', 'id']  # 三个必填字段
            for field in required_fields:  # 逐个检查有没有漏
                if field not in todo:  # 如果缺了某个字段
                    return f"Error: todo item {i} missing required field '{field}'. todos = {todos}"

            if todo['status'] not in ['pending', 'in_progress', 'completed']:  # 状态只能是这三种
                return f"Error: todo item {i} has invalid status '{todo['status']}'. Must be one of: pending, in_progress, completed. todos = {todos}"

        self.todos = todos  # 校验通过，用新清单整个替换旧的（注意：是整体替换，不是增量修改）

        # Display the updated todos using UI manager
        self._ui_manager.display_todos(todos)  # 在界面上把清单画出来

        return f"Successfully updated todo list with {len(todos)} todos"  # 告诉模型"更新成功了"

    def json_schema(self):
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
                            "description": "The updated todo list"  # 说明：这是"更新后的完整清单"，不是增量
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
            return "No todos in memory - no todos have been added yet"
        return json.dumps({"todos": self.todos}, indent=2)  # 转成带缩进的 JSON 文字，方便模型读
        # indent=2 表示每层缩进 2 个空格，纯粹为了好看

    # adapt from claude code
    def _tool_description(self):
        # 这段说明书特别长，因为它要"教会"模型什么时候该用这个工具。
        # 【中文大纲】
        #   一、什么时候该用（6 种情况）
        #      复杂多步骤任务、需要仔细规划的任务、用户明确要求列清单、
        #      用户一口气给了好几件事、刚接到新指令、开始干活前先标记进行中、
        #      干完一件立刻标记完成
        #   二、什么时候不该用（4 种情况）
        #      只有一步的、太琐碎不值得跟踪的、三步以内能搞定的、纯聊天或问知识的
        #   三、四个"该用"的例子
        #      加暗黑模式开关 / 重命名函数（15 处，8 个文件）/ 实现电商四个模块 / 优化 React 性能
        #   四、四个"不该用"的例子
        #      问怎么打印 Hello World / 问 git status 是什么 / 让加一行注释 / 让跑 npm install
        #   五、状态管理规则
        #      pending / in_progress / completed 三种状态怎么流转；
        #      同一时间只允许一个 in_progress；
        #      没完全做完不许标 completed（测试还红着、只做了一半、有报错都不算完）
        return """
Use this tool to create and manage a structured task list for your current coding session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

#### When to Use This Tool
Use this tool proactively in these scenarios:

1. Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as todos
6. When you start working on a task - Mark it as in_progress BEFORE beginning work. Ideally you should only have one todo as in_progress at a time
7. After completing a task - Mark it as completed and add any new follow-up tasks discovered during implementation

#### When NOT to Use This Tool

Skip using this tool when:
1. There is only a single, straightforward task
2. The task is trivial and tracking it provides no organizational benefit
3. The task can be completed in less than 3 trivial steps
4. The task is purely conversational or informational

NOTE that you should not use this tool if there is only one trivial task to do. In this case you are better off just doing the task directly.

#### Examples of When to Use the Todo List

<example>
User: I want to add a dark mode toggle to the application settings. Make sure you run the tests and build when you're done!
Assistant: I'll help add a dark mode toggle to your application settings. Let me create a todo list to track this implementation.
*Creates todo list with the following items:*
1. Create dark mode toggle component in Settings page
2. Add dark mode state management (context/store)
3. Implement CSS-in-JS styles for dark theme
4. Update existing components to support theme switching
5. Run tests and build process, addressing any failures or errors that occur
*Begins working on the first task*

<reasoning>
The assistant used the todo list because:
1. Adding dark mode is a multi-step feature requiring UI, state management, and styling changes
2. The user explicitly requested tests and build be run afterward
3. The assistant inferred that tests and build need to pass by adding "Ensure tests and build succeed" as the final task
</reasoning>
</example>

<example>
User: Help me rename the function getCwd to getCurrentWorkingDirectory across my project
Assistant: Let me first search through your codebase to find all occurrences of 'getCwd'.
*Uses grep or search tools to locate all instances of getCwd in the codebase*
Assistant: I've found 15 instances of 'getCwd' across 8 different files. Let me create a todo list to track these changes.
*Creates todo list with specific items for each file that needs updating*

<reasoning>
The assistant used the todo list because:
1. First, the assistant searched to understand the scope of the task
2. Upon finding multiple occurrences across different files, it determined this was a complex task with multiple steps
3. The todo list helps ensure every instance is tracked and updated systematically
4. This approach prevents missing any occurrences and maintains code consistency
</reasoning>
</example>


<example>
User: I need to implement these features for my e-commerce site: user registration, product catalog, shopping cart, and checkout flow.
Assistant: I'll help implement these features. First, let's add all the features to the todo list.
*Creates a todo list breaking down each feature into specific tasks based on the project architecture*
Assistant: Let's start with user registration. This will involve creating database models, API endpoints, and frontend forms that integrate with your existing authentication system.

<reasoning>
The assistant used the todo list because:
1. The user provided multiple complex features to implement in a comma separated list
2. The todo list helps organize these large features into manageable tasks
3. This approach allows for tracking progress across the entire implementation
</reasoning>
</example>

<example>
User: Can you help optimize my React application? It's rendering slowly and has performance issues.</user>
Assistant: I'll help optimize your React application. First, let me examine your codebase to identify potential performance bottlenecks.
*Reviews component structure, render patterns, state management, and data fetching*
Assistant: After analyzing your codebase, I've identified several performance issues. Let me create a todo list to track our optimization efforts.
*Creates todo list with items like: 1) Implement memoization for expensive calculations in ProductList, 2) Add virtualization for long lists in Dashboard, 3) Optimize image loading in Gallery component, 4) Fix state update loops in ShoppingCart, 5) Review bundle size and implement code splitting*
Let's start by implementing memoization for the expensive calculations in your ProductList component.</assistant>

<reasoning>
The assistant used the todo list because:
1. First, the assistant examined the codebase to identify specific performance issues
2. Based on this analysis, it identified multiple optimization opportunities
3. Performance optimization is a non-trivial task requiring multiple steps
4. The todo list helps methodically track improvements across different components
5. This systematic approach ensures all performance bottlenecks are addressed
</reasoning>
</example>

#### Examples of When NOT to Use the Todo List

<example>
User: How do I print 'Hello World' in Python?
Assistant: In Python, you can print "Hello World" with this simple code:

python
print("Hello World")

This will output the text "Hello World" to the console when executed.</assistant>

<reasoning>
The assistant did not use the todo list because this is a single, trivial task that can be completed in one step. There's no need to track multiple tasks or steps for such a straightforward request.
</reasoning>
</example>

<example>
User: What does the git status command do?
Assistant: The git status command shows the current state of your working directory and staging area. It displays which changes have been staged, which haven't, and which files aren't being tracked by Git. This is a fundamental command for understanding the current state of your repository before making commits.

<reasoning>
The assistant did not use the todo list because this is an informational request with no actual coding task to complete. The user is simply asking for an explanation, not for the assistant to perform multiple steps or tasks.
</reasoning>
</example>

<example>
User: Can you add a comment to the calculateTotal function to explain what it does?
Assistant: Sure, let me add a comment to the calculateTotal function to explain what it does.
* Uses the Edit tool to add a comment to the calculateTotal function *

<reasoning>
The assistant did not use the todo list because this is a single, straightforward task confined to one location in the code. Adding a comment doesn't require tracking multiple steps or systematic organization.
</reasoning>
</example>

<example>
User: Run npm install for me and tell me what happens.
Assistant: I'll run the npm install command for you.

*Executes: npm install*

The command completed successfully. Here's the output:
[Output of npm install command]

All dependencies have been installed according to your package.json file.

<reasoning>
The assistant did not use the todo list because this is a single command execution with immediate results. There are no multiple steps to track or organize, making the todo list unnecessary for this straightforward task.
</reasoning>
</example>

#### Task States and Management

1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Only have ONE task in_progress at any time
   - Complete current tasks before starting new ones
   - Remove tasks that are no longer relevant from the list entirely

3. **Task Completion Requirements**:
   - ONLY mark a task as completed when you have FULLY accomplished it
   - If you encounter errors, blockers, or cannot finish, keep the task as in_progress
   - When blocked, create a new task describing what needs to be resolved
   - Never mark a task as completed if:
     - Tests are failing
     - Implementation is partial
     - You encountered unresolved errors
     - You couldn't find necessary files or dependencies

4. **Task Breakdown**:
   - Create specific, actionable items
   - Break complex tasks into smaller, manageable steps
   - Use clear, descriptive task names

When in doubt, use this tool. Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.
        """.strip()
