# =============================================================================
# 文件：src/core/history/history_manager.py
# 作用：管"聊天记录本"。三件事：
#   1. 存消息（add_message）
#   2. 记账（记下每次用了多少 token）
#   3. 上下文快满的时候，想办法删掉一些旧消息（压缩），不然模型会"忘了"或者直接报错
#
# 为什么需要"压缩"？
#   大模型一次能读的字数是有限的（叫"上下文窗口"）。你把整本聊天记录都塞过去，
#   超过上限接口就会报错。所以快到上限时必须主动删掉一些旧内容。
#
# 这里的"一页纸"概念很重要：
#   messages_history 是一个"列表的列表"，外面那层代表一本书，里面每一页是一次对话。
#   主对话是第 0 页，子 agent 干活时会新开一页，干完就把这一页撕掉。
# =============================================================================

from abc import ABC, abstractmethod  # 导入"抽象基类"工具：用来定义"必须实现哪些方法的模板"
import copy  # copy 库：用来做"深拷贝"，复制一份互不影响的数据
from dataclasses import dataclass  # dataclass 装饰器：让"只装数据的类"写起来更省事
import os  # os 库：读环境变量
from typing import Any  # 【第 9 轮】类型标注：原来自定义函数写成 -> any（内置函数 any），是错的
from dotenv import load_dotenv  # 导入用于读取 .env 文件的函数
from ui.ui_manager import UIManager  # 导入界面管理器：压缩时要给用户提示一句
from enum import Enum  # Enum 用来做"枚举"：一组固定的选项，防止写错
from core.limits import MICROCOMPACT_TOOL_OUTPUT_CHARS  # 【2026-11-09 第 3 批】L3 二次瘦身目标字符数

# Load environment variables
load_dotenv()  # 把 .env 里的配置读进环境变量


@dataclass
class TokenUsage:
    # @dataclass 会自动帮你生成 __init__ 等方法，所以下面只要写字段名就行
    # 这个类只干一件事：把"用了多少 token"打包成一个对象
    input_tokens: int  # 输入用了多少（你发给模型的字）
    output_tokens: int  # 输出用了多少（模型回给你的字）
    total_tokens: int  # 一共用了多少


class Role(str, Enum):
    # 定义"角色"枚举：一条消息只可能是这四种身份之一
    # 括号里的 str 表示"这个枚举的值本质上是字符串"，所以它可以和普通字符串比较
    SYSTEM = "system"  # 系统：给模型看的规则设定
    USER = "user"  # 用户：你输入的内容
    TOOL = "tool"  # 工具：工具执行完返回的结果
    ASSISTANT = "assistant"  # 助手：模型说的话


class Crop_Direction(str, Enum):
    # 定义"裁剪方向"枚举：删消息时是从头删还是从尾删
    TOP = "top"  # 从最前面删（删最老的）
    BOTTOM = "bottom"  # 从最后面删（删最新的）


def message_text(message) -> str:
    """把一条消息的 content 转换成纯文本（2026-11-06 第 2 轮新增，提到模块级方便复用）

    为什么需要：content 在消息里是"内容块列表"，长这样：
        [{"type": "text", "text": "任务做完了，结论是……"}]
    直接打印或返回出去，会带着 {'type': 'text', ...} 这些噪音。
    这个函数把里面的文字抽出来拼成干净的一段话。
    """
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    # 上面这行：字典就按字典取，对象就按属性取

    if isinstance(content, str):  # 情况一：本来就是一段文字
        return content
    if isinstance(content, list):  # 情况二：是内容块列表（最常见）
        texts = []  # 准备装每一块的文字
        for block in content:  # 逐块检查
            if isinstance(block, dict) and block.get("type") == "text":  # 只挑"文字"类型的块
                texts.append(block.get("text", ""))  # 把文字取出来
        return "\n".join(texts)  # 用换行拼成一段
    return "" if content is None else str(content)  # 其他情况：能转文字就转，什么都没有就返回空串


# -----------------------------------------------------------------------------
# 【2026-11-06 第 9 轮新增】裁剪的安全边界
#
# 为什么需要这一组函数？
#   OpenAI 格式要求 assistant(tool_calls) 和紧随其后的 tool 消息必须成对出现：
#     - 留下 assistant(tool_calls) 却删掉它的 tool 结果 → 400：
#       "An assistant message with 'tool_calls' must be followed by tool messages"
#     - 留下 tool 结果却删掉它的 assistant → 400：
#       "messages with role 'tool' must be a response to a preceding message with 'tool_calls'"
#   而原来的裁剪是"按下标硬切"：crop_message 直接 messages[crop_amount:]，
#   _compress_single_session 直接 user_index+1+3，都可能正好切在一对中间。
#   结果是：上下文一到阈值、一裁剪，下一次请求就 400，整个对话直接结束。
#
# 解法：切之前把刀口"挪"到安全位置。挪的方向永远是多保留一点，
# 宁可少删几条，也不能把对话切坏。
# -----------------------------------------------------------------------------

def _role(message) -> Any:
    """读 role，字典和对象两种形态都支持（模块级版本，给下面的工具函数用）"""
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _tool_calls(message) -> list:
    """取出这条消息带 tool_calls（没有就返回空列表）

    为什么要做类型兜底？
      历史里的 assistant 消息是字典，但测试里可能用对象；
      两种情况都要能读，否则一个 AttributeError 就把裁剪打断了。
    """
    value = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    return list(value) if value else []


# -----------------------------------------------------------------------------
# 【2026-11-07 第 13.1 轮新增】恢复会话时用的"用量估算"
#
# 为什么需要估算，而不是等接口返回真实值？
#   `--continue` 恢复时只把消息列表换掉，用量账本 history_token_usage 还是空的；
#   而 _requires_compression() 看的是账本最后一条，空账本 → 直接返回 False。
#   于是"恢复一个已经用到 80% 的会话"会原封不动把整份历史发出去，
#   大概率 400（context_length_exceeded）——而"崩溃后接着聊"正是第 13 轮的目标。
#   精确值只有发完请求才知道，可压缩判断必须发生在发请求之前，所以只能估：
#   它只需要回答"大概在不在阈值附近"，不需要精确到个位。
# -----------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """按字符粗估一段文字的 token 数（2026-11-07 第 13.1 轮）

    口径（经验值，宁可略高）：
      中文 / 中文标点 / 全角符号   1 字 ≈ 1 token
      ASCII、代码、命令输出        约 3.5 字符 ≈ 1 token，即 0.28 token/字符

    为什么分两段算，而不是统一乘一个系数：
      这个项目的上下文里既有中文注释，又有大段代码和命令输出。
      统一按 0.5 算的话，纯中文会低估一半（可能撞 400），
      纯代码会高估一倍（白白多压一次）。分开算只多一个循环，误差立刻收敛。

    为什么"宁可略高"：
      估高了最多多触发一次压缩，代价是一次摘要调用的钱；
      估低了会撞 context_length_exceeded，整轮对话直接结束。两者代价不对等。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        # CJK 统一表意文字 + 中文标点 + 全角符号。中文文本里这三段几乎覆盖全部，
        # 用三个区间判断比引入 unicodedata 查表快得多，也不需要额外依赖。
        if 0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            cjk += 1
    ascii_like = len(text) - cjk
    return int(cjk + ascii_like * 0.28) + 1  # +1 防止极短文本被估成 0


