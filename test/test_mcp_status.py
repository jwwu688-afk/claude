# -*- coding: utf-8 -*-
"""`/mcp` 命令与 MCP 连接状态快照的测试（第 31 轮）

为什么单独一个文件：
  /mcp 的价值全在「状态判得对不对」——把失败的显示成已连接，比没有这条命令更糟：
  用户会照着"已连接"去查为什么工具不出现。所以五种状态（已连接 / 失败 /
  连接中 / 状态未知 / 被跳过）必须各自钉一条。

怎么跑（项目根目录）：
  python test/test_mcp_status.py

约定：不依赖 pytest，不连任何服务器，不读真实配置——全部用假对象。
"""
import asyncio
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.commands import cmd_mcp  # noqa: E402
from tools.mcp_client.client import MCPClient  # noqa: E402

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}   {detail}")


class FakeClient:
    """只带 status_snapshot 需要的属性，不碰网络、不读文件"""

    def __init__(self, servers, skipped=None, connections=None, failures=None,
                 tool_names=None, tool_errors=None, ready=True):
        # servers 的 value 用不上（只遍历键），给 None 就够
        self.config = SimpleNamespace(
            servers={name: None for name in servers},
            skipped=skipped or {},
            config_path=pathlib.Path("C:/fake/.thinker/mcp.json"),
        )
        self.connections = connections or {}
        self.failures = failures or {}
        self.tool_names = tool_names or {}
        self.tool_errors = tool_errors or {}
        # status_snapshot 里只调 self._ready.is_set()，用 lambda 顶替 asyncio.Event
        self._ready = SimpleNamespace(is_set=lambda: ready)

    def status_snapshot(self):
        """借用真实实现，只替换 self 的依赖；命令测试因此和线上走同一段逻辑"""
        return MCPClient.status_snapshot(self)


def snapshot(**kwargs):
    """把 status_snapshot 当纯函数调（不构造 MCPClient，避免它去连服务器）"""
    return MCPClient.status_snapshot(FakeClient(**kwargs))


print("\n[1] 五种状态各自判对")
items = snapshot(
    servers=["a", "b", "c", "d", "e"],
    connections={"a": object(), "b": object(), "c": object()},
    failures={"d": "FileNotFoundError: npx"},
    tool_names={"a": ["t1", "t2"]},
    # b：连上了但 tool_names 还没回填；c：连上了但 list_tools 失败
    tool_errors={"c": "TimeoutError: 超时"},
    ready=True,
)
by_name = {item["name"]: item for item in items}
check("已连接且已知工具数", by_name["a"]["state"] == "connected" and by_name["a"]["detail"] == "2 个工具", by_name["a"])
check("已连接但清单还没回填", by_name["b"]["detail"] == "已连接，正在取工具清单…", by_name["b"])
check("已连接但取清单失败", "取工具清单失败" in by_name["c"]["detail"], by_name["c"])
check("连接失败带原因", by_name["d"]["state"] == "failed" and "FileNotFoundError" in by_name["d"]["detail"], by_name["d"])
check("没记录时是状态未知", by_name["e"]["state"] == "idle", by_name["e"])

print("\n[2] 连接尚未结束 → 未出结果的显示「连接中」")
items = snapshot(servers=["slow"], ready=False)
check("状态为 connecting", items[0]["state"] == "connecting", items[0])
check("文案是连接中", "连接中" in items[0]["detail"], items[0])

print("\n[3] 被配置层跳过的条目排在最后，并带原因")
items = snapshot(
    servers=["good"],
    skipped={"remote": "类型 http（本版本只支持 stdio）", "broken": "缺少 command"},
    connections={"good": object()},
    tool_names={"good": []},
    ready=True,
)
check("正常项在前", items[0]["name"] == "good" and items[0]["detail"] == "没有提供工具", items[0])
check("跳过项在后", [i["name"] for i in items[1:]] == ["remote", "broken"], items)
check("跳过项状态正确", all(i["state"] == "skipped" for i in items[1:]), items)

print("\n[4] 什么都没有 → 空列表（让命令走「还没配置」那条）")
check("空列表", snapshot(servers=[]) == [], snapshot(servers=[]))


class FakeUI:
    """把 print_* 收进列表，方便断言文案（不碰终端，也不依赖 rich）"""

    def __init__(self):
        self.lines = []

    def print_info(self, message, emoji=""):
        self.lines.append(("info", message))

    def print_success(self, message, emoji=""):
        self.lines.append(("ok", message))

    def print_error(self, message, emoji=""):
        self.lines.append(("err", message))

    def print_warning(self, message):
        self.lines.append(("warn", message))

    def text(self):
        return "\n".join(message for _, message in self.lines)


def run_command(args, client=None):
    ui = FakeUI()
    conversation = SimpleNamespace(
        _ui_manager=ui,
        _tool_manager=SimpleNamespace(_mcp_client=client) if client is not None else None,
    )
    asyncio.run(cmd_mcp(conversation, args))
    return ui


print("\n[5] 命令输出")
ui = run_command("", FakeClient(
    servers=["weather", "broken"],
    connections={"weather": object()},
    failures={"broken": "FileNotFoundError: 找不到 npx"},
    tool_names={"weather": ["get_forecast"]},
))
check("汇总行有已连接数量", "已连接 1" in ui.text(), ui.text())
check("汇总行有失败数量", "失败 1" in ui.text(), ui.text())
check("成功行走绿色通道", any(kind == "ok" and "weather" in msg for kind, msg in ui.lines), ui.lines)
check("失败行走红色通道", any(kind == "err" and "broken" in msg for kind, msg in ui.lines), ui.lines)
check("带上了配置文件路径", "mcp.json" in ui.text(), ui.text())

ui = run_command("", FakeClient(servers=[]))
check("没有配置时给添加示例", "thinker mcp add" in ui.text(), ui.text())

ui = run_command("", None)
check("客户端缺失时如实报错", any(kind == "err" for kind, _ in ui.lines), ui.lines)

ui = run_command("add foo", FakeClient(servers=["weather"]))
check("带参数时把用户引到终端命令", "thinker mcp add foo" in ui.text(), ui.text())

ui = run_command("", FakeClient(servers=["slow"], ready=False))
check("连接中能显示出来", "连接中" in ui.text(), ui.text())

print("\n[6] 启动命令解析（Windows 上 npx → npx.cmd）")
# 这一条锁的是第 30.1 轮修掉的坑：配置里写 "npx"，CreateProcess 只补 .exe，
# 而 npm 装的是 npx.cmd，于是"配置一字不差也连不上"。
import os  # noqa: E402
import shutil  # noqa: E402

from tools.mcp_client.client import resolve_command  # noqa: E402

check("绝对路径原样返回", resolve_command(sys.executable) == sys.executable, resolve_command(sys.executable))
check(
    "不存在的路径原样返回（不掩盖真实错误）",
    resolve_command("D:/nope/not-exist.exe") == "D:/nope/not-exist.exe",
    resolve_command("D:/nope/not-exist.exe"),
)
if os.name == "nt" and shutil.which("npx"):
    resolved = resolve_command("npx")
    check("npx 解析成带扩展名的真实文件", resolved.lower().endswith(".cmd") and pathlib.Path(resolved).exists(), resolved)

print(f"\n通过 {passed} 项，失败 {failed} 项\n")
sys.exit(1 if failed else 0)
