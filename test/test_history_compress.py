# =============================================================================
# 文件：test/test_history_compress.py
# 作用：测试"上下文自动压缩"功能是否正常。
#
# 三个核心用例：
#   1. token 没超阈值时，什么都不该删
#   2. 多轮对话时长，最老的那一轮应该被整个删掉
#   3. 只有一轮对话时长，只删紧跟在用户发言后面的几条
#
# ⚠️ 一个重要的坑（看懂这个你就理解了前面代码的一个 bug）：
#   这个测试用的是 MockMessage 这个"类"（有 .role 属性），
#   而真实的对话流程存的是"字典"（要用 ["role"] 取值）。
#   因为测试用了类，所以测试能过；但真实运行时一压缩就会 AttributeError 崩掉。
#   也就是说：这个测试给了人"压缩功能正常"的错觉。
#
# 【2026-11-06 优化后的状态】
#   1. history_manager 已改成 _role_of() 统一读取，字典和对象都能用，
#      所以"真实运行时崩溃"的问题已经修掉（见优化文档第 1 轮）。
#   2. 本文件原来因为单例状态串味，跑起来必然失败；
#      现在每个用例都通过 fresh_manager() 拿全新实例，可以独立运行了。
# =============================================================================

import asyncio  # 【2026-11-06 第 2 轮新增】压缩方法已改成异步，这里要用 asyncio.run 来驱动
import sys  # 改模块搜索路径
import os  # 拼路径
from dataclasses import dataclass  # 用来自动生成简单数据类
import traceback  # 打印错误现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# 把 src 目录加进搜索路径，这样下面才能 import core.xxx

# Import directly from the history module to avoid other dependencies
from core.history.history_manager import HistoryManager, TokenUsage, Role  # 导入要测的东西


@dataclass
class MockMessage:
    # 一个"假消息"类：只有角色和内容两个字段
    # 注意：真实代码里存的是字典，这里用类是为了方便（history_manager 里已兼容两种形态）
    role: Role  # 谁说的
    content: str  # 说了什么


def fresh_manager(model_max_tokens=100, compress_threshold=0.8):
    """每个用例都拿一个全新的管理器（2026-11-06 新增）

    为什么要重置？
      HistoryManager 是单例，整个进程只有一个。
      如果不在用例之间重置，上一个用例塞进去的消息会留到下一个用例里，
      导致条数、用户消息数量全部对不上，测试就会莫名其妙地失败。
      （本文件原来就有这个毛病，跑起来必然挂掉。）
    """
    HistoryManager._instance = None  # 丢掉旧实例
    HistoryManager._initialized = False  # 清掉初始化标记，逼它重新初始化
    return HistoryManager(model_max_tokens=model_max_tokens, compress_threshold=compress_threshold)


def test_auto_compression_not_triggered():
    """测试当不满足压缩条件时，不会触发压缩"""
    print("测试: 不满足压缩条件时不触发压缩")

    manager = fresh_manager(model_max_tokens=100)  # 换成"全新管理器"（原来直接 new，会带上上个用例的数据）

    # 添加一些消息
    manager.add_message(MockMessage(Role.SYSTEM, "You are a helpful assistant"))  # 系统消息
    manager.add_message(MockMessage(Role.USER, "Hello"))  # 用户
    manager.add_message(MockMessage(Role.ASSISTANT, "Hi there!"))  # 助手

    # 创建模拟的token使用量对象
    class MockTokenUsage:
        # 在函数内部临时定义一个类，模仿接口返回的用量对象（字段名要跟真的一样）
        def __init__(self, prompt, completion, total):  # 初始化
            self.prompt_tokens = prompt  # 输入用量
            self.completion_tokens = completion  # 输出用量
            self.total_tokens = total  # 总量

    # 更新token使用量，但不超过阈值
    manager.update_token_usage(MockTokenUsage(50, 30, 80))  # 80 < 80% of 100*1024
    # 翻译：只用了 80，远远不到上限的 80%，所以不该压缩

    original_length = len(manager.get_current_messages())  # 记下现在的消息条数

    # 执行自动压缩
    asyncio.run(manager.auto_messages_compression())  # 调用自动压缩（内部会先判断要不要压）
    # 【2026-11-06 第 2 轮】压缩现在是异步方法（因为可能要调模型生成摘要），所以用 asyncio.run 驱动

    # 验证消息数量没有变化
    assert len(manager.get_current_messages()) == original_length  # 条数应该一模一样
    print("✓ 通过: 消息数量未改变")


