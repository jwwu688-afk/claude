# -*- coding: utf-8 -*-
"""`thinker mcp` 子命令的测试（第 26 轮）

为什么单独一个文件：
  这是项目里唯一一条「整份读进来、改一项、整份写回去」的路径。
  它出错不是「功能不好用」，而是把用户手写的配置抹掉——不可逆。
  所以三类必须钉死：
    1. 正常增删（add / remove / list / get 各自行为）；
    2. 写盘保护（文件坏了就停手、原文件字节不变、覆盖前有备份）；
    3. 解析边界（-- 前后、选项位置、参数原样透传）。

怎么跑（项目根目录）：
  python test/test_mcp_cli.py

约定：不依赖 pytest，不连网络、不启动任何 MCP 子进程，全部用临时目录里的假配置。
"""
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rich.console import Console  # noqa: E402

import tools.mcp_cli as mcp_cli  # noqa: E402
from tools.mcp_config import resolve_config_path  # noqa: E402

passed = 0
failed = 0


def check(label, condition, detail=""):
    """记一条断言（不抛异常，跑完统一报数）"""
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}   {detail}")


def new_config_path():
    """建一个干净临时目录，并把它设成本次测试的配置路径

    通过 THINKER_MCP_CONFIG 覆盖，绝不碰真实的 ~/.thinker/mcp.json：
    测试如果会写到用户真实配置上，跑一次测试就等于删一次用户的服务器。
    """
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="thinker-mcp-test-"))
    config_path = tmp_dir / "mcp.json"
    os.environ["THINKER_MCP_CONFIG"] = str(config_path)
    return config_path


def run(*args):
    """调用一次命令，返回（退出码, 终端输出文本）"""
    buffer = io.StringIO()
    # 把模块里的 console 换成写到内存的实例：一是测试输出干净，
    # 二是 no_color/固定宽度让断言不受终端环境影响。
    mcp_cli.console = Console(file=buffer, width=120, no_color=True, highlight=False)
    code = mcp_cli.run_mcp_cli(list(args))
    return code, buffer.getvalue()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


print("\n[1] add：创建配置")
config = new_config_path()
code, out = run("add", "12306", "--", "npx", "-y", "12306-mcp")
data = read_json(config)
check("返回码 0", code == 0, out)
check("文件已创建", config.exists())
check(
    "三项写入正确",
    data["mcpServers"]["12306"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "12306-mcp"],
    },
    data,
)
check("输出里有提示", "12306" in out and "生效" in out, out)

print("\n[2] add：选项写在名字后面也认")
code, out = run("add", "weather", "-t", "stdio", "--", "python", "weather.py")
data = read_json(config)
check("返回码 0", code == 0, out)
check("命令解析正确", data["mcpServers"]["weather"]["command"] == "python", data)

print("\n[3] add：省略 --（命令参数原样透传）")
code, out = run("add", "calc", "npx", "-y", "calc-mcp")
data = read_json(config)
check("返回码 0", code == 0, out)
check(
    "-y 没被当成自己的选项吞掉",
    data["mcpServers"]["calc"]["args"] == ["-y", "calc-mcp"],
    data["mcpServers"].get("calc"),
)

print("\n[4] add：覆盖同名 + 备份旧内容")
code, out = run("add", "12306", "--", "npx", "-y", "12306-mcp@latest")
backup = config.with_name(config.name + ".bak")
data = read_json(config)
check("返回码 0", code == 0, out)
check("提示为「已更新」", "已更新" in out, out)
check("新内容生效", data["mcpServers"]["12306"]["args"] == ["-y", "12306-mcp@latest"])
check(
    "备份里是覆盖前的旧内容",
    backup.exists() and read_json(backup)["mcpServers"]["12306"]["args"] == ["-y", "12306-mcp"],
    backup.read_text(encoding="utf-8") if backup.exists() else "无备份文件",
)

print("\n[5] add：保留文件里的其他键（不抹掉手写说明）")
config = new_config_path()
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(
    json.dumps({"_说明": ["手写注释"], "mcpServers": {"old": {"command": "x", "args": []}}}, ensure_ascii=False),
    encoding="utf-8",
)
code, out = run("add", "new", "--", "npx", "-y", "new-mcp")
data = read_json(config)
check("返回码 0", code == 0, out)
check("_说明 还在", data.get("_说明") == ["手写注释"], data)
check("旧服务器还在", "old" in data["mcpServers"], data)
check("新服务器加上了", "new" in data["mcpServers"], data)

print("\n[5.1] 写回沿用原文件的 tab 缩进")
config = new_config_path()
config.parent.mkdir(parents=True, exist_ok=True)
# 手写一个 tab 缩进的配置文件（很多编辑器/老配置就是这样），
# 改动后如果被统一成 2 空格，diff 里会显示「整份文件重写」，真改动淹没在里面。
config.write_text(
    '{\n\t"mcpServers": {\n\t\t"old": {\n\t\t\t"type": "stdio",\n\t\t\t"command": "x",\n\t\t\t"args": []\n\t\t}\n\t}\n}\n',
    encoding="utf-8",
)
code, out = run("add", "new", "--", "npx", "-y", "new-mcp")
text = config.read_text(encoding="utf-8")
check("返回码 0", code == 0, out)
check("仍是 tab 缩进", '\n\t"mcpServers"' in text and '\n  "mcpServers"' not in text, repr(text))

