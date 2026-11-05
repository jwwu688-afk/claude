# =============================================================================
# 文件：test/test_crop_message.py
# 作用：测试 HistoryManager.crop_message（裁剪聊天记录）这个函数是否正常。
#
# 它不是一个"正式"的测试框架写法，而是一堆"函数 + assert"的组合：
#   assert 是"断言"，意思是"我要求这里必须是真的"，
#   如果条件不成立，程序会立刻抛出 AssertionError 报错——那就说明测试失败。
#
# 直接运行方式：在项目根目录执行 python test/test_crop_message.py
# 它会依次跑 8 个用例，最后打印"所有测试通过"或"测试失败"。
#
# 注意：这里用的是"假消息"（一个简单的字典），并不是真的调模型，
# 所以跑起来很快、也不花钱。
# =============================================================================

import sys  # sys 库：用来改"模块搜索路径"
import os  # os 库：用来拼接路径
import traceback  # 用来打印完整的错误现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# 上一行很关键：因为我们是从 test/ 文件夹里跑脚本，Python 默认找不到 src/ 下的模块，
# 所以手动把 "../src" 这个目录塞进搜索路径的第一位（insert(0, ...)）
# __file__ 是"当前这个文件的路径"，dirname 取它所在的文件夹


from core.history.history_manager import HistoryManager, Role, Crop_Direction  # 导入要测的三样东西


def create_mock_message(role: Role, content: str) -> dict:
    """Create a mock message dictionary compatible with crop_message function（造一条假消息）"""
    return {'role': role, 'content': content}  # 直接返回一个字典，省得每次都手写


def setup_history_manager():
    """Setup a fresh HistoryManager instance for testing（准备一个干净的历史管理器）"""
    # Reset singleton instance to ensure clean state
    HistoryManager._instance = None  # 把单例清空（否则每个测试会共用同一个，互相污染）
    HistoryManager._initialized = False  # 初始化标记也清空，这样下一次才会真的重新初始化
    return HistoryManager(model_max_tokens=100, compress_threshold=0.8)
    # 造一个新的，上限设成 100（但注意：类里会优先读环境变量，实际可能还是 200）


def test_crop_insufficient_messages():
    """Test cropping when there are insufficient messages（测试：消息太少时应该拒绝裁剪）"""
    print("测试: 消息数量不足时的裁剪")

    manager = setup_history_manager()  # 准备干净的管理器

    # Test with no messages
    result = manager.crop_message(Crop_Direction.TOP, 1)  # 一条消息都没有时，试着删 1 条
    assert result == "Cannot crop: insufficient messages"  # 要求返回"消息不够"，否则测试失败

    # Test with only one message
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))  # 现在加 1 条
    result = manager.crop_message(Crop_Direction.TOP, 1)  # 再试着删
    assert result == "Cannot crop: insufficient messages"  # 还是应该拒绝

    print("✓ 通过: 消息数量不足时正确拒绝裁剪")


def test_crop_invalid_amount():
    """Test cropping with invalid crop amount（测试：删除数量不合理时应拒绝）"""
    print("\n测试: 无效裁剪数量")

    manager = setup_history_manager()

    # Add 3 messages - with crop_amount=2, check becomes 3 < 2+2 = true, so invalid
    # 翻译：加 3 条消息后，删 2 条会触发"剩余消息不够"的保护（因为 3 < 2+2 成立）
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.USER, "User message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message"))


    result = manager.crop_message(Crop_Direction.TOP, 2)  # 试着删 2 条
    assert result == "Cannot crop: invalid crop amount"  # 要求被拒绝

    print("✓ 通过: 无效裁剪数量时正确拒绝")