def test_auto_compression_multiple_sessions():
    """测试多会话压缩（用户说过好几轮时，把最早那段压成摘要）

    【第 25 轮改动】L5 从"每次删最老一轮"改成"保留最近 10 条
      （COMPACT_KEEP_RECENT）、其余整段一次压完"。

      这个用例原来只造了 2 个会话共 5 条消息（+ system = 6 条），
      全部落在保留区里，于是压缩会打印"没有可压缩的较早对话"然后一条不动——
      断言"只剩一句用户消息"必然失败。看起来像压缩坏了，实际是数据量没跟上新阈值。

      现在造 6 轮（12 条 + system = 13 条），比保留区长，压完才会剩下
      "公告 + 最近 10 条"。断言的写法也跟着换成**不依赖保留区具体条数**的版本：
      只钉"最老那句没了、最新那句还在、总条数确实变少、公告有且只有一条"。
      这样以后 COMPACT_KEEP_RECENT 从 10 调成 8 或 12，这个用例仍然有效。
    """
    print("\n测试: 多会话压缩")

    manager = fresh_manager(model_max_tokens=10, compress_threshold=0.8)  # 上限故意设很小

    # 添加系统消息
    manager.add_message(MockMessage(Role.SYSTEM, "You are a helpful assistant"))

    # 6 轮问答：第 1~2 轮会落进"被压掉"的那一段，第 6 轮一定在保留区里
    for round_index in range(1, 7):
        manager.add_message(MockMessage(Role.USER, f"Question {round_index}"))
        manager.add_message(MockMessage(Role.ASSISTANT, f"Answer {round_index}"))

    # 更新token使用量，超过阈值
    class MockTokenUsage:
        def __init__(self, prompt, completion, total):  # 同上，一个假的用量对象
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    # 【2026-11-09 五层压缩后调整】原来硬写 170000（占用 0.83）。
    #   旧逻辑"过 80% 就调摘要模型删一轮"，所以 0.83 必然走摘要；
    #   现在 0.83 落在 L2（Snip 剪中间）档，而 L2 要求"至少 50 条消息"才有得剪——
    #   本用例只有 7 条，剪不动，于是什么都不发生，断言自然失败。
    #   用例要验证的是 L5「删最老一段 + 摘要」的语义，所以把占用推到 0.96 档。
    #   用 _model_max_tokens 现算而不是写死数字：MODEL_MAX_TOKENS 从 .env 读，
    #   写死会让这个用例在别人机器上莫名其妙地挂。
    heavy_total = int(manager._model_max_tokens * 0.96)  # 0.96 → L5 档
    manager.update_token_usage(MockTokenUsage(heavy_total - 60000, 60000, heavy_total))

    # 【第 25 轮改动】L5 现在"摘要拿不到就一条都不动"（宁可多留，也不静默删）。
    #   这个用例原来没注入摘要器，靠的是旧版"没有摘要器 → 直接删最老一轮"的兜底；
    #   那条兜底跟着第 25 轮一起删了，所以必须给一个假摘要器，
    #   否则压缩会返回 reason="summarize_failed"，历史一条都不会变。
    async def fake_summarizer(msgs):
        return "之前聊过 Question 1 到 Question 2 的内容，细节已省略。"

    manager.set_summarizer(fake_summarizer)

    # Check if compression is required
    print(f"Requires compression: {manager._requires_compression()}")  # 打印：需不需要压缩
    print(f"Threshold: {manager._compress_threshold * manager._model_max_tokens}")  # 打印：阈值到底是多少
    print(f"Current usage: {manager.history_token_usage[-1].total_tokens}")  # 打印：当前用了多少
    # 这三行是调试输出，帮你确认"判断条件到底对不对"

    before_count = len(manager.get_current_messages())  # 压缩前条数，用来验"真的变少了"

    # 执行自动压缩
    asyncio.run(manager.auto_messages_compression())

    messages = manager.get_current_messages()  # 看压缩后的结果

    # Debug: Print all messages to see what happened
    print(f"Total messages after compression: {len(messages)}")  # 剩几条
    for i, msg in enumerate(messages):  # 逐条打印，方便肉眼检查
        print(f"  {i}: {msg.role} - {msg.content}")

    # 挑出"用户消息"，但排除掉那句"历史已压缩"的公告（公告也是以用户身份发的）
    user_messages = [msg for msg in messages if msg.role == Role.USER and "压缩" not in msg.content]
    contents = [msg.content for msg in user_messages]
    print(f"User messages found: {len(user_messages)}")

    assert len(messages) < before_count, f"压缩后条数没变：{before_count} → {len(messages)}"
    assert "Question 1" not in contents, f"最老的那句应该被压掉了，实际还在：{contents}"
    assert "Question 6" in contents, f"最新那句必须留着，实际：{contents}"

    # 验证有压缩通知
    compression_notices = [msg for msg in messages if "压缩" in msg.content]
    # 挑出那条公告
    assert len(compression_notices) == 1, f"公告应该只有一条，实际 {len(compression_notices)} 条"

    print("✓ 通过: 最早的一段被压成摘要，最近的对话完整保留")


