# =============================================================================
# 文件：test/test_five_layer_compress.py
# 作用：测试「上下文五层压缩」（2026-11-09 新增的 L2/L3/L4，加上原有的 L1/L5）。
#
# 为什么单独写这个文件？
#   五层压缩那次提交（7e9994a）新增了 226 行核心逻辑，但 test/ 里一条断言都没有：
#     · grep "snip|microcompact|projection" test/ 零命中
#     · test_history_compress.py 测的还是第 2 轮的「删一轮 + 摘要」老路
#   而压缩是"错了也不报错"的典型：剪错位置要么下一次请求 400，
#   要么模型莫名其妙失忆，两次都表现为"它好像变笨了"，很难归因。
#   所以这里不追求覆盖率好看，而是把「每层最可能出错的那一下」钉死。
#
# 判断"到底走了哪一层"，不看内部 tier 变量，只看四个用户能观察到的结果：
#   · 消息条数变没变        → L2 的指纹（整条删）
#   · 工具输出文本变没变短  → L3 的指纹（只折叠单条体积）
#   · 存储动没动 / 投影可用 → L4 的指纹（只换视图，不改数据）
#   · 摘要器被调用了几次    → L5 的指纹（唯一花钱的一层）
# 这样即使以后实现重写，只要行为不变，测试就还成立。
#
# 全程离线：摘要器用假函数注入，不调模型、不发请求、不花钱。
# 运行方式（项目根目录）：
#   python test/test_five_layer_compress.py
# =============================================================================

import asyncio  # 压缩链是 async，要用 asyncio.run 驱动
import copy  # 比对"投影/压缩有没有偷偷改底层存储"
import os  # 拼路径
import sys  # 改模块搜索路径 + 失败时用非 0 退出码
import traceback  # 失败时打印完整现场

# 和同目录其他测试一致：从 test/ 直接跑时，Python 找不到 src 下的包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.history.history_manager import HistoryManager, TokenUsage  # 被测主体
from core.history.history_manager import _owner_index, _role, _tool_calls  # 校验工具对是否残缺
from core.limits import MAX_TOOL_OUTPUT_CHARS, MICROCOMPACT_TOOL_OUTPUT_CHARS  # L1 / L3 的两个上限
from core.conversation import Conversation  # 只为了测 L1 的截断方法（用 __new__ 绕开初始化）


# -----------------------------------------------------------------------------
# 一、造数据的小工具
#
# 为什么不用 MockMessage 类，而是老老实实造字典？
#   真实历史里存的就是字典（session JSONL 里也是），而压缩逻辑里有多处
#   `isinstance(msg, dict)` 分支（比如 L3 取工具名）。用类造数据会把
#   "字典分支"整条漏掉——第 1 轮那个"测试能过、真机崩"的教训就是这么来的。
# -----------------------------------------------------------------------------

def mk_system(text="你是 Thinker。"):
    """一条 system 消息（压缩时永远要保住的那种）"""
    return {"role": "system", "content": [{"type": "text", "text": text}]}


def mk_user(text):
    """一条 user 消息（L2 靠它认「任务目标」，L4 靠它抽「历史线索」）"""
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def mk_assistant(text="", call_id=None, tool_name="file_read"):
    """一条 assistant 消息；给了 call_id 就带上 tool_calls

    为什么要能带 tool_calls：
      OpenAI 协议要求 assistant(tool_calls) 与随后的 tool 结果成对出现。
      L2 剪中间时如果只留 tool、把它的 owner 剪掉，下一次请求直接 400。
      这里造出真实的一对，才能测到「刀口对齐」到底有没有生效。
    """
    message = {"role": "assistant", "content": []}
    if text:
        message["content"] = [{"type": "text", "text": text}]
    if call_id:
        message["tool_calls"] = [{
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }]
    return message


