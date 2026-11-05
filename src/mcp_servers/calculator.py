# =============================================================================
# 文件：src/mcp_servers/calculator.py
# 作用：一个极简的"计算器 MCP 服务器"，用来演示 MCP 的最基本写法。
#
# 整个文件只做三件事：
#   1. 建一个 MCP 服务器对象
#   2. 定义一个加法函数，用 @mcp.tool() 把它变成"工具"
#   3. 启动服务器
#
# 想看一个 MCP 服务器最少需要多少代码？就这么点。
# =============================================================================

from typing import Any  # 类型标注工具（这里没用到，残留导入）
import httpx  # 网络请求库（这里也没用到，是从天气服务器复制过来的残留）
from mcp.server.fastmcp import FastMCP  # FastMCP：快速搭建 MCP 服务器的工具

# Initialize FastMCP server
mcp = FastMCP("calculator", log_level="WARNING")
# 创建一个名字叫 calculator 的服务器，只显示警告和错误日志


@mcp.tool()
# ↑ 加上这个装饰器，下面的函数就变成了一个工具：
#   工具名 = adder
#   工具说明 = 函数下面那段三引号文字
#   参数 = number1、number2（类型也自动识别成整数）
async def adder(number1: int, number2: int) -> int:
    """Add two numbers together and return the sum.（把两个数相加，返回和）

    Args:
        number1: The first number to add（第一个数）
        number2: The second number to add（第二个数）

    Returns:
        The sum of number1 and number2（两数之和）
    """
    return number1 + number2  # 就这么简单


if __name__ == "__main__":
    # 如果这个文件被直接运行（主程序启动它时就是）
    # Initialize and run the server
    mcp.run(transport='stdio')  # 用标准输入输出方式跑起来，等着主程序来问"你有什么工具"
