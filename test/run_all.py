#!/usr/bin/env python3
# =============================================================================
# 文件：test/run_all.py
# 作用：一条命令跑完 test/ 下所有离线脚本，最后给一份"谁挂了"的清单
#       用法：python test/run_all.py [--filter 关键字] [--timeout 秒] [--list] [--quiet]
#
# 【为什么要这个文件（2026-11-14 第 36 轮收尾）】
#   项目里 34 个测试脚本都是"函数 + assert"的手写脚本，没有 pytest 收集，
#   平时只能一个个人肉敲。README 里给的办法是：
#
#       for f in test/*.py; do python "$f" > /dev/null || echo "失败：$f"; done
#
#   这条命令有两个洞：
#     1. 它只看**退出码**。而相当一部分脚本的历史写法是"最后 print 一句
#        ❌ 测试失败，然后正常结束"——退出码照样是 0。于是它们明明挂了，
#        批量跑出来却是全绿。收尾那天就是这么发现 3 个"假绿"脚本的。
#     2. 没有汇总。跑完只留下一屏滚动日志，失败了要自己往上翻几十屏找现场。
#
#   所以这里做两件事：**双重判定**（退出码 + 输出里的失败标记）和**汇总报表**。
#   退出码非 0 一律算失败；退出码是 0 但输出里出现 ❌ / "测试失败"，
#   归为"假绿"，同样算失败——它的意思是"这个脚本的退出码不可信，该修了"。
#
# 【为什么用子进程而不是 import 进来跑】
#   每个脚本都会改全局状态：单例（Conversation / HistoryManager / ToolManager）、
#   环境变量、临时工作区。import 进同一个进程里跑，前面的脚本会把后面的污染掉，
#   出现"单独跑能过、批量跑就挂"这种最难查的问题。子进程天然隔离，代价只是启动时间。
#
# 【超时】
#   默认 300 秒。真有卡死的脚本（等 MCP 连接、等键盘输入），超时会被记为失败并
#   标上"超时"，而不是把整个回归挂在那里——这一点比"能跑多快"重要得多。
# =============================================================================

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# 项目根目录 = 本文件所在目录的上一级。所有路径都从这里推，脚本在哪个盘跑都一样。
ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "test"
SRC_DIR = ROOT / "src"

# 判定"输出里有失败"的标记。
# 只认这两个：项目里所有脚本失败时都会打 ❌ 或"测试失败"字样。
# 刻意**不**把 "Traceback" 当成标记：test_full_conversation.py 通过时也会打一段
# MCP 关闭时的 anyio traceback（无害杂音），按它判定会把通过误判成失败。
FAIL_MARKERS = ("❌", "测试失败")

# 单个脚本的默认超时（秒）
DEFAULT_TIMEOUT = 300


def discover(keyword: str = None):
    """列出要跑的脚本（按文件名排序，保证每次顺序一致）

    只收 test_*.py：run_all.py 自己不是 test_ 开头，不会被自己跑一遍。
    """
    files = sorted(TEST_DIR.glob("test_*.py"))
    if keyword:
        files = [f for f in files if keyword in f.name]
    return files


def run_one(path: Path, timeout: int):
    """跑一个脚本，返回 (状态, 耗时秒, 退出码, 合并后的输出)

    状态有三种：
      pass        —— 退出码 0，且输出里没有失败标记
      fake-green  —— 退出码 0，但输出里出现了 ❌ / "测试失败"（退出码不可信）
      fail        —— 退出码非 0（含超时，此时退出码记为 -1）

    为什么把 stdout 和 stderr 合起来看：脚本的 assert 现场走 stderr，
    而总结那一行 ❌ 走 stdout，只看一个会漏。
    """
    env = os.environ.copy()
    # 保证脚本能 import 到 src/ 下的模块：脚本内部虽然也会自己 sys.path.insert，
    # 但那依赖"从 test/ 目录算相对路径"的前提，统一在这里兜一层更稳。
    env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 脚本里有中文/emoji，个别终端编码不同也不至于抛异常
            timeout=timeout,
        )
        code, output = completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as expired:
        # 超时：把已经产生的输出留下来，方便看它卡在哪一步
        partial = (expired.stdout or "") + (expired.stderr or "")
        if isinstance(partial, bytes):  # 超时路径下 Python 可能给 bytes
            partial = partial.decode("utf-8", "replace")
        code, output = -1, partial

    elapsed = time.perf_counter() - started

    if code != 0:
        return "fail", elapsed, code, output
    if any(marker in output for marker in FAIL_MARKERS):
        return "fake-green", elapsed, code, output
    return "pass", elapsed, code, output


