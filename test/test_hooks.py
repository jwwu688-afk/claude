# -*- coding: utf-8 -*-
"""hook 机制（第 32 轮）的测试

为什么值得单独一份：
  hook 会在用户机器上执行任意 shell。它出错有两种极端：
    ① 该拦的没拦（安全口袋漏了）；
    ② 不该拦的拦了（正常干活被卡死）。
  两种都不会报错、都不会崩，只会在某天"莫名其妙"暴露出来。
  所以解析、匹配、退出码、超时、串行短路这几件事必须逐条钉住。

怎么跑（项目根目录）：
  python test/test_hooks.py

约定：不依赖 pytest，不联网，不碰真实的 ~/.thinker/settings.json——
      全部用临时目录里的脚本和配置。
"""
import asyncio
import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from core.hooks import (  # noqa: E402
    MAX_TIMEOUT,
    HookRunner,
    _matcher_matches,
)

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


TMP = pathlib.Path(tempfile.mkdtemp(prefix="thinker-hooks-"))
SETTINGS = TMP / "settings.json"


def write_settings(hooks):
    SETTINGS.write_text(json.dumps({"hooks": hooks}, ensure_ascii=False), encoding="utf-8")


def make_script(name, body):
    """写一个临时 hook 脚本，返回能直接放进 command 的字符串

    用 sys.executable 的绝对路径而不是裸 "python"：
      测试要在任何 PATH 下都能跑，而且裸命令在 Windows 上会踩 .cmd 的坑
      （这正是第 30.1 轮修的问题，测试自己不该再踩一次）。
    """
    path = TMP / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


PY_ECHO = """
import json, os, sys
data = json.load(sys.stdin)
print("tool=" + str(data.get("tool_name")))
print("project=" + os.environ.get("THINKER_PROJECT_DIR", ""))
print("session=" + str(data.get("session_id")))
"""

print("\n[1] 配置文件不存在：静默、无 hooks")
runner = HookRunner(settings_path=str(TMP / "nope.json"), project_dir=str(TMP))
check("没有 specs", runner.specs == [], runner.specs)
check("没有提示（hooks 是可选功能，不该每次启动都提示）", runner.notes == [], runner.notes)

print("\n[2] 解析：只收认识的、能用的")
write_settings(
    {
        "PreToolUse": [
            {"matcher": "file_write|file_edit", "hooks": [{"type": "command", "command": "echo hi", "timeout": 900}]}
        ],
        "Bogus": [{"hooks": [{"command": "echo x"}]}],
        "PostToolUse": [
            {"hooks": [{"type": "prompt", "prompt": "评价一下"}]},
            {"hooks": [{"command": "   "}]},
        ],
        "SessionStart": [{"hooks": [{"command": "echo start"}]}],  # 不写 type 也算 command
    }
)
runner = HookRunner(settings_path=str(SETTINGS), project_dir=str(TMP))
check("只加载了两条有效 hook", len(runner.specs) == 2, runner.specs)
check("缺省 type 按 command 处理", any(s.event == "SessionStart" for s in runner.specs), runner.specs)
check("timeout 被压到上限", runner.specs[0].timeout == float(MAX_TIMEOUT), runner.specs[0].timeout)
check("未知事件有提示", any("Bogus" in n for n in runner.notes), runner.notes)
check("不支持的 type 有提示", any("prompt" in n for n in runner.notes), runner.notes)
check("空 command 有提示", any("缺少 command" in n for n in runner.notes), runner.notes)

print("\n[3] 配置文件坏了：不抛异常，只记一条")
SETTINGS.write_text('{"hooks": { 少个引号 }}', encoding="utf-8")
runner = HookRunner(settings_path=str(SETTINGS), project_dir=str(TMP))
check("specs 为空", runner.specs == [], runner.specs)
check("提示里带行号", any("第 1 行" in n for n in runner.notes), runner.notes)

