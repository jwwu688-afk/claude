# =============================================================================
# 文件：test/test_memory.py
# 作用：验证项目记忆（THINKER.md）的加载、分层、@import 和 /init 骨架生成。
#
# 跑法：python test/test_memory.py
#
# 为什么每个用例都自己造临时目录？
#   记忆加载强依赖"当前目录长什么样"，用真实目录测等于把测试绑死在这台机器上。
#   造一个干净的三层目录，结论才可复现。
# =============================================================================

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.prompt import memory  # noqa: E402

_results = {"pass": 0, "fail": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    """记一条断言结果并打印（和项目其他测试脚本保持一样的风格）"""
    if condition:
        _results["pass"] += 1
        print(f"  ✓ {name}")
    else:
        _results["fail"] += 1
        print(f"  ✗ {name}  {detail}")


def with_isolated_global(tmp_root: Path):
    """把全局记忆路径指到临时目录，避免读到开发机上的 ~/.thinker/THINKER.md

    不隔离的话，本机一旦存在那个文件，所有断言都会被它的内容污染。
    """
    original = memory.GLOBAL_MEMORY_PATH
    memory.GLOBAL_MEMORY_PATH = tmp_root / "global" / "THINKER.md"
    return original


def test_empty(tmp: Path) -> None:
    print("测试: 没有任何记忆文件")
    original = with_isolated_global(tmp)
    try:
        check("discover 返回空列表", memory.discover(tmp) == [])
        check("load 返回空字符串", memory.load(tmp) == "")
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_single(tmp: Path) -> None:
    print("测试: 单个文件被读进来")
    root = tmp / "proj"
    root.mkdir()
    (root / "THINKER.md").write_text("注释一律用中文。", encoding="utf-8")

    original = with_isolated_global(tmp)
    try:
        text = memory.load(root)
        check("内容出现在结果里", "注释一律用中文。" in text)
        check("标注了来源文件", "THINKER.md" in text)
        check("带了引导说明", "项目记忆" in text and "以它为准" in text)
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_layer_order(tmp: Path) -> None:
    print("测试: 外层在前、内层在后（内层优先级更高）")
    outer = tmp / "layer"
    inner = outer / "sub"
    inner.mkdir(parents=True)
    (outer / "THINKER.md").write_text("OUTER_RULE", encoding="utf-8")
    (inner / "THINKER.md").write_text("INNER_RULE", encoding="utf-8")

    original = with_isolated_global(tmp)
    try:
        text = memory.load(inner)
        check("两个文件都被加载", "OUTER_RULE" in text and "INNER_RULE" in text)
        check(
            "内层排在外层之后",
            text.index("OUTER_RULE") < text.index("INNER_RULE"),
            f"outer@{text.find('OUTER_RULE')} inner@{text.find('INNER_RULE')}",
        )
        check("只加载 2 个文件", len(memory.discover(inner)) == 2, str(memory.discover(inner)))
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_import(tmp: Path) -> None:
    print("测试: @import 展开")
    root = tmp / "imp"
    root.mkdir()
    (root / "THINKER.md").write_text("主文件\n@rules.md\n", encoding="utf-8")
    (root / "rules.md").write_text("子文件的规则内容", encoding="utf-8")

    original = with_isolated_global(tmp)
    try:
        text = memory.load(root)
        check("被引入的内容出现", "子文件的规则内容" in text)
        check("保留了来源标记", "来自 rules.md" in text)
        check("@ 语法本身没被原样留下", "@rules.md" not in text.split("来自")[0])
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_import_missing(tmp: Path) -> None:
    print("测试: @import 指向不存在的文件")
    root = tmp / "imp_missing"
    root.mkdir()
    (root / "THINKER.md").write_text("@nope.md", encoding="utf-8")

    original = with_isolated_global(tmp)
    try:
        text = memory.load(root)
        check("给出提示而不是崩", "找不到文件" in text, text[:80])
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_import_cycle(tmp: Path) -> None:
    print("测试: @import 循环引用不会死循环")
    root = tmp / "imp_cycle"
    root.mkdir()
    (root / "THINKER.md").write_text("@b.md", encoding="utf-8")
    (root / "b.md").write_text("@THINKER.md", encoding="utf-8")

    original = with_isolated_global(tmp)
    old_depth = memory.MAX_IMPORT_DEPTH
    memory.MAX_IMPORT_DEPTH = 5  # 保证循环有机会被触发，而不是被深度先拦住
    try:
        text = memory.load(root)  # 能返回就说明没死循环
        check("返回了结果且提示循环", "循环引用" in text, text[:120])
    finally:
        memory.GLOBAL_MEMORY_PATH = original
        memory.MAX_IMPORT_DEPTH = old_depth


def test_truncate(tmp: Path) -> None:
    print("测试: 超长文件被截断并说明")
    root = tmp / "long"
    root.mkdir()
    (root / "THINKER.md").write_text("A" * 500, encoding="utf-8")

    original = with_isolated_global(tmp)
    old_limit = memory.MAX_FILE_CHARS
    memory.MAX_FILE_CHARS = 100
    try:
        text = memory.load(root)
        check("出现截断提示", "已截断" in text)
        check("正文没超过上限太多", len(text) < 400, f"len={len(text)}")
    finally:
        memory.GLOBAL_MEMORY_PATH = original
        memory.MAX_FILE_CHARS = old_limit


def test_init_content(tmp: Path) -> None:
    print("测试: /init 骨架内容")
    root = tmp / "initproj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'", encoding="utf-8")
    (root / "src" / "app.py").write_text("print(1)", encoding="utf-8")

    content = memory.build_init_content(root)
    check("标题正确", content.startswith("# THINKER.md"))
    check("识别出 Python 技术栈", "pyproject.toml" in content)
    check("画出了目录树", "src/" in content and "app.py" in content)
    check("保留了待填写的约定小节", "代码约定" in content and "不要做的事" in content)
    check("提示了常用命令的位置", "常用命令" in content)


