#!/usr/bin/env python3
# =============================================================================
# 文件：test/test_session_clean.py
# 作用：验证会话存档的"统计 / 压缩归档 / 解压复活 / 删除"四条路径
#       （2026-11-13 第 35 轮：会话文件清理）
#
# 【这个文件最该盯住的三件事】
#   1. **压缩不能丢数据**。用户可能过两个月翻回来看"上次那个报错怎么解决的"，
#      压完如果少一条消息，这个功能就是负资产。
#   2. **压缩失败不能删原文**。磁盘满、文件被占用时，宁可这一次不清，
#      也不能出现"归档写坏了、原文件又删了"。
#   3. **坏归档不能把整个列表弄崩**。一条 .gz 被截断（压缩到一半断电），
#      只应该让那一个会话看起来是空的，不能连累 /resume 打不开。
#
# 全程离线：只操作临时目录，不读真实的 ~/.thinker/sessions。
# =============================================================================

import gzip
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import core.session as session_module  # noqa: E402
from core.session import (  # noqa: E402
    SessionStore,
    _read_session,
    _read_text,
    _session_files,
)

passed = 0
total = 0


def check(name, condition, detail=""):
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"  ✓ {name}" + (f"：{detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"：{detail}" if detail else ""))


def fresh_dir() -> Path:
    """建一个临时目录并让 SessionStore 指向它

    为什么要改模块级 SESSIONS_DIR 而不是只传参：
      usage_report / list_sessions 都是"用 SESSIONS_DIR 找目录"的类方法，
      没有目录参数可传。改这个变量等价于"把家目录换到临时目录"，
      测完整个进程就退出，不需要还原。
    """
    directory = Path(tempfile.mkdtemp(prefix="thinker_clean_test_"))
    session_module.SESSIONS_DIR = directory
    return directory


def write_session(directory: Path, name: str, messages: int = 2, days_ago: int = 0) -> Path:
    """手写一个会话文件，并把 mtime 拨到 days_ago 天前

    为什么不走 SessionStore.create()：
      create 会生成自己的文件名（带当前时间），而这里需要精确控制
      "哪个文件看起来是旧的"。直接拼名字 + os.utime 最直接，
      而且能造出 create 造不出的极端情况（比如 0 条消息的文件）。
    """
    path = directory / name
    lines = [
        json.dumps(
            {
                "type": "header",
                "cwd": str(Path.cwd()),
                "started_at": "2026-11-01T10:00:00",
                "model": "test-model",
            },
            ensure_ascii=False,
        )
    ]
    for i in range(messages):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "ts": 1763000000 + i,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"第 {i} 个问题"}],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if days_ago:
        stamp = time.time() - days_ago * 86400
        os.utime(path, (stamp, stamp))
    return path


# -----------------------------------------------------------------------------
# 1：统计
# -----------------------------------------------------------------------------
def test_usage_report():
    print("测试: 统计（总数 / 总量 / 哪些算旧）")
    directory = fresh_dir()

    new_path = write_session(directory, "20261113-100000-aaaa.jsonl", 3, days_ago=0)
    old1 = write_session(directory, "20260101-100000-bbbb.jsonl", 5, days_ago=40)
    old2 = write_session(directory, "20260102-100000-cccc.jsonl", 2, days_ago=45)

    report = SessionStore.usage_report(days=30)

    expect_total = new_path.stat().st_size + old1.stat().st_size + old2.stat().st_size
    check("总数 3", report["count"] == 3, str(report["count"]))
    check("总字节数正确", report["total_bytes"] == expect_total, f'{report["total_bytes"]} vs {expect_total}')
    check("旧会话 2 个", report["old_count"] == 2, str(report["old_count"]))
    check(
        "旧会话字节数 = 那两之和",
        report["old_bytes"] == old1.stat().st_size + old2.stat().st_size,
        str(report["old_bytes"]),
    )
    check("没有归档时 archived_count 为 0", report["archived_count"] == 0)
    check("最早的文件名对", report["oldest_name"] == "20260101-100000-bbbb.jsonl", str(report["oldest_name"]))
    check("旧会话清单里不含新那个", new_path not in report["old_paths"], str(report["old_paths"]))


# -----------------------------------------------------------------------------
# 2：压缩
# -----------------------------------------------------------------------------
def test_compress_keeps_data():
    print("测试: 压缩归档（数据一条不少）")
    directory = fresh_dir()
    path = write_session(directory, "20260101-100000-bbbb.jsonl", 6, days_ago=40)
    before_size = path.stat().st_size
    before_text = path.read_text(encoding="utf-8")

    result = SessionStore.compress_sessions([path])

    archive = path.with_name(path.name + ".gz")
    check("报告 count = 1", result["count"] == 1, str(result["count"]))
    check("没有失败", result["failed"] == [], str(result["failed"]))
    check("原文件已删除", not path.exists())
    check("归档文件在", archive.exists(), archive.name)
    check("归档确实更小", archive.stat().st_size < before_size,
          f"{before_size} → {archive.stat().st_size}")

    # 最核心的一条：解压后必须与原文逐字节一致
    check("解压回来与原文一致", _read_text(archive) == before_text)

    messages, header, _ = _read_session(archive)
    check("归档里的消息条数对", len(messages) == 6, str(len(messages)))
    check("归档里的 header 还能读到", header.get("model") == "test-model", str(header.get("model")))


