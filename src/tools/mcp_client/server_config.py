# =============================================================================
# 文件：src/tools/mcp_client/server_config.py
# 作用：读取 MCP 服务器的配置文件（~/.thinker/mcp.json），变成程序能用的对象。
#
# 配置文件长这样：
#   {
#     "mcpServers": {
#       "weather":    { "type": "stdio", "command": "python.exe", "args": ["weather.py"] },
#       "calculator": {                        "command": "python.exe", "args": ["calculator.py"] }
#     }
#   }
#
# 也就是说：一个 JSON 对象，键是"服务器名字"，值是"怎么启动它"。
# 这个文件负责把 JSON 读进来、检查一下、变成 MCPServer 对象。
# =============================================================================

import json  # 读 JSON 文件
from pathlib import Path  # 处理文件路径
from typing import List, Dict, Optional  # 类型标注工具


class MCPServer:
    # 一个"服务器"的配置。就是个装数据的容器
    def __init__(self, name: str, type_: str, command: str, args: List[str]):
        # 注意 type_ 后面带下划线，是因为 type 是 Python 的内置函数名，不能直接当变量名
        self.name = name  # 服务器名字
        self.type = type_  # 连接类型（目前只支持 stdio）
        self.command = command  # 用哪个程序启动
        self.args = args  # 启动参数列表

    def __repr__(self):
        # __repr__ 决定"直接打印这个对象时显示什么"
        return f"<MCPServer name={self.name}, type={self.type}, command={self.command}, args={self.args}>"


class MCPConfig:
    # 配置文件读取器
    def __init__(self, config_path: str):
        self.config_path = Path(config_path).expanduser()
        # expanduser() 的作用：把路径开头的 ~ 换成你的用户目录
        # 所以 "~/.thinker/mcp.json" 会变成 "C:/Users/23889/.thinker/mcp.json"
        self.servers: Dict[str, MCPServer] = {}  # 存所有服务器，键是名字
        self._load_config()  # 造对象时就顺便把配置读进来

    def _load_config(self):
        if not self.config_path.exists():  # 如果配置文件不存在
            # 【2026-11-06 优化】原来这里是直接抛 FileNotFoundError，
            #   而这个对象在 ToolManager 创建时就会被 new 出来，
            #   结果就是：没建 ~/.thinker/mcp.json 的人，整个 CLI 根本起不来。
            #   现在改成"打印一句提示，然后当作没有外部工具"，
            #   内置工具（cmd_runner 等）该能用还是能用。
            print(f"[MCP] 未找到配置文件 {self.config_path}，本次跳过外部工具（内置工具不受影响）")
            return  # 直接结束，self.servers 保持空字典

        try:  # 【2026-11-06 优化】配置文件即使存在，也可能是坏的（手写 JSON 很容易少个逗号）
            with self.config_path.open("r", encoding="utf-8") as f:
                # with 是"用完自动关闭文件"的写法，比手动 close 安全
                # encoding="utf-8" 指定编码，防止中文路径或内容乱码
                raw_config = json.load(f)  # 把 JSON 文本变成 Python 字典
        except (json.JSONDecodeError, OSError) as e:  # JSON 语法错了，或者文件读不了
            print(f"[MCP] 配置文件解析失败（{e}），本次跳过外部工具")
            return  # 同样降级处理，不让程序崩

        if not isinstance(raw_config, dict):  # 最外层必须是一个对象（花括号）
            print("[MCP] 配置文件格式不对：最外层应该是 { } 包裹的对象，本次跳过外部工具")
            return

        mcp_servers = raw_config.get("mcpServers", {})
        # 取出 "mcpServers" 这一项，没有就给空字典（防止配置文件里没这一栏）

        if not isinstance(mcp_servers, dict):  # 这一项也必须是对象
            print("[MCP] 配置里的 mcpServers 应该是 { } 包裹的对象，本次跳过外部工具")
            return

        for name, info in mcp_servers.items():  # 遍历每个服务器：name 是名字，info 是它的配置
            try:  # 【2026-11-06 优化】每个服务器单独 try：
                #   原来只要有一个服务器写错，整份配置就全废，后面的都连不上。
                #   现在改成"跳过这个，继续看下一个"。
                if not isinstance(info, dict):  # 配置项本身不是对象
                    print(f"[MCP] 服务器 {name} 的配置格式不对，已跳过")
                    continue  # 跳过它

                type_ = info.get("type")  # 取连接类型
                if not (type_ == "stdio" or type_ is None):  # 如果不是 stdio，也没写类型
                    # only handle stdio type
                    print(f"[MCP] 服务器 {name} 的类型是 {type_}，本版本只支持 stdio，已跳过")
                    continue  # 就跳过（这意味着 HTTP / SSE 类型的服务器会被忽略）

                command = info.get("command")  # 取启动命令
                args = info.get("args", [])  # 取启动参数，没有就给空列表
                if not command:  # 【2026-11-06 优化】只要求 command 必填
                    #   原来连 args 为空也会报错，导致"不需要参数的服务器"根本配不了。
                    print(f"[MCP] 服务器 {name} 缺少 command，已跳过")
                    continue
                if not isinstance(args, list):  # 参数必须是列表
                    print(f"[MCP] 服务器 {name} 的 args 应该是列表（用 [ ]），已跳过")
                    continue

                self.servers[name] = MCPServer(name=name, type_=type_, command=command, args=args)
                # 包装成对象，存进字典
            except Exception as e:  # 兜底：万一还有没想到的情况
                print(f"[MCP] 服务器 {name} 加载失败（{e}），已跳过")

    def get_server(self, name: str) -> Optional[MCPServer]:
        return self.servers.get(name)  # 按名字取，取不到返回 None

    def list_servers(self) -> List[str]:
        return list(self.servers.keys())  # 返回所有服务器名字的列表
