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
from core.api_client import APIClient, ThinkingChunk, ToolCallChunk  # 导入 API 客户端：真正发请求给大模型的家伙；后两个是"模型还在动"的心跳
from core.commands import REGISTRY, handle_command, is_command  # 【第 14 轮】斜杠命令：在消息进历史之前截下来；【第 16 轮】REGISTRY 供 Tab 补全
from core.hooks import HookBlocked, HookRunner  # 【第 32 轮】生命周期 hook（SessionStart / PreToolUse / PostToolUse / SessionEnd）
from core.limits import MAX_TOOL_OUTPUT_CHARS  # 【第 9 轮】输出上限统一到一处，不再各处各写一个数
# 【2026-11-12 第 33 轮】工作模式：四档定义、循环切换、给模型看的提示文案。
#   注意这里只 import 纯数据和纯函数，模式状态本身存在 Conversation 实例上——
#   状态必须跟着会话走，放模块级全局会让子 agent 和 /resume 串味。
from core.modes import (
    MODE_DEFAULT,
    MODE_PLAN,
    cycle_mode,
    mode_reminder,
    normalize_mode,
    status_line,
)
from core.permissions import ACTION_ALLOW, READ_ONLY_TOOLS, classify  # 【第 9 轮】权限判定集中到 core/permissions.py
from core.prompt.prompt_manager import PromptManager  # 导入提示词管理器：负责生成"系统提示词"
from core.prompt.reminder import get_reminder  # 导入函数：生成一段"提醒模型去看 todo"的文字
from core.session import SessionStore, load_messages_for_resume  # 【第 13 轮】会话持久化
from core.stream_pump import StreamInterrupted, iter_stream_with_esc  # 【第 18.7 轮】Esc 在等待首字节时也能打断
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


# =============================================================================
# 【2026-11-11 第 26 轮】判断"这次失败要不要打一整屏 traceback"
#
# 背景：上游代理（.env 里的 OPENAI_BASE_URL）经常在读流到一半时断开，httpx 抛
#   RemoteProtocolError: peer closed connection without sending complete message
#   body (incomplete chunked read)。
#
#   这**不是** thinker 的代码有问题——下面 _run_one_iteration 的 except 里本来就有
#   "改用非流式重试"的兜底，实测几乎每次都能立刻拿到完整回答。但原来的写法是
#   不分青红皂白 print_exc()：屏幕上先一片红叉，再刷二十行 httpx 调用栈，
#   然后模型正常把答案说完。用户记住的只有"它又报错了"。
#
# 所以按异常链分类：
#   网络类（连不上 / 读超时 / 上游断流 / 5xx）→ 一行暗灰提示，安静重试；
#   其他（TypeError、KeyError、解析失败……）→ 保持原样打完整现场，那是真 bug。
# =============================================================================

# 用「类名集合」而不是 isinstance 判断，两个原因：
#   1. httpx / openai 的异常类在本项目里没有别的用处，为了判断类型去顶层 import
#      它们，等于让"只想读历史/跑测试"的场景也被 SDK 绑架；
#   2. 上游 SDK 迭代快（openai 的 APIConnectionError 就是继承 httpx 的），
#      类可能搬家/改名，按名字匹配对版本漂移更宽容。
_TRANSIENT_REASONS = (
    # 顺序有讲究：先匹配越具体的（连接/断流），再匹配泛化的超时。
    (("RemoteProtocolError", "ProtocolError", "ReadError", "NetworkError"), "上游断开连接"),
    (("ConnectError", "ConnectTimeout", "APIConnectionError"), "连不上服务器"),
    (("ReadTimeout", "WriteTimeout", "TimeoutException", "APITimeoutError"), "读取超时"),
    (("InternalServerError",), "服务端 5xx"),
    (("RateLimitError",), "触发限流（429）"),
)