def test_compress_twice_is_safe():
    print("测试: 重复压缩不报错、不会二次压缩")
    directory = fresh_dir()
    path = write_session(directory, "20260101-100000-bbbb.jsonl", 2, days_ago=40)
    SessionStore.compress_sessions([path])
    archive = path.with_name(path.name + ".gz")

    result = SessionStore.compress_sessions([archive])  # 拿归档再压一次
    check("已经是归档：跳过", result["count"] == 0, str(result["count"]))
    check("没有失败", result["failed"] == [], str(result["failed"]))
    check("归档还在", archive.exists())
    check("没有压出 .gz.gz", not archive.with_name(archive.name + ".gz").exists())


# -----------------------------------------------------------------------------
# 3：解压复活（resume 归档会话前的那一步）
# -----------------------------------------------------------------------------
def test_open_archive():
    print("测试: 归档解压复活")
    directory = fresh_dir()
    path = write_session(directory, "20260101-100000-bbbb.jsonl", 4, days_ago=40)
    original = path.read_text(encoding="utf-8")
    SessionStore.compress_sessions([path])
    archive = path.with_name(path.name + ".gz")

    restored = SessionStore.open_archive(archive)

    check("返回的是 .jsonl", restored.suffix == ".jsonl", restored.name)
    check("解压出来的文件存在", restored.exists())
    check("内容与压缩前一致", restored.read_text(encoding="utf-8") == original)
    check("归档已被收走（不留两份）", not archive.exists())

    # 普通文件直接原样返回，调用方不用先判断类型
    plain = write_session(directory, "20261113-120000-dddd.jsonl", 1)
    check("普通文件原样返回", SessionStore.open_archive(plain) == plain)


# -----------------------------------------------------------------------------
# 4：删除
# -----------------------------------------------------------------------------
def test_delete():
    print("测试: 删除")
    directory = fresh_dir()
    a = write_session(directory, "20260101-100000-aaaa.jsonl", 2, days_ago=50)
    b = write_session(directory, "20260102-100000-bbbb.jsonl", 2, days_ago=50)
    keep = write_session(directory, "20261113-100000-cccc.jsonl", 2)

    expect_freed = a.stat().st_size + b.stat().st_size
    result = SessionStore.delete_sessions([a, b])

    check("删了 2 个", result["count"] == 2, str(result["count"]))
    check("释放字节数对", result["freed_bytes"] == expect_freed, str(result["freed_bytes"]))
    check("两个都没了", not a.exists() and not b.exists())
    check("没传进来的那个还在", keep.exists())


# -----------------------------------------------------------------------------
# 5：坏归档不能把列表弄崩
# -----------------------------------------------------------------------------
def test_bad_archive_is_survivable():
    print("测试: 坏归档只影响它自己")
    directory = fresh_dir()
    good = write_session(directory, "20261113-100000-aaaa.jsonl", 2)

    bad = directory / "20260101-000000-bad0.jsonl.gz"
    bad.write_bytes(b"this is not a gzip file at all")  # 压缩到一半断电的样子

    check("读坏归档返回空串", _read_text(bad) == "")
    messages, header, compaction = _read_session(bad)
    check("坏归档解析成空会话", messages == [] and header == {}, str(messages))
    check("而且没抛异常", True)

    files = _session_files(directory)
    check("列表里两个都在（坏的也列出来）", len(files) == 2, str([p.name for p in files]))
    check("好的那个不受影响", good in files)

    sessions = SessionStore.list_sessions()
    check("list_sessions 没崩", isinstance(sessions, list))


# -----------------------------------------------------------------------------
# 6：归档文件禁止追加
# -----------------------------------------------------------------------------
def test_append_refused_on_archive():
    print("测试: 归档文件拒写（防止把整份历史写坏）")
    directory = fresh_dir()
    path = write_session(directory, "20260101-100000-bbbb.jsonl", 3, days_ago=40)
    SessionStore.compress_sessions([path])
    archive = path.with_name(path.name + ".gz")
    before_bytes = archive.read_bytes()

    # 直接拿归档路径造一个 store 往上追加 —— 正常流程不会这样，
    # 但 open_archive 解压失败时（磁盘满）就会走到这条路。
    store = SessionStore(archive)
    store.append_message({"role": "user", "content": "这条不该被写进去"})

    check("归档字节没变", archive.read_bytes() == before_bytes)
    check("归档仍能正常解压", len(_read_session(archive)[0]) == 3, str(len(_read_session(archive)[0])))