def test_auto_compression_single_session():
    """测试单会话压缩（只有一轮对话时，只删紧跟用户发言后的几条）"""
    print("\n测试: 单会话压缩")

    manager = fresh_manager(model_max_tokens=10, compress_threshold=0.8)

    # 添加系统消息
    manager.add_message(MockMessage(Role.SYSTEM, "You are a helpful assistant"))

    # 单个会话，多个assistant/tool消息
    manager.add_message(MockMessage(Role.USER, "Complex question"))  # 用户只问了一句
    manager.add_message(MockMessage(Role.ASSISTANT, "First response"))  # 后面全是来回调工具
    manager.add_message(MockMessage(Role.TOOL, "Tool call 1"))
    manager.add_message(MockMessage(Role.ASSISTANT, "Second response"))
    manager.add_message(MockMessage(Role.TOOL, "Tool call 2"))
    manager.add_message(MockMessage(Role.ASSISTANT, "Final response"))

    # 更新token使用量，超过阈值
    class MockTokenUsage:
        def __init__(self, prompt, completion, total):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    # 同用例 2：0.96 才落在 L5 档（会调摘要、删紧跟在用户后面的几条）。
    heavy_total = int(manager._model_max_tokens * 0.96)
    manager.update_token_usage(MockTokenUsage(heavy_total - 60000, 60000, heavy_total))  # 超阈值

    original_length = len(manager.get_current_messages())  # 记下原条数

    # 执行自动压缩
    asyncio.run(manager.auto_messages_compression())

    messages = manager.get_current_messages()

    # 【第 25 轮改动】这个用例的场景是"只有一句用户指令、后面全是工具来回"。
    #   在旧版"删最老一轮"的语义下，它能把 user 后面那几条 assistant/tool 删掉，
    #   所以断言"条数变少 + 公告一条"。
    #
    #   第 25 轮之后，L5 的刀口有两道护栏：
    #     · 不允许越过最后一条用户消息（否则模型忘了当前要做什么）；
    #     · 不允许越过第一条实质消息。
    #   6 条消息本来就没超出保留区（10 条），刀口直接落到 0 → 返回
    #   reason="nothing_to_compress"，**一条都不动**。这是设计意图，不是回归：
    #   唯一的用户指令被压掉等于把任务弄丢（这种情况该用 L3 折叠工具结果）。
    #
    #   所以这里把断言反过来写：条数一条不变、唯一那句指令还在、没有公告。
    #   这样将来如果谁把护栏去掉了，这条用例会立刻红，而不是悄悄通过。
    assert len(messages) == original_length, f"不该压：{original_length} → {len(messages)}"

    # 验证用户消息保留
    user_messages = [msg for msg in messages if msg.role == Role.USER and "压缩" not in msg.content]
    assert len(user_messages) == 1  # 用户那句还在
    assert user_messages[0].content == "Complex question"  # 内容没变

    # 没有压 → 不该有公告（公告本身也会占一条消息、还会让模型以为历史被删过）
    compression_notices = [msg for msg in messages if "压缩" in msg.content]
    assert len(compression_notices) == 0, f"没压却插了公告：{compression_notices}"

    print("✓ 通过: 单会话长工具链不压 L5，唯一的用户指令完整保留")


