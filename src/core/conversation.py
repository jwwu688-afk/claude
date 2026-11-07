# =============================================================================
# 文件：src/core/conversation.py
# 作用：整个程序的「大脑」，也是最核心的一个文件。它负责把一次聊天串起来：
#
#   把消息发给模型 → 模型说要调工具 → 执行工具 → 把结果放回消息列表 → 再发给模型 → ...
#
# 这个"再发一次"的动作，原来是靠函数自己调用自己完成的（叫"递归"）。
# 【2026-11-06 第 8 轮】已改成 while 循环驱动 _run_one_iteration()：
#   每用一次工具、每等一次用户输入，原来都会让调用栈厚一层，
#   Python 默认递归上限 1000，长任务跑到几百轮就会 RecursionError。
#   现在栈深度始终是常数，跑多少轮都不会涨。
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
import os  # 【第 13 轮】取当前目录，写进会话文件的 header
import traceback  # traceback 库：程序出错时，用它把"出错的完整现场"打印出来，方便排查
from core.api_client import APIClient  # 导入 API 客户端：真正发请求给大模型的家伙
from core.limits import MAX_TOOL_OUTPUT_CHARS  # 【第 9 轮】输出上限统一到一处，不再各处各写一个数
from core.permissions import READ_ONLY_TOOLS, classify  # 【第 9 轮】权限判定集中到 core/permissions.py
from core.prompt.prompt_manager import PromptManager  # 导入提示词管理器：负责生成"系统提示词"
from core.prompt.reminder import get_reminder  # 导入函数：生成一段"提醒模型去看 todo"的文字
from core.session import SessionStore, load_messages_for_resume  # 【第 13 轮】会话持久化
from tools.tool_manager import ToolManager  # 导入工具管理器：负责注册工具、执行工具
from ui.esc_watcher import EscWatcher  # 【第 13 轮】Esc 打断
from ui.ui_manager import UIManager  # 导入界面管理器：负责显示和要输入
from ui.theme import DOT  # 【第 4 轮】状态行里的分隔点 ·
from .history.history_manager import HistoryManager, message_text  # 导入历史管理器；message_text 用来把消息转成纯文本


# 【2026-11-06 第 2 轮新增】压缩旧对话时用的提示词。
# 核心思路：明确告诉模型"哪些必须留、哪些可以丢"，否则它会写一堆没用的客套话。
# 【2026-11-06 第 6 轮新增】单条工具输出写进对话历史时，最多保留多少字符。
#   为什么要在这里截，而不是只在界面上截？
#     界面截断只是"不刷屏"，模型拿到的仍然是全文（可能几万字），
#     照样会把上下文吃满、把预算烧掉。真正省钱的截断必须发生在"写进历史之前"。
# 【2026-11-06 第 9 轮】这个数字移到 core/limits.py，和 cmd_runner 共用同一个。
#   原来这里有 12000、cmd_runner 里有 30000，两刀砍在同一个输出上，
#   30000 那一刀等于白写；file_read 读满 2000 行也会被这里腰斩。
#   导入的那一行在上面（from core.limits import MAX_TOOL_OUTPUT_CHARS）。

