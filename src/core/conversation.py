# =============================================================================
# 文件：src/core/conversation.py
# 作用：整个程序的「大脑」，也是最核心的一个文件。它负责把一次聊天串起来：
#
#   把消息发给模型 → 模型说要调工具 → 执行工具 → 把结果放回消息列表 → 再发给模型 → ...
#
# 这个"再发一次"的动作是靠函数自己调用自己完成的（叫"递归"），
# 所以你会看到 _recursive_message_handling 这个方法里面又调用了它自己。
#
# 另外这里还管着"子 agent"（subagent）：主 agent 可以把一个复杂任务
# 分包给一个临时的小 agent 去做，那个小 agent 的聊天记录会单独放在一边，
# 做完只把结论带回来。_task_depth 这个计数器就是用来区分"我现在是主 agent 还是子 agent"。
# =============================================================================

"""
Conversation management with refactored UI components and history manager integration.
（上面这段是原作者的英文说明，意思是：对话管理，UI 拆成了独立组件，历史记录交给 HistoryManager 管）
"""

import asyncio  # 【2026-11-06 第 2 轮新增】摘要生成要把同步的接口调用丢到线程池，需要用到它
import json  # json 库：把模型给的"参数字符串"解析成真正的 Python 字典
import traceback  # traceback 库：程序出错时，用它把"出错的完整现场"打印出来，方便排查
from core.api_client import APIClient  # 导入 API 客户端：真正发请求给大模型的家伙
from core.prompt.prompt_manager import PromptManager  # 导入提示词管理器：负责生成"系统提示词"
from core.prompt.reminder import get_reminder  # 导入函数：生成一段"提醒模型去看 todo"的文字
from tools.tool_manager import ToolManager  # 导入工具管理器：负责注册工具、执行工具
from ui.ui_manager import UIManager  # 导入界面管理器：负责显示和要输入
from ui.theme import DOT  # 【第 4 轮】状态行里的分隔点 ·
from .history.history_manager import HistoryManager, message_text  # 导入历史管理器；message_text 用来把消息转成纯文本


# 【2026-11-06 第 2 轮新增】压缩旧对话时用的提示词。
# 核心思路：明确告诉模型"哪些必须留、哪些可以丢"，否则它会写一堆没用的客套话。
SUMMARY_SYSTEM_PROMPT = """你是一个对话压缩器。

用户会给你一段命令行编程助手的工作记录。请把它压缩成一段中文摘要（不超过 400 字），
供后续继续工作时参考。

必须保留的内容：
1. 用户的目标、明确要求和约束条件
2. 已经做过的关键决定，以及为什么这么决定
3. 改动过的文件路径，以及每个文件做了什么改动（新建/修改/删除）
4. 遇到过的错误和对应的解决办法
5. 还没做完的事、下一步计划、当前的待办清单
6. 用户明确表达过的偏好（比如"不要加注释""用中文回复"）

可以丢弃的内容：
寒暄、重复的试探、工具输出的具体内容（除非结论重要）、已被否决的方案细节。

只输出摘要正文，不要写"以下是摘要"之类的话。"""
# 注意上面这行开头的 "." 表示"同一个文件夹下的"，是相对导入