def _tool_call_arguments(call) -> str:
    """取出一次工具申请的参数字符串（字典和对象两种形态都支持）

    为什么单独抽出来：历史里 assistant 的 tool_calls 从 JSONL 读回来是字典，
    而运行时（直接来自 SDK）是对象。两种都得能读，否则估算会漏掉最大的一块。
    """
    function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
    if function is None:
        return ""
    arguments = function.get("arguments") if isinstance(function, dict) else getattr(function, "arguments", "")
    return arguments or ""


def estimate_messages_tokens(messages: list) -> int:
    """估一整段消息列表的 token 数（2026-11-07 第 13.1 轮）

    除了正文，还要算两样容易被漏掉的东西：

      1. tool_calls 的参数。一次 file_edit 的 new_string 可能有几千字符，
         它不在 content 里。只算 content 会严重低估——而"改文件的会话"
         恰恰是最容易撑爆上下文的那类。
      2. 每条消息的固定开销。role、分隔符、结构包装都要占 token，
         按 4 token/条算（OpenAI 文档给的数量级），几十条消息加起来也有几百。

    空消息列表返回 0，调用方据此跳过记账。
    """
    total = 0
    for message in messages:
        total += 4  # 每条消息的格式开销
        total += estimate_tokens(message_text(message))  # 正文（含内容块列表）
        for call in _tool_calls(message):  # 工具申请的参数
            total += estimate_tokens(_tool_call_arguments(call))
    return total


def _owner_index(messages: list, index: int) -> int:
    """找出 index 这条 tool 消息"属于"哪个 assistant（往前找最近一条带 tool_calls 的）

    返回 -1 表示找不到主人——说明这条消息本身就是残缺的（测试用的假数据常见），
    这种情况下我们不去猜，交给调用方决定（一律不改刀口）。
    """
    for i in range(index - 1, -1, -1):
        if _role(messages[i]) == Role.ASSISTANT and _tool_calls(messages[i]):
            return i
    return -1


def align_top_cut(messages: list, index: int) -> int:
    """把"从 index 往后保留"的刀口往前挪到安全位置（第 9 轮新增）

    不安全的样子：messages[index] 是 tool，但它的 assistant 已经被切掉了。
    这时把刀口挪到那条 assistant 上，保住整对。
    """
    index = max(0, min(index, len(messages)))
    while 0 < index < len(messages) and _role(messages[index]) == Role.TOOL:
        owner = _owner_index(messages, index)
        if owner < 0 or owner >= index:  # 没主人（残缺数据）→ 不去猜
            break
        index = owner  # 刀口退到主人那一行
    return index


def align_bottom_cut(messages: list, index: int) -> int:
    """把"保留 [0:index]"的刀口往前挪到安全位置（第 9 轮新增）

    不安全的样子：messages[index-1] 是带 tool_calls 的 assistant，
    但它的 tool 结果在 index 之后被切掉了。这时连这条 assistant 一起切掉。
    """
    index = max(0, min(index, len(messages)))
    while 0 < index <= len(messages) and _tool_calls(messages[index - 1]):
        index -= 1
    return index


def make_note_message(text: str) -> dict:
    """把一段摘要包成一条"用户消息"（第 9 轮新增，给 crop_message 用）

    为什么用 user 身份？
      和自动压缩时的公告一致（见 _create_compression_notice）：
      模型对"用户说的"接受度最高，而且不需要额外解释这是什么结构。
    """
    return {
        "role": Role.USER,
        "content": [{"type": "text", "text": text}],
    }


class BaseHistoryManager(ABC):
    # 定义一个"抽象基类"，相当于"合同模板"：
    # 谁继承我，就必须把下面带 @abstractmethod 的方法全部实现，少一个都不能创建对象
    def __init__(self):
        self.messages_history = [[]]  # 一本书，一开始只有一页空纸
        self.history_token_usage = []  # 每页对应的 token 用量记录
        # 【2026-11-06 第 9 轮新增】"当前这份用量是否已经压缩过"。
        #   False = 刚拿到新用量，还没压；True = 这份用量已经处理过，别再压第二遍。
        #   配合 auto_messages_compression() 使用，防止压缩连环触发。
        self._compressed_for_current_usage = False

    @abstractmethod
    def add_message(self, message) -> None:
        # 抽象方法：只有声明，没有实现（pass 就是什么都不做）
        # 具体怎么存，交给子类决定
        pass

    @abstractmethod
    def update_token_usage(self, token_usage) -> None:
        pass

    @abstractmethod
    def get_current_messages(self) -> Any:
        pass

    async def auto_messages_compression(self) -> None:
        # 这是个"已经写好"的普通方法，子类可以直接用
        # 逻辑很简单：先问"要不要压缩"，要就压
        # 【2026-11-06 第 2 轮优化】从同步改成异步（async）：
        #   因为现在压缩时会调用大模型生成摘要，而调模型是异步操作。
        #   调用方也要相应加上 await（conversation.py 的两处已同步修改）。
        #
        # 【2026-11-06 第 9 轮修复】压缩后会挂一个"已处理"标记（见下面几行）。
        #   为什么必须挂？
        #     压缩不会改变 history_token_usage[-1] 里那个"上一轮请求用了多少"，
        #     所以 _requires_compression() 压完还是 True。
        #     而 Conversation._run_one_iteration 在一轮里会调这个方法两次
        #     （请求前一次、收到回复后又一次），下一次迭代还要再调。
        #     结果就是：一旦过线，就会连续压好几次，每次都真调一次摘要模型——
        #     钱花了、消息也被多删了。
        #   现在压完就标记"这份用量已经处理过"，直到下一次真正的请求
        #     更新用量（update_token_usage 会把标记清掉）才允许再压。
        #
        # 【2026-11-09 五层压缩】压缩从「一跳到底」改成「四档区间映射、落点直做」：
        #   原来 _requires_compression() 一旦判真，就直接 _compress_current_message()
        #   （调摘要模型，最贵的手段）。现在按"压力"分层，四档阈值真正参与判断：
        #     占用 < 0.80            → 不压缩
        #     占用 [0.80, 0.85)      → 只做 L2（Snip 剪中间）
        #     占用 [0.85, 0.90)      → 只做 L3（Microcompact 二次瘦身）
        #     占用 [0.90, 0.95)      → 只做 L4（Context Collapse 只读投影）
        #     占用 >= 0.95           → 只做 L5（调模型全量摘要，兜底）
        #   关键：「落点直做」——落在哪档就只做那一档，不从低层往上爬、也不叠加。
        #   这是"区间映射"的字面语义：压力多大，就上对应重量的那一种手段。
        #   本批次只实现 L2 和 L5；L3/L4 留待后续批次，届时在下面的预留位接入。
        if self._compressed_for_current_usage:  # 这份用量已经压过了
            return
        if not self._requires_compression():  # 低于 0.8 总闸，不用压
            return

        # 根据当前占用比例，直接算出「这一轮该做哪一层」（落点直做，不爬层）。
        # tier 语义：1=L2, 2=L3, 3=L4, 4=L5。四档阈值在此真正生效。
        ratio = self._current_usage_ratio()
        if ratio < self._snip_threshold:
            tier = 1
        elif ratio < self._micro_threshold:
            tier = 2
        elif ratio < self._collapse_threshold:
            tier = 3
        else:
            tier = 4

        # 按落点执行对应层。每一层都是「只做自己这一件事」，做完本轮即止。
        if tier == 1:
            # ② L2：零成本剪中间（保头 + 尾47，剪中间换占位，不调模型）。
            # 循环剪到「够用」或「没得剪」：L2 内部自己降到位，因为这一档只有它可用。
            for _ in range(len(self.messages_history[-1])):
                if not self._requires_compression():  # 每剪一次就重新判断，够用了就停
                    self._compressed_for_current_usage = True
                    return
                if not self._snip_oldest_round():  # 没得剪了，退出本层
                    break
        # ③ L3（tier == 2，占用 0.85~0.90）：旧工具结果二次瘦身（不调模型）。
        #   对「较早」的大工具输出（超过 8000 字符）掐头留尾压到 8000，最近 3 条保持完整。
        #   这是「不删消息、只缩短单条体积」的手段，和 L2 的「删消息条数」互补。
        elif tier == 2:
            self._microcompact_tool_outputs()
        # ④ L4（tier == 3，占用 0.90~0.95）：这里「故意留空」，不做任何事。
        #   为什么留空？因为 L4 是「只读投影」，它不改 messages_history 存储，
        #   而是「发请求时换个压缩视图」。这个动作不在压缩链里做，而是由
        #   conversation.py 的 _get_messages_with_cache_mark 在发请求前通过
        #   should_use_projection() 判断后调用 get_projection() 完成。
        #   所以 tier == 3 这一档「落点」的活，其实已经被发请求路径接走了，
        #   压缩链里自然无事可做——留空即是正确行为，不是没实现。
        #
        elif tier == 4:
            # ⑤ L5：压力已到 0.95，才调模型做全量摘要（最贵，兜底）。
            await self._compress_current_message()

        self._compressed_for_current_usage = True  # 本轮用量已处理，不再重复压

    @abstractmethod
    def _requires_compression(self) -> bool:
        pass

    @abstractmethod
    async def _compress_current_message(self, announce: bool = True) -> None:
        # 【2026-11-06 第 2 轮优化】抽象方法也改成异步，子类实现必须用它
        # 【2026-11-08 第 14.5 轮】多了 announce 参数：/compact 手动压缩时，
        #   由命令层自己打提示语，这里不要再说"上下文太长了"——用户明明是主动压的。
        pass

    @abstractmethod
    def start_new_chat(self) -> None:
        pass

    @abstractmethod
    def finish_chat_get_response(self) -> str:
        pass