print("\n[4] matcher 语法")
check("空 = 全部", _matcher_matches("", "cmd_runner"))
check("* = 全部", _matcher_matches("*", "cmd_runner"))
check("精确匹配", _matcher_matches("file_write", "file_write"))
check("| 分隔多个", _matcher_matches("file_write|file_edit", "file_edit"))
check("不匹配就是 False", not _matcher_matches("file_write", "cmd_runner"))
check("正则", _matcher_matches("mcp__.*", "mcp__github__list"))
check("大小写敏感（和 Claude Code 一致）", not _matcher_matches("Bash", "bash"))


async def run_case(hooks, event, payload=None, tool_name=None):
    """把一份配置写下去，跑一次事件，返回 outcome"""
    write_settings(hooks)
    runner = HookRunner(settings_path=str(SETTINGS), project_dir=str(TMP))
    payload = payload or {"session_id": "s1", "cwd": str(TMP), "hook_event_name": event, "tool_name": tool_name, "tool_input": {}}
    return await runner.run_event(event, payload, tool_name=tool_name)


async def main():
    print("\n[5] exit 0 + stdout → 上下文，且 stdin/环境变量都到位")
    echo = make_script("echo.py", PY_ECHO)
    outcome = await run_case(
        {"PreToolUse": [{"hooks": [{"command": echo}]}]}, "PreToolUse", tool_name="file_read"
    )
    check("不阻断", not outcome.blocked, outcome.reason)
    check("stdout 进 context", "tool=file_read" in outcome.context, outcome.context)
    check("THINKER_PROJECT_DIR 已注入", f"project={TMP}" in outcome.context, outcome.context)
    check("session_id 原样传给脚本", "session=s1" in outcome.context, outcome.context)

    print("\n[5.1] 中文输出不乱码")
    py_cn = make_script("cn.py", 'print("工作区干净")\n')
    outcome = await run_case({"SessionStart": [{"hooks": [{"command": py_cn}]}]}, "SessionStart")
    check("Python 脚本的中文正常", outcome.context == "工作区干净", repr(outcome.context))

    if sys.platform == "win32":
        # cmd 内建命令按控制台代码页输出（中文是 cp936），这条专门盯"解码兜底"那段。
        # 实测过：用 locale.getpreferredencoding() 会返回 utf-8（环境开了 UTF-8 模式），
        # 解出来全是问号，必须走 oem/mbcs。
        outcome = await run_case({"SessionStart": [{"hooks": [{"command": "echo 中文测试"}]}]}, "SessionStart")
        check("cmd echo 的中文正常", outcome.context == "中文测试", repr(outcome.context))

    print("\n[6] exit 2 → 阻断，stderr 当原因")
    blocker = make_script("block.py", "import sys\nprint('别删这个目录', file=sys.stderr)\nsys.exit(2)\n")
    outcome = await run_case({"PreToolUse": [{"hooks": [{"command": blocker}]}]}, "PreToolUse", tool_name="cmd_runner")
    check("blocked", outcome.blocked)
    check("原因是 stderr 原文", outcome.reason == "别删这个目录", outcome.reason)

    print("\n[7] exit 0 + JSON decision=block 也能阻断")
    json_blocker = make_script(
        "json_block.py",
        'import json, sys\nprint(json.dumps({"decision": "block", "reason": "json 拦的"}))\n',
    )
    outcome = await run_case({"PreToolUse": [{"hooks": [{"command": json_blocker}]}]}, "PreToolUse", tool_name="x")
    check("blocked", outcome.blocked)
    check("reason 来自 JSON", outcome.reason == "json 拦的", outcome.reason)

    print("\n[8] 退出码 1 → 非阻断错误")
    failer = make_script("fail.py", "import sys\nprint('脚本自己炸了', file=sys.stderr)\nsys.exit(1)\n")
    outcome = await run_case({"PreToolUse": [{"hooks": [{"command": failer}]}]}, "PreToolUse", tool_name="x")
    check("不阻断", not outcome.blocked, outcome.reason)
    check("记进 errors", any("退出码 1" in e for e in outcome.errors), outcome.errors)

    print("\n[9] 超时 → 非阻断 + 提示")
    slow = make_script("slow.py", "import time\ntime.sleep(5)\n")
    outcome = await run_case(
        {"SessionStart": [{"hooks": [{"command": slow, "timeout": 0.5}]}]}, "SessionStart"
    )
    check("不阻断", not outcome.blocked, outcome.reason)
    check("记进 errors", any("超时" in e for e in outcome.errors), outcome.errors)

    print("\n[10] 串行短路：第一个拦下后，后面的 hook 不再执行")
    marker = TMP / "should-not-exist.txt"
    recorder = make_script(
        "record.py",
        f"import pathlib\npathlib.Path(r'{marker}').write_text('ran', encoding='utf-8')\n",
    )
    outcome = await run_case(
        {"PreToolUse": [{"hooks": [{"command": blocker}, {"command": recorder}]}]}, "PreToolUse", tool_name="x"
    )
    check("整体 blocked", outcome.blocked)
    check("后面的 hook 没跑", not marker.exists(), marker)

    print("\n[11] additionalContext 取出来")
    ctx = make_script(
        "ctx.py",
        'import json\nprint(json.dumps({"hookSpecificOutput": {"additionalContext": "格式化完了"}}))\n',
    )
    outcome = await run_case({"PostToolUse": [{"hooks": [{"command": ctx}]}]}, "PostToolUse", tool_name="file_write")
    check("context 正确", outcome.context == "格式化完了", outcome.context)

    print("\n[12] matcher 生效：不匹配的工具不触发")
    outcome = await run_case(
        {"PreToolUse": [{"matcher": "file_write", "hooks": [{"command": blocker}]}]},
        "PreToolUse",
        tool_name="cmd_runner",
    )
    check("其他工具放行", not outcome.blocked, outcome.reason)

    print("\n[13] 超长输出被截断")
    long_out = make_script("long.py", 'print("x" * 10000)\n')
    outcome = await run_case({"SessionStart": [{"hooks": [{"command": long_out}]}]}, "SessionStart")
    check("输出被截到上限附近", len(outcome.context) < 4200, len(outcome.context))
    check("末尾说明了截断", "已截断" in outcome.context, outcome.context[-60:])

    long_err = make_script("long_err.py", 'import sys\nprint("e" * 5000, file=sys.stderr)\nsys.exit(2)\n')
    outcome = await run_case({"PreToolUse": [{"hooks": [{"command": long_err}]}]}, "PreToolUse", tool_name="x")
    check("阻断原因也被截断", outcome.blocked and len(outcome.reason) < 1100, len(outcome.reason))

    print("\n[14] 失败提示节流（同一 hook 最多报 3 次）")
    write_settings({"PreToolUse": [{"hooks": [{"command": failer}]}]})
    runner = HookRunner(settings_path=str(SETTINGS), project_dir=str(TMP))
    payload = {"session_id": "s", "cwd": str(TMP), "hook_event_name": "PreToolUse", "tool_name": "x", "tool_input": {}}
    results = []
    for _ in range(5):  # 同一个实例连跑 5 次，计数才累计
        outcome = await runner.run_event("PreToolUse", payload, tool_name="x")
        results.append(outcome.errors)
    check("前三次都提示", [len(item) for item in results[:3]] == [1, 1, 1], [len(item) for item in results])
    check("第四次说明不再提示", len(results[3]) == 1 and "不再提示" in results[3][0], results[3])
    check("第五次彻底安静", results[4] == [], results[4])
    check("失败本身仍不阻断", not outcome.blocked, outcome.reason)


asyncio.run(main())

shutil.rmtree(TMP, ignore_errors=True)

print(f"\n通过 {passed} 项，失败 {failed} 项\n")
sys.exit(1 if failed else 0)
