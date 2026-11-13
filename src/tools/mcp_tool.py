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

    @property
    def is_readonly(self) -> bool:
        """服务器有没有声明"这个工具只读"（2026-11-13 第 33.5 轮新增）

        MCP 规范里 Tool 带一个 annotations，其中 readOnlyHint 表示"调用它不会
        改动任何东西"。计划模式要用这个信息：声明了的直接放行，没声明的只能问用户。

        三种情况都要兼容，因为不同 mcp 版本 / 不同服务器写法不一致：
          1. annotations 是对象、字段叫 readOnlyHint（mcp 的 pydantic 模型，camelCase）；
          2. annotations 是对象、字段叫 read_only_hint（Python 习惯写法）；
          3. annotations 干脆是 dict（有些服务器直接透传 JSON）。

        取不到一律返回 False——**没声明就当能写**，这是安全方向：
        大不了多问用户一次，而不是把一个会提交工单的工具静默放出去。
        """
        annotations = getattr(self.tool, "annotations", None)
        if annotations is None:
            return False

        hint = getattr(annotations, "readOnlyHint", None)
        if hint is None:
            hint = getattr(annotations, "read_only_hint", None)
        if hint is None and isinstance(annotations, dict):
            hint = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
        return bool(hint)

    async def act(self, **kwargs):
        # 执行：把参数原样转发给远程服务器，等它返回结果
        try:  # 试
            response = await self.session.call_tool(self.tool.name, kwargs)
            # call_tool 就是"远程调用"：告诉服务器"请执行你那个叫 xxx 的工具，参数是这些"
            return self._content_to_text(response.content)
            # 【2026-11-07 修复】原来直接返回 response.content，那是一个 TextContent 对象列表，
            # 拼进对话后模型看到的是 [TextContent(type='text', text='6', annotations=None, meta=None)]
            # ——相当于把 Python 的 repr 喂给模型，它得自己猜哪个才是结果。
            # 现在把里面的 text 抽出来，模型直接看到 "6"。
        except Exception as e:  # 如果远程调用失败（服务器挂了、参数不对等）
            return f"调用外部 MCP 工具 {self.tool.name} 时出错：{e}"  # 返回错误信息，让模型知道

    @staticmethod
    def _content_to_text(content) -> str:
        """把 MCP 返回的内容列表压成纯文本（2026-11-07 新增）

        MCP 的返回值是一组内容块（可能有多段文字、图片、资源引用）。
        我们这里只处理最常用的文本块；遇到别的类型就退回 str()，至少不丢信息。
        多段之间用换行连接，保持"服务器给几段就是几段"的顺序。
        """
        if isinstance(content, str):  # 有些实现直接给字符串
            return content
        if isinstance(content, (list, tuple)):  # 标准形态：内容块列表
            parts = []
            for item in content:
                text = getattr(item, "text", None)  # TextContent 的正文
                parts.append(text if text is not None else str(item))
            return "\n".join(parts)
        return str(content)

    def _input_schema(self) -> dict:
        """取外部工具的参数格式，兼容 mcp 库 1.x 和 2.x（2026-11-07 新增）

        为什么要兼容：两个环境装的 mcp 版本不一样——
          mcp 1.x（claude 环境，1.14.1）字段叫 `inputSchema`（camelCase，照抄 JSON Schema 原文）
          mcp 2.x（thinker 环境，2.3.0）改成了 `input_schema`（snake_case，符合 Python 习惯）
        直接写死任何一个，另一个环境启动时就会抛
        `AttributeError: 'Tool' object has no attribute 'inputSchema'`。

        先取新名字，取不到再取老名字，两个都没有就当空参数（server 不接受参数的工具确实存在）。
        """
        schema = getattr(self.tool, "input_schema", None)  # mcp 2.x
        if schema is None:
            schema = getattr(self.tool, "inputSchema", None)  # mcp 1.x
        return schema or {}

    def json_schema(self):
        # 直接把服务器给的说明转成我们需要的格式
        schema = self._input_schema()
        return {
            "type": "function",
            "function": {
                "name": self.tool.name,  # 工具名（服务器起的）
                "description": self.tool.description,  # 工具说明（服务器写的）
                "parameters": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    # 参数格式：直接用服务器给的（.get 表示取不到就给空字典）
                    "required": schema.get("required", [])
                    # 必填参数：取不到给空列表（OpenAI 规范要求是数组，给 {} 会被某些服务端拒绝）
                }
            }
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        return ""  # 空实现，因为说明直接用服务器的 self.tool.description，不需要这个方法
