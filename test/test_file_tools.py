# =============================================================================
# 文件：test/test_file_tools.py
# 作用：测试第 8 轮新增的五个文件工具（file_read / file_write / file_edit /
#       file_glob / file_grep），以及它们共用的路径边界、先读后写等规则。
#
# 和 test_crop_message.py 一样，这里不是"正式测试框架"，而是一堆
# "函数 + assert"：assert 要求某件事必须为真，不成立就抛 AssertionError。
# 直接运行方式：在项目根目录执行 python test/test_file_tools.py
#
# 注意：全程用临时目录造假文件，不碰项目里的真实源码，也不调模型 API，
# 所以跑起来很快、不花钱、不会改坏任何东西。
#
# 【2026-11-06 第 8 轮】新增本文件。
# =============================================================================

import asyncio  # 工具的 act() 是 async，要用 asyncio.run 把它跑起来
import os  # 用来改环境变量（测试工作区边界开关）
from pathlib import Path  # 路径对象，拼临时目录里的文件路径
import sys  # 改"模块搜索路径"
import tempfile  # 造临时目录，测试完自动删掉
import traceback  # 失败时打印完整的错误现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# 和 test_crop_message.py 一样：从 test/ 跑脚本时 Python 找不到 src/ 下的模块，
# 所以手动把 "../src" 塞进搜索路径第一位

from tools import file_tools  # 导入整个模块（要改它的 WORKSPACE_ROOT，不能只导入类）
from tools.file_tools import (  # 导入五个工具类和"已读文件"记录表
    FileRead, FileWrite, FileEdit, FileGlob, FileGrep, _read_files,
)


_TMP = None  # 全局：当前这个临时工作区（每个用例开始前会重建一次）


def run(coro):
    """同步地跑一次异步工具调用（每个断言单独开一个事件循环）"""
    return asyncio.run(coro)


def setup_workspace():
    """准备一个干净的工作区，返回它的路径（2026-11-06 第 8 轮新增）

    为什么要每次重建？
      五个工具共用模块级的 WORKSPACE_ROOT 和 _read_files，
      上一个用例留下的文件和"已读记录"会污染下一个用例。
    """
    global _TMP
    if _TMP is not None:  # 先把上一个临时目录清掉
        _TMP.cleanup()
    _TMP = tempfile.TemporaryDirectory(prefix="thinker_filetools_")  # 新建一个
    root = Path(_TMP.name).resolve()
    file_tools.WORKSPACE_ROOT = root  # 把工具认的工作区指到临时目录
    _read_files.clear()  # 清空"已读文件"记录
    return root