def test_token_usage_update():
    """测试token使用量更新（记账是否正确）"""
    print("\n测试: Token使用量更新")

    manager = fresh_manager(model_max_tokens=100)  # 造一个全新的管理器

    # 模拟token使用量对象（类似OpenAI的返回）
    class MockTokenUsage:
        def __init__(self, prompt, completion, total):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    mock_usage = MockTokenUsage(100, 50, 150)  # 假装用了 100 输入 + 50 输出 = 150
    manager.update_token_usage(mock_usage)  # 记账

    assert len(manager.history_token_usage) == 1  # 应该有 1 条记录
    usage = manager.history_token_usage[0]  # 取出来
    assert usage.input_tokens == 100  # 输入对不对
    assert usage.output_tokens == 50  # 输出对不对
    assert usage.total_tokens == 150  # 总数对不对

    print("✓ 通过: Token使用量正确更新")


def test_requires_compression_logic():
    """测试压缩条件判断逻辑（什么时候该压、什么时候不该压）"""
    print("\n测试: 压缩条件判断")

    manager = fresh_manager(model_max_tokens=100, compress_threshold=0.8)

    # 没有token使用记录时
    assert not manager._requires_compression()  # 还没用过，当然不用压

    # token使用量低于阈值时
    class MockTokenUsage:
        def __init__(self, prompt, completion, total):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    manager.update_token_usage(MockTokenUsage(30, 20, 50))  # 只用了 50
    assert not manager._requires_compression()  # 远远没到阈值，不压

    # token使用量超过阈值时
    manager.update_token_usage(MockTokenUsage(100000, 70000, 170000))  # 170000 > 80% of 200*1024
    assert manager._requires_compression()  # 超了，该压了

    print("✓ 通过: 压缩条件判断逻辑正确")


def run_all_tests():
    """运行所有测试"""
    print("开始运行历史压缩测试...\n")

    try:  # 整体包一层
        test_auto_compression_not_triggered()  # 用例 1
        test_auto_compression_multiple_sessions()  # 用例 2
        test_auto_compression_single_session()  # 用例 3
        test_token_usage_update()  # 用例 4
        test_requires_compression_logic()  # 用例 5

        print("\n🎉 所有测试通过!")
    except Exception as e:  # 任何一个挂了
        print(f"\n❌ 测试失败: {e}")
        traceback.print_exc()  # 打印现场
        # 【2026-11-09 补测时新增】必须用非 0 退出码结束。
        #   原来这里只打印就结束了，进程退出码仍然是 0——批量跑测试的脚本
        #   （以及 CI）根本看不出这个文件挂了。五层压缩改行为之后，
        #   本文件的两个用例其实一直在失败，却因为退出码是 0 被当成"全绿"，
        #   直到按输出内容逐行检查才发现。失败必须让调用方感知到。
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()  # 直接运行本文件时开跑