def mk_tool(text, call_id, name="file_read"):
    """一条 tool 结果（结构与 Conversation._add_tool_response 完全一致）"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": [{"type": "text", "text": text}],
    }


def fresh_manager():
    """每个用例都拿一个全新的 HistoryManager（单例必须手动重置）

    不重置的后果：上一个用例塞进去的 120 条消息会留到下一个用例，
    条数断言全部对不上，而且报错信息会指向完全无关的地方。
    现有 test_history_compress.py 的 fresh_manager() 也是这个套路。

    【2026-11-13 补】四个阈值必须在这里钉死，不能让它读 .env。
      原因是 HistoryManager.__init__ 的阈值全部走 os.getenv(..., 默认值)：
      用户自己的 .env 一旦调过线（本机现在是 COMPRESS 0.70 / SNIP 0.75 /
      MICRO 0.85 / COLLAPSE 0.90），测试里那些"0.82 该走 L2、0.87 该走 L3、
      0.92 该走 L4"的断言就会整体错档——代码没坏，阈值被环境改掉了。
      这种失败最难查：单独跑测试全绿，换台机器全红，看起来像分支丢了改动。
      钉住之后，这个文件只验"分档路由逻辑对不对"，用户实际阈值由 .env 决定、
      与这里无关（想验阈值可以另写一条读 .env 的用例，但别混进路由测试）。
    """
    HistoryManager._instance = None  # 丢掉旧实例
    HistoryManager._initialized = False  # 清掉初始化标记，逼它重新 __init__
    manager = HistoryManager()  # 新实例
    manager._ui_manager = _SilentUI()  # 压缩会打提示语，测试里静音
    # 与 .env.example 的默认值对齐：0.80 总闸 / 0.85 L2 / 0.90 L3 / 0.95 L4
    manager._compress_threshold = 0.80
    manager._snip_threshold = 0.85
    manager._micro_threshold = 0.90
    manager._collapse_threshold = 0.95
    return manager


class _SilentUI:
    """把界面上所有方法都变成"什么都不做"

    为什么需要：压缩链会调 print_info / print_warning 打提示，
    测试输出里夹一堆带颜色的中文会淹没真正的断言结果。
    用 __getattr__ 兜底，以后 UI 加新方法也不用回来改这里。
    """

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop


def mock_sdk_usage(total_tokens):
    """造一个"接口返回的用量对象"

    为什么不直接传 TokenUsage：
      update_token_usage 的入参契约是**接口原始对象**（字段叫 prompt_tokens /
      completion_tokens），在函数内部才转成我们自己的 TokenUsage
      （字段叫 input_tokens / output_tokens）。传错类型会直接
      AttributeError: 'TokenUsage' object has no attribute 'prompt_tokens'——
      这个坑测试里踩一次就够了，写个小工厂统一造。
    """

    class _MockSDKUsage:
        def __init__(self, total):
            self.prompt_tokens = total
            self.completion_tokens = 0
            self.total_tokens = total

    return _MockSDKUsage(total_tokens)


def set_ratio(manager, ratio):
    """把「当前上下文占用」直接设成指定比例

    为什么不构造真实消息让估算函数算？
      估算是按字符粗估的，想精确落在 0.82 / 0.85 / 0.90 这些边界上，
      得反复调消息长度，测试会变得又长又脆。这里直接写用量账本，
      压缩链读的就是这个数（_current_usage_ratio），等价于"真实占用到了这个位置"。

    注意两个细节：
      1. 必须重置 _compressed_for_current_usage，否则上一轮压过的标记会让
         这一轮直接 return（这是压缩链的防连压机制，见 auto_messages_compression）。
      2. 用 int() 截断而不是四舍五入：边界用例要靠截断方向来保证落在预期那一档。
    """
    total = int(manager._model_max_tokens * ratio)
    usage = TokenUsage(input_tokens=total, output_tokens=0, total_tokens=total)
    if manager.history_token_usage:
        manager.history_token_usage[-1] = usage
    else:
        manager.history_token_usage.append(usage)
    manager._compressed_for_current_usage = False


def build_long_history(manager, rounds=40, tool_size=200):
    """造一段"长到必须压缩"的对话：system + N 轮（user → assistant(tool_calls) → tool）

    每轮 3 条，40 轮 = 121 条，刚好越过 L2 的 50 条门槛。
    tool_size 默认 200 字：L2 用例不关心单条大小，给小的就行，跑得快。
    L3 用例会传 9000（> MICROCOMPACT_TOOL_OUTPUT_CHARS），才能触发折叠。
    """
    manager.add_message(mk_system())
    for i in range(rounds):
        manager.add_message(mk_user(f"任务 {i}"))
        manager.add_message(mk_assistant(f"处理任务 {i}", call_id=f"call_{i}"))
        manager.add_message(mk_tool(f"TOOL_TEXT_{i}" + "x" * tool_size, call_id=f"call_{i}"))


def orphan_tool_count(messages):
    """数一数有多少条 tool 消息找不到它的 assistant（孤儿 tool）

    孤儿 tool 是 400 的直接原因，所以每个动过刀口的用例都要查这一项：
      "messages with role 'tool' must be a response to a preceding
       message with 'tool_calls'"
    判据复用生产代码的 _owner_index，避免测试自己另写一套"什么叫成对"。
    """
    return sum(
        1 for i, msg in enumerate(messages)
        if _role(msg) == "tool" and _owner_index(messages, i) < 0
    )


def all_text(messages):
    """把整页消息的文字拼起来，用于"某个字符串还在不在"这类断言"""
    parts = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            parts.extend(b.get("text", "") for b in content if isinstance(b, dict))
        elif isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


# -----------------------------------------------------------------------------
# 二、L1：工具输出进门先消毒（写进历史那一刻掐头去尾）
# -----------------------------------------------------------------------------

def test_l1_short_output_untouched():
    """不超过上限的输出必须原样保留

    为什么值得测：L1 是每一条工具结果都要过的闸，一旦它误伤短输出，
    "读一个 20 行的小文件"都会收到半截内容，而且没有任何报错。
    """
    print("测试 L1: 短输出不被截断")

    conversation = object.__new__(Conversation)  # 绕开 __init__：这里只用到一个纯计算方法
    text = "hello world" * 10
    assert conversation._truncate_tool_output(text) == text

    print("✓ 通过")


def test_l1_long_output_keeps_head_and_tail():
    """超长输出：留头留尾、中间给省略说明，且总长比原文短

    只断言"长度变小"不够——那可能是从头砍的。这里还要求：
      · 开头的内容还在（定位"这条命令在干什么"）
      · 结尾的内容还在（最后一行往往是最终结果或报错）
      · 中间那句省略说明在（否则模型以为数据只有这么多）
    """
    print("测试 L1: 超长输出留头留尾")

    conversation = object.__new__(Conversation)
    head = "HEAD_MARK" + "a" * 100
    tail = "b" * 100 + "TAIL_MARK"
    text = head + "c" * (MAX_TOOL_OUTPUT_CHARS * 2) + tail

    result = conversation._truncate_tool_output(text)

    assert len(result) < len(text)  # 确实截了
    assert result.startswith("HEAD_MARK")  # 头保住了
    assert result.endswith("TAIL_MARK")  # 尾保住了
    assert "中间省略了" in result  # 有省略说明
    assert "file_read 的 offset / limit" in result  # 说明里教了怎么取全文（Windows 上可用的办法）

    print("✓ 通过")


def test_l1_boundary_not_truncated():
    """长度正好等于上限时不该截断（边界值最容易写成 >= / > 搞反）"""
    print("测试 L1: 恰好等于上限不截断")

    conversation = object.__new__(Conversation)
    text = "a" * MAX_TOOL_OUTPUT_CHARS
    assert conversation._truncate_tool_output(text) == text

    print("✓ 通过")


# -----------------------------------------------------------------------------
# 三、L2：Snip 剪中间（零成本，删消息条数）
# -----------------------------------------------------------------------------

def test_l2_too_short_untouched():
    """消息不足 50 条时不剪

    为什么门槛是 50：要保住「system + 第一个 user」和「最近 47 条」，
    46 条以下根本没东西可剪。写死这条断言，是为了防止有人把 47 改成 7
    之后，短对话也被莫名其妙删掉一半。
    """
    print("测试 L2: 短对话不剪")

    manager = fresh_manager()
    build_long_history(manager, rounds=10)  # 31 条
    before = manager.get_current_messages()

    assert manager._snip_oldest_round() is False
    assert len(manager.get_current_messages()) == len(before)

    print("✓ 通过")


def test_l2_keeps_head_and_tail():
    """长对话：条数变少，但 system、第一个 user、最后 47 条必须一字不动

    这三样是 L2 的全部意义：
      system      → 规则设定，丢了模型就变野
      第一个 user → 最初的任務目标，丢了它会开始做别的事
      最近 47 条  → 正在推进的子任务，丢了等于失忆
    """
    print("测试 L2: 保头保尾剪中间")

    manager = fresh_manager()
    build_long_history(manager, rounds=40)
    before = manager.get_current_messages()

    assert manager._snip_oldest_round() is True
    after = manager.get_current_messages()

    assert len(after) < len(before)  # 确实剪掉了东西
    assert after[0]["role"] == "system"  # system 还在
    assert any(m["role"] == "user" and "任务 0" in all_text([m]) for m in after)  # 第一个 user 还在

    # 尾部 47 条必须完全一致（顺序、内容、结构都不能变）
    assert after[-47:] == before[-47:]

    # 中间插入了一条"已裁剪"公告，且是 user 身份（模型对 user 说的接受度最高）
    notices = [m for m in after if "已裁剪中间" in all_text([m])]
    assert len(notices) == 1
    assert notices[0]["role"] == "user"

    print("✓ 通过")


def test_l2_cut_never_splits_tool_pair():
    """刀口落在 tool 消息上时，必须往前挪到它的 assistant（否则下一次请求 400）

    怎么构造出这种局面：
      每轮 3 条，len = 3R + 1 时 tail_start = 3R - 46，下标模 3 恰好落在 assistant；
      末尾再追加一条 user，整体偏移 1，tail_start 就正好压到一条 tool 上。
    这正是真实会话里最常见的形状（tool 结果紧跟在 assistant 后面）。
    """
    print("测试 L2: 刀口不拆工具对")

    manager = fresh_manager()
    build_long_history(manager, rounds=40)
    manager.add_message(mk_user("最后再问一句"))  # 制造偏移，让刀口落在 tool 上

    before = manager.get_current_messages()
    cut_position = len(before) - 47
    assert _role(before[cut_position]) == "tool", "构造失败：刀口没落在 tool 上，这个用例就失去意义了"

    assert manager._snip_oldest_round() is True
    after = manager.get_current_messages()

    assert orphan_tool_count(after) == 0  # 没有孤儿 tool
    # 刀口被往前挪了，所以尾巴至少保留 47 条（宁可多留，不能切坏）
    assert len(after) >= 47

    print("✓ 通过")


def test_l2_recomputes_usage():
    """剪完之后用量账本要重算，否则会连环触发压缩

    为什么必须重算：账本里还是"剪之前"那个巨大的数字的话，
    下一次 auto_messages_compression 一看还超阈值，又剪一次——
    一直剪到没得剪，白删一堆消息。
    """
    print("测试 L2: 剪后重算用量")

    manager = fresh_manager()
    build_long_history(manager, rounds=40, tool_size=4000)
    set_ratio(manager, 0.82)
    before_total = manager.history_token_usage[-1].total_tokens

    assert manager._snip_oldest_round() is True
    after_total = manager.history_token_usage[-1].total_tokens

    assert after_total < before_total  # 账本确实变小了
    assert after_total == manager.history_token_usage[-1].total_tokens

    print("✓ 通过")


# -----------------------------------------------------------------------------
# 四、L3：Microcompact 折叠旧工具结果（零成本，减单条体积）
# -----------------------------------------------------------------------------

def test_l3_no_checker_means_no_fold():
    """没注入"可重复查询"判断函数时，一条都不许折叠（保守优先）

    这是 L3 最重要的安全阀：判断不了"重跑有没有副作用"时，
    宁可多留 9000 字，也不能把 file_write 的结果折叠成"重跑即可"——
    模型照着重跑一次，文件就被重复改了一遍。
    """
    print("测试 L3: 未注入 checker 时保守不折叠")

    manager = fresh_manager()
    manager.set_repeatable_checker(None)
    build_long_history(manager, rounds=5, tool_size=MICROCOMPACT_TOOL_OUTPUT_CHARS + 1000)
    before = manager.get_current_messages()

    assert manager._microcompact_tool_outputs() is False
    assert manager.get_current_messages() == before

    print("✓ 通过")


def test_l3_folds_old_repeatable_only():
    """只折叠「较早 + 可重复查询 + 够大」的 tool 结果，最近 3 条与写操作不动

    四个条件缺一不可，正好对应四个常见误解：
      · 不限早晚 → 正在依据的结果被折掉，模型下一步就瞎了
      · 不限工具 → 写操作被折叠，重跑等于重复副作用
      · 不限大小 → 8000 字以下也折，白丢信息
      · 全折       → 等于把工具结果全删了，模型只能反复重跑
    """
    print("测试 L3: 只折叠较早的可重复结果")

    manager = fresh_manager()
    # file_read 可重复查询；file_write 有副作用。这份 checker 就是生产环境
    # ToolManager 注入的那个东西的等价物（工具类上的 is_repeatable 属性）
    manager.set_repeatable_checker(lambda name: name == "file_read")

    big = MICROCOMPACT_TOOL_OUTPUT_CHARS + 1000  # 9000 字，越过折叠线
    manager.add_message(mk_system())
    # 前两条：大 file_read，应该被折叠
    for i in range(2):
        manager.add_message(mk_user(f"读文件 {i}"))
        manager.add_message(mk_assistant("", call_id=f"r{i}", tool_name="file_read"))
        manager.add_message(mk_tool("OLD_READ_" + "x" * big, call_id=f"r{i}", name="file_read"))
    # 第三条：大 file_write，不许折叠
    manager.add_message(mk_user("写文件"))
    manager.add_message(mk_assistant("", call_id="w0", tool_name="file_write"))
    manager.add_message(mk_tool("WRITE_RESULT_" + "y" * big, call_id="w0", name="file_write"))
    # 后三条：大 file_read，但在"最近 3 条"窗口内，不许折叠
    recent_markers = []
    for i in range(3):
        marker = f"RECENT_READ_{i}_"
        recent_markers.append(marker)
        manager.add_message(mk_user(f"再读 {i}"))
        manager.add_message(mk_assistant("", call_id=f"c{i}", tool_name="file_read"))
        manager.add_message(mk_tool(marker + "z" * big, call_id=f"c{i}", name="file_read"))

    assert manager._microcompact_tool_outputs() is True
    messages = manager.get_current_messages()

    assert len(messages) == 1 + 2 * 3 + 3 + 3 * 3  # 条数一条没少（L3 不删消息）
    body = all_text(messages)

    assert "OLD_READ_" not in body  # 两条早期读结果被折掉
    assert body.count("已折叠") == 2  # 折叠占位正好两条
    assert "WRITE_RESULT_" in body  # 写操作结果原样保留
    assert all(marker in body for marker in recent_markers)  # 最近 3 条原样保留

    print("✓ 通过")


def test_l3_small_output_not_folded():
    """小于 8000 字的输出即使较早也不折（折了省不下空间，还丢信息）"""
    print("测试 L3: 小输出不折叠")

    manager = fresh_manager()
    manager.set_repeatable_checker(lambda name: True)  # 极端放行，验证"大小"这道闸单独有效
    manager.add_message(mk_system())
    for i in range(5):
        manager.add_message(mk_user(f"小查询 {i}"))
        manager.add_message(mk_assistant("", call_id=f"s{i}"))
        manager.add_message(mk_tool(f"SMALL_{i}", call_id=f"s{i}"))

    assert manager._microcompact_tool_outputs() is False
    assert "SMALL_0" in all_text(manager.get_current_messages())

    print("✓ 通过")


# -----------------------------------------------------------------------------
# 五、L4：Context Collapse 只读投影（零成本，不动存储）
# -----------------------------------------------------------------------------

def build_projection_history(manager, rounds=6):
    """造 6 轮对话，供 L4 投影使用（投影需要 > recent_rounds 轮才有得折叠）"""
    manager.add_message(mk_system("SYSTEM_RULE"))
    for i in range(rounds):
        manager.add_message(mk_user(f"第 {i} 轮目标"))
        manager.add_message(mk_assistant(f"第 {i} 轮思考", call_id=f"p{i}"))
        manager.add_message(mk_tool(f"第 {i} 轮工具输出", call_id=f"p{i}"))


def test_l4_projection_window():
    """投影只在 [0.90, 0.95) 生效；低于、高于都交给别人

    为什么边界要测：
      低于 0.90 时投影是白费（L3 已经够用，白白让模型少看细节）；
      高于 0.95 时投影必须退场，否则 L5 摘要永远等不到触发，
      上下文会一直接近爆掉却没人兜底。
    """
    print("测试 L4: 投影的触发窗口")

    manager = fresh_manager()
    build_projection_history(manager)

    set_ratio(manager, 0.89)
    assert manager.should_use_projection() is False  # 还没到 L4 档

    set_ratio(manager, 0.90)
    assert manager.should_use_projection() is True  # 下界含等号

    set_ratio(manager, 0.9499)
    assert manager.should_use_projection() is True  # 上界是开区间

    set_ratio(manager, 0.95)
    assert manager.should_use_projection() is False  # 交给 L5

    print("✓ 通过")


def test_l4_projection_does_not_touch_storage():
    """投影是"只读视图"：拿投影前后，底层存储必须一模一样

    这是 L4 与 L2/L3/L5 的本质区别，也是最容易被后人"优化"坏的地方——
    "反正都要压缩，顺手把存储也改了吧"→ 数据没了，再也 rewind 不回去。
    """
    print("测试 L4: 投影不改存储")

    manager = fresh_manager()
    build_projection_history(manager)
    before = copy.deepcopy(manager.get_current_messages())

    projection = manager.get_projection()

    assert manager.get_current_messages() == before  # 存储一字未动
    assert len(projection) < len(before)  # 视图确实变短了

    # 改动投影不能反向污染存储（返回值必须是深拷贝）
    projection[0]["content"] = [{"type": "text", "text": "被测试改坏了"}]
    assert manager.get_current_messages() == before

    print("✓ 通过")


def test_l4_projection_keeps_goals_drops_details():
    """投影保留：system、最近 3 轮完整、早期 user 的目标；丢弃：早期过程细节

    这条断言直接对应"投影"的设计意图：
      留结论/目标，去过程/细节。如果早期 tool 输出还在，那投影就没省下什么；
      如果早期 user 目标也丢了，模型会忘记"这一路到底在干什么"。
    """
    print("测试 L4: 投影留目标去细节")

    manager = fresh_manager()
    build_projection_history(manager, rounds=6)
    projection = manager.get_projection(recent_rounds=3)
    body = all_text(projection)

    assert projection[0]["role"] == "system"  # 规则必须留
    assert "SYSTEM_RULE" in body
    assert "第 0 轮目标" in body  # 早期 user 的目标以线索形式保留
    assert "第 0 轮思考" not in body  # 早期 assistant 的思考被丢掉
    assert "第 0 轮工具输出" not in body  # 早期工具细节被丢掉
    assert "第 5 轮思考" in body  # 最近一轮完整保留

    print("✓ 通过")


# -----------------------------------------------------------------------------
# 六、分档路由：同一个 auto_messages_compression 入口，按占用走不同的层
#
# 这一组是本次补测的核心：五层压缩最容易出的错不是"某层写错"，
# 而是"档位串了"——该走 L3 的时候去调了摘要模型（白花钱），
# 或该走 L5 的时候只做了投影（上下文没真降下来）。
# -----------------------------------------------------------------------------

def test_route_082_uses_snip():
    """占用 0.82 → L2：条数减少，且一次摘要都不调（零成本）"""
    print("测试 分档: 0.82 走 L2")

    manager = fresh_manager()
    build_long_history(manager, rounds=40)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.82)
    before_count = len(manager.get_current_messages())

    asyncio.run(manager.auto_messages_compression())

    assert len(manager.get_current_messages()) < before_count  # 条数变少 = L2 的特征
    assert calls["n"] == 0  # 没调模型

    print("✓ 通过")


def test_route_087_uses_microcompact():
    """占用 0.87 → L3：条数不变、旧工具输出被折叠，且不调模型

    为什么"条数不变"是关键判据：
      L2 和 L3 都会让上下文变小，但只有 L2 会减少消息条数。
      如果这条断言挂了（条数变少），说明 0.87 被错误地路由到了 L2，
      那 L3 就永远没有机会执行，五层退化成四层。
    """
    print("测试 分档: 0.87 走 L3")

    manager = fresh_manager()
    manager.set_repeatable_checker(lambda name: name == "file_read")
    build_long_history(manager, rounds=40, tool_size=MICROCOMPACT_TOOL_OUTPUT_CHARS + 500)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.87)
    before = manager.get_current_messages()

    asyncio.run(manager.auto_messages_compression())

    after = manager.get_current_messages()
    assert len(after) == len(before)  # 条数没变 = 不是 L2
    assert "已折叠" in all_text(after)  # 有折叠占位 = 走了 L3
    assert calls["n"] == 0  # 没调模型

    print("✓ 通过")


def test_route_092_uses_projection_only():
    """占用 0.92 → L4：存储一字不动、不调模型，但请求时会用投影

    为什么"存储不动"是正确行为而不是没实现：
      L4 的定位就是"不改数据、只换视角"。真正生效点在发请求那条路径
      （Conversation._get_messages_with_cache_mark 里读 should_use_projection）。
      这里同时验证两件事，缺一不可：存储没动 + 投影开关已打开。
    """
    print("测试 分档: 0.92 走 L4")

    manager = fresh_manager()
    build_projection_history(manager, rounds=8)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.92)
    before = copy.deepcopy(manager.get_current_messages())

    asyncio.run(manager.auto_messages_compression())

    assert manager.get_current_messages() == before  # 存储没动
    assert calls["n"] == 0  # 没调模型
    assert manager.should_use_projection() is True  # 投影已就绪

    print("✓ 通过")


def test_route_096_uses_summary():
    """占用 0.96 → L5：真的调一次摘要器，旧的一轮被摘掉（唯一花钱的层）"""
    print("测试 分档: 0.96 走 L5")

    manager = fresh_manager()
    build_projection_history(manager, rounds=8)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.96)
    before_count = len(manager.get_current_messages())

    asyncio.run(manager.auto_messages_compression())

    assert calls["n"] == 1  # 摘要器被调用且只调一次
    after = manager.get_current_messages()
    assert len(after) < before_count  # 老的一轮被删（真删存储）
    assert "测试摘要" in all_text(after)  # 摘要进了对话，模型不会真的忘事
    assert manager.should_use_projection() is False  # 交给 L5 后投影退场

    print("✓ 通过")


def test_route_080_boundary_not_compressed():
    """占用正好 0.80 → 不压（总闸是「大于」不是「大于等于」）

    这条边界很容易被后改成 >=，后果是：一进入 80% 就每轮都尝试压缩，
    而 80% 是设计上的"安全水位"，本来就该先不动。
    """
    print("测试 分档: 0.80 边界不压")

    manager = fresh_manager()
    build_long_history(manager, rounds=40)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.80)
    before = manager.get_current_messages()

    asyncio.run(manager.auto_messages_compression())

    assert len(manager.get_current_messages()) == len(before)
    assert calls["n"] == 0

    print("✓ 通过")


def test_same_usage_not_compressed_twice():
    """同一份用量不重复压（防连压机制）

    背景：_run_one_iteration 一轮里会调两次 auto_messages_compression
    （发请求前、收到回复后）。没有这个标记的话，一旦过线就会连续压，
    每次都真调一次摘要模型——钱花了、消息也被多删了。
    """
    print("测试 分档: 同用量不重复压")

    manager = fresh_manager()
    build_projection_history(manager, rounds=8)
    calls = inject_counting_summarizer(manager)
    set_ratio(manager, 0.96)

    asyncio.run(manager.auto_messages_compression())
    assert calls["n"] == 1

    asyncio.run(manager.auto_messages_compression())  # 同一份用量再调一次
    assert calls["n"] == 1  # 不该再压

    # 【第 25 轮改动】原来这里断言"真来了新用量，标记清掉，允许再压一次"（== 2）。
    #   第 25 轮把 L5 改成"保留最近 10 条、其余整段一次压完"之后，这个预期失效了——
    #   压完一次，保留区之外已经没有任何内容可压，再触发会走
    #   reason="only_previous_summary" 直接 return（**不调模型、不花钱**），
    #   这正是那次改动想要的：L5 是"一压到底"，不是"每轮压一点"。
    #   所以这里改成断言"新用量也骗不出第二次摘要调用"——验的目标从
    #   "标记会不会清"变成"清掉之后也没东西可压"，两层都有价值。
    manager.update_token_usage(mock_sdk_usage(int(manager._model_max_tokens * 0.96)))
    asyncio.run(manager.auto_messages_compression())
    assert calls["n"] == 1  # 已经压到底了：新用量不会再调一次摘要

    print("✓ 通过")


def inject_counting_summarizer(manager):
    """注入一个"只计数、不联网"的假摘要器

    为什么不用真的摘要器：
      这里测的是"路由对不对"，不是"摘要写得好不好"。真摘要要发一次
      请求、花一次钱，而且会让测试依赖网络和 API Key。
      假函数签名一致（async，收 messages，返回 str），压缩链完全分辨不出来。
    """
    calls = {"n": 0}

    async def fake_summarizer(_messages):
        calls["n"] += 1
        return "测试摘要"

    manager.set_summarizer(fake_summarizer)
    return calls


# -----------------------------------------------------------------------------
# 七、跑全部
# -----------------------------------------------------------------------------

def run_all_tests():
    """逐个跑，统计通过数；有任何失败就用非 0 退出码退出

    为什么这里一定要 sys.exit(1)：
      老测试（test_history_compress.py）的 run_all_tests 捕获异常后只打印，
      进程退出码仍然是 0。批量跑测试时根本看不出它挂了——
      这次补测顺手把这个坑记住：新文件的失败必须让 CI / 脚本感知到。
    """
    print("开始运行五层压缩测试（全程离线，不调模型）...\n")

    cases = [
        test_l1_short_output_untouched,
        test_l1_long_output_keeps_head_and_tail,
        test_l1_boundary_not_truncated,
        test_l2_too_short_untouched,
        test_l2_keeps_head_and_tail,
        test_l2_cut_never_splits_tool_pair,
        test_l2_recomputes_usage,
        test_l3_no_checker_means_no_fold,
        test_l3_folds_old_repeatable_only,
        test_l3_small_output_not_folded,
        test_l4_projection_window,
        test_l4_projection_does_not_touch_storage,
        test_l4_projection_keeps_goals_drops_details,
        test_route_082_uses_snip,
        test_route_087_uses_microcompact,
        test_route_092_uses_projection_only,
        test_route_096_uses_summary,
        test_route_080_boundary_not_compressed,
        test_same_usage_not_compressed_twice,
    ]

    failures = []
    for case in cases:
        try:
            case()
        except Exception as e:  # 任何一个用例挂了都记下来，继续跑后面
            failures.append((case.__name__, e))
            print(f"❌ {case.__name__} 失败：{e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    if failures:
        print(f"❌ {len(failures)} 个用例失败（通过 {len(cases) - len(failures)}/{len(cases)}）")
        for name, _ in failures:
            print(f"   - {name}")
        sys.exit(1)  # 关键：让调用方知道失败了

    print(f"🎉 所有测试通过（{len(cases)}/{len(cases)}）")


if __name__ == "__main__":
    run_all_tests()
