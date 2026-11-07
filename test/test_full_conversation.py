#!/usr/bin/env python3
# ↑ 上面这行叫 shebang（读作"协邦"），在 Linux/Mac 上表示"用 python3 来运行这个文件"。
#   在 Windows 上没什么用，只是习惯性保留。
# =============================================================================
# 文件：test/test_full_conversation.py
# 作用：检查"对话总管"能不能正常把流式显示跑起来。
#
# 它**不会**真的去问模型问题（因为那要花钱、要联网），
# 只检查两件事：
#   1. 相关的方法是不是都存在
#   2. 把一段文字逐字"流式"打印出来，看效果对不对
#
# 所以它更像一个"冒烟测试"：只是确认机器没坏，不验证业务逻辑。
# =============================================================================

"""
Test the full conversation flow with streaming（测试完整的对话流程，带流式输出）
"""
import sys  # 改模块搜索路径
import os  # 拼路径
import asyncio  # 用 asyncio.run 来启动异步函数

# Add src directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))  # 拿到当前文件所在文件夹的绝对路径
src_path = os.path.join(current_dir, '../src')  # 拼出 ../src 这个目录
sys.path.insert(0, src_path)  # 把它插到搜索路径最前面

# 【2026-11-07 第 13.1 轮】构造 Conversation 会顺带构造 APIClient，
# 而 APIClient 要求三个环境变量齐全，缺一个就抛 ValueError。
# 这里用 setdefault 给一份假配置：不覆盖 .env 或系统里已有的真实值，
# 而且本测试全程不发网络请求（只检查方法存在 + 本地流式打印）。
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-unit-test")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from core.conversation import Conversation  # 导入对话总管


async def test_conversation_integration():
    """Test that conversation class can handle streaming properly（检查对话类能不能处理流式输出）"""
    print("🧪 Testing conversation streaming integration...")  # 打印"开始测试"

    conv = None  # 【第 13.1 轮】提前声明：finally 里要用它，而 Conversation() 自己也可能抛错
    try:  # 整体包一层 try，出错就打印原因并返回 False
        conv = Conversation()  # 创建对话总管（单例，全程序一个）

        # Test the streaming message handling directly
        # 【2026-11-07 第 13.1 轮】改成通过 add_message 往里加。
        #   原来写的是 `conv.messages = [...]`，但 messages 是只读 property
        #   （内部返回 get_current_messages() 的深拷贝，就是为了防止外部直接改），
        #   赋值必然抛 AttributeError，所以这个冒烟测试从来没跑通过——
        #   报的还是一个"can't set attribute"，看着像环境问题，实际是测试自己写错。
        conv.add_message({"role": "system", "content": "你是一个乐于助人的助手。"})  # 系统设定
        conv.add_message({"role": "user", "content": "简短地跟我打个招呼"})  # 用户请求

        print("📡 Testing recursive_message_handling with streaming...")  # 打印进度

        # Simulate one round of message handling
        # This should use streaming internally
        # 翻译：想模拟一轮完整的消息处理（但下面的代码并没有真的调用它）

        # We can't easily test the full interactive flow without user input,
        # but we can test that the methods exist and work
        # 翻译：没有用户输入的话，没法完整测交互流程，所以退一步——只检查方法存不存在

        # Test that streaming methods exist
        api_client = conv._api_client  # 从对话总管里取出 API 客户端
        if hasattr(api_client, 'get_completion_stream'):  # hasattr 检查"这个对象有没有这个方法"
            print("✅ Streaming API method available")  # 有，就打个对勾
        else:  # 没有
            print("❌ Streaming API method missing")  # 打叉
            return False  # 返回失败

        if hasattr(conv, 'print_streaming_content'):  # 再检查另一个方法
            print("✅ Streaming print method available")
        else:
            print("❌ Streaming print method missing")
            return False

        # Test streaming content printing
        print("📝 Testing streaming content display:")
        print("🤖 ", end="", flush=True)
        # end="" 表示"打印完不换行"（默认会换行）
        # flush=True 表示"立刻输出，不要攒在缓冲区里"，这样才看得到逐字效果

        test_content = "This is a test of streaming content display."  # 要"流式"打印的测试文字
        for char in test_content:  # 一个字一个字地遍历
            conv.print_streaming_content(char)  # 显示这一个字
            await asyncio.sleep(0.01)  # 停 0.01 秒，模拟模型吐字的间隔（不然刷得太快看不见）
        print()  # New line（最后换一行）

        print("🎉 Conversation streaming integration test passed!")
        return True  # 返回成功

    except Exception as e:  # 任何意外
        print(f"\n❌ Conversation integration test failed: {e}")  # 打印失败原因
        import traceback  # 这里才导入 traceback（一般应该放文件开头）
        traceback.print_exc()  # 打印完整的出错现场
        return False

    finally:
        # 【2026-11-07 第 13.1 轮】把 MCP 后台任务收掉。
        #   构造 Conversation 会顺带建 ToolManager → MCPClient，后者起了一个
        #   "连接 → 待命 → 关闭"的后台任务。不收尾的话，asyncio.run 结束时
        #   它是被"从外部取消"的，anyio 会在另一个任务里关闭 cancel scope，于是打印：
        #     RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
        #   测试结果是过了，但屏幕上那段红字会被当成真的失败。
        #   main.py 里的 `finally: await conversation.shutdown()` 解决的是同一件事。
        if conv is not None and hasattr(conv, "shutdown"):
            try:
                await conv.shutdown()
            except Exception:  # 收尾失败不该改变测试结论
                pass


if __name__ == "__main__":
    success = asyncio.run(test_conversation_integration())
    # asyncio.run 负责启动异步世界，把上面那个 async 函数跑完，拿到它的返回值
    sys.exit(0 if success else 1)
    # sys.exit 用来设置"退出码"：0 表示成功，1 表示失败。
    # 这样在自动化脚本里，别人能根据退出码判断这个测试过没过。