class Conversation:
    """
    对话总管。整个程序只会有一个（下面用了单例写法）。

    它自己不干具体活，而是当"调度员"：
      - 要发请求   → 找 _api_client
      - 要执行工具 → 找 _tool_manager
      - 要显示内容 → 找 _ui_manager
      - 要存消息   → 找 _history_manager
    """

    _instance = None  # 类属性：存放唯一实例，一开始为空
    _initialized = False  # 类属性：记录是否已初始化过
    _tool_manager = None  # 类属性：工具管理器（全程序共用一个）
    _api_client = None  # 类属性：API 客户端
    _ui_manager = None  # 类属性：界面管理器
    _history_manager = None  # 类属性：历史管理器
    _prompt_manager = None  # 类属性：提示词管理器
    _task_depth = 0  # 计数器：0 表示"我是主 agent"，大于 0 表示"我正在被某个子任务调用"

    def __new__(cls):
        """Singleton pattern implementation.（单例模式：保证全程序只有一个 Conversation）"""
        if cls._instance is None:  # 如果还没造过
            cls._instance = super(Conversation, cls).__new__(cls)  # 造一个存起来
        return cls._instance  # 已经造过就直接返回旧的

    def __init__(self):
        """Initialize the conversation with required managers.（把几个"管家"都创建出来）"""
        if not self._initialized:  # 只有第一次创建时才做这件事
            self._tool_manager = ToolManager()  # 建工具管理器（它会顺便去连 MCP 服务器）
            self._api_client = APIClient()  # 建 API 客户端（它会读 .env 里的配置）
            self._ui_manager = UIManager()  # 建界面管理器
            self._history_manager = HistoryManager()  # 建历史管理器
            # 【2026-11-06 第 2 轮优化】把"摘要生成器"交给历史管理器：
            #   以后上下文太长需要压缩时，它会先调这个方法把旧对话总结成摘要，
            #   而不是直接把旧消息删掉（那样模型会真的忘事）。
            self._history_manager.set_summarizer(self._summarize_messages)
            self._prompt_manager = PromptManager()  # 建提示词管理器
            self._initialized = True  # 打上标记，下次不再重复创建

    @property
    def messages(self):
        """Get current messages from history manager.（取出当前这一轮对话的全部消息）"""
        return self._history_manager.get_current_messages()  # 转手问历史管理器要

    @property
    def is_subagent(self) -> bool:
        """True while running inside a delegated task (_task_depth > 0).

        翻译：只要 _task_depth 大于 0，说明我正跑在别人派给我的子任务里。
        用途有两个：
          1. 决定"我还能不能再把活派给别人"（只有主 agent 能派）
          2. 决定"活干完了是停下来交差，还是继续问用户要输入"
        """
        return self._task_depth > 0  # 深度大于 0 就是子 agent

    def add_message(self, message):
        """Add message through history manager.（往当前对话里追加一条消息）"""
        self._history_manager.add_message(message)  # 转手交给历史管理器

    async def start_conversation(self):
        """Start a new conversation.（开始一次全新的对话：这是用户按下回车后的第一步）"""
        # Initialize with system message
        system_message = {
            # 一条消息就是一个字典，必须有两样东西：role（谁说的）和 content（说了什么）
            "role": "system",  # system 表示"系统设定"，是给模型看的规则，用户一般看不到
            "content": [
                # content 是一个列表，里面可以放多个"内容块"
                # 之所以用列表而不是直接写字符串，是因为以后可以塞图片、文件等不同类型的内容
                {"type": "text", "text": self._prompt_manager.get_system_prompt()}
                # 这一块是文字，内容是提示词管理器生成的"系统提示词"（人设、规则、当前环境）
            ]
        }
        self.add_message(system_message)  # 把系统提示词放进消息列表（永远排在最前面）

        user_input = await self._ui_manager.get_user_input()  # 等用户打字，await 表示"在这儿等"
        user_message = {
            "role": "user",  # user 表示"用户说的"
            "content": [
                {"type": "text", "text": user_input}  # 用户输入的文字
            ]
        }
        self.add_message(user_message)  # 放进消息列表

        try:  # 开始试：后面的整个对话循环都可能出错
            await self._recursive_message_handling()  # 进入主循环（下文详解）
        except Exception as e:  # 如果循环里出现任何没被处理的错误
            self._ui_manager.print_error(f"发生系统错误：{e}")  # 在界面上报个错
            traceback.print_exc()  # 再把完整的错误现场打印出来，方便定位问题

    async def start_task(self, task_system_prompt: str, user_input: str) -> str:
        """
        开始一个"子任务"。这是 Task 工具（就是让模型能派活给子 agent）的入口。

        关键点：它会在消息历史里"新开一页"，
        所以子 agent 看不到主对话的历史，主对话也不会被子 agent 的啰嗦过程污染。
        干完活以后，只把最后一条回复带回去。
        """
        # Initialize with system message
        self._task_depth += 1  # 深度 +1：从现在起，我就是个子 agent 了
        self._history_manager.start_new_chat()  # 新开一页消息记录（相当于翻到新的一页纸）
        system_message = {
            "role": "system",  # 子 agent 也有自己的人设
            "content": [
                {"type": "text", "text": task_system_prompt}  # 子 agent 专用的系统提示词
            ]
        }
        self.add_message(system_message)  # 注意：因为刚翻到新一页，所以这条会加在新页面上

        user_message = {
            "role": "user",  # 把"要干的活"当作一条用户消息交给子 agent
            "content": [
                {"type": "text", "text": user_input}
            ]
        }
        self.add_message(user_message)  # 同样加在新页面上

        try:  # 开始试
            await self._recursive_message_handling()  # 子 agent 也走同一套主循环
        except Exception as e:  # 如果子任务出错了
            self._ui_manager.print_error(f"执行子任务时发生系统错误：{e}")  # 报错
            traceback.print_exc()  # 打印现场
            self._task_depth -= 1  # 深度 -1，把计数器还原
            self._discard_current_chat()  # 【2026-11-06 优化】把出错的那一页消息丢掉
            # 【2026-11-06 优化】原来是 sys.exit(1)，直接把整个程序干掉：
            #   子 agent 只要有一个工具抛异常，你的 CLI 就退出、主对话上下文全丢。
            #   现在改成"把错误当成子任务的结果交回去"，主 agent 可以自己决定换个做法。
            return f"子任务执行失败：{e}"  # 返回失败说明，让主 agent 看到并自行补救
        self._task_depth -= 1  # 正常结束时也要把深度还原
        return self._history_manager.finish_chat_get_response()  # 收起这一页，把最后一条回复返回给主 agent

    def _discard_current_chat(self):
        """丢掉当前这一页消息，并且保证历史栈不会错位（2026-11-06 新增）

        背景：start_task 会"翻到新的一页"写子 agent 的对话，
        正常结束时会用 finish_chat_get_response() 把这一页收掉。
        但如果中途出错提前返回，这一页就留在栈上了——
        下次再开子任务时，"当前页"会指错位置，整个对话就乱套了。
        """
        if len(self._history_manager.messages_history) >= 2:  # 至少有两页，说明确实有一页待收
            self._history_manager.messages_history.pop()  # 把这一页撕掉
            if self._history_manager.history_token_usage:  # 用量记录也跟着删一条，保持两边对齐
                self._history_manager.history_token_usage.pop()

    async def _recursive_message_handling(self):
        """
        主循环，整个程序的心跳。每调用一次，就是"问模型一次 → 处理它的回答"。

        流程：
          1. 看看上下文是不是太长了，太长就先压缩
          2. 把消息 + 工具清单打包，发给模型
          3. 模型边想边说，界面边收边显示
          4. 如果模型说要调工具 → 执行工具 → 把结果塞回消息列表 → 回到第 1 步（递归）
          5. 如果模型没调工具 → 这一轮说完了 → 等用户下一句话 → 回到第 1 步（递归）
        """

        # Check for auto compression
        await self._history_manager.auto_messages_compression()  # 问一句："是不是该压缩了？"需要就压

        request = {
            "messages": self._get_messages_with_cache_mark(),  # 全部对话消息（带缓存标记）
            # Only the main agent receives the `task` tool; sub-agents cannot delegate.
            # 翻译：只有主 agent 能拿到 task 工具，子 agent 拿不到，所以没法继续往下派活
            "tools": await self._get_available_tools(),  # 我能用的所有工具清单
        }

        # Start assistant response
        # 【第 4 轮】圆点不再单独打印：它现在画在流式区域内部，
        # 这样模型"只调工具不说话"时，屏幕上不会多出一个空圆点。

        # Use streaming API for response
        try:  # 开始试：这一段是"收流式回复"
            stream_generator = self._api_client.get_completion_stream(request)  # 拿到数据流

            # Validate stream generator
            if stream_generator is None:  # 如果压根没拿到流（说明前面就出问题了）
                raise Exception("领取数据流失败：API 客户端没有返回任何内容")

            response_message = None  # 准备装"模型这一轮的完整回复"
            full_content = ""  # 准备攒"模型说的所有文字"
            token_usage = None  # 准备装 token 用量

            # Ensure stream_generator is iterable
            try:  # 再试一层：确认拿到的确实能一段段读
                iterator = iter(stream_generator)  # 把它变成"迭代器"
            except TypeError:  # 如果转换失败，说明类型不对
                raise Exception(f"返回的数据流无法逐段读取，类型是：{type(stream_generator)}")

            # Start streaming display
            self._ui_manager.start_stream_display()  # 开启"实时刷新显示"模式（界面上会动）

            # Process streaming response
            for chunk in iterator:  # 一段一段收
                if isinstance(chunk, str):  # 情况一：这是一小段文字
                    # This is content chunk
                    full_content += chunk  # 拼到总文字上
                    self._ui_manager.print_streaming_content(chunk)  # 立刻显示出来
                elif hasattr(chunk, 'role') and chunk.role == 'assistant':  # 情况二：这是最后那条完整消息
                    # This is the final message object (ChatCompletionMessage)
                    response_message = chunk  # 存起来
                    # Extract usage information if present
                    if hasattr(chunk, 'usage') and chunk.usage:  # 如果它带着用量
                        token_usage = chunk.usage  # 记下来
                    break  # 已经拿到完整消息了，后面的不用再读，跳出循环
                elif hasattr(chunk, 'usage') and chunk.usage:  # 情况三：这是个独立的用量数据
                    # This is standalone token usage information
                    token_usage = chunk.usage  # 记下来

            # End streaming display
            self._ui_manager.stop_stream_display()  # 关闭实时刷新模式

            # If no complete response message, create one
            if response_message is None:  # 万一没收到"完整消息"（有些接口不返回）
                response_message = self._create_simple_message(full_content)  # 就用攒下来的文字自己造一个

        except Exception as e:  # 如果流式这一套失败了
            self._ui_manager.print_error(f"处理流式响应时出错：{e}")  # 报错
            self._ui_manager.print_info(f"错误类型：{type(e).__name__}")  # 打印错误类型
            traceback.print_exc()  # 打印完整现场

            # Fallback to non-streaming mode
            try:  # 再试：退一步，用"非流式"方式重发一次（不要逐字效果了，能拿到结果就行）
                self._ui_manager.print_info("正在改用非流式模式重试……")
                response_message, token_usage = self._api_client.get_completion(request)  # 一次性拿完整结果
                self._ui_manager.print_assistant_message(response_message.content)  # 把内容显示出来

                # Update token usage in history manager
                if token_usage:  # 如果拿到用量
                    self._history_manager.update_token_usage(token_usage)  # 交给历史管理器记账

            except Exception as fallback_error:  # 如果连非流式也失败了
                self._ui_manager.print_error(f"非流式模式也失败了：{fallback_error}")
                # Create error response
                response_message = self._create_error_message(str(e))  # 造一条"出错提示"当作模型的回复
                self._ui_manager.print_assistant_message(response_message.content)  # 显示出来
                return  # 直接结束这一轮，不再往下走

        if token_usage:  # 如果拿到了用量（流式成功时走这里）
            self._history_manager.update_token_usage(token_usage)  # 记账

        # Add response to message history through history manager
        assistant_message = {
            "role": "assistant",  # 这是模型自己说的话，要存进历史
            "content": response_message.content,  # 它说的文字内容
            "tool_calls": response_message.tool_calls if hasattr(response_message, 'tool_calls') and response_message.tool_calls else None
            # 它提出的工具申请（没有就是 None）
            # 这行的意思：如果有 tool_calls 且不为空，就存下来；否则存 None
        }
        self.add_message(assistant_message)  # 存进历史

        # Check for auto compression
        await self._history_manager.auto_messages_compression()  # 又变长了，再检查一次要不要压缩

        # Handle tool calls
        if hasattr(response_message, 'tool_calls') and response_message.tool_calls is not None and len(response_message.tool_calls) > 0:
            # 情况 A：模型要用工具（可能一次要用好几个）
            await self._handle_tool_calls(response_message.tool_calls)  # 一个个执行
            # Update token usage in history manager
            self._print_context_window_and_total_cost()  # 打印"上下文用了百分之几、一共花了多少钱"
            await self._recursive_message_handling()  # 递归：把工具结果再发给模型，让它接着说
        else:
            # 情况 B：模型没要用工具，说明这一轮它说完了
            self._print_context_window_and_total_cost()  # 打印用量
            # No tool calls, wait for user input
            if self._task_depth > 0:  # 如果我是子 agent
                return  # 活干完了，直接返回，把结论交给主 agent——不要再去问用户要输入
            user_input = await self._ui_manager.get_user_input()  # 主 agent 就等用户说下一句
            user_message = {
                "role": "user",  # 用户说的
                "content": [
                    {"type": "text", "text": user_input}  # 用户输入的内容
                ]
            }
            self.add_message(user_message)  # 存进历史
            await self._recursive_message_handling()  # 递归：带着新问题再问模型

    def _print_context_window_and_total_cost(self):
        # 打印一行状态："上下文窗口用了 12.3%，一共花了 0.45 美元"
        self._ui_manager.print_status(f"上下文 {self._history_manager.current_context_window}% {DOT} 花费 ${self._api_client.total_cost}")

    # -------------------------------------------------------------------------
    # 【2026-11-06 第 2 轮新增】摘要式压缩相关
    # -------------------------------------------------------------------------
    async def _summarize_messages(self, messages) -> str:
        """把一批旧消息压缩成一段摘要（交给大模型做）

        这个方法是"摘要生成器"，在 __init__ 里交给 HistoryManager。
        上下文快满时，HistoryManager 会调用它，用返回的摘要替换掉被删掉的旧消息。
        调模型失败时异常会往上抛，由 HistoryManager 捕获并退回"直接删除"。
        """
        transcript = self._messages_to_transcript(messages)  # 先把消息拼成一段可读的文字
        if not transcript.strip():  # 什么都没有就不浪费一次请求了
            return ""

        request = {
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": SUMMARY_SYSTEM_PROMPT}]},
                # 系统提示词：告诉模型"你的任务是把下面这段对话压缩成摘要"
                {"role": "user", "content": [{"type": "text", "text": transcript}]},
                # 用户消息：真正要压缩的对话内容
            ]
        }

        loop = asyncio.get_running_loop()  # 拿到当前的事件循环
        message, token_usage = await loop.run_in_executor(
            None, self._api_client.get_completion, request
        )
        # 为什么要用 run_in_executor？
        #   get_completion 是"同步"的（会一直等到接口返回）。
        #   直接在异步函数里调用，会把整个事件循环卡住，界面就不动了。
        #   丢到线程池里跑，界面上还能继续转圈。

        if token_usage:  # 这次调用也花了钱，记一笔
            self._history_manager.update_token_usage(token_usage)
        return message.content or ""  # 返回摘要正文

    def _messages_to_transcript(self, messages, max_chars: int = 24000) -> str:
        """把消息列表拼成一段"人能看懂"的文字，用来喂给摘要模型（第 2 轮新增）

        格式大概是：
            user: 帮我把 xxx 改成 yyy
            assistant: 好的，我先看一下文件
            assistant -> tool cmd_runner({"command": "ls"})
            tool: a.py b.py
        """
        lines = []  # 存每一行
        for message in messages:  # 逐条消息
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
            # 取角色（字典 / 对象两种形态都兼容）

            text = message_text(message).strip()  # 取出这条消息的文字内容
            if text:  # 有文字才记
                lines.append(f"{role}: {text}")

            tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
            # 取出这条消息里的"工具调用"（可能没有）
            if tool_calls:  # 有就逐条记下来——这些"改了什么文件"的信息对摘要很关键
                for call in tool_calls:
                    function = getattr(call, "function", None)  # 正常是对象形态
                    name = getattr(function, "name", None)  # 工具名
                    arguments = getattr(function, "arguments", None)  # 参数（JSON 字符串）
                    if name is None and isinstance(call, dict):  # 万一是字典形态
                        name = call.get("function", {}).get("name", "")
                        arguments = call.get("function", {}).get("arguments", "")
                    lines.append(f"assistant -> tool {name}({arguments})")

        transcript = "\n".join(lines)  # 拼成整段

        if len(transcript) > max_chars:  # 太长的话摘要模型自己也读不完
            head = transcript[: int(max_chars * 0.7)]  # 留开头 70%（通常是目标和背景）
            tail = transcript[-int(max_chars * 0.3):]  # 留结尾 30%（通常是最新的进展）
            transcript = head + "\n... [middle omitted] ...\n" + tail
        return transcript

    async def shutdown(self):
        """程序退出前收尾（2026-11-06 第 2 轮新增）

        主要是关掉 MCP 那些外部服务器进程。不关的话它们会变成孤儿进程留在后台，
        而且退出时还会打一段 cancel scope 的报错。
        """
        await self._tool_manager.shutdown()

    def _get_messages_with_cache_mark(self):
        """Get messages with cache mark.（给最后一条消息打上"可缓存"标记）"""
        messages = self._history_manager.get_current_messages()  # 取出全部消息（拿到的是一份副本）
        if messages and "content" in messages[-1] and messages[-1]["content"]:
            # 如果消息列表不为空，最后一条有 content 字段，而且 content 不为空
            messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
            # [-1] 表示"最后一个"。这行是给最后一条消息的最后一个内容块加一个标记，
            # 告诉支持缓存的接口："这段内容可以缓存起来，下次别重复收费"。
            # 注意：只有 Anthropic 官方接口认这个字段，走 OpenRouter 时它通常会被忽略。
        return messages  # 返回加工后的消息列表

    async def _get_available_tools(self):
        """
        Tool schemas to send with this request.（这次请求要带上的工具清单）

        主 agent（深度 0）能看到全部工具；
        子 agent（深度 > 0）看到的清单里没有 task，所以它根本不会想到要去派活。
        这叫"从源头掐断"：连选项都不给它，比事后拦它更可靠。

        为什么是 async？因为要等 MCP 工具注册完（连外部服务器要时间）。
        """
        exclude = {"task"} if self.is_subagent else None  # 如果是子 agent，就排除 task 工具
        return await self._tool_manager.get_tools_description(exclude=exclude)  # 向工具管理器要清单

    async def _handle_tool_calls(self, tool_calls):
        """Handle tool calls with user approval when needed.（逐个处理模型的工具申请）"""
        for i, tool_call in enumerate(tool_calls):  # 一个个处理，i 是序号
            is_last_tool = (i == len(tool_calls) - 1)  # 标记"这是不是最后一个"（最后一个后面要加提醒文字）
            try:  # 试：解析参数
                args = json.loads(tool_call.function.arguments)  # 把"参数字符串"变成真正的字典
            except json.JSONDecodeError as e:  # 如果参数不是合法 JSON（模型写错了）
                self._ui_manager.print_error(f"Tool parameter parsing failed: {e}")  # 报错
                tool_response = {
                    "role": "tool",  # tool 表示"这是工具返回的结果"
                    "tool_call_id": tool_call.id,  # 对应哪一次申请（必须对上号）
                    "name": tool_call.function.name,  # 是哪个工具
                    "content": [
                        {"type": "text", "text": "工具调用失败：参数不是合法的 JSON，请检查 tool 的 arguments 格式后重试。"}
                        # 告诉模型"你参数写错了"，让它重新写一遍
                    ]
                }
                self.add_message(tool_response)  # 存进历史
                continue  # 跳过这一个，处理下一个

            # Check if user approval is needed
            # 【2026-11-06 优化】默认值从 False 改成 True（失败时更安全）
            #   原来的逻辑：模型没写 need_user_approve 就当 False，直接执行。
            #   问题是"要不要问用户"全凭模型自己说，它漏写或者故意写成 false，
            #   危险命令就会静默跑掉。现在改成"没明确说就默认要问"。
            #   注意这只是兜底，真正的权限规则（白名单/黑名单/路径边界）在后续优化里做。
            need_user_approve = args.get('need_user_approve', True)
            should_execute = True  # 默认"要执行"（下面如果要问用户，会根据用户的回答改写它）

            if need_user_approve:  # 如果模型说这个操作有风险、需要用户点头
                approval_content = f"Tool: {tool_call.function.name}, args: {args}"  # 拼一句"我要干什么"给用户看
                should_execute, content = await self._ui_manager.wait_for_user_approval(approval_content)
                # 等用户回答，返回两个值：要不要执行、如果拒绝的话理由是什么

            if should_execute:  # 用户同意（或本来就不需要问）
                await self._execute_tool(tool_call, args, is_last_tool)  # 真正执行
            else:  # 用户拒绝了
                self._add_tool_response(tool_call, f"用户拒绝执行这个工具。用户的说明：{content}", is_last_tool)
                # 把"用户拒绝了，理由是..."当作工具结果告诉模型，让它换个做法

    async def _execute_tool(self, tool_call, args, is_last_tool=False):
        """Execute a tool call and handle the response.（执行一个工具，成功失败都要有回应）"""
        tool_args = {k: v for k, v in args.items() if k != 'need_user_approve'}
        # 上面这行是"过滤字典"：把 need_user_approve 这个标记去掉，
        # 因为它是给我们看的，不是给工具本身用的参数。用的是字典推导式。
        self._ui_manager.show_preparing_tool(tool_call.function.name, tool_args)  # 显示"准备调用 xx 工具"

        try:  # 试：执行工具
            tool_response = await self._tool_manager.run_tool(tool_call.function.name, **tool_args)
            # 让工具管理器去执行。**tool_args 是把字典拆成一个个参数传进去
            self._ui_manager.show_tool_execution(
                tool_call.function.name,  # 工具名
                tool_args,  # 参数
                success=True,  # 成功标记
                result=str(tool_response)  # 结果转成字符串再显示
            )
            self._add_tool_response(tool_call, str(tool_response), is_last_tool)  # 把结果交给模型
        except Exception as e:  # 如果工具执行时抛错
            # Enhanced error handling for tool execution
            self._ui_manager.show_tool_execution(
                tool_call.function.name,
                tool_args,
                success=False,  # 这次是失败
                result=str(e)  # 错误信息
            )
            self._add_tool_response(tool_call, f"工具调用失败，原因：{str(e)}", is_last_tool)
            # 关键设计：工具失败不是"程序崩了"，而是"给模型一条失败消息"，
            # 让模型自己看到错误、自己想办法补救。这是 agent 能自我纠错的核心。

    def _add_tool_response(self, tool_call, content, is_last_tool=False):
        """Add tool response to message history through history manager.（把工具结果存进历史）"""
        tool_content = [{"type": "text", "text": content}]  # 包成标准的"内容块"

        # Add reminder after the last tool call in each iteration
        if is_last_tool:  # 只有最后一个工具结果后面才加提醒
            reminder_content = get_reminder()  # 生成一段提醒（内容是当前的 todo 列表 + 一句叮嘱）
            tool_content.append({"type": "text", "text": reminder_content})
            # 为什么加在工具结果里而不是单独发一条消息？因为这样模型一定会读到，不占额外消息位

        tool_message = {
            "role": "tool",  # 角色是"工具结果"
            "tool_call_id": tool_call.id,  # 对应哪次申请
            "name": tool_call.function.name,  # 工具名
            "content": tool_content  # 内容（可能有一段结果 + 一段提醒）
        }
        self.add_message(tool_message)  # 存进历史

    def _create_simple_message(self, content):
        """Create a simple message object.（自己造一个"回复对象"，模仿 SDK 的格式）"""
        class SimpleMessage:  # 在函数内部临时定义一个类（用完就没人记得它了）
            def __init__(self, content):  # 初始化方法
                self.content = content  # 文字内容
                self.role = "assistant"  # 角色固定是助手
                self.tool_calls = None  # 没有工具申请

        return SimpleMessage(content)  # 造一个返回

    def _create_error_message(self, error_msg):
        """Create an error message object.（造一个"出错回复对象"）"""
        class ErrorMessage:  # 同样临时定义一个类
            def __init__(self, error_msg):  # 初始化
                self.content = f"抱歉，我遇到一个技术问题：{error_msg}"  # 给用户看的道歉文案
                self.role = "assistant"  # 角色是助手
                self.tool_calls = None  # 没有工具申请

        return ErrorMessage(error_msg)  # 返回

    def print_streaming_content(self, content):
        """Delegate streaming content printing to UI manager (for compatibility).（转交给界面管理器显示）"""
        self._ui_manager.print_streaming_content(content)  # 纯粹转手，为了保留旧接口

# =============================================================================
# 备注（读代码时可以先跳过，了解大概后回来看）：
#
# 1. 【2026-11-06 已修复】start_task 出错时原来用 sys.exit(1) 把整个程序干掉。
#    现在改成返回 "Task failed: ..." 交给主 agent，同时用 _discard_current_chat()
#    把出错的那一页消息收掉，避免历史栈错位。
#
# 2. _get_messages_with_cache_mark 只给"最后一条消息"打缓存标记。
#    但最后一条每轮都在变，所以实际上很难命中缓存。
#    真正有效的位置是 system 提示词和 tools 清单（它们每轮都一样）。
#
# 3. 整个类是单例，子 agent 复用的是同一个对象，靠 _task_depth 和
#    "消息历史翻页"来隔离。这样一次只能跑一个子 agent，没法并行。
#
# 4. 权限判断目前还是"模型说什么就是什么"（只是把默认值改成了要问）。
#    真正的权限规则（命令白名单、危险命令黑名单、工作区路径边界）待后续优化。
# =============================================================================
