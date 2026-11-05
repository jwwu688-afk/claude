#!/usr/bin/env python3
# 表示"用 python3 运行本文件"（Windows 上没实际作用）
# =============================================================================
# 文件：test/test_streaming_with_tools.py
# 作用：测试"流式输出 + 工具调用"能不能配合工作。
#
# 和 test_streaming.py 的区别：这次把工具清单也一起发过去了，
# 并且故意问一个"需要动手"的问题（列出当前目录的文件），
# 看模型会不会申请调用 cmd_runner。
#
# ⚠️ 两个注意点：
#   1. 这个脚本会真的调用大模型，会花钱
#   2. 下面有一处漏了 await（见对应行的注释），所以实际很可能跑不通
# =============================================================================

"""
Test streaming functionality with tool calls（测试带工具调用的流式输出）
"""
import sys  # 改模块搜索路径
import os  # 拼路径

# Add src directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))  # 当前文件所在文件夹
src_path = os.path.join(current_dir, '../src')  # ../src
sys.path.insert(0, src_path)  # 插到搜索路径最前面

from core.api_client import APIClient  # 导入 API 客户端
from tools.tool_manager import ToolManager  # 导入工具管理器


def test_streaming_with_tools():
    """Test streaming with tool calls（测试带工具调用的流式输出）"""
    print("🧪 Testing streaming functionality with tools...")

    try:  # 整体包一层
        client = APIClient()  # 创建 API 客户端
        tool_manager = ToolManager()  # 创建工具管理器（会顺便在后台连 MCP 服务器）

        # Test request that should trigger a tool call
        request_params = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},  # 系统设定
                {"role": "user", "content": "Please list the files in the current directory"}  # 让它列文件
            ],
            "tools": tool_manager.get_tools_description()
            # ⚠️ 这里漏了 await！get_tools_description 是异步方法，
            #    不写 await 拿到的是一个"协程对象"（还没开始执行的东西），
            #    直接塞进请求参数里，接口那边会报错。
            #    正确写法：await tool_manager.get_tools_description()，并且整个函数要改成 async。
        }

        print("📡 Making streaming request with tools...")  # 提示发请求
        print("📝 Streaming response:")
        print("🤖 ", end="", flush=True)  # 机器人头像，不换行

        stream_gen = client.get_completion_stream(request_params)  # 拿数据流
        full_content = ""  # 攒文字
        message_obj = None  # 装完整消息

        for chunk in stream_gen:  # 一段段收
            if isinstance(chunk, str):  # 是文字
                print(chunk, end="", flush=True)  # 立刻打印
                full_content += chunk  # 攒起来
            else:  # 是完整消息
                message_obj = chunk
                print(f"\n✅ Received final message object")
                print(f"   Content length: {len(chunk.content) if chunk.content else 0}")  # 内容长度
                print(f"   Has tool_calls: {hasattr(chunk, 'tool_calls') and chunk.tool_calls is not None}")
                if hasattr(chunk, 'tool_calls') and chunk.tool_calls:  # 如果真的有工具申请
                    print(f"   Number of tool calls: {len(chunk.tool_calls)}")  # 有几个
                    for i, tc in enumerate(chunk.tool_calls):  # 逐个打印
                        print(f"   Tool call {i+1}: {tc.function.name}")  # 第几个、叫什么名字
                break  # 拿到完整消息就停

        print(f"\n📊 Results:")  # 打印结果
        print(f"   Streamed content: '{full_content.strip()}'")  # 收到的文字（去掉首尾空白）
        print(f"   Tool calls detected: {message_obj and hasattr(message_obj, 'tool_calls') and message_obj.tool_calls is not None}")
        # 上面这行是一串 and 连起来判断：前一个为假就直接停，不会报错

        print("🎉 Tool streaming test completed successfully!")
        return True  # 不管有没有真的调到工具，只要没抛异常就算成功

    except Exception as e:  # 任何意外
        print(f"\n❌ Tool streaming test failed: {e}")
        import traceback  # 这里才导入
        traceback.print_exc()  # 打印现场
        return False


if __name__ == "__main__":
    success = test_streaming_with_tools()  # 跑测试
    sys.exit(0 if success else 1)  # 用退出码报告结果
