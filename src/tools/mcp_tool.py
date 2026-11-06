# =============================================================================
# 文件：src/tools/mcp_tool.py
# 作用：把"外部 MCP 服务器提供的工具"包装成"我们内部的工具"。
#
# 背景：MCP（模型上下文协议）是一个标准，让不同的程序都能提供工具。
#       比如别人写了个天气服务器，只要符合 MCP 标准，
#       我们的程序连上它，就能用它的 get_forecast 工具。
#
# 但外部工具和我们的工具"长相不同"：
#   我们的工具是"类"，有 act() 和 json_schema()
#   外部工具只是一个"描述 + 一个远程调用入口"
# 所以需要这个"适配器"来翻译一下。这个设计模式叫"适配器模式"。
# =============================================================================

import json  # 导入了但没用到（可以忽略）
from tools.base_tool import BaseTool  # 导入父类，让包装后的对象长得跟普通工具一样
from mcp import ClientSession, Tool  # 导入 MCP 的"会话"和"工具描述"两个类型


class McpTool(BaseTool):
    def __init__(self, tool: Tool, session: ClientSession):
        # tool：服务器给出的工具描述（名字、说明、参数格式）
        # session：跟那个服务器的连接，调用工具时就靠它
        super().__init__()  # 父类初始化
        self.session = session  # 存下连接
        self.tool = tool  # 存下工具描述

    @staticmethod
    def get_tool_name():
        return ""  # 静态方法返回空字符串，因为真正的名字要从 self.tool.name 取（每个外部工具都不一样）

    async def act(self, **kwargs):
        # 执行：把参数原样转发给远程服务器，等它返回结果
        try:  # 试
            response = await self.session.call_tool(self.tool.name, kwargs)
            # call_tool 就是"远程调用"：告诉服务器"请执行你那个叫 xxx 的工具，参数是这些"
            return response.content  # 把服务器的返回内容交回去
        except Exception as e:  # 如果远程调用失败（服务器挂了、参数不对等）
            return f"调用外部 MCP 工具 {self.tool.name} 时出错：{e}"  # 返回错误信息，让模型知道

    def json_schema(self):
        # 直接把服务器给的说明转成我们需要的格式
        return {
            "type": "function",
            "function": {
                "name": self.tool.name,  # 工具名（服务器起的）
                "description": self.tool.description,  # 工具说明（服务器写的）
                "parameters": {
                    "type": "object",
                    "properties": self.tool.inputSchema.get("properties", {}),
                    # 参数格式：直接用服务器给的（.get 表示取不到就给空字典）
                    "required": self.tool.inputSchema.get("required", {})
                    # 必填参数：同理（取不到给空字典，其实这里该给空列表更规范）
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        return ""  # 空实现，因为说明直接用服务器的 self.tool.description，不需要这个方法