# 【2026-11-06 第 8 轮】只读工具清单：这些工具不改磁盘文件、不启动外部进程，
# 没必要每次都弹窗打断用户。第 8 轮新加的三个文件检索工具都在这里。
# 注意 smart_context_cropper 不在此列——它会真的删掉聊天记录，属于有风险的操作。
# 【2026-11-06 第 9 轮】清单本体搬到 core/permissions.py（上面的 import 已引入），
#   这里保留名字是为了兼容：测试和其他模块仍在 from core.conversation import READ_ONLY_TOOLS。
__all__ = ["Conversation", "READ_ONLY_TOOLS"]
# ↑ 为什么需要这行？
#   READ_ONLY_TOOLS 在本文件里已经不再被直接用到（判定全交给 classify 了），
#   但它是"对外导出的名字"——test/test_conversation_loop.py 就是从这个模块 import 它的。
#   不声明 __all__ 的话，PyCharm / flake8 会把它标成"导入后未使用"（黄色波浪线）。
#   写进 __all__ 等于告诉静态检查：这是我故意留着给外面用的，别报。

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
            # 【2026-11-07 第 13 轮】会话落盘 + Esc 打断
            self._session = None  # 当前会话的存储句柄，start_conversation 时才创建
            self._esc = EscWatcher()  # 监听 Esc 的后台线程（不支持的平台会自动降级）
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
        """Add message through history manager.（往当前对话里追加一条消息）

        【第 13 轮】顺手把消息写进会话文件，这样进程被杀也不会丢上下文。
        只在主 agent 时写：子 agent 的消息属于"临时草稿"，最后只会把结论交回来，
        把它们也落盘会让会话文件里混进大量无关内容，恢复时反而干扰模型。
        """
        self._history_manager.add_message(message)  # 转手交给历史管理器
        # 【第 13 轮】用 getattr 兜底而不是直接 self._session：
        #   单例的 __init__ 只在第一次执行，某些测试/子 agent 场景下对象是先 __new__ 出来的，
        #   属性可能还没被赋值。用 getattr 拿默认值，比依赖初始化顺序更稳。
        session = getattr(self, "_session", None)
        if session is not None and not self.is_subagent:
            session.append_message(message)  # 追加一行到 JSONL

    async def start_conversation(self, resume_path=None):
        """Start a new conversation.（开始一次全新的对话：这是用户按下回车后的第一步）

        参数 resume_path（第 13 轮新增）：
          传了会话文件路径 → 恢复那次对话，接着聊；
          传 None → 全新开始。
        """
        # 【第 12 轮】先把记忆目录建出来。
        #   为什么要在这里做：模型第一次想写记忆时，如果目录不存在，
        #   file_write 可能因为"父目录不存在"失败，那次记忆就丢了。
        #   提前建成空目录，写入路径就永远是通的。exist_ok=True，重复调用无副作用。
        from core.auto_memory import ensure_dir as ensure_memory_dir

        ensure_memory_dir()

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

        # 【第 13 轮】建立会话存储。
        #   恢复时直接续写**原来那个文件**，而不是新建一个——这样一次对话
        #   从头到尾都在同一个 JSONL 里，翻起来是连续的，也不会重复存旧消息。
        if resume_path:
            self._session = SessionStore(resume_path)
            restored = load_messages_for_resume(resume_path)
            # 系统提示词用新生成的替换旧的：里面的日期、目录、项目记忆可能是几天前的，
            # 照搬会让模型拿着过期信息干活。user / assistant / tool 消息原样保留。
            self._history_manager.messages_history[-1] = [system_message] + restored

            # 【2026-11-07 第 13.1 轮】恢复完必须补一次用量估算。
            #   改前这里只换了消息列表，没碰 history_token_usage——
            #   它还是空的，而 _requires_compression() 看到空账本就返回 False，
            #   于是恢复一个已经用到 80% 的长会话时，第一次请求会把整份历史原样发出去，
            #   大概率 400（context_length_exceeded）。
            #   注意 system_message 也要算进去：它是最大的一块固定开销。
            estimated_tokens = self._history_manager.seed_usage_from_messages(
                [system_message] + restored
            )
            self._ui_manager.print_info(
                f"已恢复上次会话：{len(restored)} 条消息"
                f"（按字符估算上下文约 {estimated_tokens:,} token）"
            )
        else:
            self._session = SessionStore.create(
                cwd=os.getcwd(),
                model=getattr(self._api_client, "model", "") or "",
            )
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
        """主循环驱动器（2026-11-06 第 8 轮重写）

        原来整个方法是"自己调自己"的递归：每用一次工具、每等一次用户输入，
        调用栈就厚一层。Python 默认递归上限是 1000（本机实测），
        一次长任务跑上几百轮就会抛 RecursionError，而且异常冒到最外层
        会被 start_conversation 捕获成"发生系统错误"，整个对话直接结束。

        现在改成 while：每一轮交给 _run_one_iteration() 干完
        "问模型一次 + 处理工具结果"，由它决定还要不要继续转：
          返回 True  → 还有后续（工具结果要再问、或要等用户下一句）
          返回 False → 这一页该收尾（子 agent 交差、请求彻底失败）

        栈深度始终只有一层，跑多少轮都不会涨；异常也照样往上冒，
        和原来的捕获行为一致。

        为什么保留这个老名字？start_conversation / start_task 都调它，
        改名要牵动多处；而且它确实还是"整个对话的心跳"。
        """
        while True:
            should_continue = await self._run_one_iteration()  # 跑一轮
            if not should_continue:  # 该收尾了
                return

    async def _run_one_iteration(self) -> bool:
        """单层迭代：问模型一次 → 处理它的回答（2026-11-06 第 8 轮从递归体拆出）

        流程：
          1. 看看上下文是不是太长了，太长就先压缩
          2. 把消息 + 工具清单打包，发给模型
          3. 模型边想边说，界面边收边显示
          4. 如果模型说要调工具 → 执行工具 → 把结果塞回消息列表 → 返回 True 让外层再转一圈
          5. 如果模型没调工具 → 这一轮说完了 → 等用户下一句话 → 同样返回 True

        返回值含义见 _recursive_message_handling 的说明。
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

            # 【第 13 轮】开始监听 Esc。
            #   放 start() 之前先 reset()：上一轮可能按过 Esc，标志不清会让这一轮
            #   刚开始就被判成"已中断"。
            #   try/finally 保证无论怎么退出（正常读完、中断、抛异常）都会 stop，
            #   否则监听线程会活到下一次 start，和新线程抢键盘。
            esc = getattr(self, "_esc", None)  # 兜底：没初始化过就当没有 Esc 打断
            if esc is not None:
                esc.reset()
                esc.start()
            interrupted = False  # 这一轮是不是被用户打断的

            # Process streaming response
            try:
                for chunk in iterator:  # 一段一段收
                    if esc is not None and esc.triggered:  # 用户按了 Esc
                        interrupted = True
                        break
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
            finally:
                if esc is not None:
                    esc.stop()  # 收掉监听线程

            # End streaming display
            # 中断时不打 `Cooked for Ns` 收尾行——那一轮根本没跑完，打上去是误导。
            self._ui_manager.stop_stream_display(show_done=not interrupted)

            # 【第 13 轮】被中断：保住已经说出来的部分，然后直接问用户下一步。
            if interrupted:
                text = full_content.strip()
                if text:  # 已经输出过内容才存，一个字都没说就没什么好留的
                    # 标上 [已中断]：模型下次读历史时能看出"这条是被打断的半截话"，
                    # 不会误以为那就是它的完整回答。
                    self.add_message({
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"{text}\n\n[用户中断]"}],
                    })
                self._ui_manager.print_info("已中断（Esc）。直接说你想怎么改。")
                if self.is_subagent:
                    return False  # 子 agent 被打断：这一页该收尾了
                # 主 agent：等用户下一句话。这里手动走一遍"等输入"的流程，
                # 而不是 return True 让外层再转一圈——那样会立刻又去请求模型，
                # 用户根本没机会改口。
                user_input = await self._ui_manager.get_user_input()
                user_message = {
                    "role": "user",
                    "content": [{"type": "text", "text": user_input}],
                }
                self.add_message(user_message)
                return True

            # If no complete response message, create one
            if response_message is None:  # 万一没收到"完整消息"（有些接口不返回）
                response_message = self._create_simple_message(full_content)  # 就用攒下来的文字自己造一个

        except Exception as e:  # 如果流式这一套失败了
            # 【第 10 轮】异常路径也必须把等待动画收掉。
            #   原来 stop_stream_display() 只写在 for 循环后面，网络中断或
            #   解析出错时会直接跳到 except，spinner 线程就一直活着——
            #   下一轮再启动一个，两个线程往同一行写，屏幕剧烈闪烁。
            self._ui_manager.stop_stream_display(show_done=False)
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
                return False  # 【第 8 轮】彻底失败：让外层 while 停下来，别再往下走

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
            return True  # 【第 8 轮】交给外层 while 再转一圈：把工具结果发给模型，让它接着说
        else:
            # 情况 B：模型没要用工具，说明这一轮它说完了
            self._print_context_window_and_total_cost()  # 打印用量
            # No tool calls, wait for user input
            if self._task_depth > 0:  # 如果我是子 agent
                return False  # 【第 8 轮】活干完了，让外层 while 停下，把结论交给主 agent——不要再去问用户要输入
            user_input = await self._ui_manager.get_user_input()  # 主 agent 就等用户说下一句
            user_message = {
                "role": "user",  # 用户说的
                "content": [
                    {"type": "text", "text": user_input}  # 用户输入的内容
                ]
            }
            self.add_message(user_message)  # 存进历史
            return True  # 【第 8 轮】交给外层 while 再转一圈：带着新问题再问模型

    def _print_context_window_and_total_cost(self):
        # 打印一行状态："上下文窗口用了 12.3%，一共花了 0.45 美元"
        # 【第 5 轮】按上下文水位给状态行变色：过半发黄，超过 75% 发红，快满了自己会"跳"出来
        used_text = self._history_manager.current_context_window
        try:
            used = float(used_text)  # 这里拿到的是字符串形式的百分比，转成数字才能比较
        except ValueError:
            used = 0.0
        if used >= 75:
            level = "err"
        elif used >= 50:
            level = "warn"
        else:
            level = "info"
        cache_part = ""  # 【第 6 轮】有缓存命中就显示出来，方便判断缓存到底有没有生效
        if self._api_client.cached_tokens:
            # 【2026-11-06 第 8.2 轮】从"命中 8640 个 token"改成百分比。
            #   原因：绝对数没有参照系，8640 可能是 20 万输入的 4%（很差），
            #   也可能是 1 万输入的 86%（很好），用户每次都得自己心算。
            cache_part = f" {DOT} 缓存命中 {self._api_client.cache_hit_rate:.0f}%"
        self._ui_manager.print_status(
            f"上下文 {used_text}% {DOT} 花费 ${self._api_client.total_cost}{cache_part}", level
        )

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
            None,
            self._api_client.get_completion,
            request,
            self._api_client.summary_model,  # 【第 6 轮】摘要单独用（更便宜的）模型
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
        """给消息打"提示词缓存"断点（2026-11-06 第 6 轮重写）

        原来的写法有两个问题：
          1. 不管什么接口都往消息里塞 cache_control。这是 Anthropic 的私有字段，
             发给 OpenAI 官方接口会直接报 400（Unrecognized request argument）。
          2. 只给"最后一条消息"打一个点。系统提示词这种"每轮都一样"的内容
             反而没被单独标记，白白浪费了命中率最高的那一段。

        现在改成：
          - 只有支持缓存的接口才打标记（Anthropic / 模型名含 claude / 显式开启）
          - 打两个断点：系统提示词（永远命中）+ 最后一条消息（增量命中）
          - 最多 4 个（Anthropic 的上限，多了会被拒绝）
        """
        messages = self._history_manager.get_current_messages()  # 取出全部消息（是一份副本，随便改）
        if not messages:  # 空列表没什么好标的
            return messages
        if not self._api_client.prompt_cache_enabled:  # 当前接口不支持缓存
            return messages  # 一个字段都不加，避免请求被拒

        breakpoints = []  # 记录要打标记的消息下标
        for index, message in enumerate(messages):  # 找第一个系统消息
            if message.get("role") == "system":
                breakpoints.append(index)
                break

        last_index = len(messages) - 1
        if messages[last_index].get("role") != "system":  # 最后一条不是系统消息才单独标一次
            breakpoints.append(last_index)

        for index in breakpoints[:4]:  # Anthropic 最多接受 4 个断点
            self._mark_cache_breakpoint(messages[index])
        return messages

    @staticmethod
    def _mark_cache_breakpoint(message):
        """把一条消息的最后一个内容块标记为缓存断点（2026-11-06 第 6 轮新增）

        缓存断点的含义是"从开头到这里为止的内容都可以缓存"。
        所以标记必须落在"内容块的最后一个"上——标在中间不会生效。
        """
        content = message.get("content")
        if isinstance(content, str):  # 有些消息的 content 直接是字符串
            content = [{"type": "text", "text": content}]  # 统一包装成内容块列表
            message["content"] = content
        if isinstance(content, list) and content:  # 最后一个块是字典才能加字段
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {"type": "ephemeral"}

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

    @staticmethod
    def _needs_user_approval(tool_name: str, args: dict) -> bool:
        """判断这次工具调用要不要弹确认（2026-11-06 第 8 轮新增，第 9 轮改成规则判定）

        第 8 轮的写法是"模型显式写了就听它的"。这个设计有个致命问题：
        被判断的人自己当裁判——模型给 `del /s /q` 标个 false，命令就静默执行了。

        第 9 轮把判定搬到 core/permissions.py，三条规则：
          1. 命中危险模式 → 必须问（模型的 false 也不放行，回车默认拒绝）
          2. 只读工具 / 只读命令 → 不问
          3. 其余一切 → 必须问

        这个方法只是薄薄一层转手，保留它是因为测试和别处还在调它。
        想看完整规则请读 core/permissions.py 的 classify()。
        """
        return classify(tool_name, args).need_approval

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
            # 【2026-11-06 第 9 轮】判定交给 core/permissions.py 的 classify()：
            #   它不只回答"要不要问"，还告诉我们是"低危 / 普通 / 高危"。
            #   高危（rm -rf、格式化、强制推送……）会额外做两件事：
            #     1. 在屏幕上标出来，让用户知道自己面对的是什么
            #     2. 把"回车即同意"翻转成"回车即拒绝"，必须明确打 y 才放行
            decision = classify(tool_call.function.name, args)
            need_user_approve = decision.need_approval
            should_execute = True  # 默认"要执行"（下面如果要问用户，会根据用户的回答改写它）
            content = ""  # 用户拒绝时说的话（没有就是空串）

            # 【2026-11-06 第 8.2 轮】先把"要干什么"画出来，再问"要不要干"。
            #   改前是反过来的：先弹一段 "Tool: xxx, args: {...}"，用户点完同意，
            #   下面才出现 ⏺ cmd_runner(dir) 那行。结果是同一个命令在屏幕上出现两次，
            #   第一次还是一坨没排版的字典。现在头部只画一次，顺序也符合人的阅读习惯。
            self._ui_manager.show_preparing_tool(tool_call.function.name, args)

            if decision.is_high_risk:  # 【第 9 轮】高危操作先亮红牌再问
                self._ui_manager.print_warning(f"高危操作（{decision.reason}）：回车将默认拒绝，确认执行请输入 y。")

            if need_user_approve:  # 如果规则判定这个操作需要用户点头
                # 【第 8.2 轮】问题只问"是否执行"，参数已经在上面那行里了。
                #   回车即同意；输入别的内容 = 拒绝 + 那句内容作为说明（见 interpret_approval）。
                # 【第 9 轮】高危操作传 default_yes=False：不填就打回，必须显式确认。
                #   这个参数第 8.2 轮就在接口里留好了，本轮终于用上。
                should_execute, content = await self._ui_manager.wait_for_user_approval(
                    default_yes=not decision.is_high_risk
                )

            if should_execute:  # 用户同意（或本来就不需要问）
                # show_header=False：头部上面已经画过了，别再画一遍
                await self._execute_tool(tool_call, args, is_last_tool, show_header=False)
            else:  # 用户拒绝了
                # 【第 8.2 轮】补一行"已拒绝"，否则用户不知道自己那一下有没有生效
                self._ui_manager.show_tool_denied(content)
                self._add_tool_response(tool_call, f"用户拒绝执行这个工具。用户的说明：{content}", is_last_tool)
                # 把"用户拒绝了，理由是..."当作工具结果告诉模型，让它换个做法

    async def _execute_tool(self, tool_call, args, is_last_tool=False, show_header=True):
        """Execute a tool call and handle the response.（执行一个工具，成功失败都要有回应）

        参数 show_header（2026-11-06 第 8.2 轮新增）：
          False 表示"⏺ 工具名(参数) 这一行已经画过了"。
          需要审批的调用会先画头再提问，所以进来时开关是关的；
          只读工具直接进这里，开关还是开的，自己画。
        """
        tool_args = {k: v for k, v in args.items() if k != 'need_user_approve'}
        # 上面这行是"过滤字典"：把 need_user_approve 这个标记去掉，
        # 因为它是给我们看的，不是给工具本身用的参数。用的是字典推导式。
        if show_header:  # 没画过才画，避免同一个调用出现两行头
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
        """Add tool response to message history through history manager.（把工具结果存进历史）

        【2026-11-06 第 6 轮】在写进历史前先截断。原因是：
          界面上的截断只影响"你看到什么"，模型收到的还是全文。
          一条 `dir /s` 或 `npm install` 的输出动辄几万字，直接吃满上下文。
        """
        tool_content = [{"type": "text", "text": self._truncate_tool_output(content)}]  # 包成标准的"内容块"（已截断）

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

    def _truncate_tool_output(self, content) -> str:
        """把过长的工具输出掐头去尾（2026-11-06 第 6 轮新增，第 9 轮改了提示语）

        策略：保留开头 70% + 结尾 30%，中间替换成一句说明。
        为什么留头尾？
          开头通常是"命令在干什么、有没有报错"，结尾通常是"最终结果"，
          中间的大段重复日志恰恰是最没用的部分。
        为什么要在说明里教它怎么取全文？
          否则模型以为数据只有这么多，会基于残缺信息做判断。

        【第 9 轮】提示语原来教模型用 `sed -n` / `grep` 分段取——那是 Linux 命令，
        这个项目跑在 Windows 的 cmd.exe 上，照着执行只会失败。现在改成教它用
        file_read 的 offset / limit，或者给 cmd_runner 换一条更窄的命令。
        """
        text = content if isinstance(content, str) else str(content)  # 统一成字符串
        if len(text) <= MAX_TOOL_OUTPUT_CHARS:  # 没超限，原样返回
            return text
        head_length = int(MAX_TOOL_OUTPUT_CHARS * 0.7)  # 开头保留多少
        tail_length = MAX_TOOL_OUTPUT_CHARS - head_length  # 结尾保留多少
        omitted = len(text) - MAX_TOOL_OUTPUT_CHARS  # 中间省略了多少
        return (
            text[:head_length]
            + f"\n\n... [输出过长，中间省略了 {omitted} 个字符。"
            + "如需完整内容：读文件请用 file_read 的 offset / limit 分段；"
            + "查命令输出请把命令写得更窄（例如加 include 过滤、或用 file_grep 精确定位）。] ...\n\n"
            + text[-tail_length:]
        )

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
# 4. 【第 8 轮已部分处理】权限判断主要还是"模型说什么就是什么"，
#    本轮只加了"只读工具免确认"的过渡规则（见 _needs_user_approval）。
#    真正的权限规则（命令白名单、危险命令黑名单、工作区路径边界、
#    以及"不再询问"的记忆）放在第 9 轮做。
#
# 5. 【第 8 轮已完成】主循环原来是递归，已改成 while + 单层迭代
#    （_recursive_message_handling → _run_one_iteration），栈深度不再随轮数增长。
# =============================================================================