def test_crop_no_user_messages():
    """Test cropping when no user messages exist（测试：一条用户消息都没有时应拒绝）"""
    print("\n测试: 没有用户消息时的裁剪")

    manager = setup_history_manager()

    # Add 5 messages with no user messages to pass the count check
    # 翻译：加 5 条但没有一条是用户说的，这样能通过"数量检查"，走到"找不到用户消息"那一关
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Tool message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Another assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Another tool message"))

    result = manager.crop_message(Crop_Direction.TOP, 1)  # 试着删 1 条
    assert result == "Cannot crop: no user messages found"  # 要求返回"没找到用户消息"

    print("✓ 通过: 没有用户消息时正确拒绝裁剪")


def test_crop_would_remove_latest_user_message():
    """Test cropping that would remove the latest user message（测试：不许删掉用户最新说的那句）"""
    print("\n测试: 尝试裁剪最新用户消息时的保护")

    manager = setup_history_manager()

    # Add 8 messages with user message in the middle
    # 翻译：加 8 条，用户消息放在中间（第 5 条，索引是 4）
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message 1"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message 2"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message 3"))
    manager.add_message(create_mock_message(Role.USER, "Latest user message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Final assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Tool message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Another assistant message"))

    # Try to crop 5 messages from top, which would remove the user message at index 4
    # 翻译：从顶部删 5 条，就会把索引 4 那条用户消息删掉——必须被拦住
    result = manager.crop_message(Crop_Direction.TOP, 5)
    assert result == "Cannot crop: can't crop the latest user message"  # 要求拒绝

    print("✓ 通过: 正确保护最新用户消息不被裁剪")


def test_crop_top_success():
    """Test successful top cropping（测试：从顶部正常裁剪）"""
    print("\n测试: 从顶部成功裁剪")

    manager = setup_history_manager()

    # Add 6 messages to have enough for cropping
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Old assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Old tool message"))
    manager.add_message(create_mock_message(Role.USER, "User message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Recent assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Recent tool message"))

    # Crop 2 messages from top
    result = manager.crop_message(Crop_Direction.TOP, 2)  # 从顶部删 2 条（会删掉前两条非系统消息）
    assert result == "Crop message successful"  # 要求成功

    messages = manager.get_current_messages()  # 看看删完以后剩下什么

    # Should have 4 messages left (system + user + 2 recent messages)
    # 注意：注释说剩 4 条，但下面的 assert 要求 5 条。
    # 实际逻辑是"系统消息单独挑出来又拼回去了"，所以总条数没减少那么多。
    # 注释和断言对不上，说明原作者改过实现但忘了改注释。
    assert len(messages) == 5

    # Should preserve system messages
    system_messages = [msg for msg in messages if msg['role'] == Role.SYSTEM]  # 挑出所有系统消息
    assert len(system_messages) == 1  # 应该还剩 1 条
    assert system_messages[0]['content'] == "System message"  # 而且内容没变

    # Should preserve user message and later messages
    assert messages[2]['content'] == "User message"  # 第 3 条应该是用户消息（说明它前面的都被删了）
    assert messages[3]['content'] == "Recent assistant message"  # 第 4 条
    assert messages[4]['content'] == "Recent tool message"  # 第 5 条

    print("✓ 通过: 从顶部成功裁剪，保留系统消息和用户消息后的内容")


def test_crop_bottom_success():
    """Test successful bottom cropping（测试：从底部正常裁剪）"""
    print("\n测试: 从底部成功裁剪")

    manager = setup_history_manager()

    # Add multiple messages
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.USER, "User message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message 1"))
    manager.add_message(create_mock_message(Role.TOOL, "Tool message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message 2"))

    # Crop 2 messages from bottom
    result = manager.crop_message(Crop_Direction.BOTTOM, 2)  # 从尾部删 2 条
    assert result == "Crop message successful"

    messages = manager.get_current_messages()

    # Should have 3 messages left
    assert len(messages) == 3  # 从 5 条删掉最后 2 条，应该剩 3 条

    # Should preserve user message (cannot be cropped)
    user_messages = [msg for msg in messages if msg['role'] == Role.USER]
    assert len(user_messages) == 1  # 用户消息还在
    assert user_messages[0]['content'] == "User message"

    # Should preserve earlier messages
    assert messages[0]['content'] == "System message"  # 第 1 条
    assert messages[1]['content'] == "User message"  # 第 2 条
    assert messages[2]['content'] == "Assistant message 1"  # 第 3 条

    print("✓ 通过: 从底部成功裁剪，保护最新用户消息")