class HistoryManager(BaseHistoryManager):
    # 真正的实现类。下面的方法就是对上面那份"合同"的逐条兑现
    _instance = None  # 单例：唯一实例
    _initialized = False  # 是否已初始化

    def __new__(cls, model_max_tokens: int = 200, compress_threshold: float = 0.8):
        # 参数里的 = 200 和 = 0.8 是默认值，不传就用它们
        if cls._instance is None:  # 没造过就造
            cls._instance = super().__new__(cls)
        return cls._instance  # 造过就返回旧的

    def __init__(self, model_max_tokens: int = 200, compress_threshold: float = 0.8):
        if not self._initialized:  # 只初始化一次
            super().__init__()  # 先调用父类的初始化（建那本书和用量记录）
            self._ui_manager = UIManager()  # 拿一个界面管理器，压缩时要提示用户
            self._model_max_tokens = int(os.getenv("MODEL_MAX_TOKENS", model_max_tokens)) * 1024
            # 从 .env 读 MODEL_MAX_TOKENS，单位是"k"（千），所以要乘 1024 换成实际数字
            # 比如配置里写 200，算出来就是 204800，表示模型一次最多能读约 20 万字
            self._compress_threshold = float(os.getenv("COMPRESS_THRESHOLD", compress_threshold))
            # 压缩阈值：用到 80%（0.8）就开始压缩，留点余量免得撞上限
            self._summarizer = None
            # 【2026-11-06 第 2 轮新增】"摘要生成器"，是一个异步函数：传入一批消息，返回一段摘要文字。
            #   为什么要留成"外部注入"而不是在这里直接调模型？
            #     1. 历史管理器不该知道 API 密钥、模型名这些事（分层清晰）
            #     2. 单元测试里不注入它，压缩就自动退回"直接删除"的老做法，不会真的去花钱调模型
            self._repeatable_checker = None
            # 【2026-11-09 第 3 批新增】"工具是否可重复查询"的判断函数：传入工具名，返回布尔。
            #   为什么要注入而不是在 history_manager 里维护一份工具名白名单？
            #     1. "哪些工具可重复查询"是工具的固有属性（已经声明在各工具类的 is_repeatable 上），
            #        不该再在历史管理器里抄一份硬编码清单，否则加新工具时两处要同步改，容易漏。
            #     2. 历史管理器也不该直接 import ToolManager（会引入循环依赖：
            #        smart_context_cropper 依赖 HistoryManager，而 ToolManager 又登记了它）。
            #     3. 不注入时（比如单元测试）返回 None，压缩层会"保守处理"——宁可多保留、
            #        也不误折叠有副作用工具的结果。这和 set_summarizer 的注入思路一致。
            # 【2026-11-09 五层压缩】四档分层阈值：把 [0,1] 切成五段，决定「压力有多大、用哪层手段」。
            #   _compress_threshold(0.8) 是"总闸"，超过才进入压缩链；
            #   0.85/0.9/0.95 三档是"分层触发线"，对应 L2 Snip / L3 Microcompact / L4 Collapse。
            #   执行时从低到高递进：先试便宜层，每层做完重新估 token，降回 0.8 以下就停。
            self._snip_threshold = float(os.getenv("SNIP_THRESHOLD", 0.85))
            self._micro_threshold = float(os.getenv("MICRO_THRESHOLD", 0.90))
            self._collapse_threshold = float(os.getenv("COLLAPSE_THRESHOLD", 0.95))
            self._initialized = True  # 打标记

    def set_summarizer(self, summarizer):
        """注入摘要生成器（2026-11-06 第 2 轮新增）

        summarizer 的签名要求：async def fn(messages: list) -> str
        由 Conversation 在启动时注入；不注入则压缩时直接丢弃旧消息。
        """
        self._summarizer = summarizer

    def set_repeatable_checker(self, checker):
        """注入「工具是否可重复查询」的判断函数（2026-11-09 第 3 批新增）

        checker 的签名要求：def fn(tool_name: str) -> bool
        由 Conversation 在启动时注入（从 ToolManager 拿到"工具名→is_repeatable"的查询）。
        不注入时 _repeatable_checker 为 None，L3 压缩会"保守处理"（见 _microcompact_tool_outputs）：
        宁可多保留有副作用工具的结果，也不误折叠。
        """
        self._repeatable_checker = checker

    def _is_repeatable_tool(self, tool_name: str) -> bool:
        """判断一个工具的结果能否安全折叠成「重跑即可」（2026-11-09 第 3 批新增）

        为什么单独抽个方法而不是在 _microcompact_tool_outputs 里直接调 _repeatable_checker？
          因为 checker 可能为 None（没注入，比如单元测试），这里统一兜底：
          没注入就返回 False（保守），调用处不用每次判断 None。

        为什么没注入就返回 False 而不是 True？
          False 意味着"这个工具的结果不能折叠"，于是 L3 会跳过它、保留原文。
          这是保守方向：宁可多保留一点、少压缩一点，也不能误把"有副作用的写操作结果"
          折叠成"重跑即可"，否则模型会以为重跑能无害拿回数据，实际却重复改了文件。
        """
        if self._repeatable_checker is None:
            return False
        try:
            return bool(self._repeatable_checker(tool_name))
        except Exception:
            return False  # checker 抛异常（比如遇到未知工具）也按"不可折叠"处理，保守第一

    def seed_usage_from_messages(self, messages: list) -> int:
        """把恢复回来的历史折算成一条用量记录（2026-11-07 第 13.1 轮新增）

        什么时候调：`Conversation.start_conversation` 走 resume 分支、
        把 messages_history[-1] 换成「新 system + 恢复的旧消息」之后，立刻调一次。

        为什么这件事不能省：
          第 13 轮做完 `--continue` 之后，`history_token_usage` 在恢复路径上从来没人写过。
          它是个空列表，而 `_requires_compression()` 的第一句就是
          `if self._compress_threshold and self.history_token_usage:`——
          空列表直接短路成 False。于是恢复一份"上次已经用到 80%"的会话，
          第一次请求会把整份历史原样发出去，大概率 400。
          也就是说：恢复功能本身能用，但"恢复一个长会话"这个最该被支持的场景反而最容易崩。

        为什么写进 history_token_usage[-1]（覆盖）而不是累加：
          这份账本的口径是"当前这一页纸总共占了多少"，不是"本次新增多少"。
          恢复出来的是完整一页，所以应当覆盖成一个整页的估值。

        返回值是估算出的 token 数，方便调用方在界面上打一行提示。
        """
        estimated = estimate_messages_tokens(messages)
        usage = TokenUsage(input_tokens=estimated, output_tokens=0, total_tokens=estimated)
        # output_tokens 记 0：恢复的历史全是"输入"，之前生成的那些回复早就计过费了，
        # 这里只为回答"上下文有多满"，不参与成本统计。

        if self.history_token_usage:  # 正常应该为空（刚启动），但不假设调用方一定这么用
            self.history_token_usage[-1] = usage
        else:
            self.history_token_usage.append(usage)

        # 清掉"已压过"标记：新的用量意味着可以重新判断要不要压。
        # 这个标记是第 9 轮为防连环压缩加的（见 auto_messages_compression），
        # 恢复场景必须重置，否则第一次判断会被误跳过。
        self._compressed_for_current_usage = False
        return estimated

    def add_message(self, message) -> None:
        self.messages_history[-1].append(message)  # 往"当前这一页"的末尾追加一条
        # [-1] 就是"最后一页"的意思

    # mustn't crop the latest user input message
    # mustn't crop_amount < current_messages - 1
    # ↑ 原作者的两句提醒：不能把"用户最新说的那句"删掉，删除数量也不能超范围
    #
    # 【2026-11-06 第 9 轮】新增 note 参数，并把刀口对齐到"工具调用配对"的安全位置。
    #   note：被删内容的摘要，会插在"被删掉的那一段原来的位置"上。
    #         原来这个参数在 smart_context_cropper 里是必填的，但工具层拿到手就扔了，
    #         等于删掉的信息真的消失了——现在补上。
    def crop_message(self, crop_direction: Crop_Direction, crop_amount: int, note: str = "") -> str:
        current_messages = self.messages_history[-1]  # 拿到当前这一页

        if len(current_messages) <= 1:  # 只有 1 条或更少，删了就没内容了
            return "无法裁剪：消息数量太少。"  # 返回一句"删不了"

        if len(current_messages) < crop_amount + 2:  # 要删的数量太大，剩下的不够
            return "无法裁剪：裁剪数量无效（要删的比剩下的还多）。"

        # find the latest user message index
        latest_user_index = -1  # 先假设"没找到用户消息"，用 -1 表示
        for i in range(len(current_messages) - 1, -1, -1):
            # range(开始, 结束, 步长)：从最后一条往前走，也就是"从后往前找"
            # 【第 9 轮】改用 _role()，字典和对象两种形态都能读（原来只支持字典）
            if _role(current_messages[i]) == Role.USER:  # 找到一条"用户说的话"
                latest_user_index = i  # 记下它的位置
                break  # 找到就停（从后往前找，找到的就是最新的那条）

        if latest_user_index == -1:  # 一条用户消息都没找到
            return "无法裁剪：没有找到任何用户消息。"

        # ensure not to crop the latest user input message
        if crop_direction == Crop_Direction.TOP:  # 如果是从头删
            max_crop_amount = latest_user_index  # 最多只能删到"最新用户消息"之前
        else:  # BOTTOM，从尾删
            max_crop_amount = len(current_messages) - latest_user_index - 1  # 最多只能删到"最新用户消息"之后

        if crop_amount > max_crop_amount:  # 想删的比允许的还多
            return "无法裁剪：不能删掉用户最新说的那条消息。"  # 拒绝，保护用户最新输入

        note_messages = [make_note_message(note)] if note and note.strip() else []
        # 【第 9 轮】把摘要包成消息，插到"被删掉的那一段"原来的位置

        if crop_direction == Crop_Direction.TOP:
            # crop from the top, keeping system messages and content after the latest user message
            intended_cut = crop_amount  # 原本想从这里开始保留
            cut = align_top_cut(current_messages, intended_cut)  # 【第 9 轮】挪到安全位置
            system_messages = [msg for msg in current_messages if _role(msg) == Role.SYSTEM]
            # 上面这行是列表推导式：把"所有系统消息"单独挑出来，因为系统提示词永远不能删
            cropped_messages = system_messages + note_messages + current_messages[cut:]
            # 结果是：系统消息 + 摘要 + 从第 cut 条开始往后的内容
        else:  # BOTTOM
            # crop from the bottom, ensuring not to crop the latest user message
            intended_cut = len(current_messages) - crop_amount
            cut = align_bottom_cut(current_messages, intended_cut)  # 【第 9 轮】挪到安全位置
            cropped_messages = current_messages[:cut] + note_messages
            # [:] 的意思是"从头取到第 cut 条之前"，也就是把后面那些砍掉

        self.messages_history[-1] = cropped_messages  # 用新列表替换掉当前这一页

        skipped = intended_cut - cut  # 为了保住完整性，实际少删了几条
        if skipped > 0:
            return (f"裁剪成功，但为保证工具调用与其结果不被拆开，实际少删了 {skipped} 条。")
        return "裁剪成功。"  # 告诉调用方"删成功了"

    @property
    def current_context_window(self):
        """get current context window usage percentage（算出上下文用了百分之几）"""
        if not self.history_token_usage or self._model_max_tokens == 0:
            # 如果还没有用量记录，或者上限配置是 0（没法当除数）
            return "0.0"  # 就当用了 0%
        return f"{100 * self.history_token_usage[-1].total_tokens / self._model_max_tokens:.1f}"
        # 公式：用掉的 token ÷ 总上限 × 100，保留 1 位小数
        # 前面那个 f 表示这是"格式化字符串"，花括号里的表达式会被算出来填进去

    def update_token_usage(self, token_usage) -> None:
        token_usage = TokenUsage(
            # 把接口返回的"原始用量对象"转成我们自己的 TokenUsage（字段名不一样，要对应上）
            input_tokens=token_usage.prompt_tokens,  # prompt_tokens 对应"输入"
            output_tokens=token_usage.completion_tokens,  # completion_tokens 对应"输出"
            total_tokens=token_usage.total_tokens  # 总数
        )

        if len(self.history_token_usage) == 0:  # 如果一次都没记过
            self.history_token_usage.append(token_usage)  # 就新增一条
        else:  # 记过的话
            self.history_token_usage[-1] = token_usage  # 用最新的覆盖最后一条
        # 注意这里用的是"覆盖"而不是"累加"：因为每次请求返回的 total_tokens
        # 已经是"这一整轮对话的总量"，不是"本次新增量"，所以覆盖才是对的

        # 【2026-11-06 第 9 轮】来了新的用量数字，说明上下文已经变了，
        #   之前那次压缩的"已处理"标记要清掉，允许按新数字重新判断要不要压。
        self._compressed_for_current_usage = False

    def get_current_messages(self) -> Any:
        return copy.deepcopy(self.messages_history[-1])  # 深拷贝一份返回
        # 为什么要复制？因为调用方（比如加缓存标记）会修改返回的列表，
        # 不复制的话会把原始记录也改掉

    def get_projection(self, recent_rounds: int = 3) -> list:
        """L4 Context Collapse：生成一份「只读投影」，不改底层存储（2026-11-09 五层压缩第 2 批）

        核心思想（对齐 Claude Code 的 Context Collapse）：
          底层 messages_history[-1] 一字不动，这里只返回一份「压缩视图」给模型看。
          原始数据始终完整保留，随时可以 rewind 到完整历史——这是它和 L2/L3/L5
          最大的不同：那三层都是"真删/真改存储"，这一层是"只换视角、不动数据"。

        投影规则（recent_rounds=3，已和用户确认）：
          - system 消息：全部保留（规则设定永远不能丢）。
          - 最近 3 轮对话：完整保留（当前正在推进的工作，细节都要）。
          - 更早的对话：折叠成一条"骨架公告"，只留早期 user 消息的文字作为"历史任务线索"，
            丢弃早期 assistant 的思考、tool 调用细节和 tool 结果——这些已经被消化过，且可重取回。

        为什么要保留早期 user 消息的文字？
          早期每轮 user 消息通常是"一个已完成的子任务的目标"，保留它们能让模型
          知道"之前一路做了哪些事"，而不用保留每一步的详细过程。这正是投影的意义：
          留"结论/目标"、去"过程/细节"。

        返回值是一份深拷贝出来的新列表，和 messages_history 完全解耦，
        调用方怎么改它都不会影响底层存储。
        """
        current_messages = self.messages_history[-1]  # 底层存储（只读，不动它）

        # 找出所有 user 消息的位置，用来界定"最近 recent_rounds 轮"的起点。
        user_indices = self._get_user_message_indices(current_messages)
        if len(user_indices) <= recent_rounds:
            # 对话轮数本来就 <= recent_rounds，没有"更早期"需要折叠，直接返回完整副本。
            return copy.deepcopy(current_messages)

        # 第 (len - recent_rounds) 个 user 消息，就是"最近 recent_rounds 轮"的第一句。
        # 例：共 10 轮、recent_rounds=3 → 从第 7 个 user 消息（下标 user_indices[7]）开始是最近 3 轮。
        recent_start = user_indices[len(user_indices) - recent_rounds]

        # —— 拆成三段：早期（折叠）+ 近期（保留）——
        early = current_messages[:recent_start]   # 更早的部分（要被折叠）
        recent = current_messages[recent_start:]  # 最近 recent_rounds 轮（完整保留）

        # system 消息全保留（它们可能在 early 里，也可能单独）。
        system_messages = [msg for msg in early if _role(msg) == Role.SYSTEM]

        # 早期部分里，只抽取 user 消息的文字，作为"历史任务线索"。
        early_user_texts = []
        for msg in early:
            if _role(msg) == Role.USER:
                text = message_text(msg).strip()
                if text:
                    early_user_texts.append(text)

        # 折叠公告：把早期 user 消息的目标拼成一条骨架，告诉模型"之前做过这些事"。
        if early_user_texts:
            skeleton = "以下是更早对话的任务线索（详细过程已折叠，如需可重新查看）：\n" + "\n".join(
                f"- {t}" for t in early_user_texts
            )
        else:
            skeleton = "（更早的对话已折叠，无用户输入线索）"
        skeleton_notice = make_note_message(skeleton)

        # 投影 = system + 骨架公告 + 近期完整对话。返回的是全新列表，与底层存储解耦。
        return system_messages + [skeleton_notice] + copy.deepcopy(recent)

    def start_new_chat(self) -> None:
        self.messages_history.append([])  # 新开一页空纸
        self.history_token_usage.append(TokenUsage(0, 0, 0))  # 新页面的用量从 0 开始记

    def finish_chat_get_response(self) -> str:
        assert len(self.messages_history) >= 2, "there must more than or equal to 2 messages in history"
        # assert 是"断言"：条件不成立就直接抛错。这里确保至少有两页，否则说明逻辑出问题了
        task_messages = self.messages_history.pop()  # pop() 把最后一页整页撕下来，并拿到手
        self.history_token_usage.pop()  # 用量记录也撕掉
        if not task_messages:  # 【2026-11-06 优化】万一这一页是空的
            return ""  # 返回空字符串，避免下面取最后一条时越界
        response = self._content_to_text(task_messages[-1])  # 取出最后一条消息，并转成纯文本
        return response  # 返回给主 agent

    def _content_to_text(self, message) -> str:
        """把一条消息的 content 转换成纯文本（2026-11-06 新增，第 2 轮改为复用模块级函数）"""
        return message_text(message)  # 具体实现在文件上方的 message_text() 里，这里只是保留方法入口

    def _set_message_text(self, message, text: str) -> None:
        """把一条消息的 content 整体替换成「单个文本块」（2026-11-09 五层压缩第 3 批新增）

        为什么单独写这个方法，而不是在 _microcompact_tool_outputs 里直接改？
          L3 瘦身要把折叠后的文字写回消息，而消息的 content 有两种形态：
            - 字典（历史里存的，或从 JSONL 读回来的）→ content 是 [{"type":"text","text":...}]
            - 对象（运行时直接来自 SDK）→ content 是对象属性
          直接在调用处判断形态，会把「改 content」的细节撒得到处都是。
          这里统一收敛：不管原来是 str 还是内容块列表，都重写成标准的单个文本块，
          保证下游（发请求、估算 token）读到的结构一致。

        为什么重写成「单个文本块」而不是保留原来的多块结构？
          L3 的产物就是「一段折叠后的纯文本」，原来的多块（比如图片、多个 text 块）
          在工具输出里几乎不会出现，工具结果本质上就一段文字，重写成单块最干净，
          也让 message_text() 能一次读全，不遗漏。
        """
        if isinstance(message, dict):
            message["content"] = [{"type": "text", "text": text}]  # 字典形态：重写 content 键
        else:
            message.content = [{"type": "text", "text": text}]  # 对象形态：重写 content 属性


    def _requires_compression(self) -> bool:
        if self._compress_threshold and self.history_token_usage:  # 有阈值配置，也有用量记录
            current_usage = self.history_token_usage[-1]  # 拿最新一次的用量
            return current_usage.total_tokens > self._compress_threshold * self._model_max_tokens
            # 如果"用掉的"已经超过"上限 × 0.8"，就返回 True（该压了）
        return False  # 其他情况都说"不用压"

    def _current_usage_ratio(self) -> float:
        """算出当前上下文占用比例（0~1 之间的小数），供分层压缩判断用

        为什么单独抽一个方法，而不是在 _requires_compression 里硬写除法？
          分层压缩需要在每个层级"重新评估"占了多少，好决定要不要进下一层。
          每次都写一遍 `total_tokens / max_tokens` 既重复又容易漏掉除零判断，
          抽出来一处维护。没有用量记录时返回 0（当没占用）。
        """
        if not self.history_token_usage or self._model_max_tokens == 0:
            return 0.0
        return self.history_token_usage[-1].total_tokens / self._model_max_tokens

    def should_use_projection(self) -> bool:
        """判断「发请求时是否该用只读投影」而不是完整历史（2026-11-09 第 2 批新增）

        和 auto_messages_compression 的"落点直做"是什么关系？
          那是"占用到了就动手压缩"；而这一层 L4 是"不改存储、只在发请求时换个视图"。
          两者是配套的：当占用落在 [micro_threshold, collapse_threshold) 这一段
          （即 0.90 ~ 0.95，正是四档区间映射里的 L4 档），且 conversation 要发请求时，
          就用投影替代完整历史——既不真删数据，又让模型只看到压缩后的样子，
          从而省下这一轮请求的输入 token。

        判断口径：占用落在 [micro_threshold, collapse_threshold) 之间（0.90 ~ 0.95）。
          为什么是「下界 >= 0.90、上界 < 0.95」而不是单边？
            0.90 是 L4 的触发线：占用到了这里，就说明 L2/L3 那两档的手段已经"不够用"，
              该换"只读投影"这个更省输入 token 的视角了。
            0.95 是 L5 的触发线：一旦 >= 0.95，就该走 auto_messages_compression 里的
              L5（调模型全量摘要），那是"真删存储"，投影就该退场、不再抢戏。
          所以投影只负责 [0.90, 0.95) 这一小段，上下去向都由别的层接管。
        返回值只在 conversation 发请求前被读取，不影响任何存储。
        """
        ratio = self._current_usage_ratio()
        # 落在 [micro_threshold, collapse_threshold) 区间才投影；>= collapse 交给 L5 摘要。
        return self._micro_threshold <= ratio < self._collapse_threshold

    def _snip_oldest_round(self) -> bool:
        """L2 Snip：零成本「保两头、剪中间」（2026-11-09 五层压缩第 1 批，对齐 Claude Code snipCompact）

        学 Claude Code 的做法（snipCompact.ts：KEEP_FIRST=3 / KEEP_RECENT=47）：
          - 「头」= system 消息 + 第一个 user 消息（任务目标所在，永远保留）；
          - 「尾」= 最近 47 条消息（近期上下文，最相关）；
          - 「中间」整段删掉，替换成一条本地占位公告，**不调任何模型**。
        这样既快速减少消息数量，又不丢"任务目标"和"最近进展"这两样最关键的东西。

        和原来自家 _compress_multiple_sessions 的区别：
          那个方法删最老一轮后紧接着调 _summarize_if_possible（一次 API、花钱）。
          这一层是纯删除 + 固定占位文案，零 API 成本——先用便宜手段，够用就停。

        返回值：True 表示真的剪掉了一段；False 表示"消息太少、没得剪"。

        为什么"尾"要保留最近 47 条？
          47 这个数来自 Claude Code 的经验值：最近 47 条通常能覆盖"当前正在推进的子任务"的
          完整上下文（读文件 → 改代码 → 跑测试的整条链路），再往前往往已经是上一个任务了。
          而"头"保留 system + 第一个 user，是为了让模型始终记得"最开始要解决什么问题"。
        """
        current_messages = self.messages_history[-1]  # 拿到当前这一页

        # 消息太少（比如还没有 47 条尾巴可留）时，剪中间没意义，直接返回 False。
        # 这里用一个比 47 略大的门槛：至少要有「头 + 若干中间 + 47 尾」才值得剪。
        if len(current_messages) <= 50:
            return False

        # —— 确定"头"：system 消息 + 第一个 user 消息 ——
        head = []  # 准备装"头"的那几条
        first_user_index = -1  # 第一个 user 消息的位置（任务目标所在）
        for i, msg in enumerate(current_messages):
            if _role(msg) == Role.SYSTEM:
                head.append(msg)  # 所有 system 消息都进"头"
            elif first_user_index == -1 and _role(msg) == Role.USER:
                first_user_index = i  # 记下第一个 user 的位置
                head.append(msg)  # 第一个 user 也进"头"
                break  # 头找齐了，停
        # 走到这，head 里是「所有 system + 第一个 user」。它们是最该保住的"任务目标"。

        # —— 确定"尾"：最近 47 条 ——
        # 从尾部往回取 47 条，但刀口必须落在"安全位置"（不能把 assistant(tool_calls) 和它的 tool 结果拆开）。
        tail_start = len(current_messages) - 47  # 尾巴想从这开始保留
        tail_start = align_top_cut(current_messages, tail_start)  # 刀口对齐到安全位置（可能往前挪）

        # —— 防一手：如果"尾"的起点比"头"的末尾还靠前（说明头和尾重叠了），说明消息其实没多到需要剪 ——
        head_end = first_user_index + 1  # "头"占用的最后一位下标（开区间）
        if tail_start <= head_end:
            return False  # 头和尾已经接上了，中间没东西可剪

        # —— 组装：头 + 占位公告 + 尾 ——
        removed_count = tail_start - head_end  # 中间被剪掉了多少条
        notice = make_note_message(f"[已裁剪中间 {removed_count} 条消息以节省上下文空间]")
        tail = current_messages[tail_start:]  # 尾 = 从安全刀口往后的 47 条（可能因对齐少几条）

        self.messages_history[-1] = head + [notice] + tail  # 新的一页 = 头 + 占位 + 尾

        # 裁剪后用量账本失真（删了一堆消息），重算成"当前这一页的估值"，
        # 让下一层判断用的是新数字。账本可能为空，用 append 兜底（同 seed_usage_from_messages）。
        remaining_tokens = estimate_messages_tokens(self.messages_history[-1])
        remaining_usage = TokenUsage(
            input_tokens=remaining_tokens, output_tokens=0, total_tokens=remaining_tokens,
        )
        if self.history_token_usage:
            self.history_token_usage[-1] = remaining_usage
        else:
            self.history_token_usage.append(remaining_usage)
        return True

    def _microcompact_tool_outputs(self, keep_recent: int = 3) -> bool:
        """L3 Microcompact：把「较早的可重复查询工具结果」折叠成极简占位（2026-11-09 第 3 批，对齐 Claude Code）

        核心思想（对齐 Claude Code 的 micro_compact）：
          工具结果刚写进历史时允许最宽到 30000 字符（MAX_TOOL_OUTPUT_CHARS），
          那是"上下文还空"时给模型的方便——一次看全、少反复读文件。
          但对话变长后，这些"早就被模型消费过的大块输出"是最该先压的对象。

        关键：**只折叠「可重复查询」工具的结果**（is_repeatable=True 的那类）：
          - file_read / file_grep / file_glob 这类"读操作"，重跑一遍能拿到（甚至更新鲜的）
            相同结果，所以折叠成一句"已压缩，重跑即可"完全无损。
          - file_write / file_edit / cmd_runner 这类"有副作用的操作"，执行一次就改变一次
            系统状态，重跑不是"拿回相同结果"而是"再做一次副作用"。它们的结果**绝不折叠**，
            否则模型会以为重跑能无害拿回数据，实际却重复改了文件、重复跑了测试。

        占位文案（对齐 Claude Code 的 "Earlier tool result compacted. Re-run if needed."，改用中文）：
          [更早的工具结果已折叠，需要完整内容时请重新运行对应工具。]

        和 L1（_truncate_tool_output）有什么区别？
          L1 是"写进历史那一刻"掐头留尾到 30000，属于"进门前先消毒"，对所有工具一视同仁；
          L3 是"对话变长后"对可重复查询的工具结果做整条折叠，属于"进门后、发现挤了再瘦身"。
          两者是不同时机的两道闸：先给足、后收紧。

        和 L2（_snip_oldest_round）有什么区别？
          L2 是"整条删掉"中间的消息（保头+尾47）；L3 是"不删消息、只把消息里的
          工具输出文字换成一短句"。L2 砍的是"消息条数"，L3 砍的是"单条消息的体积"。
          两者互补：一个解决"消息太多"，一个解决"单条太大"。

        返回值：True 表示真的折叠了至少一条工具结果；False 表示"没有可折叠的"。

        为什么保留最近 keep_recent 条工具结果完整？
          最近几条工具结果通常属于"当前正在推进的子任务"，模型正在依据它们
          做下一步判断，压了会丢关键信息；而更早的工具结果结论已被消化，
          可以放心折叠。这和 L4 投影"保最近 3 轮"的思路一脉相承。
        """
        current_messages = self.messages_history[-1]  # 拿到当前这一页

        # 找出所有 tool 消息的下标，再从右往左数 keep_recent 条，得到"要保留完整"的边界。
        tool_indices = [i for i, msg in enumerate(current_messages) if _role(msg) == Role.TOOL]
        if len(tool_indices) <= keep_recent:
            return False  # 工具结果总共就 <= keep_recent 条，没有"较早的"可压

        # 最近 keep_recent 条工具结果的下标集合（这些保持原样）。
        recent_tool_indices = set(tool_indices[-keep_recent:])

        changed = False  # 记录本轮是否真的压过任何一条
        for i, msg in enumerate(current_messages):
            if i in recent_tool_indices or _role(msg) != Role.TOOL:
                continue  # 最近几条或非 tool 消息，跳过

            # —— 关键判断：这个工具的结果能不能安全折叠成"重跑即可" ——
            # 只有"可重复查询"（读操作）的工具才折叠；有副作用的（写/执行）绝不碰。
            tool_name = msg.get("name") if isinstance(msg, dict) else getattr(msg, "name", None)
            if not tool_name or not self._is_repeatable_tool(tool_name):
                continue  # 拿不到工具名，或这个工具不可重复查询 → 保守跳过，保留原文

            text = message_text(msg)  # 取出这条工具输出的纯文本
            if len(text) <= MICROCOMPACT_TOOL_OUTPUT_CHARS:
                continue  # 本来就不大（<= 8000 字），不值得折叠，留着更省事

            # —— 折叠成极简占位（整条替换，不再掐头留尾） ——
            # 为什么整条替换而不是保留 8000 字摘要？
            #   可重复查询的工具，重跑成本极低，且能拿到"更新鲜"的结果。
            #   保留 8000 字摘要反而是"既占空间、又可能过时"——不如一句占位，
            #   模型需要时自己重跑 file_read / file_grep 拿最新结果。
            # 文案用中文：这句会直接进模型上下文，中文更贴合本项目的沟通习惯，
            #   也方便模型在中文场景下自然理解"这条结果被折叠过、需要就重跑"。
            folded = "[更早的工具结果已折叠，需要完整内容时请重新运行对应工具。]"
            self._set_message_text(msg, folded)
            changed = True

        if changed:
            # 折叠后用量账本失真（好几条工具输出变短了），重算成当前页的估值，
            # 让上层"是否还超阈值"的判断用的是新数字。账本可能为空，append 兜底。
            remaining_tokens = estimate_messages_tokens(current_messages)
            remaining_usage = TokenUsage(
                input_tokens=remaining_tokens, output_tokens=0, total_tokens=remaining_tokens,
            )
            if self.history_token_usage:
                self.history_token_usage[-1] = remaining_usage
            else:
                self.history_token_usage.append(remaining_usage)
        return changed

    # if there is more than one  session, compress the oldest chat session
    # else compress the current roll assistant output
    # tell the model we have compressed the messages
    async def _compress_current_message(self, announce: bool = True) -> None:
        """Compress current message history to save context window space（真正执行压缩）

        参数 announce（2026-11-08 第 14.5 轮新增）：
          True（默认）→ 自己打一句"上下文太长了，正在自动压缩…"，
                        这是自动压缩的路径，用户没主动要求，需要知道发生了什么；
          False       → 不打这句。/compact 是用户点名的，提示语由命令层统一说，
                        再说一遍"太长了"既多余、又和用户的心智不符（他本来就嫌长）。
        """
        if announce:
            self._ui_manager.print_info("上下文太长了，正在自动压缩…")  # 【第 4 轮】改成暗色提示，不再占据"模型发言"的位置

        current_messages = self.messages_history[-1]  # 拿到当前这一页
        user_indices = self._get_user_message_indices(current_messages)  # 找出所有"用户消息"的位置

        if len(user_indices) > 1:  # 如果用户已经说过好几轮了
            await self._compress_multiple_sessions(current_messages, user_indices)  # 就把最老的那一轮整体删掉
        elif len(user_indices) == 1:  # 如果只有一轮（用户只说过一句，后面全是来回调工具）
            # delete_message_num is hardcode to 3 right now
            await self._compress_single_session(current_messages, user_indices[0], 3)  # 就删掉紧跟在它后面的 3 条
        # 一条用户消息都没有（页面里只有 system）时什么都不做：
        #   没有"一轮对话"可以压，硬删只会把系统提示词也弄丢。

    async def compact_now(self) -> dict:
        """手动压缩一次，不看过没过阈值（2026-11-08 第 14.5 轮新增）

        【和 auto_messages_compression() 的分工】
          自动那条只在"超过 80%"时动手，目的是防止上下文爆掉，属于兜底；
          这条是用户 /compact 点名的：什么时候压由人决定。
          典型场景——刚让模型试了七八种写法，过程很长但结论只有一句，
          这时上下文可能才 30%，压掉之后每一轮都省，却永远等不到 80%。
          只有自动压缩的话，这种"该压但没到线"的情况用户没有手段。

        【为什么先算压缩前的估算值】
          压缩是"消息整体替换"，压完就看不出原来多大了。
          而用户最想知道的就是"省了多少"，所以要在动手前留一份快照。
          用 estimate_messages_tokens（第 13.1 轮加的）而不是接口返回的真实值：
          这里没发请求，只能估；估算口径和恢复会话时一致，两个数字可比。

        【三个副作用，必须让调用方知道】
          1. 用量账本会被重算成压缩后的估算值。不重算的话 /cost 和状态行
             会一直显示压缩前的占用，用户会以为压缩没生效——明明压了，数字没动，
             这种"看起来没用"比真的没用更劝退。
          2. 打上 _compressed_for_current_usage 标记，防止紧接着的自动检查
             又压一次（第 9 轮那个"一旦过线连压三次"的坑）。
          3. **前缀缓存失效**：消息列表整体换了，下一轮输入按原价计费。
             这条在代码里没法消除，只能由 /compact 的提示语告诉用户，
             否则他看到 /cost 里花费跳涨会以为出了 bug。

        返回：{"before_count", "after_count", "before_tokens", "after_tokens"}
              为什么返回字典而不是直接打印：命令层要统一措辞和缩进，
              测试里也不想让输出混进断言。
        """
        before_messages = self.messages_history[-1]
        before_count = len(before_messages)
        before_tokens = estimate_messages_tokens(before_messages)

        # announce=False：提示语交给 /compact 说，这里不要再说"上下文太长了"
        await self._compress_current_message(announce=False)

        after_messages = self.messages_history[-1]
        after_count = len(after_messages)
        after_tokens = estimate_messages_tokens(after_messages)

        if after_count:  # 压完至少还有 system，正常不会是 0；防一手空页
            usage = TokenUsage(input_tokens=after_tokens, output_tokens=0, total_tokens=after_tokens)
            # output_tokens 记 0：压缩本身不产生"模型的输出"，它只是在整理输入。
            if self.history_token_usage:
                self.history_token_usage[-1] = usage
            else:
                self.history_token_usage.append(usage)

        self._compressed_for_current_usage = True  # 本轮用量已处理，别让自动压缩再压一次

        return {
            "before_count": before_count,
            "after_count": after_count,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        }

    @staticmethod
    def _role_of(message) -> Any:
        """读取一条消息的 role，字典和对象两种写法都支持（2026-11-06 新增）

        为什么需要这个函数？
          历史记录里存的是"字典"，但单元测试里用的是"自定义对象"。
          字典要写 msg["role"]，对象要写 msg.role，两者不能通用。
          所以统一从这里取，谁都不用再关心传进来的是哪种形态。
        """
        if isinstance(message, dict):  # 如果是字典（真实运行时的形态）
            return message.get("role")  # 用中括号取，取不到返回 None
        return getattr(message, "role", None)  # 否则按对象取属性，取不到返回 None

    def _get_user_message_indices(self, messages: list) -> list[int]:
        """Get index positions of all user messages（找出所有用户消息的位置）"""
        return [i for i, msg in enumerate(messages) if self._role_of(msg) == Role.USER]
        # 列表推导式：遍历每一条，如果是用户说的，就把它的位置 i 记下来
        # 【2026-11-06 优化】原来写的是 msg.role，但真实消息是字典，会抛 AttributeError。
        #    现在统一走 _role_of()，字典和对象两种形态都能读（细节见该方法）。

    async def _compress_multiple_sessions(self, messages: list, user_indices: list[int]) -> None:
        """delete the oldest chat session（删掉最老的那一轮对话）

        【2026-11-06 第 2 轮优化】从"直接删"改成"先摘要、再删"：
          被删掉的那段内容会先交给大模型总结成一段话，
          再把这段话放进对话里。这样模型不会真的"忘事"。
        """
        second_oldest_user_index = user_indices[1]  # 找出"第二老"的那句用户消息的位置

        system_messages = [msg for msg in messages[:second_oldest_user_index] if self._role_of(msg) == Role.SYSTEM]
        # 从开头到"第二老"之前，把里面的系统消息挑出来（保住规则设定）
        # 【2026-11-06 优化】同样把 msg.role 改成 _role_of(msg)

        removed_messages = [msg for msg in messages[:second_oldest_user_index]
                            if self._role_of(msg) != Role.SYSTEM]
        # 【2026-11-06 第 2 轮新增】算清楚"到底要删掉哪些"（不含系统消息），
        #   这一段就是要送去生成摘要的原料

        recent_messages = messages[second_oldest_user_index:]  # 从"第二老"开始往后的全部内容（保住近期对话）

        summary = await self._summarize_if_possible(removed_messages)  # 让模型总结这段被删的内容
        self.messages_history[-1] = system_messages + self._create_compression_notice(messages, summary) + recent_messages
        # 新的这一页 = 系统消息 + 摘要公告 + 近期对话

    async def _compress_single_session(self, messages: list, user_index: int, delete_message_num: int) -> None:
        """delete assistant messages and tool message close to user input（删掉紧跟用户发言后的若干条）"""
        system_messages = [msg for msg in messages[:user_index] if self._role_of(msg) == Role.SYSTEM]  # 保住系统消息
        # 【2026-11-06 优化】同样把 msg.role 改成 _role_of(msg)

        start_index = min(user_index + 1 + delete_message_num, len(messages))
        # 计算"从第几条开始保留"：用户消息后面跳过 delete_message_num 条
        # min(..., len(messages)) 是防止算出来的位置超出列表长度

        # 【2026-11-06 第 9 轮修复】如果刀口正好落在一条 tool 消息上，
        #   说明它上面那条 assistant(tool_calls) 会被删掉，而结果却被留下——
        #   下一次请求就会 400（role=tool 找不到对应的 tool_calls）。
        #   这里把刀口往后推到这一组工具结果之后，整组一起删。
        while start_index < len(messages) and _role(messages[start_index]) == Role.TOOL:
            start_index += 1

        removed_messages = messages[user_index + 1:start_index]  # 【2026-11-06 第 2 轮新增】要删掉的那几条
        summary = await self._summarize_if_possible(removed_messages)  # 先总结

        user_message = [messages[user_index]] + self._create_compression_notice(messages, summary) + messages[start_index:]
        # 新的内容 = 用户那句话 + 摘要公告 + 剩下的内容

        self.messages_history[-1] = system_messages + user_message  # 拼成新的这一页

    async def _summarize_if_possible(self, messages: list):
        """尝试把一段消息总结成摘要；做不到就返回 None（2026-11-06 第 2 轮新增）

        三种情况会返回 None（也就是退回"直接删除"的老做法）：
          1. 没有注入摘要器（比如单元测试环境）
          2. 要删的消息是空的
          3. 调模型失败（没网、没额度、超时……）——这种情况不能卡死对话，必须降级
        """
        if not messages or self._summarizer is None:  # 情况 1、2
            return None
        try:
            summary = await self._summarizer(messages)  # 调用外部注入的摘要函数
        except Exception as e:  # 情况 3
            print(f"[压缩] 生成摘要失败（{e}），本次退回为直接删除旧消息")
            return None
        if not summary or not summary.strip():  # 模型返回了空内容
            return None
        return summary.strip()

    def _create_compression_notice(self, messages: list, summary: str = None) -> list:
        """create compression notice（造一条"历史已压缩"的公告）

        【2026-11-06 第 2 轮优化】多了一个 summary 参数：
          有摘要 → 公告里带上摘要内容，模型能知道之前发生了什么
          没摘要 → 退回原来那句话
        """
        if not messages:  # 如果消息列表是空的
            return []  # 就返回空列表，不用公告

        # 【2026-11-06 优化】原来 content 直接写的是字符串，
        #   而其他消息的 content 都是 [{"type": "text", "text": ...}] 这种"内容块列表"。
        #   两种结构混在一起，任何按统一格式读 content 的代码（比如 _content_to_text）
        #   都会出错。现在统一成内容块列表。
        # 【2026-11-06 第 3 轮】公告文字改成中文（它是作为一条"用户消息"进入对话的，模型会读到）
        if summary:  # 有摘要
            notice_text = (
                "[为节省上下文空间，之前的对话已被压缩。以下是被压缩部分的摘要：]\n" + summary
            )
        else:  # 没摘要，用原来那句
            notice_text = "[之前的对话历史已被压缩，以节省上下文空间]"

        first = messages[0]  # 看一眼现有消息是什么形态
        if isinstance(first, dict):  # 真实运行时都是字典
            compression_notice = {
                "role": Role.USER,  # 用"用户"身份说这句话，模型更容易接受
                "content": [{"type": "text", "text": notice_text}]  # 和其他消息结构保持一致
            }
        else:  # 少数情况（比如单元测试里用的是自定义对象）
            compression_notice = type(first)(role=Role.USER, content=notice_text)
            # 按同样的类型造一条，字段直接给字符串，这样那些按对象访问的代码也不会崩
        return [compression_notice]  # 包成列表返回

# =============================================================================
# 备注（这段代码里有两个需要注意的地方）
#
# 1. 【2026-11-06 已修复】属性访问的 bug：
#    原来 _get_user_message_indices、_compress_multiple_sessions、
#    _compress_single_session 里都写的是 msg.role，但真实消息是字典，
#    所以 token 一超过 80% 触发压缩就会抛 AttributeError 崩掉。
#    现在统一改用 _role_of() 读取，字典和对象两种形态都能用。
#
# 2. 【2026-11-06 第 2 轮已修复】压缩的策略原来是"硬删消息"，只留一句"压缩过了"的公告，
#    模型会真的忘记之前聊了什么。
#    现在改成"摘要式压缩"：删之前先让模型把这段内容总结成一段摘要，
#    再把摘要放进对话里。摘要器由 Conversation 注入（set_summarizer），
#    没有注入或者调模型失败时，会自动退回"直接删除"的老做法。
# =============================================================================