print("\n[6] add：各种用法错误都不该动磁盘")
config = new_config_path()
run("add", "keep", "--", "npx", "-y", "keep-mcp")
before = config.read_bytes()

cases = [
    (("add", "broken"), "缺少命令"),
    (("add", "bad/name", "--", "npx", "x"), "名字含斜杠"),
    (("add", "space name", "--", "npx", "x"), "名字含空格"),
    (("add", "h", "--transport", "http", "--", "npx", "x"), "http 类型"),
    (("add", "h", "--transport=sse", "--", "npx", "x"), "sse 类型（等号写法）"),
    (("add", "u", "--scope", "user", "--", "npx", "x"), "不认识的选项"),
    (("add", "12306", "-s", "user", "--", "npx", "y"), "-s 写法"),
    (("add",), "只有 add"),
]
for args, label in cases:
    code, out = run(*args)
    check(f"{label} → 返回码 1", code == 1, out)
check("失败后文件字节完全没变", config.read_bytes() == before, config.read_text(encoding="utf-8"))

# 第 30.2 轮：按 claude 习惯写 -s user 的人，要被告知"我们没有 scope"，并直接给正确写法。
# 不能只回一句"不认识的选项"——那是在他已经写对参数名的情况下说没听懂。
code, out = run("add", "12306", "-s", "user", "--", "npx", "y")
check("--scope 的提示说明原因", "没有 --scope" in out, out)
check("--scope 的提示给出正确写法", "thinker mcp add 12306 -- npx -y 12306-mcp" in out, out)

print("\n[7] 配置文件是坏 JSON：必须中止，不能覆盖")
config = new_config_path()
config.parent.mkdir(parents=True, exist_ok=True)
broken = '{"mcpServers": { 少个引号 }}'
config.write_text(broken, encoding="utf-8")
before = config.read_bytes()
code, out = run("add", "x", "--", "npx", "y")
check("返回码 1", code == 1, out)
check("报错提到 JSON", "JSON" in out, out)
check("原文件字节没变", config.read_bytes() == before)
code, out = run("remove", "x")
check("remove 同样中止", code == 1 and config.read_bytes() == before, out)

print("\n[8] remove：删除 / 别名 / 不存在")
config = new_config_path()
run("add", "a1", "--", "npx", "-y", "a1")
run("add", "a2", "--", "npx", "-y", "a2")
code, out = run("remove", "a1")
data = read_json(config)
check("返回码 0", code == 0, out)
check("该条已删除", "a1" not in data["mcpServers"], data)
check("其他条目没被连累", "a2" in data["mcpServers"], data)
check("输出里回显了被删的命令", "npx -y a1" in out, out)

code, out = run("rm", "a2")  # 别名
check("rm 别名可用", code == 0 and "a2" not in read_json(config)["mcpServers"], out)

code, out = run("remove", "never-exists")
check("删不存在的 → 返回码 1", code == 1, out)
check("提示里列出已有名字", "现有" in out, out)

print("\n[9] list / get")
config = new_config_path()
run("add", "12306", "--", "npx", "-y", "12306-mcp")
code, out = run("list")
check("list 返回码 0", code == 0, out)
check("list 含名字", "12306" in out, out)
check("list 含完整命令", "npx -y 12306-mcp" in out, out)

code, out = run("get", "12306")
check("get 返回码 0", code == 0, out)
check("get 输出 JSON 原文", '"command": "npx"' in out, out)

code, out = run("get", "nope")
check("get 不存在的 → 返回码 1", code == 1, out)

# 非 stdio 的条目要明确标成「忽略」，否则用户会去查为什么工具没出现
config.write_text(
    json.dumps({"mcpServers": {"remote": {"type": "http", "command": "x", "args": []}}}),
    encoding="utf-8",
)
code, out = run("list")
check("非 stdio 标注为忽略", "忽略" in out, out)

print("\n[10] 空配置 / 帮助 / 未知子命令")
config = new_config_path()
code, out = run("list")
check("没有配置文件时 list 不报错", code == 0 and "还没有配置" in out, out)

code, out = run()
check("不带子命令 → 打印帮助、返回 0", code == 0 and "add" in out, out)
code, out = run("--help")
check("--help 返回 0", code == 0, out)
code, out = run("nonsense")
check("未知子命令 → 返回码 1", code == 1, out)

print("\n[11] 路径覆盖：THINKER_MCP_CONFIG 生效")
check("resolve_config_path 指向测试文件", resolve_config_path() == pathlib.Path(os.environ["THINKER_MCP_CONFIG"]))

# 收尾：把临时目录清掉，别在系统 temp 里留一堆 mcp.json
shutil.rmtree(pathlib.Path(os.environ["THINKER_MCP_CONFIG"]).parent, ignore_errors=True)
os.environ.pop("THINKER_MCP_CONFIG", None)

print(f"\n通过 {passed} 项，失败 {failed} 项\n")
sys.exit(1 if failed else 0)
