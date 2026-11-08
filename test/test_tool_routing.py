# =============================================================================
# 文件：test/test_tool_routing.py
# 作用：验证"用 shell 找文件时，结果末尾会补一句改用 file_glob 的引导"（第 15.3 轮）
#
# 【为什么要有这个测试】
#   第 15.1 轮改了提示词、第 15.2 轮删了路径限制，模型**依然**连发 6 条
#   dir / Get-ChildItem 去找一份简历（会话 20261108-154549，加载的已经是新提示词）。
#   所以加了这层"事后引导"：命令照常执行，只在结果末尾追加一行建议。
#
#   这层逻辑有三个容易出错的地方，都必须被测试守住：
#     1. 识别太宽 → 普通 `dir`、`git status` 也被加提示，变成噪音；
#     2. 识别太窄 → 这次实际用到的 `dir /s /b A\*简历* B\*简历*` 反而没命中；
#     3. 提示拼错位置 → 追加到失败分支之外，或者把结果本身覆盖掉。
#
# 【为什么不用 pytest】
#   和项目里其他测试保持一致：独立脚本、零依赖、用 🎉/❌ 报结论。
# =============================================================================

import asyncio
import sys
import traceback
from pathlib import Path

src_path = str(Path(__file__).resolve().parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from tools.cmd_runner import CmdRunner  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """一条断言：通过就打一行，失败就记下来（最后统一报，别中途中断）"""
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


def test_detect() -> None:
    """识别"这条命令是不是在按文件名找文件" """
    print("\n[1] 命令识别")

    # 该命中的：都是"递归"或"带通配符"的查找写法
    should_hit = [
        # 这条是用户实际踩到的那条（连发 3 条里的第 1 条），必须命中
        r"dir /s /b  C:\Users\Jarvis\Desktop\*简历*  C:\Users\Jarvis\Desktop\*resume* 2>nul | sort",
        r"dir /s /b C:\Users\Jarvis",
        r"dir /b C:\Users\Jarvis\Desktop\*.pdf",
        r"Get-ChildItem -Path D:\ -Recurse -File",
        r'find . -name "*.py"',
        r"where /r C:\ python.exe",
    ]
    for cmd in should_hit:
        check(f"命中  {cmd[:52]}", CmdRunner._looks_like_file_search(cmd) is True)

    # 不该命中的：普通列目录、跑测试、git、查版本
    should_miss = [
        r"dir C:\Users\Jarvis\Desktop",   # 不递归也不带通配符 → 正当需求，别加噪音
        r"Get-ChildItem C:\Users",        # 不递归
        "pytest -q",
        "python --version",
        "git status",
        "echo hello",
        "",                                # 空字符串不能抛异常
    ]
    for cmd in should_miss:
        check(f"不命中 {cmd[:52] or '(空)'}", CmdRunner._looks_like_file_search(cmd) is False)


def test_hint_appended() -> None:
    """真跑命令：查找类要带提示，普通命令不能带"""
    print("\n[2] 执行后的结果")

    # 用项目内的小目录，避免扫盘
    async def run_search():
        return await CmdRunner().act(command=r"dir /s /b D:\code\thinker\test\*.py", timeout=20)

    async def run_plain():
        return await CmdRunner().act(command="echo thinker_ok", timeout=10)

    search_out = asyncio.run(run_search())
    check("查找命令：结果里带提示", "[thinker 提示]" in search_out, search_out[-80:])
    check("查找命令：结果本身没被覆盖", ".py" in search_out, search_out[:80])
    check("提示里给了可照抄的写法", "file_glob(pattern=" in search_out)

    plain_out = asyncio.run(run_plain())
    check("普通命令：不带提示", "[thinker 提示]" not in plain_out, repr(plain_out))
    check("普通命令：输出正常", "thinker_ok" in plain_out, repr(plain_out))

    # 空命令直接返回提示语，不能崩、也不该拼上查找引导
    empty_out = asyncio.run(CmdRunner().act(command=""))
    check("空命令：给出提示且不追加引导", "没有提供任何命令" in empty_out and "[thinker 提示]" not in empty_out)


def run_all() -> int:
    print("开始运行工具路由测试…")
    try:
        test_detect()
        test_hint_appended()
    except Exception:
        print("\n❌ 测试执行中抛出异常：")
        traceback.print_exc()
        return 1

    print()
    if _failed == 0:
        print(f"🎉 所有测试通过（{_passed}/{_passed}）")
        return 0
    print(f"❌ 失败 {_failed} 项（通过 {_passed} 项）")
    return 1


if __name__ == "__main__":
    sys.exit(run_all())
