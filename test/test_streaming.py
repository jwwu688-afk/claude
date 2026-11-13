#!/usr/bin/env python3
# 表示"用 python3 运行本文件"（Windows 上没实际作用）
# =============================================================================
# 文件：test/test_streaming.py
# 作用：测试"流式输出"能不能正常工作。
#
# ⚠️ 这个脚本会真的调用大模型接口，也就是说：会花钱、需要联网、需要 .env 里配好密钥。
#    所以它不是那种"随便跑跑"的测试，跑之前心里有数。
#
# 它做的事情：
#   1. 发一条"用三个词打招呼"的请求
#   2. 一边收一边打印（你应该能看到字一个个蹦出来）
#   3. 最后统计收到了多少字、有没有拿到那条完整消息
# =============================================================================

"""
Simple test script to verify streaming functionality（验证流式功能的简单脚本）
"""
import sys  # 改模块搜索路径
import os  # 拼路径

# Add src directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))  # 当前文件所在文件夹
src_path = os.path.join(current_dir, '../src')  # ../src 目录
sys.path.insert(0, src_path)  # 插到搜索路径最前面

from core.api_client import APIClient  # 导入 API 客户端


def test_streaming():
    """Test the streaming API functionality（测试流式接口）"""
    print("🧪 Testing streaming functionality...")

    try:  # 整体包一层
        client = APIClient()  # 创建客户端（会读 .env 里的密钥和接口地址）

        # Verify the method exists
        if not hasattr(client, 'get_completion_stream'):  # 如果连这个方法都没有
            print("❌ get_completion_stream method not found!")
            return False  # 直接判失败

        print("✅ Streaming method found")  # 方法在，继续

        # Simple test request
        request_params = {
            # 准备请求参数
            "messages": [
                {"role": "system", "content": "你是一个乐于助人的助手。"},  # 系统设定
                {"role": "user", "content": "用三个词跟我打个招呼"}  # 要求"用三个词打招呼"
            ]
            # 注意这里没有传 tools（工具清单），所以模型不会调工具，只会说话
        }

        print("📡 Making streaming request...")  # 提示"正在发请求"
        print("📝 Streaming response:")  # 提示"下面是模型的回复"
        print("🤖 ", end="", flush=True)  # 打印机器人头像，不换行，立刻输出

        # Test the streaming generator
        stream_gen = client.get_completion_stream(request_params)  # 拿到数据流（此时还没真正开始收）
        full_content = ""  # 攒文字用的
        message_obj = None  # 装最后那条完整消息

        for chunk in stream_gen:  # 一段段收
            if isinstance(chunk, str):  # 如果这一片是文字
                print(chunk, end="", flush=True)  # 立刻打印，不换行
                full_content += chunk  # 攒起来
            else:  # 否则就是最后那条完整消息对象
                message_obj = chunk  # 存起来
                print(f"\n✅ Received final message object")  # 提示"收到完整消息了"
                print(f"   Content length: {len(chunk.content) if chunk.content else 0}")
                # 三元表达式的意思：有内容就取长度，没内容就是 0
                print(f"   Has tool_calls: {hasattr(chunk, 'tool_calls') and chunk.tool_calls is not None}")
                # 顺便看看有没有工具调用（这次应该没有）
                break  # 拿到完整消息就跳出循环

        print(f"\n📊 Streaming stats:")  # 打印统计
        print(f"   Streamed content length: {len(full_content)}")  # 一共收了多少字
        print(f"   Final message content length: {len(message_obj.content) if message_obj and message_obj.content else 0}")
        # 完整消息里的内容长度（正常应该和上面那个数字一样）

        if full_content and message_obj:  # 如果文字和完整消息都拿到了
            print("🎉 Streaming test completed successfully!")
            return True  # 判成功
        else:  # 缺一个
            print("❌ No content received")
            return False  # 判失败

    except Exception as e:  # 任何意外（比如密钥没配、网络不通）
        print(f"\n❌ Streaming test failed: {e}")
        import traceback  # 这里才导入
        traceback.print_exc()  # 打印现场
        return False


if __name__ == "__main__":
    success = test_streaming()  # 跑测试
    sys.exit(0 if success else 1)  # 用退出码告诉外界成功还是失败