def write(root: Path, relative: str, text: str) -> Path:
    """在临时工作区里造一个 UTF-8 文本文件，返回它的路径"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)  # 父目录不存在就建出来
    path.write_text(text, encoding="utf-8")  # 明确指定 UTF-8，别让 Windows 猜编码
    return path


def test_read_basic():
    """测试：读文件要带行号、要有总行数回执、要记下"已读" """
    print("测试: file_read 基本读取")
    root = setup_workspace()
    write(root, "a.py", "第一行\n第二行\n第三行")

    out = run(FileRead().act(path="a.py"))  # 相对路径：应该自动补上工作区根目录

    assert "1\t第一行" in out, out  # 行号 + 制表符 + 内容
    assert "3\t第三行" in out, out
    assert "共 3 行" in out, out  # 回执里要说明文件一共有多少行
    assert str(root / "a.py") in _read_files, "读完后应该记下内容指纹"  # 供后续 edit 校验
    print("✓ 通过: 带行号输出，且记录了已读指纹")


def test_read_offset_limit():
    """测试：offset / limit 能正确分段，且不会把没读的行混进来"""
    print("\n测试: file_read 分段读取")
    root = setup_workspace()
    write(root, "long.txt", "\n".join(f"line{i}" for i in range(10)))

    out = run(FileRead().act(path="long.txt", offset=3, limit=2))

    assert "4\tline3" in out, out  # 第 4 行是 line3（行号从 1 开始）
    assert "5\tline4" in out, out
    assert "line0" not in out, out  # 前面的行不该出现
    assert "line5" not in out, out  # 后面的行也不该出现
    assert "本次显示第 4-5 行" in out, out
    print("✓ 通过: 分段读取范围正确")


def test_read_long_line_and_empty():
    """测试：超长行截断、空文件给明确提示"""
    print("\n测试: file_read 长行与空文件")
    root = setup_workspace()
    write(root, "big.txt", "x" * 3000)  # 一行 3000 个字符，超过 MAX_LINE_CHARS
    out = run(FileRead().act(path="big.txt"))
    assert "已截断" in out, out  # 要告诉模型"这行不完整"
    assert "x" * 2000 in out, out  # 前 2000 个字符还在

    write(root, "empty.txt", "")
    out = run(FileRead().act(path="empty.txt"))
    assert "是空文件" in out, out  # 别让模型以为读取失败
    print("✓ 通过: 长行截断与空文件提示")


def test_read_errors():
    """测试：文件不存在、给的是目录、二进制、路径越界都要给中文提示"""
    print("\n测试: file_read 各类错误")
    root = setup_workspace()

    out = run(FileRead().act(path="missing.py"))
    assert "文件不存在" in out, out

    (root / "adir").mkdir()
    out = run(FileRead().act(path="adir"))
    assert "这不是一个文件" in out, out

    (root / "blob.bin").write_bytes(b"\x00\x01\x02\x00")  # 前几个字节里有 \x00
    out = run(FileRead().act(path="blob.bin"))
    assert "二进制文件" in out, out

    outside = root.parent / "outside.txt"  # 工作区外面（父目录）的一个路径
    out = run(FileRead().act(path=str(outside)))
    assert "路径超出工作区" in out, out

    out = run(FileRead().act(path=""))  # 参数本身为空
    assert "路径参数无效" in out, out
    print("✓ 通过: 五类错误都给出可理解的中文提示")


def test_write_basic():
    """测试：新建文件、中文内容按 UTF-8 落盘、回执正确"""
    print("\n测试: file_write 新建文件")
    root = setup_workspace()

    out = run(FileWrite().act(path="b.txt", content="hello\n世界"))
    assert "已写入" in out, out
    assert (root / "b.txt").read_text(encoding="utf-8") == "hello\n世界", "落盘内容不对"
    assert "2 行" in out, out
    assert str(root / "b.txt") in _read_files, "写完后应记下内容指纹，方便紧接着 edit"
    # 紧接着改一次：验证"写完后记下的指纹"和磁盘上的字节一致，不会被过期检测误拦
    out = run(FileEdit().act(path="b.txt", old_string="hello", new_string="hi"))
    assert "已修改" in out, out
    print("✓ 通过: 新建文件并按 UTF-8 正确落盘，写后可接着 edit")


def test_write_guards():
    """测试：父目录不存在、覆盖未读文件、内容类型错误都要拦住"""
    print("\n测试: file_write 的三道保护")
    root = setup_workspace()

    out = run(FileWrite().act(path="nodir/c.txt", content="x"))
    assert "父目录不存在" in out, out

    write(root, "exist.txt", "旧内容")
    out = run(FileWrite().act(path="exist.txt", content="新内容"))
    assert "必须先用 file_read" in out, out  # 没读过不许覆盖
    assert (root / "exist.txt").read_text(encoding="utf-8") == "旧内容", "被拒绝时不能改动文件"

    out = run(FileWrite().act(path="bad.txt", content=["不是字符串"]))
    assert "参数类型错误" in out, out

    run(FileRead().act(path="exist.txt"))  # 先读一遍
    out = run(FileWrite().act(path="exist.txt", content="新内容"))
    assert "已写入" in out, out  # 读过之后就能覆盖
    print("✓ 通过: 三道保护都生效，读过之后允许覆盖")


def test_edit_requires_read():
    """测试：没读过就想改，必须拒绝"""
    print("\n测试: file_edit 先读后写门禁")
    root = setup_workspace()
    write(root, "c.py", "def foo():\n    return 1\n")

    out = run(FileEdit().act(path="c.py", old_string="return 1", new_string="return 2"))
    assert "必须先用 file_read" in out, out
    assert (root / "c.py").read_text(encoding="utf-8") == "def foo():\n    return 1\n", "被拒绝时文件不能变"
    print("✓ 通过: 未读先写被拒绝")


def test_edit_success_and_diff():
    """测试：改完要返回 unified diff，且磁盘内容真的变了"""
    print("\n测试: file_edit 修改并返回差异")
    root = setup_workspace()
    path = write(root, "c.py", "def foo():\n    return 1\n")

    run(FileRead().act(path="c.py"))
    out = run(FileEdit().act(path="c.py", old_string="return 1", new_string="return 2"))

    assert "已修改" in out, out
    assert "+    return 2" in out, out  # diff 里的新增行
    assert "-    return 1" in out, out  # diff 里的删除行
    assert "return 2" in path.read_text(encoding="utf-8"), "磁盘内容没改成功"
    assert "替换 1 处" in out, out
    print("✓ 通过: 修改成功且返回 +/- 差异")


def test_edit_errors():
    """测试：原文找不到、不唯一、新旧相同、原文为空都要给提示"""
    print("\n测试: file_edit 参数校验")
    root = setup_workspace()
    write(root, "d.py", "abc\nabc\nxyz\n")
    run(FileRead().act(path="d.py"))

    out = run(FileEdit().act(path="d.py", old_string="不存在", new_string="x"))
    assert "没有找到" in out, out

    out = run(FileEdit().act(path="d.py", old_string="abc", new_string="ABC"))
    assert "出现了 2 次" in out, out  # 不唯一，不能瞎改

    out = run(FileEdit().act(path="d.py", old_string="xyz", new_string="xyz"))
    assert "完全一样" in out, out

    out = run(FileEdit().act(path="d.py", old_string="", new_string="x"))
    assert "不能为空" in out, out
    print("✓ 通过: 四类参数错误都给出中文提示")


def test_edit_stale_and_replace_all():
    """测试：读完后文件被外部改动要拒绝；replace_all 能全部替换"""
    print("\n测试: file_edit 过期检测与全部替换")
    root = setup_workspace()
    path = write(root, "e.py", "x\nx\nx\n")

    run(FileRead().act(path="e.py"))
    path.write_text("别的程序改过了\n", encoding="utf-8")  # 模拟读完之后的外部改动
    out = run(FileEdit().act(path="e.py", old_string="x", new_string="y", replace_all=True))
    assert "被改动过" in out, out  # 不许拿旧内容覆盖新内容

    path.write_text("x\nx\nx\n", encoding="utf-8")
    run(FileRead().act(path="e.py"))  # 重新读一遍
    out = run(FileEdit().act(path="e.py", old_string="x", new_string="y", replace_all=True))
    assert "替换 3 处" in out, out
    assert path.read_text(encoding="utf-8") == "y\ny\ny\n", "三处都应被替换"
    print("✓ 通过: 过期检测与 replace_all 都生效")


def test_edit_crlf_and_multiline():
    """测试：改 CRLF 文件不能产生双重回车，跨行 old_string 要能自动适配换行

    这一条对应真机调试里发现的真实 bug：Windows 上 write_text 会把 \n 转成 \r\n，
    而读取是按原始字节解码的，改一次就变成 \r\r\n（凭空多出空行）。
    """
    print("\n测试: file_edit 处理 CRLF 与跨行替换")
    root = setup_workspace()
    path = root / "crlf.py"
    path.write_bytes(b"line1\r\nline2\r\n")  # 明确用 CRLF 造文件，不依赖系统默认行为

    run(FileRead().act(path="crlf.py"))
    # 模型从带行号输出里抄来的内容用的是 \n，这里照样应该能匹配上
    out = run(FileEdit().act(path="crlf.py", old_string="line1\nline2", new_string="first\nsecond"))

    assert "已修改" in out, out
    assert path.read_bytes() == b"first\r\nsecond\r\n", "应该保持 CRLF，不能变成 \\r\\r\\n"
    assert b"\r\r\n" not in path.read_bytes(), "出现了双重回车"
    print("✓ 通过: CRLF 文件跨行替换后换行风格不变")


def test_glob_basic():
    """测试：按通配符找文件、忽略缓存目录、无匹配与空模式的提示"""
    print("\n测试: file_glob 查找文件")
    root = setup_workspace()
    write(root, "src/a.py", "a")
    write(root, "src/sub/b.py", "b")
    write(root, "src/__pycache__/cached.py", "c")  # 应该被忽略
    write(root, "note.md", "d")

    out = run(FileGlob().act(pattern="**/*.py"))
    assert "src/a.py" in out, out
    assert "src/sub/b.py" in out, out
    assert "__pycache__" not in out, out  # 缓存目录不能混进结果

    out = run(FileGlob().act(pattern="src/*.py"))
    assert "src/a.py" in out, out
    assert "src/sub/b.py" not in out, out  # 单星号不跨目录

    out = run(FileGlob().act(pattern="**/*.md"))
    assert "note.md" in out, out

    out = run(FileGlob().act(pattern=""))
    assert "请提供文件名模式" in out, out

    out = run(FileGlob().act(pattern="**/*.zzz"))
    assert "没有找到匹配" in out, out
    print("✓ 通过: 通配符、递归、忽略目录与空结果提示")


def test_grep_basic():
    """测试：按内容搜索、include 过滤、大小写开关、非法正则、无匹配"""
    print("\n测试: file_grep 搜索内容")
    root = setup_workspace()
    write(root, "a.py", "class Foo:\n    pass\n")
    write(root, "b.py", "foo = 1\n")
    write(root, "c.md", "foo in markdown\n")

    out = run(FileGrep().act(pattern="foo"))
    assert "a.py:1:class Foo:" in out, out  # 默认不区分大小写
    assert "b.py:1:foo = 1" in out, out
    assert "c.md:1:foo in markdown" in out, out

    out = run(FileGrep().act(pattern="foo", include="*.py"))
    assert "a.py" in out, out
    assert "c.md" not in out, out  # include 把 md 排除了

    out = run(FileGrep().act(pattern="Foo", case_sensitive=True))
    assert "a.py" in out, out
    assert "b.py" not in out, out  # 区分大小写时小写 foo 不该命中

    out = run(FileGrep().act(pattern="["))
    assert "正则表达式无效" in out, out

    out = run(FileGrep().act(pattern="zzzzzz"))
    assert "没有找到匹配" in out, out
    print("✓ 通过: 搜索、过滤、大小写与错误提示")


def test_workspace_boundary_switch():
    """测试：默认挡住工作区外；设 THINKER_ALLOW_OUTSIDE=1 后放行"""
    print("\n测试: 工作区边界开关")
    root = setup_workspace()
    other = tempfile.TemporaryDirectory(prefix="thinker_outside_")  # 另一个目录，模拟工作区外
    outside_file = Path(other.name) / "outside.txt"
    outside_file.write_text("外面的内容", encoding="utf-8")

    out = run(FileRead().act(path=str(outside_file)))
    assert "路径超出工作区" in out, out  # 默认拒绝

    os.environ["THINKER_ALLOW_OUTSIDE"] = "1"  # 打开开关
    try:
        out = run(FileRead().act(path=str(outside_file)))
        assert "外面的内容" in out, out  # 放行后能读到
    finally:
        os.environ.pop("THINKER_ALLOW_OUTSIDE", None)  # 一定要还原，别影响后面的用例
        other.cleanup()  # 删掉临时目录
    print("✓ 通过: 默认拒绝越界，开关打开后放行")


def test_schemas_and_prompt_consistency():
    """测试：五个工具的自我介绍完整，且提示词里不再教模型用不存在的工具"""
    print("\n测试: 工具说明书与提示词一致性")

    expected = {  # 工具名 -> 必填参数
        "file_read": ["path"],
        "file_write": ["path", "content"],
        "file_edit": ["path", "old_string", "new_string"],
        "file_glob": ["pattern"],
        "file_grep": ["pattern"],
    }
    for tool in (FileRead(), FileWrite(), FileEdit(), FileGlob(), FileGrep()):
        schema = tool.json_schema()
        assert schema["type"] == "function", schema
        assert schema["function"]["name"] in expected, schema
        params = schema["function"]["parameters"]
        assert params["required"] == expected[schema["function"]["name"]], params
        assert "need_user_approve" in params["properties"], "所有新工具都要带授权开关"
        assert len(schema["function"]["description"]) > 50, "说明书不能是空的"
    print("  · 五个工具的 schema 均完整")

    # 静态检查：读源码文本，确认老的 Linux 命令指引已经被换掉
    src_root = Path(__file__).resolve().parent.parent / "src"
    cmd_doc = (src_root / "tools" / "cmd_runner.py").read_text(encoding="utf-8")
    assert "sed -i \"起始行,结束行 c 新内容\"" not in cmd_doc, "cmd_runner 说明书还在教 sed -i"
    assert "file_read" in cmd_doc, "cmd_runner 说明书应指向新的文件工具"

    subagent = (src_root / "tools" / "subagent" / "subagent_manager.py").read_text(encoding="utf-8")
    assert "Grep 或 Glob" not in subagent, "子 agent 人设里还写着不存在的工具名"
    assert "file_glob" in subagent, "子 agent 人设应改用真实工具名"

    system_rule = (src_root / "core" / "prompt" / "system_rule.py").read_text(encoding="utf-8")
    assert "file_edit" in system_rule and "file_grep" in system_rule, "系统提示词应包含工具选择规则"
    print("  · 三处提示词均已指向真实工具")
    print("✓ 通过: 说明书与提示词一致")


def test_tool_manager_registration():
    """测试：五个工具真的被登记进工具表（同步实例化，不连 MCP 服务器）"""
    print("\n测试: ToolManager 登记")
    from tools.tool_manager import ToolManager  # 放在这里导入，避免前面用例受它影响

    ToolManager._instance = None  # 清掉单例，保证拿到干净的工具表
    ToolManager._initialized = False
    manager = ToolManager()  # 没有运行中的事件循环，不会去连 MCP 服务器
    names = list(manager.tools.keys())

    for name in ("file_read", "file_write", "file_edit", "file_glob", "file_grep"):
        assert name in names, f"{name} 没有登记进工具表"
    assert names[-1] == "file_grep", "新工具应排在清单靠后位置"
    assert len(names) == 9, f"内置工具应该是 4 个旧的 + 5 个新的，实际 {len(names)}"
    print("✓ 通过: 5 个新工具已登记，共 9 个内置工具")


def run_all_tests():
    """运行所有测试（把上面 16 个用例挨个跑一遍）"""
    print("开始运行文件工具测试...\n")

    tests = [
        test_read_basic,
        test_read_offset_limit,
        test_read_long_line_and_empty,
        test_read_errors,
        test_write_basic,
        test_write_guards,
        test_edit_requires_read,
        test_edit_success_and_diff,
        test_edit_errors,
        test_edit_stale_and_replace_all,
        test_edit_crlf_and_multiline,
        test_glob_basic,
        test_grep_basic,
        test_workspace_boundary_switch,
        test_schemas_and_prompt_consistency,
        test_tool_manager_registration,
    ]

    passed = 0
    try:  # 整体包一层 try，任何一个用例失败都能被抓住
        for test in tests:
            test()  # 跑一个用例
            passed += 1
        print(f"\n🎉 所有测试通过（{passed}/{len(tests)}）")
    except Exception as e:  # 任何一个失败都会跳到这里
        print(f"\n❌ 测试失败（已通过 {passed}/{len(tests)}）：{e}")
        traceback.print_exc()  # 打印完整的出错现场
    finally:
        if _TMP is not None:  # 不管成功失败都把临时目录收掉
            _TMP.cleanup()


if __name__ == "__main__":
    # 直接运行这个文件时才执行（被 import 时不执行）
    run_all_tests()  # 开跑