def test_write_init(tmp: Path) -> None:
    print("测试: 写入骨架时不覆盖已有文件")
    root = tmp / "writeproj"
    root.mkdir()
    target = root / "THINKER.md"

    first = memory.write_init_file(root)
    check("文件被创建", first.exists())
    check("内容非空", len(first.read_text(encoding="utf-8")) > 200)

    target.write_text("我自己写的规则", encoding="utf-8")
    memory.write_init_file(root)  # 不带 force
    check("已存在时不覆盖", target.read_text(encoding="utf-8") == "我自己写的规则")

    memory.write_init_file(root, force=True)  # 带 force
    check("force 时重新生成", "代码约定" in target.read_text(encoding="utf-8"))


def test_write_global(tmp: Path) -> None:
    print("测试: 生成全局记忆骨架")
    fake_home = tmp / "fake_home" / "THINKER.md"
    original = memory.GLOBAL_MEMORY_PATH
    memory.GLOBAL_MEMORY_PATH = fake_home  # 不碰真实的 ~/.thinker
    try:
        path = memory.write_init_file(scope="global")
        check("文件被创建", path.exists())
        check("路径指向全局位置", path == fake_home, str(path))

        content = path.read_text(encoding="utf-8")
        check("标题是全局记忆", content.startswith("# 全局记忆"))
        check("含说话方式一节", "说话方式" in content)
        check("含红线（不要自动提交）", "不要自动 git commit" in content)
        check("不含项目专属内容", "目录结构" not in content and "技术栈" not in content)

        fake_home.write_text("我自己写的全局规则", encoding="utf-8")
        memory.write_init_file(scope="global")
        check("已存在时不覆盖", fake_home.read_text(encoding="utf-8") == "我自己写的全局规则")

        memory.write_init_file(scope="global", force=True)
        check("force 时重新生成", "全局记忆" in fake_home.read_text(encoding="utf-8"))
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_global_loaded_first(tmp: Path) -> None:
    print("测试: 全局记忆先于项目记忆加载")
    root = tmp / "gproj"
    root.mkdir()
    (root / "THINKER.md").write_text("PROJECT_RULE", encoding="utf-8")

    fake_home = tmp / "home2" / "THINKER.md"
    fake_home.parent.mkdir(parents=True)
    fake_home.write_text("GLOBAL_RULE", encoding="utf-8")

    original = memory.GLOBAL_MEMORY_PATH
    memory.GLOBAL_MEMORY_PATH = fake_home
    try:
        text = memory.load(root)
        check("两个都在", "GLOBAL_RULE" in text and "PROJECT_RULE" in text)
        check(
            "全局排在项目之前",
            text.index("GLOBAL_RULE") < text.index("PROJECT_RULE"),
        )
    finally:
        memory.GLOBAL_MEMORY_PATH = original


def test_prompt_manager(tmp: Path) -> None:
    print("测试: 系统提示词里包含项目记忆")
    root = tmp / "pmproj"
    root.mkdir()
    (root / "THINKER.md").write_text("PROJECT_MEMO_MARKER", encoding="utf-8")

    original = with_isolated_global(tmp)
    old_cwd = Path.cwd()
    try:
        import os

        os.chdir(root)  # PromptManager 走的是 cwd，只能真的切目录
        from core.prompt.prompt_manager import PromptManager

        prompt = PromptManager().get_system_prompt()
        check("包含记忆内容", "PROJECT_MEMO_MARKER" in prompt)
        check("包含行为规矩", "语气与风格" in prompt, prompt[:60])
        check("包含环境信息", "当前工作目录" in prompt)
        check(
            "顺序是 规矩 → 记忆 → 环境",
            prompt.index("语气与风格") < prompt.index("PROJECT_MEMO_MARKER") < prompt.index("当前工作目录"),
        )
    finally:
        import os

        os.chdir(old_cwd)
        memory.GLOBAL_MEMORY_PATH = original


def test_no_memory_section(tmp: Path) -> None:
    print("测试: 没有记忆文件时，系统提示词里不出现空的记忆段")
    root = tmp / "nomemo"
    root.mkdir()

    original = with_isolated_global(tmp)
    old_cwd = Path.cwd()
    try:
        import os

        os.chdir(root)
        from core.prompt.prompt_manager import PromptManager

        prompt = PromptManager().get_system_prompt()
        check("没有『项目记忆』标题", "## 项目记忆" not in prompt)
        check("规矩和环境仍然都在", "语气与风格" in prompt and "当前工作目录" in prompt)
    finally:
        import os

        os.chdir(old_cwd)
        memory.GLOBAL_MEMORY_PATH = original


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="thinker_memory_") as raw:
        tmp = Path(raw)
        test_empty(tmp)
        test_single(tmp)
        test_layer_order(tmp)
        test_import(tmp)
        test_import_missing(tmp)
        test_import_cycle(tmp)
        test_truncate(tmp)
        test_init_content(tmp)
        test_write_init(tmp)
        test_write_global(tmp)
        test_global_loaded_first(tmp)
        test_prompt_manager(tmp)
        test_no_memory_section(tmp)

    total = _results["pass"] + _results["fail"]
    print()
    if _results["fail"] == 0:
        print(f"🎉 所有测试通过（{_results['pass']}/{total}）")
    else:
        print(f"❌ {_results['fail']} 项失败（通过 {_results['pass']}/{total}）")
        sys.exit(1)


if __name__ == "__main__":
    main()