def tail(text: str, lines: int = 12) -> str:
    """取输出末尾若干行，作为失败现场

    为什么只看末尾：脚本的失败原因几乎总在最后几行（断言文本 + traceback 尾部），
    头部几十行只是过程日志，全打出来反而把关键信息埋了。
    """
    rows = [row for row in text.splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="跑完 test/ 下所有离线脚本并汇总（不联网、不消耗模型额度）"
    )
    parser.add_argument("-k", "--filter", default=None, help="只跑文件名包含该关键字的脚本")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单个脚本超时秒数")
    parser.add_argument("--list", action="store_true", help="只列出会跑哪些脚本，不执行")
    parser.add_argument("-q", "--quiet", action="store_true", help="只打结果行，不打每个脚本的诊断输出")
    args = parser.parse_args()

    scripts = discover(args.filter)
    if not scripts:
        print(f"没有匹配的脚本（目录：{TEST_DIR}，关键字：{args.filter}）")
        return 1

    if args.list:
        print(f"将运行 {len(scripts)} 个脚本：")
        for path in scripts:
            print(f"  · {path.name}")
        return 0

    print(f"开始回归：{len(scripts)} 个脚本，超时 {args.timeout}s/个\n")

    results = []  # [(状态, 脚本名, 耗时, 退出码, 输出)]
    total_started = time.perf_counter()

    for index, path in enumerate(scripts, 1):
        status, elapsed, code, output = run_one(path, args.timeout)
        results.append((status, path.name, elapsed, code, output))

        # 结果行的符号沿用项目既有约定：✓ 通过、✗ 失败
        mark = {"pass": "✓", "fake-green": "!", "fail": "✗"}[status]
        note = ""
        if status == "fake-green":
            note = "  ← 退出码 0 但有失败输出（假绿，退出码不可信）"
        elif status == "fail" and code == -1:
            note = "  ← 超时"
        print(f"[{index:>2}/{len(scripts)}] {mark} {path.name}  {elapsed:.1f}s{note}")

        # 不安静模式下，失败当场把现场贴出来（回头再翻汇总也行，但当场看最省事）
        if status != "pass" and not args.quiet:
            for row in tail(output).splitlines():
                print(f"      {row}")

    total_elapsed = time.perf_counter() - total_started

    passed = [r for r in results if r[0] == "pass"]
    fake = [r for r in results if r[0] == "fake-green"]
    failed = [r for r in results if r[0] == "fail"]

    print("\n" + "-" * 60)
    print(
        f"合计：通过 {len(passed)} / 假绿 {len(fake)} / 失败 {len(failed)}"
        f"（共 {len(results)}，用时 {total_elapsed:.1f}s）"
    )

    if fake:
        print("\n假绿（退出码是 0，但输出里报了失败，批量脚本会把它当成功）：")
        for _, name, _, _, _ in fake:
            print(f"  · {name}")
        print("  修法：让脚本失败时返回非 0（run_all_tests 返回 1，__main__ 里 sys.exit）")

    if failed:
        print("\n失败清单：")
        for _, name, elapsed, code, output in failed:
            reason = "超时" if code == -1 else f"退出码 {code}"
            print(f"  · {name}（{reason}，{elapsed:.1f}s）")
            for row in tail(output, 8).splitlines():
                print(f"      {row}")

    if not fake and not failed:
        print("🎉 全部通过")
        return 0

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 手动中断不该打印一整段 traceback，用户自己按的 Ctrl+C，他知道发生了什么
        print("\n已中断。")
        sys.exit(130)