def test_crop_multiple_user_messages():
    """Test cropping with multiple user messages（测试：用户说过好几轮的情况）"""
    print("\n测试: 多个用户消息的裁剪")

    manager = setup_history_manager()

    # Add messages with multiple user messages
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.USER, "First user message"))  # 第 1 轮
    manager.add_message(create_mock_message(Role.ASSISTANT, "First assistant response"))
    manager.add_message(create_mock_message(Role.USER, "Second user message"))  # 第 2 轮
    manager.add_message(create_mock_message(Role.ASSISTANT, "Second assistant response"))
    manager.add_message(create_mock_message(Role.USER, "Latest user message"))  # 第 3 轮（最新）
    manager.add_message(create_mock_message(Role.ASSISTANT, "Latest assistant response"))

    # Crop from top - latest user is at index 5, so we can crop up to 5 messages
    result = manager.crop_message(Crop_Direction.TOP, 2)  # 删最前面的 2 条（系统消息会被保住）
    assert result == "Crop message successful"

    messages = manager.get_current_messages()

    # Should preserve system message and content after cropping
    assert messages[0]['role'] == Role.SYSTEM  # 第 1 条永远是系统消息

    # Should preserve the latest user message
    user_messages = [msg for msg in messages if msg['role'] == Role.USER]  # 挑出所有用户消息
    latest_user = user_messages[-1]  # 取最后一个（最新的）
    assert latest_user['content'] == "Latest user message"  # 确认最新的那句还在

    print("✓ 通过: 多用户消息场景下正确裁剪")


def test_crop_edge_cases():
    """Test edge cases for cropping（测试：边界情况，刚好够删或刚好不够删）"""
    print("\n测试: 边界情况")

    manager = setup_history_manager()

    # Add minimum viable messages for cropping (5 messages)
    manager.add_message(create_mock_message(Role.SYSTEM, "System message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Assistant message"))
    manager.add_message(create_mock_message(Role.USER, "User message"))
    manager.add_message(create_mock_message(Role.ASSISTANT, "Final assistant message"))
    manager.add_message(create_mock_message(Role.TOOL, "Tool message"))

    # Test maximum valid crop amount from top
    result = manager.crop_message(Crop_Direction.TOP, 1)  # 只删 1 条，这是下限
    assert result == "Crop message successful"

    messages = manager.get_current_messages()
    assert len(messages) == 5  # system + user + 剩下的（系统消息被保住了，所以还是 5 条）

    print("✓ 通过: 边界情况处理正确")


def run_all_tests():
    """运行所有测试（把上面 8 个用例挨个跑一遍）"""
    print("开始运行 crop_message 函数测试...\n")

    try:  # 整体包一层 try，任何一个用例失败都能被抓住
        test_crop_insufficient_messages()  # 用例 1
        test_crop_invalid_amount()  # 用例 2
        test_crop_no_user_messages()  # 用例 3
        test_crop_would_remove_latest_user_message()  # 用例 4
        test_crop_top_success()  # 用例 5
        test_crop_bottom_success()  # 用例 6
        test_crop_multiple_user_messages()  # 用例 7
        test_crop_edge_cases()  # 用例 8

        print("\n🎉 所有测试通过!")  # 全过了才打这句
    except Exception as e:  # 任何一个失败都会跳到这里
        print(f"\n❌ 测试失败: {e}")  # 打印失败原因
        traceback.print_exc()  # 打印完整的出错现场（哪一行出的问题）


if __name__ == "__main__":
    # 直接运行这个文件时才执行（被 import 时不执行）
    run_all_tests()  # 开跑
