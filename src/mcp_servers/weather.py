# =============================================================================
# 文件：src/mcp_servers/weather.py
# 作用：一个独立的"天气 MCP 服务器"（演示用）。
#
# 它不是我们的主程序的一部分，而是一个"小服务"：
# 主程序会单独启动它，然后通过 MCP 协议问它"能干什么"。
# 它说"我能查预警、能查天气预报"，主程序就把这两个能力包装成工具给模型用。
#
# 数据来源：美国国家气象局（NWS）的公开接口。所以只支持美国的地点。
#
# 你可以把它当成"怎么写一个 MCP 服务器"的模板：
#   1. 建一个 FastMCP 对象
#   2. 用 @mcp.tool() 装饰函数，那个函数就变成一个工具了
#   3. 最后 mcp.run() 把它跑起来
# =============================================================================

from typing import Any  # 类型标注工具
import httpx  # httpx 是一个"能异步发网络请求"的库（类似 requests，但支持异步）
from mcp.server.fastmcp import FastMCP  # FastMCP 是官方提供的"快速搭服务器"工具

# Initialize FastMCP server
mcp = FastMCP("weather", log_level="WARNING")
# 创建一个 MCP 服务器对象，名字叫 weather，日志级别设为 WARNING（只显示警告和错误，不啰嗦）

# Constants
NWS_API_BASE = "https://api.weather.gov"  # 气象局接口的地址前缀（常量用全大写，是 Python 的惯例）
USER_AGENT = "weather-app/1.0"  # 请求时自报家门（这个接口要求必须带，否则会被拒绝）


async def make_nws_request(url: str) -> dict[str, Any] | None:
    """Make a request to the NWS API with proper error handling.（带错误处理的请求函数）"""
    headers = {
        # 请求头：相当于信封上写的附加信息
        "User-Agent": USER_AGENT,  # 自报身份（气象局强制要求）
        "Accept": "application/geo+json"  # 告诉服务器"我希望返回这种格式"
    }
    async with httpx.AsyncClient() as client:
        # 创建一个异步 HTTP 客户端，with 保证用完自动关闭连接
        try:  # 试
            response = await client.get(url, headers=headers, timeout=30.0)  # 发 GET 请求，最多等 30 秒
            response.raise_for_status()  # 如果服务器返回错误码（4xx/5xx），这里会抛出异常
            return response.json()  # 把返回的 JSON 文本解析成字典
        except Exception:  # 任何错误（网络不通、超时、返回错误码、JSON 格式不对）
            return None  # 统一返回 None，让调用方自己判断"没拿到数据"


def format_alert(feature: dict) -> str:
    """Format an alert feature into a readable string.（把一条预警格式化成人能读的文字）"""
    props = feature["properties"]  # 取出"属性"这一层（气象局的返回结构就是这样套的）
    return f"""
Event: {props.get('event', 'Unknown')}
Area: {props.get('areaDesc', 'Unknown')}
Severity: {props.get('severity', 'Unknown')}
Description: {props.get('description', 'No description available')}
Instructions: {props.get('instruction', 'No specific instructions provided')}
"""
    # .get('字段名', '默认值') 的意思是：有这个字段就取，没有就用后面的默认值。
    # 这样就算气象局漏了某个字段，也不会报错。
    # 中文含义依次是：事件类型、影响区域、严重程度、详细描述、应对指引


@mcp.tool()
# ↑ 这个装饰器是"点石成金"的关键：加上它，下面的函数就自动变成一个 MCP 工具，
#   函数名 = 工具名，函数说明 = 工具说明，参数名 = 参数名。
async def get_alerts(state: str) -> str:
    """Get weather alerts for a US state.（查询某个美国州的天气预警）

    Args:
        state: Two-letter US state code (e.g. CA, NY)（两位的州代码，比如 CA、NY）
    """
    url = f"{NWS_API_BASE}/alerts/active/area/{state}"  # 拼出完整的接口地址
    data = await make_nws_request(url)  # 发请求拿数据

    if not data or "features" not in data:  # 如果没拿到数据，或者返回里没有 features 这一项
        return "Unable to fetch alerts or no alerts found."  # 告诉调用方"拿不到"

    if not data["features"]:  # 如果 features 存在但是空的
        return "No active alerts for this state."  # 说明"这个州目前没有预警"

    alerts = [format_alert(feature) for feature in data["features"]]  # 把每条预警都格式化（列表推导式）
    return "\n---\n".join(alerts)  # 用分隔线把它们连成一大段文字


@mcp.tool()  # 再来一个工具
async def get_forecast(latitude: float, longitude: float) -> str:
    """Get weather forecast for a location.（查询某个位置的天气预报）

    Args:
        latitude: Latitude of the location（纬度）
        longitude: Longitude of the location（经度）
    """
    # First get the forecast grid endpoint
    points_url = f"{NWS_API_BASE}/points/{latitude},{longitude}"  # 第一步：用经纬度查"网格点信息"
    points_data = await make_nws_request(points_url)  # 发请求

    if not points_data:  # 没拿到
        return "Unable to fetch forecast data for this location."  # 告诉调用方

    # Get the forecast URL from the points response
    forecast_url = points_data["properties"]["forecast"]  # 第二步：从返回里取出"真正的预报地址"
    # 这是气象局接口的特点：要两步。先问"这个经纬度对应哪个网格"，再问"那个网格的预报"
    forecast_data = await make_nws_request(forecast_url)  # 再发一次请求

    if not forecast_data:  # 还是没拿到
        return "Unable to fetch detailed forecast."

    # Format the periods into a readable forecast
    periods = forecast_data["properties"]["periods"]  # 取出"各个时段"的列表
    forecasts = []  # 准备装结果
    for period in periods[:5]:  # Only show next 5 periods（只取前 5 个时段，太多了刷屏）
        # periods[:5] 的意思是"从头取到第 5 个之前"，也就是前 5 个
        forecast = f"""
{period['name']}:
Temperature: {period['temperature']}°{period['temperatureUnit']}
Wind: {period['windSpeed']} {period['windDirection']}
Forecast: {period['detailedForecast']}
"""
        # 中文含义：时段名、温度、风速风向、详细预报
        forecasts.append(forecast)  # 加进列表

    return "\n---\n".join(forecasts)  # 连成一大段返回


if __name__ == "__main__":
    # 如果这个文件是被"直接运行"的（主程序启动它时就是这种情况）
    # Initialize and run the server
    mcp.run(transport='stdio')  # 启动服务器，用标准输入输出方式通信（MCP 最常用的方式）