def test_tiny_session_is_skipped():
    """压不下来（归档反而更大）时必须撤销，原文原样留着

    为什么会有这种情况：gzip 光头部固定开销就二十来字节。一个只有一行 header
    的会话文件才十几个字节，压完必然更大。真实场景里对应"开一次、问一句就退出"
    的小会话——那种会话压了不省空间，/resume 还要多一步解压，不如不动。
    """
    print("测试: 压不下来的极小会话原样保留")
    directory = fresh_dir()
    path = directory / "20260101-100000-tiny.jsonl"
    path.write_text('{"type": "header"}\n', encoding="utf-8")
    stamp = time.time() - 40 * 86400
    os.utime(path, (stamp, stamp))
    before = path.stat().st_size

    result = SessionStore.compress_sessions([path])

    archive = path.with_name(path.name + ".gz")
    check("没有算进已归档", result["count"] == 0, str(result["count"]))
    check("记进了 skipped", result["skipped"] == 1, str(result["skipped"]))
    check("原文还在", path.exists())
    check("原文大小没变", path.stat().st_size == before)
    check("没留下那个更大的归档", not archive.exists())


def test_read_header_only():
    """只读第一行拿 header（第 36 轮）：普通文件、归档、坏文件、非 header 首行

    这里验的是**正确性**。"不读全量"是设计目标，没法在断言里直接测
    （只能测耗时，而耗时在不同机器上不稳，会变成假失败）。
    但正确性必须先立住：读 header 的逻辑一旦把"第一行不是 header"当成
    header，按 cwd 过滤就会漏会话，那种 bug 比慢更难发现。
    """
    print("测试: read_header 只读第一行")
    directory = fresh_dir()

    plain = write_session(directory, "20261113-100000-aaaa.jsonl", 2)
    header = session_module.read_header(plain)
    check("普通文件：能拿到 header", header.get("model") == "test-model", str(header.get("model")))
    check("普通文件：cwd 也对", header.get("cwd") == str(Path.cwd()), str(header.get("cwd")))

    # 归档之后仍要能读出来（走 gzip 读第一行，不解全量）
    archive_source = write_session(directory, "20260101-100000-bbbb.jsonl", 20, days_ago=40)
    SessionStore.compress_sessions([archive_source])
    archive = archive_source.with_name(archive_source.name + ".gz")
    check("归档文件：也能拿到 header", session_module.read_header(archive).get("model") == "test-model")

    # 坏归档：返回空字典，不抛
    bad = directory / "20260101-000000-bad0.jsonl.gz"
    bad.write_bytes(b"broken gzip")
    check("坏归档：返回空字典", session_module.read_header(bad) == {})

    # 第一行不是 header（老格式 / 手改过的文件）：不能硬当成 header
    weird = directory / "20260101-000000-weir.jsonl"
    weird.write_text(
        json.dumps({"type": "message", "message": {"role": "user", "content": "x"}}) + "\n",
        encoding="utf-8",
    )
    check("第一行是消息：返回空字典", session_module.read_header(weird) == {})

    # 空文件
    empty = directory / "20260101-000000-empt.jsonl"
    empty.write_text("", encoding="utf-8")
    check("空文件：返回空字典", session_module.read_header(empty) == {})


def test_list_sessions_filters_by_cwd():
    """按 cwd 过滤时，普通文件和归档都要认（read_header 的真实用途）"""
    print("测试: list_sessions 按项目过滤（含归档）")
    directory = fresh_dir()

    here = str(Path.cwd())
    mine = write_session(directory, "20261113-110000-aaaa.jsonl", 2)
    mine_old = write_session(directory, "20260101-100000-bbbb.jsonl", 3, days_ago=40)

    # 另一个项目的会话：直接把 header 里的 cwd 改掉
    other_path = directory / "20261113-120000-cccc.jsonl"
    other_path.write_text(
        json.dumps({"type": "header", "cwd": "D:\\some\\other\\project", "started_at": "2026-11-13T12:00:00"})
        + "\n",
        encoding="utf-8",
    )

    SessionStore.compress_sessions([mine_old])  # 把它变成归档
    mine_old_archive = mine_old.with_name(mine_old.name + ".gz")

    found = SessionStore.list_sessions(here)
    names = [p.name for p in found]
    check("本项目两个都列出来（含归档）", mine.name in names and mine_old_archive.name in names, str(names))
    check("别的项目不列", other_path.name not in names, str(names))
    check("归档那个确实在列表里", mine_old_archive.exists())


if __name__ == "__main__":
    print("🧪 会话存档清理（离线）\n")
    try:
        test_usage_report()
        print()
        test_compress_keeps_data()
        print()
        test_compress_twice_is_safe()
        print()
        test_open_archive()
        print()
        test_delete()
        print()
        test_bad_archive_is_survivable()
        print()
        test_append_refused_on_archive()
        print()
        test_tiny_session_is_skipped()
        print()
        test_read_header_only()
        print()
        test_list_sessions_filters_by_cwd()
        print()
    finally:
        # 临时目录整份删掉：里面全是测试造的会话文件
        shutil.rmtree(session_module.SESSIONS_DIR, ignore_errors=True)

    if passed == total:
        print(f"🎉 所有测试通过（{passed}/{total}）")
        sys.exit(0)
    print(f"❌ 有 {total - passed} 项失败（{passed}/{total}）")
    sys.exit(1)