def _iter_exception_chain(exc):
    """从外层异常开始，沿 __cause__ / __context__ 一路吐到最内层（2026-11-11 第 26 轮新增）

    为什么要走到底：api_client 把真正的异常包成了 StreamReadError，
    外面看到的 type(e).__name__ 永远是 "StreamReadError"，只有顺着 __cause__
    才能摸到 httpx.RemoteProtocolError。

    用 id() 去重防环：raise A from B、B 又 from A 这种构造虽然少见，
    但一旦成环，没去重的 while 会直接死循环——错误处理代码本身卡死最难查。
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _classify_transient(exc):
    """这次异常能不能靠重试自愈（2026-11-11 第 26 轮新增）

    返回 (是否网络类, 一行中文原因)。不是网络类时返回 (False, "")，
    调用方据此决定"安静重试"还是"打完整 traceback"。

    判断方式：把整条异常链上每一层的类名收进一个集合，再和关键词表求交集。
    只看最内层是不够的——有些代理库会在外面再包一层自己的异常。
    """
    names = {type(e).__name__ for e in _iter_exception_chain(exc)}
    for keywords, reason in _TRANSIENT_REASONS:
        if names & set(keywords):
            return True, reason
    return False, ""


# =============================================================================
# 【2026-11-08 第 14.2 轮】把 SDK 的 tool_calls 对象转成普通字典
#
# 【为什么必须转，两个原因一个比一个硬】
#   1. 会崩。assistant 消息里的 tool_calls 来自 openai SDK，是 pydantic 对象
#      （ChatCompletionMessageFunctionToolCall）。第 13 轮加了"每条消息即时落盘"之后，
#      SessionStore 会拿 json.dumps 写文件，而它不认识 pydantic 对象：
#        TypeError: Object of type ChatCompletionMessageFunctionToolCall is not JSON serializable
#      于是**这个会话第一次真正用上工具就会中断**，而且断在"模型已经答完、
#      正要去执行工具"的那一刻——用户看到的是半截回答加一段红色堆栈。
#   2. 前后不一致。内存里是对象、从 JSONL 读回来是字典，同一份历史有两种形态。
#      第 9 轮修压缩裁剪时就为此写了一堆"字典和对象都支持"的兜底
#      （见 history_manager 的 _role / _tool_calls），每处都要两条分支。
#      在入口统一成字典，后面所有代码就只需要认一种形态。
#
# 【为什么放在 add_message 而不是 _run_one_iteration】
#   add_message 是**所有**消息进入历史的唯一入口（user / assistant / tool / system）。
#   在入口处规范化，等于给整条链路加了一道闸；在某一类消息上单独处理，
#   下次换个地方塞进对象又会漏。
# =============================================================================

def normalize_message(message):
    """把消息里的 tool_calls 对象转成字典，不需要转换时原样返回（2026-11-08 第 14.2 轮）

    参数 message：一条消息。正常是 dict；测试里也可能传别的形态，那就原样返回
                  （这个函数只负责"能转就转"，不负责类型校验）。

    返回：转换过就是**新字典**，没转换就是传入的那个对象。

    边界与取舍：
      · tool_calls 为 None 或空列表 → 原样返回，不制造一个空列表塞进去。
        为什么在意这个：空列表和 None 在 OpenAI 接口里语义不同，
        None 表示"这条消息没申请工具"，空列表则可能被当成"申请了零个"。
      · 已经是字典（从 JSONL 读回来的）→ 原样返回，不做无意义的复制。
      · **不原地修改传入的字典**。调用方（_run_one_iteration）构造完
        assistant_message 还会用它，原地改会让"存进历史的那份"和
        "调用方手里那份"悄悄变成两个版本，排查起来极难。
    """
    if not isinstance(message, dict):  # 不是字典（测试里的自定义对象）就不管
        return message

    tool_calls = message.get("tool_calls")
    if not tool_calls:  # None 或空列表：没什么可转的
        return message

    if all(isinstance(call, dict) for call in tool_calls):
        return message  # 已经是干净数据（恢复会话时就是这种），省一次复制

    normalized = dict(message)  # 浅拷贝：只替换 tool_calls 这一个字段
    normalized["tool_calls"] = [_tool_call_to_dict(call) for call in tool_calls]
    return normalized


def _tool_call_to_dict(call) -> dict:
    """把单个 tool_call 转成 OpenAI 标准字典（2026-11-08 第 14.2 轮）

    优先用 pydantic 自己的 model_dump()：它知道哪些字段该带、哪些是内部字段，
    比手写字段映射可靠（SDK 以后加字段也不会漏）。
    v2 叫 model_dump()，v1 叫 dict()，两个都试。
    都拿不到才退回手写——那时候宁可少几个字段，也不能让整条消息存不进去：
    消息存不进去等于这段对话在恢复时会缺一环，比字段不全严重得多。

    边界：model_dump() 也可能抛异常（自定义对象、字段里有不可序列化的东西），
    所以调用外面套了 try；异常一律当作"这条路走不通"，换下一种方式。
    """
    if isinstance(call, dict):  # 已经是字典，直接返回
        return call

    for method_name in ("model_dump", "dict"):  # pydantic v2 / v1
        method = getattr(call, method_name, None)
        if not callable(method):
            continue
        try:
            dumped = method()
        except Exception:
            continue  # 这条转换路径不可用，试下一条
        if isinstance(dumped, dict):
            return dumped

    # 兜底：手写三个 OpenAI 规定必有的字段。
    # 这里用 getattr 带默认值，因为 call 可能是个残缺对象（测试造的假数据）。
    function = getattr(call, "function", None)
    return {
        "id": getattr(call, "id", None),
        "type": getattr(call, "type", "function"),
        "function": {
            "name": getattr(function, "name", None),
            "arguments": getattr(function, "arguments", "") or "",
        },
    }


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
    # 【2026-11-09 第 18 轮】当前正在跑的子 agent 定义栈（AgentSpec）。
    #   为什么需要"栈"而不是一个字段：子 agent 不能再派活（两道闸），所以理论上只有一层；
    #   但万一将来放开嵌套，或者某次异常没清干净，用栈能让"当前是谁"始终有据可查，
    #   比一个会被覆盖的字段好排查。主 agent 时栈为空。
    _agent_stack = []

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
            # 【2026-11-09 五层压缩第 3 批】把"工具是否可重复查询"的判断函数交给历史管理器：
            #   L3 Microcompact 折叠旧工具结果时，只折叠"可重复查询"（读操作）的结果，
            #   有副作用的（写文件/跑命令）绝不折叠——否则模型会误以为重跑能无害拿回数据。
            #   这里用 lambda 包一层，把"查工具属性"这件事从 ToolManager 转交给 HistoryManager，
            #   避免 HistoryManager 直接依赖 ToolManager（那会形成循环导入）。
            self._history_manager.set_repeatable_checker(
                lambda name: self._tool_manager.is_tool_repeatable(name)
            )
            self._prompt_manager = PromptManager()  # 建提示词管理器
            # 【2026-11-07 第 13 轮】会话落盘 + Esc 打断
            self._session = None  # 当前会话的存储句柄，start_conversation 时才创建
            # 【第 18 轮】每个实例自己持一份栈。虽然 Conversation 是单例、类属性也能用，
            #   但测试会通过 __new__ 造裸对象（不经 __init__），类属性容易串味道；
            #   current_agent 里用 getattr 兜底，两条路都不会炸。
            self._agent_stack = []
            # 【2026-11-10 第 21 轮】"待发消息"槽位。斜杠命令 /技能名 在这里放一段
            #   已经渲染好的技能正文，_read_user_input 会把它当成用户这一句返回，
            #   于是它走正常的 add_message → 发模型流程。普通命令永远是 None。
            self._pending_input = None
            # 【2026-11-12 第 33 轮】当前工作模式（四档，见 core/modes.py）。
            #   为什么放在 Conversation 上而不是全局：
            #     它是**会话级**状态，跟着这次对话走；放模块级全局会让
            #     子 agent、/resume 的另一条会话、测试之间互相串味。
            #   为什么要给默认值：单例的 __init__ 只跑一次，测试会用 __new__
            #     造裸对象（不走 __init__），那种对象上访问它会 AttributeError。
            #     后面所有读取点都用 getattr(self, "_mode", MODE_DEFAULT) 兜底。
            self._mode = MODE_DEFAULT
            # 【第 33 轮】模式切换后"还没告诉模型"的提示。
            #   切换发生在等用户输入的时候，此刻没有请求可发；把提示存在这里，
            #   等用户真正说下一句时拼进那条 user 消息（见 _user_message）。
            #   为什么不留一条单独的 system 消息：那会让历史多一条消息、
            #   而且有些接口对连续 system 消息的处理不一致；拼进 user 消息的第二块
            #   最稳，也不影响前缀缓存（它在尾部）。
            self._mode_notice = None
            self._esc = EscWatcher()  # 监听 Esc 的后台线程（不支持的平台会自动降级）
            # 【2026-11-12 第 32 轮】hook 执行器 + SessionStart 的上下文。
            #   _hook_context 在 start_conversation 里由 SessionStart 的 stdout 填充，
            #   再由 build_system_message 拼进系统提示词末尾——放属性而不是直接改
            #   PromptManager，是因为后者是单例、被 /resume 和子 agent 共用，
            #   往里塞会话级状态会串味。
            self._hooks = HookRunner()
            self._hook_context = ""
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

    @property
    def current_agent(self):
        """当前正在跑的子 agent 定义（AgentSpec）；主 agent 时返回 None（第 18 轮新增）

        三个地方要用它：
          1. `_get_available_tools()` —— 按它的 tools 白名单决定发哪些工具；
          2. `_execute_tool()` —— 硬调越权工具时拦住；
          3. `_run_one_iteration()` —— 按它的 model 覆盖本轮模型。

        用 getattr 兜底的理由和 add_message 里一样：单例的 __init__ 只跑一次，
        测试/裸对象可能先 __new__ 出来，属性还没赋值。返回 None 表示"当主 agent 处理"，
        这是安全方向（主 agent 本来就有全部权限）。
        """
        stack = getattr(self, "_agent_stack", None)
        return stack[-1] if stack else None

    def add_message(self, message):
        """Add message through history manager.（往当前对话里追加一条消息）

        【第 13 轮】顺手把消息写进会话文件，这样进程被杀也不会丢上下文。
        只在主 agent 时写：子 agent 的消息属于"临时草稿"，最后只会把结论交回来，
        把它们也落盘会让会话文件里混进大量无关内容，恢复时反而干扰模型。

        【2026-11-08 第 14.2 轮】进历史前先调用 normalize_message()：
          模型的回复带着 SDK 的 tool_calls **对象**，直接落盘会抛
          TypeError: ... is not JSON serializable，把整轮对话打断（详见该函数注释）。
          这一步必须在 add_message 里做、而不是在 _run_one_iteration 里做：
          这里是所有消息进历史的唯一入口，闸口只有一道才不会漏。
        """
        message = normalize_message(message)  # 转成可序列化的纯数据
        self._history_manager.add_message(message)  # 转手交给历史管理器

        # 【2026-11-10 第 21.7 轮】会话文件**懒创建**：第一条"真正发给模型的消息"出现时才落盘。
        #   改前：启动（或 /clear）就立刻建 JSONL——用户开 thinker 看一眼 /cost 就退出，
        #   也会留下一个只有 header + system 的空会话文件，/resume 列表被这种空壳填满。
        #   现在的口径：**系统提示词只进内存**；斜杠命令不进历史、自然也不建文件；
        #   第一条 user / assistant / tool 消息出现时，才把"header + 系统提示词 +
        #   到目前为止的全部消息"一次性写进新文件。
        #   为什么把整页一起补写而不是只写当前这条：文件格式约定是
        #   "header 行 + 按顺序的消息行"，漏掉 system 行的话 --continue 恢复时
        #   就少了系统提示词的占位（恢复逻辑会替换 system，但格式上得有这一行）。
        #   为什么子 agent 不触发创建：它们不落盘（见下面的 is_subagent 判断），
        #   而且子 agent 运行的前提是主对话已经有用户消息了。
        session = getattr(self, "_session", None)
        if session is None and not self.is_subagent and message.get("role") != "system":
            self._session = SessionStore.create(
                cwd=os.getcwd(),
                model=getattr(self._api_client, "model", "") or "",
                messages=self.messages,  # 当前这页全部消息（含本条），一次补写
            )
            return  # 这条消息已经随 create 写进去了，再 append 就重复了
        # 【第 13 轮】用 getattr 兜底而不是直接 self._session：
        #   单例的 __init__ 只在第一次执行，某些测试/子 agent 场景下对象是先 __new__ 出来的，
        #   属性可能还没被赋值。用 getattr 拿默认值，比依赖初始化顺序更稳。
        session = getattr(self, "_session", None)
        if session is not None and not self.is_subagent:
            session.append_message(message)  # 追加一行到 JSONL

    def build_system_message(self) -> dict:
        """造一条系统消息（2026-11-07 第 14 轮抽出）

        为什么抽出来：新建会话、`--continue` 恢复、`/resume` 切会话，三处都要造它。
        原来只有第一处有，另外两处得各自复制一遍字典字面量——
        系统提示词的包装结构一旦变（比如以后要加图片块），复制的那两处必然漏改。

        【2026-11-12 第 32 轮】末尾追加 SessionStart hook 的输出。
          为什么接在最后、而不是塞进 PromptManager：
            ① PromptManager 是单例，被 /resume 和子 agent 共用，往里放会话级状态会串味；
            ② 环境信息本来就该排在提示词尾部（第 11 轮的判断），hook 的 git status /
               待办列表正好属于同一类"本轮环境"，接在后面读起来是连贯的。
          没有 hook 输出时（绝大多数人）拼接结果和以前一字不差。
        """
        prompt = self._prompt_manager.get_system_prompt()
        if getattr(self, "_hook_context", ""):
            prompt = f"{prompt}\n\n## 会话启动时注入的信息（hook 生成）\n{self._hook_context}"

        return {
            "role": "system",  # system 表示"系统设定"，是给模型看的规则，用户一般看不到
            "content": [
                # content 用列表而不是字符串：以后可以塞图片、文件等不同类型的内容块
                {"type": "text", "text": prompt}
            ],
        }

    def _load_session_into_history(self, path):
        """把某个会话文件的内容装进当前历史（2026-11-07 第 14 轮抽出）

        返回 (恢复的消息条数, 估算 token 数)。

        启动时的 `--continue` 和运行中的 `/resume` 走的是同一段逻辑：
          读文件 → 换消息列表 → 补用量账本 → 换会话句柄。
        四步缺一不可，尤其是"补用量账本"——第 13.1 轮就是漏了它，
        恢复长会话后第一次请求会绕过压缩直接 400。

        为什么不用「清空再逐条 append」：
          append 会走 add_message，进而触发会话落盘。
          恢复时把从文件读出来的消息再写回同一个文件，等于每条都存两遍，
          文件会越滚越大。直接替换列表就绕过了这条路径。
        """
        restored = load_messages_for_resume(path)  # 丢掉旧 system，只取对话本身
        system_message = self.build_system_message()  # 用今天的日期/目录/记忆重新生成
        self._history_manager.messages_history[-1] = [system_message] + restored
        estimated = self._history_manager.seed_usage_from_messages([system_message] + restored)
        self._session = SessionStore(path)  # 之后的每条新消息继续写进这个文件
        return len(restored), estimated

    def reset_conversation(self):
        """清空当前上下文，并换一个新的会话文件（2026-11-08 第 14.5 轮新增）

        返回 (旧会话文件路径, 新会话文件路径)。旧的可能为 None（还没建过会话）。

        【为什么"清空"必须同时换会话文件】
          只把内存里这一页清掉的话，旧内容仍然躺在 JSONL 里。
          用户下次 `--continue`（或用 /resume 选中它），那些"已经清掉"的对话
          会原样回来——他会认为命令没生效，而且完全没法察觉
          是"清了内存、文件还在"。
          换成新文件之后两条路都不丢：新会话从零开始，旧文件完整保留，
          想回去随时 /resume。

        【为什么用 add_message 放回 system，而不是直接拼一行】
          add_message 会同时写历史和落盘，复用它就顺带享受了 normalize_message
          那一层处理，不必再多写一份"怎么序列化"的逻辑。

        【为什么最后要重算用量】
          清空前账本记的是"十几条消息、8000 token"，清空后如果不改，
          /cost 和状态行会继续显示那个数字，用户会以为没清干净。
          这里用和恢复会话同一个估算函数，保证两处口径一致。
        """
        old_path = getattr(getattr(self, "_session", None), "path", None)

        # 【第 21.7 轮】不再立刻建新文件，和启动时一样懒创建：
        #   用户 /clear 之后可能又只敲了个 /cost 就退出，立刻建文件同样会留下空壳。
        #   self._session 置 None 之后，第一条真正的消息会走 add_message 里的
        #   懒创建分支，把 system + 那条消息一起写进新文件。
        self._session = None

        self._history_manager.messages_history[-1] = []  # 清空当前这一页
        self.add_message(self.build_system_message())  # 重新放回系统提示词（只进内存）
        self._history_manager.seed_usage_from_messages(self.messages)  # 重算占用

        # 第二个返回值可能是 None：还没有新文件（懒创建）。
        #   调用方（cmd_clear）要按 None 单独提示，不能直接 .name。
        return old_path, None

    async def _read_user_input(self) -> str:
        """读一行用户输入；如果是斜杠命令，本地处理完继续等（2026-11-07 第 14 轮新增）

        为什么要收成一个方法，而不是三个调用点各写一遍：
          主 agent 有三处会等输入——开场、Esc 中断之后、一轮说完之后。
          三处各写一遍判断，将来加第四处必然漏；收在这里，
          "命令对所有用户输入都生效"就是结构上保证的，不靠人记得。

        为什么命令处理完要接着 while，而不是直接返回一个空串：
          `/cost` 打完用户当然还要继续说下一句。返回空串当用户消息的话，
          模型会收到一条空消息，还要为它付一次输入费用——而这条命令
          问的偏偏是"我花了多少钱"。

        子 agent 不走这里：它们不读用户输入（`_task_depth > 0` 时主循环直接返回）。
        这里保留一个兜底判断，是为了将来万一有别的地方调用。

        【第 21 轮】多了一个 pending 分支：技能的斜杠命令（/pdf a.pdf）在 handler 里
          把渲染好的正文放进"待发消息"槽位，这里取出来当成用户这一句返回。
          为什么放在 handle_command 之后、add_message 之前：
            · 之后：只有命令 handler 会写 pending，普通命令的 pending 永远是 None，
              所以"斜杠命令不进历史、不计费"的规矩一字不变（第 14 轮）；
            · 之前：返回的这段话会走正常流程被 add_message 记进历史、发给模型——
              这正是技能想要的效果（"我说了这段要求"）。
          为什么不走"先让模型调一次 skill 工具"：
            用户是主动点名的，语义上就是"我说了这段要求"；而且那样要多花一次
            往返（模型先决定调工具、再看着正文干活），白付一次输入。
        """
        while True:
            # 【第 16 轮】把补全源传进去：用户敲 "/" 之后按 Tab，就能把命令名补全。
            #   传 None 的调用点（授权提问、选会话编号）不受影响，仍然走老路。
            text = await self._ui_manager.get_user_input(completer=self._complete_command)
            if not is_command(text) or self._task_depth > 0:
                return text
            await handle_command(text, self)  # 处理完继续循环，等用户真正想说的话

            # 【第 21 轮】技能命令会在这里留下待发正文。取出即清空（见 take_pending_input），
            #   所以同一段正文不可能被发两次、也不会串到下一句输入上。
            pending = self.take_pending_input()
            if pending:
                return pending  # 当成用户这一句，正常进历史

    def set_pending_input(self, text: str) -> None:
        """放一段"等着被当成用户输入"的文字（2026-11-10 第 21 轮新增）

        谁会用：技能的斜杠命令 handler。它把渲染好的技能正文放进来，
        让主循环把它当成用户这一句处理，而不是自己直接去调模型
        （那样会绕过 add_message，这段话就不在历史里了，下一轮模型会"忘了自己读过"）。

        为什么只留一个槽位、后写覆盖先写：
          一次输入只可能触发一条命令，两条命令同时写 pending 是不可能的。
          用列表反而会引出"要不要按顺序发、发完要不要清空"这些没必要的分支。
        """
        self._pending_input = text

    # =========================================================================
    # 【2026-11-12 第 33 轮】工作模式：状态、切换、以及"什么时候告诉模型"
    #
    # 为什么这一组方法直接放在 Conversation 上、不另做一个 ModeManager：
    #   模式的全部状态就是一个字符串 + 一个待发提示，没有任何需要封装的资源。
    #   多一个单例只会多一处"初始化时序"的坑（项目里已经有四个单例了），
    #   而它需要读写的东西（_ui_manager、历史注入点）本来就在 Conversation 手里。
    # =========================================================================

    @property
    def mode(self) -> str:
        """当前工作模式（2026-11-12 第 33 轮新增）

        用 getattr 兜底的理由见 __init__ 里 _mode 的注释：
        测试可能先 __new__ 造裸对象、不走 __init__，那种对象上没有这个属性。
        返回默认档是安全方向——问得最多的那一档。
        """
        return getattr(self, "_mode", MODE_DEFAULT)

    def set_mode(self, raw) -> tuple:
        """切换工作模式（2026-11-12 第 33 轮新增；33.3 轮去掉 announce）

        返回 (是否切换成功, 给用户看的一句话)。失败时成功标志为 False，
        调用方据此决定要不要列出可选模式——这里不打印错误，因为
        `/mode`、启动参数、快捷键三个入口的错误文案不该一模一样。

        为什么没有 announce 了：第 33 轮它控制"切换时要不要打一行/刷底栏"，
        而现在底栏不显示模式、`/mode` 又自己负责打印那一行状态，
        两条路都不需要 set_mode 亲自往屏幕上写东西。留着一个"什么也不做"的
        参数，只会让调用方以为它还在起作用。

        为什么切换时还要写 _mode_notice：
          切换发生在等用户输入的时候，此刻没有请求在飞。
          模型要到用户说下一句才知道模式变了——那时 _user_message 会把
          这段提示拼进同一条消息。不这么做的话，模型会拿着旧模式的认知
          继续干活（最典型的是 plan 切回 accept 之后，它还在问"要不要我动手"）。
        """
        name = normalize_mode(raw)
        if name is None:
            return False, f"未知模式：{raw}"

        if name == self.mode:
            # 已经是这一档：仍然返回一行状态，让快捷键路径把它刷在输入行上方。
            # 为什么不返回"已经是计划模式，没有变化"：那是一句日志，
            # 连按到循环回原档时会多出一行噪音；状态行覆盖同一行，视觉上没变化。
            return True, status_line(name)

        self._mode = name
        self._mode_notice = mode_reminder(name)

        # 【第 33.3 轮】切换只做两件事：更新状态、把新档位告诉输入层（供状态行上色）。
        #   屏幕上那句话由调用方决定怎么显示——/mode 用 print_mode_status 打一行，
        #   快捷键交给 LineEditor 原地覆盖。set_mode 不再碰界面。
        message = status_line(name)
        self._ui_manager.set_mode_state(name)
        return True, message

    def cycle_mode(self) -> tuple:
        """按 Shift+Tab 的顺序切到下一档（2026-11-12 第 33 轮新增）

        顺序由 core/modes.py 的 MODE_SPECS 决定（default → plan → accept → auto），
        这里不重复写一遍——两处各写一份顺序，加第五档时必然漏一处。
        """
        return self.set_mode(cycle_mode(self.mode))

    def _cycle_mode_from_key(self) -> tuple:
        """快捷键按下时的回调（2026-11-12 第 33 轮新增）

        为什么单独包一层、不把 set_mode 直接交给 UI：
          set_mode 只改状态、不打印，但"这一行由谁画"必须由调用方决定。
          快捷键这条路上光标还停在输入行中间，只有 LineEditor 知道当前屏幕状态
          （它会"清行 → 打提示 → 重画输入行"）。包一层把这个入口显式标出来，
          以后再加"从别的地方切模式"时，能一眼看到这里还有一条专用通道。
        """
        return self.cycle_mode()

    def _user_message(self, text: str) -> dict:
        """构造一条用户消息，必要时把"模式已切换"的提示拼在后面（第 33 轮新增）

        为什么把提示作为**第二个内容块**，而不是直接改用户输入的文字：
          用户看到的必须是他自己说的话；模型可以多看到一块"当前模式"。
          分块之后，将来要拆、要换成 system 消息，都只是改这里一处。

        为什么提示放在用户文字**后面**：
          模型是按顺序读的，"用户让我做 X，当前模式是 plan"读起来是
          "这条要求受这个约束"；反过来会变成"模式是 plan，然后用户让我做 X"，
          约束容易被当成背景信息忽略掉。

        为什么取出即清空：和 _pending_input 同一个理由——同一条提示只该发一次。
        忘了清会让之后每一轮都白带一段文字，还重复计费。
        """
        content = [{"type": "text", "text": text}]
        notice = getattr(self, "_mode_notice", None)
        if notice:
            content.append({"type": "text", "text": notice})
            self._mode_notice = None
        return {"role": "user", "content": content}

    def take_pending_input(self):

        """取出待发消息，**取出即清空**；没有就返回 None（2026-11-10 第 21 轮新增）

        为什么取出就清空，而不是让调用方自己清：
          这里最容易出的 bug 是"忘了清"，后果是同一段技能正文被重复当成用户消息，
          每一轮都白发一次、还重复计费。把清空和取出绑在同一个动作里，
          调用方就没有机会写错。

        为什么用 getattr 拿默认值：
          Conversation 是单例，测试和子 agent 场景会先 __new__ 造裸对象、不走 __init__，
          那种对象上没有 _pending_input。返回 None 等于"没有待发消息"，是安全方向。
        """
        pending = getattr(self, "_pending_input", None)
        self._pending_input = None
        return pending

    @staticmethod
    def _complete_command(text: str) -> list:
        """给 Tab 补全提供候选（2026-11-08 第 16 轮新增）

        只补**命令名**，不补参数。三个判断：

          1. 不是 "/" 开头 → 没有候选。正常对话按 Tab 不该有任何反应。
          2. 已经有空格 → 没有候选。说明用户已经在写参数了
             （比如 `/memory 笔记.md`），这时候还弹出命令名列表只会碍事。
          3. 其余 → 把注册表里所有命令名列出来。

        为什么直接用 REGISTRY 而不另外维护一份清单：
          第 14 轮起命令都登记在 REGISTRY 里，`/help` 也是遍历它生成的。
          另写一份"补全用的命令名清单"，等于同一件事维护两处——
          新增命令时一定会漏更新，然后在补全里消失，而且没人会想到是这里的问题。

        为什么返回"完整候选"而不是"前缀匹配的结果"：
          过滤和补全的逻辑统一交给 LineEditor.resolve_completion，
          这里只回答"有哪些可能"。职责分开，测试也好写（纯函数，不用碰键盘）。
        """
        if not text.startswith("/") or " " in text:
            return []
        return ["/" + name for name in sorted(REGISTRY)]

    # =========================================================================
    # 【第 32 轮】hook 的四个触发点集中在这里，方便对照 settings.json 的事件名。
    #   刻意不散落在各处：以后加 Stop / UserPromptSubmit 时，
    #   "一共支持哪些事件"应该能在一屏里数清楚。

    def _hook_payload(self, event: str, tool_name: str = None, tool_input: dict = None) -> dict:
        """拼出 hook 子进程的 stdin 内容（字段名对齐 Claude Code）

        session_id 可能为空串：第 21.7 轮起会话文件是懒创建的，
        全新启动、还没发过消息时 self._session 是 None。
        空串比省略这个键更好——脚本里写 data["session_id"] 不会 KeyError。
        """
        session_id = getattr(self._session, "id", "") if self._session is not None else ""
        payload = {
            "session_id": session_id or "",
            "cwd": os.getcwd(),
            "hook_event_name": event,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
            payload["tool_input"] = tool_input or {}
        return payload

    def _report_hook_problems(self, outcome) -> None:
        """把加载期问题和运行期错误打出来（只在不影响流程时用）

        为什么统一走 print_warning 而不是静默：hook 最气人的失败方式就是"配了没生效"。
        用户没有任何反馈，只能怀疑自己写错了事件名 / 放错了文件。
        """
        for note in outcome.errors:
            self._ui_manager.print_warning(f"[hooks] {note}")

    async def _run_session_start_hooks(self) -> None:
        """SessionStart：在系统提示词造出来之前跑，stdout 进上下文（第 32 轮新增）

        为什么 SessionStart 不能阻断启动：
          它的定位是"给模型塞点环境信息"。exit 2 在 Claude Code 里也只是把 stderr
          给用户看。为一条取 git status 的脚本拒绝启动整个 CLI，是本末倒置。
        """
        # 加载期的问题（未知事件名、JSON 写坏）在这里报一次就清掉。
        # 不清的话，/resume 之后重建系统提示词时会再刷一遍，看起来像新问题。
        for note in self._hooks.notes:
            self._ui_manager.print_warning(f"[hooks] {note}")
        self._hooks.notes.clear()

        # 【第 32 轮】配了 hook 就报一行。hook 是典型的"配了却看不见"的功能：
        #   SessionStart 的输出进系统提示词（不打印），PreToolUse 平时也不出声，
        #   用户唯一能感知的时刻是"被拦了一次"。没有这一行，他只能怀疑配置没读到。
        #   和技能那行提示（_print_skill_hint）是同一个理由：功能做得再好，
        #   不写在用户会看的地方等于没做。
        if self._hooks.specs:
            self._ui_manager.print_info(
                f"已加载 {len(self._hooks.specs)} 个 hook（{self._hooks.settings_path}）"
            )

        outcome = await self._hooks.run_event(
            "SessionStart", self._hook_payload("SessionStart")
        )
        self._hook_context = outcome.context  # 由 build_system_message 拼进提示词
        self._report_hook_problems(outcome)
        if outcome.blocked:
            # 脚本写了 exit 2：不拦启动，但要让人知道这句话没被当成"环境信息"
            self._ui_manager.print_warning(f"[hooks] SessionStart 返回了阻断信号，已忽略：{outcome.reason}")

    async def _pre_tool_hooks(self, tool_name: str, tool_input: dict):
        """PreToolUse：返回拒绝原因（None 表示放行）（第 32 轮新增）

        调用时机是"权限确认之前"：用户按下同意之后才被告知"hook 不让干"，
        那一次确认就白点了。放前面还有个好处——被 hook 拦掉的调用
        根本不会出现高危提示，屏幕上更干净。
        """
        outcome = await self._hooks.run_event(
            "PreToolUse", self._hook_payload("PreToolUse", tool_name, tool_input), tool_name=tool_name
        )
        self._report_hook_problems(outcome)
        if outcome.blocked:
            return outcome.reason or f"PreToolUse hook 拒绝了 {tool_name}"
        return None

    async def _post_tool_hooks(self, tool_name: str, tool_input: dict, result):
        """PostToolUse：工具成功执行之后跑（第 32 轮新增）

        返回"要追加到工具结果末尾的文本"（没有就是空串）。
        为什么追加而不是替换结果：PostToolUse 的典型用途是格式化 / 记日志 / 提醒，
        把工具真正返回的内容换掉，模型就看不到它要的数据了。
        （要让 hook 改写结果，用 Claude Code 的 updatedInput/decision 那套，P3 再说。）
        """
        outcome = await self._hooks.run_event(
            "PostToolUse",
            self._hook_payload("PostToolUse", tool_name, tool_input),
            tool_name=tool_name,
        )
        self._report_hook_problems(outcome)
        if outcome.blocked:
            # PostToolUse 的 exit 2 语义：工具已经跑完了，拦不住，只能把话带给模型
            return f"\n\n[PostToolUse hook] {outcome.reason}"
        return f"\n\n[PostToolUse hook] {outcome.context}" if outcome.context else ""

    async def _run_session_end_hooks(self) -> None:
        """SessionEnd：退出前跑（第 32 轮新增）

        放在 shutdown 的最前面、并且自己兜住异常：
        后面还要关 MCP 进程，一条清理脚本抛错不能把"关连接"也拖下水。
        """
        try:
            outcome = await self._hooks.run_event(
                "SessionEnd", self._hook_payload("SessionEnd")
            )
            self._report_hook_problems(outcome)
        except Exception as error:  # 退出路径上任何异常都不该冒泡（已经在 finally 里了）
            self._ui_manager.print_warning(f"[hooks] SessionEnd 执行异常：{type(error).__name__}: {error}")

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

        # 【2026-11-12 第 33 轮】把模式相关的两个钩子交给输入层：
        #   · cycler：快捷键按下时调用，负责真正切档并返回提示文案；
        #   · state ：当前档位，底栏用它决定显示什么名字、什么颜色。
        # 每次 start_conversation 都设一遍是幂等的（/resume 会再进来一次），
        # 代价是两次属性赋值，换来的是"任何进入对话的路径都保证钩子已就位"。
        self._ui_manager.set_mode_cycler(self._cycle_mode_from_key)
        self._ui_manager.set_mode_state(self.mode)

        # 【第 32 轮】SessionStart 必须在 build_system_message 之前：
        #   它的 stdout 要拼进系统提示词，晚一步这一轮就少了那段环境信息。
        await self._run_session_start_hooks()

        # Initialize with system message
        # 【第 14 轮】改成调用 build_system_message()：恢复会话、/resume 切会话也要造它，
        #   三处共用一份，包装结构变了才不用改三遍。
        system_message = self.build_system_message()

        # 【第 13 轮】建立会话存储。
        #   恢复时直接续写**原来那个文件**，而不是新建一个——这样一次对话
        #   从头到尾都在同一个 JSONL 里，翻起来是连续的，也不会重复存旧消息。
        if resume_path:
            # 【第 14 轮】整段抽进 _load_session_into_history()：
            #   运行中的 /resume 用的是同一份逻辑。两处各写一遍的话，
            #   第 13.1 轮补的那个"用量估算"迟早只补在一处。
            count, estimated_tokens = self._load_session_into_history(resume_path)
            self._ui_manager.print_info(
                f"已恢复上次会话：{count} 条消息"
                f"（按字符估算上下文约 {estimated_tokens:,} token）"
            )
        else:
            # 【第 21.7 轮】启动时**不再立刻建会话文件**（懒创建）。
            #   改前这里就 SessionStore.create(...)：用户开 thinker 看一眼 /cost 就退出，
            #   也会留下一个空会话文件，/resume 列表被这种空壳填满。
            #   现在 self._session 保持 None，第一条真正发给模型的消息
            #   （add_message 里的懒创建分支）才落盘；系统提示词只进内存。
            #   为什么 system 也不写：它每轮都由 build_system_message 重新生成
            #   （日期/目录会变），落盘的意义只在"文件格式上有这一行"，
            #   那件事由懒创建时的 messages=self.messages 一次性补齐。
            self._session = None
            self.add_message(system_message)  # 把系统提示词放进消息列表（永远排在最前面）

        # 【第 14 轮】走 _read_user_input：斜杠命令在这里被截下，不进历史、不发模型
        user_input = await self._read_user_input()
        # 【第 33 轮】改走 _user_message：模式切换后的提示要拼进这条消息。
        #   为什么不在上面直接改 user_input：用户看到的和模型看到的应该分开处理。
        self.add_message(self._user_message(user_input))  # 放进消息列表

        try:  # 开始试：后面的整个对话循环都可能出错
            await self._recursive_message_handling()  # 进入主循环（下文详解）
        except Exception as e:  # 如果循环里出现任何没被处理的错误
            self._ui_manager.print_error(f"发生系统错误：{e}")  # 在界面上报个错
            traceback.print_exc()  # 再把完整的错误现场打印出来，方便定位问题

    async def start_task(self, task_system_prompt: str = None, user_input: str = "", agent=None) -> str:
        """
        开始一个"子任务"。这是 Task 工具（就是让模型能派活给子 agent）的入口。

        关键点：它会在消息历史里"新开一页"，
        所以子 agent 看不到主对话的历史，主对话也不会被子 agent 的啰嗦过程污染。
        干完活以后，只把最后一条回复带回去。

        【第 18 轮】新增 agent 参数（AgentSpec）。
          为什么不能只传一段 prompt：子 agent 类型化之后，每个类型除了人设，
          还带着「能用哪些工具」和「用哪个模型」。只传文本的话，这两样带不进来，
          Conversation 也就没法在清单层和执行层按类型收窄。
          兼容：agent 为 None 时仍用 task_system_prompt（脚本/测试的老路还能走）。
        """
        # 【第 18 轮】深度和栈必须同进同出，所以入栈紧跟在深度 +1 之后，
        #   出栈统一放在下面的 finally 里（异常、失败返回、正常交差三条路都会走到）。
        self._task_depth += 1  # 深度 +1：从现在起，我就是个子 agent 了
        self._agent_stack.append(agent)  # 记下"这一层是谁在干活"

        self._history_manager.start_new_chat()  # 新开一页消息记录（相当于翻到新的一页纸）
        system_prompt_text = task_system_prompt
        if agent is not None:
            # 局部 import：agent_spec 会去 import core.prompt.environment，
            # 放在模块顶部会让"对话总管 → 子 agent 模块 → 提示词"这条链提前成形，
            # 以后 environment 里想 import conversation 就会成环。
            from tools.subagent.agent_spec import build_system_prompt

            system_prompt_text = build_system_prompt(agent)

        system_message = {
            "role": "system",  # 子 agent 也有自己的人设
            "content": [
                {"type": "text", "text": system_prompt_text}  # 子 agent 专用的系统提示词
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
            self._discard_current_chat()  # 【2026-11-06 优化】把出错的那一页消息丢掉
            # 【2026-11-06 优化】原来是 sys.exit(1)，直接把整个程序干掉：
            #   子 agent 只要有一个工具抛异常，你的 CLI 就退出、主对话上下文全丢。
            #   现在改成"把错误当成子任务的结果交回去"，主 agent 可以自己决定换个做法。
            return f"子任务执行失败：{e}"  # 返回失败说明，让主 agent 看到并自行补救
        finally:
            # 【第 18 轮】深度的还原从"两条路各减一次"改成 finally 只减一次。
            #   改前正常路径减一次、异常路径减一次，看着对称，但以后加第三条 return
            #   就一定会漏；而漏掉的后果是"之后所有对话都以为自己在子 agent 里"，
            #   表现为主 agent 突然拿不到 task 工具、不再问用户输入——极难归因。
            self._agent_stack.pop()  # 出栈，和上面的 append 严格配对
            self._task_depth -= 1  # 深度还原
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

        # 【第 18 轮】子 agent 可以自带模型（md 里写 model: xxx）。
        #   为什么不直接改 self._api_client.model：那是全局状态，子 agent 结束后
        #   还得改回来，一旦中途异常就永久改了主 agent 的模型。
        #   走 request 字段是"只对这一轮生效"，天然没有还原问题。
        #   APIClient 那边已改成"request 里有就用 request 的"（见 get_completion_stream）。
        agent = self.current_agent
        if agent is not None and agent.model:
            request["model"] = agent.model

        # Start assistant response
        # 【第 4 轮】圆点不再单独打印：它现在画在流式区域内部，
        # 这样模型"只调工具不说话"时，屏幕上不会多出一个空圆点。

        # Use streaming API for response
        # 【第 26 轮】在 try 之前先把"已经显示到屏幕上的半截内容"初始化掉。
        #   原来它只写在 try 内部（下面的 full_content = ""）。如果
        #   get_completion_stream() 在**建立连接**时就抛异常——上游断流最常见的
        #   形态——except 里那句 `if full_content.strip()` 会直接 NameError，
        #   把一次本可自动重试的网络抖动升级成真正的崩溃。
        full_content = ""
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
                # 【第 18.7 轮】改走 iter_stream_with_esc：读流的活交给后台线程，
                #   这里每 50ms 醒一次先看 Esc 再看队列。
                #   改前是 `for chunk in iterator` + 循环体内检查 Esc——模型思考时
                #   （首字节还没到）循环体一次都不执行，那 11 秒里按 Esc 完全没反应。
                for chunk in iter_stream_with_esc(iterator, esc):  # 一段一段收
                    # 【第 28 轮】先处理"思考心跳"。
                    #   推理模型在开口前可能想几分钟，这期间 content 一直是空的。
                    #   原来这类 chunk 在 api_client 里被静默丢掉，界面完全不知道
                    #   网络还在传数据，只能干等——用户看到的就是"光标在闪、像死机"。
                    #   现在把字数转给界面，让 spinner 那行的数字持续上涨。
                    #   注意它不能进 full_content：思考内容一旦被当成"模型说过的话"
                    #   存进历史，下一轮会连内心独白一起发回去，上下文暴涨。
                    if isinstance(chunk, ThinkingChunk):
                        self._ui_manager.note_thinking(chunk.chars)
                        continue
                    # 【第 30 轮】工具参数生成阶段的心跳。
                    #   模型说完"我继续改 effects.js"之后，可能还要好几秒才把
                    #   file_edit 的参数吐完；这段时间 api_client 只往本地拼参数、
                    #   不产出正文，而 spinner 已经在第一个正文字到达时被停掉，
                    #   屏幕上一动不动，看起来就是卡死。把"参数又长了一截"转给
                    #   界面，让它在正文下方单独一行画心跳。
                    #   同样必须 continue：它不是文字，绝不能进 full_content / 历史，
                    #   否则工具参数会被当成"模型说过的话"，下一轮原样发回去。
                    if isinstance(chunk, ToolCallChunk):
                        self._ui_manager.note_tool_call(chunk.chars)
                        continue
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
            except StreamInterrupted:
                # 【第 18.7 轮】Esc 打断走这里，不再在循环体里 break。
                #   为什么用异常：打断可能发生在"还没收到任何数据"的时候，
                #   那时根本没有循环体可执行，只能在等的地方抛出来。
                #   已经输出的半截内容保留（下面 interrupted 分支统一处理）。
                interrupted = True
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
                user_input = await self._read_user_input()  # 【第 14 轮】命令也在这里截
                # 【第 33 轮】同样走 _user_message：中断这条路上切换过模式也要告知模型
                self.add_message(self._user_message(user_input))
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

            # 【第 26 轮】先把异常分成"网络抖动"和"真 bug"两类，再决定怎么报。
            #   网络类不打 traceback：它不是用户要处理的事，而且下面马上就会重试成功；
            #   在这里刷二十行 httpx 调用栈，只会让人以为 CLI 崩了。
            transient, reason = _classify_transient(e)
            if transient:
                self._ui_manager.print_info(f"流式响应被中断（{reason}），自动改用非流式重试……")
                # 断在回答中途时，屏幕上已经有半截内容；非流式重试会再完整输出一遍，
                # 用户看到"两段一模一样的开头"会以为程序抽风。提前说一句，避免误会。
                if full_content.strip():
                    self._ui_manager.print_info("上面已输出的半截作废，下面这条是完整回答。")
            else:
                self._ui_manager.print_error(f"处理流式响应时出错：{e}")  # 报错
                self._ui_manager.print_info(f"错误类型：{type(e).__name__}")  # 打印错误类型
                traceback.print_exc()  # 打印完整现场（只有非网络错误才值得看调用栈）

            # Fallback to non-streaming mode
            try:  # 再试：退一步，用"非流式"方式重发一次（不要逐字效果了，能拿到结果就行）
                if not transient:  # 网络类在上面已经把"自动重试"说过了，别重复同一句话
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
            user_input = await self._read_user_input()  # 主 agent 就等用户说下一句（【第 14 轮】命令已在此截下）
            # 【第 33 轮】走 _user_message：把"模式已切换"的提示拼成第二个内容块
            self.add_message(self._user_message(user_input))  # 存进历史
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
        # 【2026-11-13 第 33.4 轮】子 agent 期间标明这是谁的账本。
        #   为什么必须标：子 agent 会 start_new_chat() 压一页新历史，
        #   用量账本也跟着压入一条 0，于是这行百分比从 48.5% 直接跳到 1.0%——
        #   数字本身没错（那确实是子 agent 刚起步的占用），但和主对话那一行
        #   长得一模一样，用户看到的就是"我的上下文被清空了"。
        #   主对话的数字并没有丢，只是被压在栈底；子 agent 交差后会自动跳回来。
        #   代价只有一个词，换来的是这条路不再需要解释。
        label = "子 agent 上下文" if self.is_subagent else "上下文"
        self._ui_manager.print_status(
            f"{label} {used_text}% {DOT} 花费 ${self._api_client.total_cost}{cache_part}", level
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
        # 【第 32 轮】SessionEnd 放在关 MCP 之前：清理脚本可能还要用外部工具，
        #   先拆连接再跑 hook，脚本看到的就是一个半拆掉的环境。
        #   _run_session_end_hooks 内部自己兜异常，这里不必再包 try。
        await self._run_session_end_hooks()
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
        # 【2026-11-09 五层压缩第 2 批】按需用「只读投影」替代完整历史：
        #   占用落在 [micro_threshold, collapse_threshold)（0.90~0.95，即 L4 档）时，
        #   用 get_projection() 拿压缩视图发给模型，底层存储一字不动。
        #   这是 L4 Context Collapse 的落地——不改数据、只换视角。
        #   一旦 >= collapse_threshold（0.95），就交给 auto_messages_compression 的 L5
        #   调模型全量摘要（真删存储），投影自动退场。
        if self._history_manager.should_use_projection():
            messages = self._history_manager.get_projection()
        else:
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

        【第 18 轮】再加一道「类型白名单」：
          每个子 agent 定义里可以写 tools（比如 explore 只有 file_glob/file_grep/file_read），
          这里把 allow 传给工具管理器，模型连"越权工具长什么样"都看不到。
          和 exclude 的分工：exclude 是"任何子 agent 都不给 task"，
          allow 是"这个类型只给这几个"。两者都生效，不用在调用处手工求差集。

        为什么是 async？因为要等 MCP 工具注册完（连外部服务器要时间）。

        【第 21 轮】子 agent 的排除名单从 {"task"} 变成 {"task", "skill"}。
          两个理由，都不是"顺手省点 token"：
            1. 子 agent 的提示词是刻意精简的（agent_spec.build_system_prompt 只给三行环境）。
               技能正文动辄两三千字，可能比它手上的任务本身还长，而它只需要"找东西/看代码"；
            2. 技能正文是写给**主 agent** 的，里面很可能写着"用 todo_write 记一下进度"
               "拿不准就派个子 agent"。子 agent 拿到这些指令会去撞 todo_write——
               而那个工具的状态存在单例实例上，一撞就覆盖主 agent 的待办清单。
          这是两道闸里的第一道（清单层），第二道在 _is_tool_allowed。

        【第 33 轮】曾在这里按模式剪过清单（plan 下只发只读工具）。
        【第 33.5 轮撤掉那一层】改成 Claude Code 的方向：**工具照常下发，
          由权限层在执行前裁决**。撤掉的两个理由都是实测出来的：
            · 模型看不到 file_write，写方案时就说不出"用哪个工具改哪一处"，
              方案粒度只到"改这里"，落地时还得再猜一遍；
            · cmd_runner 被白名单卡死，`node --check`、`Get-ChildItem` 这类只读命令
              全被挡，计划模式几乎没法调研。
          安全边界没变松：classify() 的 plan 分支仍然会 deny 写工具与写命令，
          只是"看不到"变成了"看得见、动不了"，模型知道该往哪个方向写方案。
        """
        exclude = {"task", "skill"} if self.is_subagent else None  # 子 agent：派活和加载技能都不给
        allow = None  # None = 不限制（主 agent、或"继承全部"的子 agent）
        agent = self.current_agent
        if agent is not None and agent.tools is not None:
            # 白名单里再剔一次 task：万一用户在自己写的 md 里塞了 task（加载时已经告警并剔除），
            # 或者以后有人手工 register 时绕过了解析，这里仍然兜住。
            # 【第 21 轮】skill 同理：白名单里写了也不给，和 exclude 那条保持一致。
            allow = {name for name in agent.tools if name not in ("task", "skill")}

        # 【第 33.5 轮】这里不再按模式剪枝——见方法文档里那段撤掉清单层的说明。
        return await self._tool_manager.get_tools_description(exclude=exclude, allow=allow)  # 向工具管理器要清单

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

    def _readonly_hint(self, tool_name: str) -> bool:
        """这个工具是否"自己声明了只读"（2026-11-13 第 33.5 轮新增）

        目前只有 MCP 工具能给出这个信息（服务端 Tool.annotations.readOnlyHint）。
        计划模式用它决定"MCP 工具直接放行"还是"问用户一次"——和 Claude Code 一致：
        声明了只读就不打扰，没声明就请求权限。

        为什么包一层 try、取不到就当"不是只读"：
          MCP 工具是后台异步注册的，对话第一轮时工具表可能还没填满；
          测试里的裸对象也可能没有 _tool_manager。这些情况下返回 False 是安全方向——
          多问一次确认，而不是把一个会提交工单的外部工具静默放出去。
        """
        manager = getattr(self, "_tool_manager", None)
        if manager is None:
            return False
        try:
            return manager.is_readonly_tool(tool_name)
        except Exception:
            return False

    async def _handle_tool_calls(self, tool_calls):
        """处理模型这一轮申请的所有工具（第 15 轮：连续的只读调用并发执行）

        【改前的情况】
          一个 for 循环里逐个 await：模型想读 5 个文件，就是 5 次
          "发出去 → 等结果 → 再发下一个"。其中绝大部分时间在等磁盘，纯白等。

        【改后的划分】
          连续的只读调用攒成一批并发跑；其余（写文件、跑命令、需要审批的、高危的）
          保持串行——它们之间有先后依赖，并行会把语义搞乱。

        【为什么只并"连续"的一段，而不是把整轮所有只读的都挑出来】
          模型给出的工具顺序，就是它自己排的执行意图：
            file_read(a.py) → file_edit(a.py) → file_read(b.py)
          把第三个提前到第一个旁边并发，读到的就是改动前的内容。
          只并"相邻的一串只读"，语义上最安全，而收益已经拿到了——
          真实使用里最高频的就是"一次读好几个文件"。

        参数 tool_calls：模型这一轮申请的工具（SDK 对象，带 id / function）。
        """
        total = len(tool_calls)

        # 先把参数统一解析一遍，供"收集批次"和"单条执行"两条路复用。
        # 为什么不在各自的分支里临时解析：批次收集需要**提前**看后面几条能不能并，
        # 到那时候再解析就是同一份 JSON 解两次。
        parsed_args = []
        for call in tool_calls:
            try:
                parsed_args.append(json.loads(call.function.arguments))
            except json.JSONDecodeError as e:
                parsed_args.append(e)  # 解析失败的异常先存着，轮到它时再报错

        index = 0
        while index < total:
            tool_call = tool_calls[index]
            parsed = parsed_args[index]
            is_last_tool = (index == total - 1)  # 整轮最后一条要挂 todo 提醒

            if isinstance(parsed, json.JSONDecodeError):
                # 模型把参数写成了非法 JSON。这里不抛异常，而是造一条"失败结果"塞回历史，
                # 让它下一轮自己改对（和工具执行失败的处理一致）。
                self._ui_manager.print_error(f"Tool parameter parsing failed: {parsed}")
                self.add_message({
                    "role": "tool",
                    "tool_call_id": tool_call.id,  # 必须对上号，否则接口会拒
                    "name": tool_call.function.name,
                    "content": [
                        {"type": "text", "text": "工具调用失败：参数不是合法的 JSON，请检查 tool 的 arguments 格式后重试。"}
                    ],
                })
                index += 1
                continue

            args = parsed
            # 【第 33 轮】把当前模式传进去：判定结果从"问/不问"变成三态
            # 【第 33.5 轮】再多传一个"是否自声明只读"——MCP 工具靠它决定
            #   计划模式下直接放行还是问用户一次（Claude Code 的做法）。
            decision = classify(
                tool_call.function.name, args, self.mode,
                self._readonly_hint(tool_call.function.name),
            )

            # 先看看能不能攒一批并发（至少两条才值得并，一条并了反而多一层开销）
            batch, next_index = self._collect_parallel_batch(tool_calls, parsed_args, index)
            if len(batch) >= 2:
                # 这批的最后一条是不是整轮的最后一个？是就把 todo 提醒挂上去
                await self._execute_tools_parallel(batch, reminder_on_last=(next_index == total))
                index = next_index
                continue

            # ---- 单条路径：和以前完全一样（含审批、拒绝、高危提示）----
            # 【第 32 轮】PreToolUse 放在权限确认之前：被 hook 拦掉的调用不该先让用户点一次同意。
            #   批量路径在 _execute_tools_parallel 的 run_one 里跑（那边每条都要过，
            #   因为收集批次时只判断了"能不能并发"，没跑过 hook）。
            hook_reason = await self._pre_tool_hooks(tool_call.function.name, args)
            if hook_reason:
                # 先画"它想干什么"再报拦截原因：用户需要看到被拦的是哪条命令
                self._ui_manager.show_preparing_tool(tool_call.function.name, args)
                self._ui_manager.print_warning(f"被 hook 拦下：{hook_reason}")
                self._add_tool_response(
                    tool_call,
                    "这次调用被 PreToolUse hook 拒绝，工具没有执行。\n"
                    f"原因：{hook_reason}\n"
                    "如果这是误拦截，请换个做法；确实无法完成就直接向用户说明。",
                    is_last_tool,
                )
                index += 1
                continue

            # 【第 33 轮】deny：计划模式下的写操作。
            #   为什么放在 hook 之后：hook 是用户自己配的规则（可能要记审计日志），
            #   被 hook 拦下的调用同样不该执行；先让 hook 看一眼，再按模式拒绝。
            #   为什么放在 show_preparing_tool 之前：被拒绝的调用没有"执行"这一步，
            #   屏幕上画一个工具头再跟上"没执行"，看起来像是执行失败，
            #   而它其实是"压根没允许"。一行拦截提示更准确。
            if decision.is_denied:
                # 【第 33.5 轮】提示里补上"被拦的具体是什么"。
                #   第 33 轮只打了工具名（`已拦截（计划模式）：cmd_runner`），
                #   用户看不出拦的是哪条命令——尤其是 cmd_runner 这种一个工具
                #   对应无数命令的情况，这条提示等于没说。
                #   format_tool_call 会把参数压成一行摘要并截断，和批准弹窗
                #   用的是同一个函数，屏幕上不会出现两种写法。
                shown = self._ui_manager.format_tool_call(tool_call.function.name, args)
                self._ui_manager.print_warning(f"已拦截（计划模式）：{shown}")
                # 完整理由写回历史，模型下一轮据此改道。理由里已经写明"改用只读工具"
                # 和"让用户切模式"两条出路，所以这里不需要再补话。
                self._add_tool_response(
                    tool_call,
                    f"这次调用被拦截，工具没有执行。\n{decision.reason}",
                    is_last_tool,
                )
                index += 1
                continue

            # 【2026-11-06 第 8.2 轮】先把"要干什么"画出来，再问"要不要干"。
            #   改前是反过来的：先弹一段 "Tool: xxx, args: {...}"，用户点完同意，
            #   下面才出现 ⏺ cmd_runner(dir) 那行。结果是同一个命令在屏幕上出现两次，
            #   第一次还是一坨没排版的字典。现在头部只画一次，顺序也符合人的阅读习惯。
            self._ui_manager.show_preparing_tool(tool_call.function.name, args)

            if decision.is_high_risk and decision.need_approval:  # 【第 9 轮】高危操作先亮红牌再问
                self._ui_manager.print_warning(f"高危操作（{decision.reason}）：回车将默认拒绝，确认执行请输入 y。")
            elif decision.is_high_risk:
                # 【第 33 轮】自动模式：高危但不停下来问。不阻断，只留一行痕迹——
                #   用户要的是"全放开"，但事后完全不知道跑过一条 `del /s /q`
                #   属于装瞎。这行提示不影响任何行为，删掉也只是少一行输出。
                self._ui_manager.print_warning(f"自动模式执行高危操作（{decision.reason}）")

            should_execute = True  # 默认执行；下面要问用户时再按回答改写
            content = ""  # 用户拒绝时说的话

            if decision.need_approval:
                # 【第 8.2 轮】问题只问"是否执行"，参数已经在上面那行里了。
                # 【第 9 轮】高危操作传 default_yes=False：不填就打回，必须显式确认。
                should_execute, content = await self._ui_manager.wait_for_user_approval(
                    default_yes=not decision.is_high_risk
                )

            if should_execute:
                # show_header=False：头部上面已经画过了，别再画一遍
                await self._execute_tool(tool_call, args, is_last_tool, show_header=False)
            else:  # 用户拒绝了
                self._ui_manager.show_tool_denied(content)
                self._add_tool_response(tool_call, f"用户拒绝执行这个工具。用户的说明：{content}", is_last_tool)
                # 把"用户拒绝了，理由是..."当作工具结果告诉模型，让它换个做法

            index += 1

    @staticmethod
    def _can_run_in_parallel(tool_name: str, decision) -> bool:
        """这次调用能不能并进批次（2026-11-08 第 15 轮新增）

        三个条件缺一不可：
          1. 在只读工具清单里——只有只读才没有先后依赖；
          2. 判定结果必须是 allow——要停下来问人（ask），并行就没意义（人只有一个）；
             被模式拒绝（deny）的也不并发，它根本不会执行，凑进批次只会把
             "拦截提示"和真正执行的结果混在一起。
          3. 不是高危——高危必须单独走，合批会把"回车默认拒绝"的提示挤在一起，
             用户根本分不清那句话对应哪条命令。

        只认 READ_ONLY_TOOLS 这个集合，不自己判断"这个工具看起来是只读的"：
          判据只有一份（core/permissions.py），两处各判一次迟早不一致。

        【第 33 轮】把 `not need_approval` 换成 `action == allow`：
          三态之后 "不需要审批" 不等于 "可以执行"——deny 也不审批。
          用 action 判断才没有歧义。
        """
        return (
            tool_name in READ_ONLY_TOOLS
            and decision.action == ACTION_ALLOW
            and not decision.is_high_risk
        )

    def _collect_parallel_batch(self, tool_calls, parsed_args, start):
        """从 start 开始往后收集"连续可并发"的调用（第 15 轮新增）

        返回 (batch, next_index)：
          batch      —— [(tool_call, args), ...]。**可能是空列表**：
                        说明 start 这一条自己就不能并发（写操作、要审批、高危），
                        调用方据此走单条路径，把审批流程走完整。
          next_index —— 下一条要处理的序号（遇到不可并发的就停在那里）

        三种情况会停下：
          · 参数不是合法 JSON——它需要单独回一条"参数错误"，不能并进批次；
          · 工具不是只读的、或需要审批——见 _can_run_in_parallel；
          · 已经到这一轮末尾。

        复杂度说明：最坏情况每条只调用一次 classify，整轮 O(n)；
        因为收集过的下标不会再被收集（index 直接跳到 next_index），不会重复判。
        """
        batch = []
        index = start
        total = len(tool_calls)

        while index < total:
            parsed = parsed_args[index]
            if isinstance(parsed, json.JSONDecodeError):  # 留给单条路径单独报错
                break
            name = tool_calls[index].function.name
            # 【第 33 轮】带上当前模式：plan 模式下只读工具仍是 allow，
            #   判定结果和 default 一样，所以这一处不需要额外分支。
            # 【第 33.5 轮】自声明只读的 MCP 工具会走 allow，同样可以并发。
            if not self._can_run_in_parallel(
                name, classify(name, parsed, self.mode, self._readonly_hint(name))
            ):
                break
            batch.append((tool_calls[index], parsed))
            index += 1

        return batch, index

    async def _execute_tools_parallel(self, batch, reminder_on_last=False):
        """并发执行一批只读工具，结果按申请顺序写回（2026-11-08 第 15 轮新增）

        两个必须守住的东西：

          1. **界面先画完所有头，再统一画结果。**
             边跑边画的话，屏幕上会变成"第二个头 → 第一个结果"的交叉顺序，
             用户根本对不上哪条结果属于哪次调用。

          2. **历史按申请顺序写回，不按完成顺序。**
             并发完成顺序是不确定的；而历史顺序会被落盘、被下一轮请求读到。
             真按完成顺序写，同一个会话每次重放的消息顺序都不一样，
             排查问题时没法对照，缓存前缀也会随机失效。
             （tool 消息之间本来靠 tool_call_id 对应，顺序不影响接口，
               但"稳定"比"合法"更值钱。）

        参数 batch：[(tool_call, args), ...]，调用方保证长度 ≥ 2 且都可并发。
        参数 reminder_on_last：这批的最后一条是不是整轮最后一条，是就挂 todo 提醒。
        """
        # 1) 先把这一批的"⏺ 工具名(参数)"全部画出来
        for call, args in batch:
            self._ui_manager.show_preparing_tool(
                call.function.name,
                {k: v for k, v in args.items() if k != "need_user_approve"},
            )

        # 2) 并发执行
        #    为什么不用 gather(return_exceptions=True)：
        #      那样拿回来的是"结果或异常对象"混在一起，类型要再分一次。
        #      这里让 run_one 自己兜住异常，语义更清楚：返回值正常 = 成功，是 Exception = 失败。
        #    为什么要兜异常：第 14.2 轮起 run_tool 会把异常往上抛（不再吞成字符串），
        #      如果不管，一条失败会把整批 gather 打断——"一个文件读不到，其他四个也不读了"，
        #      这比串行还糟。
        async def run_one(call, args):
            tool_args = {k: v for k, v in args.items() if k != "need_user_approve"}
            try:
                # 【第 32 轮】批里每一条都要过 PreToolUse——收集批次时只判断了
                #   "能不能并发"，并没有跑过 hook（并发判断是同步的，hook 是异步的）。
                #   用异常表示拒绝：gather 里只中断这一条，同批其他条照常完成。
                reason = await self._pre_tool_hooks(call.function.name, tool_args)
                if reason:
                    raise HookBlocked(reason)

                result = await self._tool_manager.run_tool(call.function.name, **tool_args)

                # PostToolUse 只在成功之后跑；追加文本和结果一起写回历史
                extra = await self._post_tool_hooks(call.function.name, tool_args, result)
                if extra and isinstance(result, str):
                    return result + extra
                return result
            except Exception as e:
                return e

        results = await asyncio.gather(*(run_one(call, args) for call, args in batch))

        # 3) 按申请顺序写回界面和历史
        for offset, ((call, args), result) in enumerate(zip(batch, results)):
            tool_args = {k: v for k, v in args.items() if k != "need_user_approve"}
            is_last = reminder_on_last and offset == len(batch) - 1
            if isinstance(result, Exception):
                self._finish_tool_call(call, tool_args, result, is_last, error=True)
            else:
                self._finish_tool_call(call, tool_args, result, is_last)

    def _finish_tool_call(self, tool_call, tool_args, result, is_last_tool, error=False):
        """把一次工具执行的结果同时写进界面和历史（2026-11-08 第 15 轮抽出）

        为什么抽出来：串行路径（_execute_tool）和并发路径（_execute_tools_parallel）
        都要做"显示结果 + 写进历史"这两步。复制两份的话，
        以后改失败提示的措辞（比如补一句"建议先 file_read 再 file_edit"）必然只改一处。

        参数 error=True 表示 result 其实是异常对象：
          界面标红；写进历史时加上"工具调用失败，原因："前缀，
          让模型能区分"工具返回的内容"和"工具没跑成"——这是 agent 自我纠错的入口。
        """
        text = str(result)
        self._ui_manager.show_tool_execution(
            tool_call.function.name,
            tool_args,
            success=not error,
            result=text,
        )
        self._add_tool_response(
            tool_call,
            f"工具调用失败，原因：{text}" if error else text,
            is_last_tool,
        )

    def _is_tool_allowed(self, tool_name: str) -> bool:
        """这个工具对「当前正在干活的 agent」是否可用（第 18 轮新增）

        三层白名单里的第二层（第一层是清单里不给，第三层是说明书里只说有哪几个）。
        为什么清单层不够：模型是有记忆的，它可能凭上一轮的工具清单、或者凭训练里的
        习惯直接写出一个工具名。清单层只是"看不到"，不是"用不了"。
        这里在执行前再拦一次，把拒绝理由写进工具结果——模型下一轮就能改道。

        三种情况：
          · 主 agent（current_agent 为 None）→ 全部允许；
          · 子 agent 且 tools 为 None（继承全部）→ 允许，但 task 永远不许；
          · 子 agent 且 tools 有值 → 只允许名单里的，task 永远不许。

        【第 33 轮】曾在这里加过一道"当前模式"的闸（plan 下白名单外的工具直接不可用）。
        【第 33.5 轮撤掉】模式约束现在只在权限层实现（classify 的 plan 分支）。
          为什么能撤：清单层也不剪了，工具全部下发；模式要不要执行由 classify 判，
          那里能给出"为什么不行、方案该怎么写"的具体理由，比这里一句
          "不可用"更有用。两层各判一次，反而容易出现"清单给了、这里拦了、
          模型不知道听谁的"这种自相矛盾。
          子 agent 的白名单闸保留在下面——那是"子 agent 永远比父更窄"的约束，
          和模式无关。
        """
        agent = self.current_agent
        if agent is None:
            return True  # 主 agent：没有任何限制
        if tool_name == "task":
            # 子 agent 不能再派活。这是三道闸里最里的一道，和 task.py 里的第二道重复，
            # 但重复是故意的：task.py 那层只拦"从 task 工具进来的调用"，
            # 这一层拦的是"任何路径发起的调用"。
            return False
        if tool_name == "skill":
            # 【第 21 轮】子 agent 也不能加载技能。清单层已经不给它看了（_get_available_tools），
            # 但模型可能凭上一轮的工具清单、或者凭训练里的习惯直接写出工具名，
            # 所以执行层再拦一次。拒绝理由要写清"自己去干"，否则模型会绕着试。
            return False
        if agent.tools is None:
            return True  # 继承全部（除 task）
        return tool_name in agent.tools

    async def _execute_tool(self, tool_call, args, is_last_tool=False, show_header=True):
        """Execute a tool call and handle the response.（执行一个工具，成功失败都要有回应）

        参数 show_header（2026-11-06 第 8.2 轮新增）：
          False 表示"⏺ 工具名(参数) 这一行已经画过了"。
          需要审批的调用会先画头再提问，所以进来时开关是关的；
          只读工具直接进这里，开关还是开的，自己画。

        【2026-11-08 第 15 轮】成功/失败的收尾统一交给 _finish_tool_call，
          这里只保留"执行"这一段——两条路径（串行/并发）共用同一套收尾。
        """
        tool_args = {k: v for k, v in args.items() if k != 'need_user_approve'}
        # 上面这行是"过滤字典"：把 need_user_approve 这个标记去掉，
        # 因为它是给我们看的，不是给工具本身用的参数。用的是字典推导式。

        # 【第 18 轮】执行层硬闸：类型白名单外的工具，就算模型硬调也执行不了。
        #   为什么拒绝理由要带上"可用工具清单"：
        #     只说"不可用"的话，模型下一轮十有八九再猜一个名字撞一次；
        #     把清单给它，同一轮就能改道（这个教训在 task 的未知类型提示上已经吃过一次）。
        if not self._is_tool_allowed(tool_call.function.name):
            agent = self.current_agent
            available = agent.tools_text() if agent is not None else "（未知）"
            agent_name = agent.name if agent is not None else "未知类型"
            reason = (
                f"工具 '{tool_call.function.name}' 对当前子 agent（{agent_name}）不可用。\n"
                f"当前可用的工具：{available}。\n"
                "请改用这些工具完成任务；确实做不到就直接在汇报里说明，不要绕道。"
            )
            self._finish_tool_call(tool_call, reason, is_last_tool, error=True)
            return

        if show_header:  # 没画过才画，避免同一个调用出现两行头
            self._ui_manager.show_preparing_tool(tool_call.function.name, tool_args)  # 显示"准备调用 xx 工具"

        try:  # 试：执行工具
            tool_response = await self._tool_manager.run_tool(tool_call.function.name, **tool_args)
            # 让工具管理器去执行。**tool_args 是把字典拆成一个个参数传进去
        except Exception as e:  # 如果工具执行时抛错（第 14.2 轮起会真的抛上来）
            # 关键设计：工具失败不是"程序崩了"，而是"给模型一条失败消息"，
            # 让模型自己看到错误、自己想办法补救。这是 agent 能自我纠错的核心。
            self._finish_tool_call(tool_call, tool_args, e, is_last_tool, error=True)
            return

        # 【第 32 轮】PostToolUse：成功之后、写回之前跑。
        #   放在这里而不是更上层，是因为只有这一处知道"工具真的成功返回了"——
        #   失败路径已经 return 了，不会误触发格式化/记日志这类 hook。
        extra = await self._post_tool_hooks(tool_call.function.name, tool_args, tool_response)
        if extra and isinstance(tool_response, str):
            tool_response = tool_response + extra

        self._finish_tool_call(tool_call, tool_args, tool_response, is_last_tool)

    def _add_tool_response(self, tool_call, content, is_last_tool=False):
        """Add tool response to message history through history manager.（把工具结果存进历史）

        【2026-11-06 第 6 轮】在写进历史前先截断。原因是：
          界面上的截断只影响"你看到什么"，模型收到的还是全文。
          一条 `dir /s` 或 `npm install` 的输出动辄几万字，直接吃满上下文。
        """
        tool_content = [{"type": "text", "text": self._truncate_tool_output(content)}]  # 包成标准的"内容块"（已截断）

        # Add reminder after the last tool call in each iteration
        if is_last_tool:  # 只有最后一个工具结果后面才加提醒
            # 【第 33 轮】把当前模式传给 reminder：模式说明每轮附在最后，
            #   模型对最近内容的注意力最强，适合放"这一轮按什么规矩来"。
            reminder_content = get_reminder(self.mode)
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
#    真正的权限规则（命令白名单、危险命令黑名单、
#    以及"不再询问"的记忆）放在第 9 轮做。
#    【第 15.2 轮注】原来这里还列了"工作区路径边界"，它当时做在
#    file_tools._resolve_path 里，后来整条删了（只拦 file_*、拦不住 cmd_runner，
#    等于逼模型绕路）。真要重做，位置应该在 core/permissions.py 的 classify。
#
# 5. 【第 8 轮已完成】主循环原来是递归，已改成 while + 单层迭代
#    （_recursive_message_handling → _run_one_iteration），栈深度不再随轮数增长。
# =============================================================================
