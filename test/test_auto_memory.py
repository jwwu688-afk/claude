# =============================================================================
# 文件：test/test_auto_memory.py
# 作用：验证"自动记忆"（MEMORY.md / memory 目录）的路径计算、索引读取、
#       注入文本生成，以及最关键的权限放行边界。
#
# 跑法：python test/test_auto_memory.py
#
# 为什么权限部分要单独测：
#   这是本轮唯一"主动放宽"的地方。放宽错了（比如 is_memory_path 判断太松），
#   模型就能不经确认改任意文件。所以既有"该放行的要放行"，也有
#   "不该放行的绝不放行"（相似路径、父目录、别的工作区）。
# =============================================================================

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import auto_memory  # noqa: E402

_results = {"pass": 0, "fail": 0}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _results["pass"] += 1
        print(f"  ✓ {name}")
    else:
        _results["fail"] += 1
        print(f"  ✗ {name}  {detail}")


def use_tmp_projects(tmp: Path) -> Path:
    """把记忆根目录指到临时位置，返回旧值（调用方负责还原）

    不隔离的话会读写真实的 ~/.thinker/projects，测试跑一次就污染一次。
    """
    original = auto_memory.PROJECTS_DIR
    auto_memory.PROJECTS_DIR = tmp / "projects"
    return original


def test_slug(tmp: Path) -> None:
    print("测试: 项目路径转目录名")
    check(
        "Windows 盘符路径",
        auto_memory.project_slug(Path(r"D:\code\thinker")) == "D--code-thinker",
        auto_memory.project_slug(Path(r"D:\code\thinker")),
    )
    check(
        "正斜杠也能处理",
        auto_memory.project_slug(Path("D:/code/thinker")) == "D--code-thinker",
    )
    check(
        "不同项目不会撞名",
        auto_memory.project_slug(Path(r"D:\code\a")) != auto_memory.project_slug(Path(r"D:\code\b")),
    )


def test_paths(tmp: Path) -> None:
    print("测试: 记忆目录与索引路径")
    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj"
        project.mkdir()
        root = auto_memory.memory_root(project)
        check("目录在 projects 下", str(root).endswith("memory"), str(root))
        check("索引文件名正确", auto_memory.index_path(project).name == "MEMORY.md")

        check("建目录前不存在", not root.is_dir())
        auto_memory.ensure_dir(project)
        check("ensure_dir 之后存在", root.is_dir())
        auto_memory.ensure_dir(project)
        check("重复调用不报错", root.is_dir())
    finally:
        auto_memory.PROJECTS_DIR = original


def test_is_memory_path(tmp: Path) -> None:
    print("测试: 记忆目录边界（放开要准，不能放开过头）")
    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj2"
        project.mkdir()
        root = auto_memory.memory_root(project)
        root.mkdir(parents=True)

        check("目录内的文件 → 是", auto_memory.is_memory_path(root / "a.md", project))
        check("子目录 → 是", auto_memory.is_memory_path(root / "sub" / "b.md", project))
        check("目录本身 → 是", auto_memory.is_memory_path(root, project))
        check("上一级目录 → 不是", not auto_memory.is_memory_path(root.parent, project))
        check("同名前缀的邻居 → 不是", not auto_memory.is_memory_path(str(root) + "-other/a.md", project))
        check("工作区文件 → 不是", not auto_memory.is_memory_path(Path(__file__)))
        check("空路径 → 不是", not auto_memory.is_memory_path(""))
    finally:
        auto_memory.PROJECTS_DIR = original


def test_index(tmp: Path) -> None:
    print("测试: 索引读取与截断")
    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj3"
        project.mkdir()
        check("没有索引时返回空串", auto_memory.load_index(project) == "")

        auto_memory.ensure_dir(project)
        index = auto_memory.index_path(project)
        index.write_text("# 索引\n- [偏好简洁](a.md) — 回复要短\n", encoding="utf-8")
        text = auto_memory.load_index(project)
        check("读到了内容", "偏好简洁" in text)

        # 超行数截断
        index.write_text("\n".join(f"第 {i} 行" for i in range(300)), encoding="utf-8")
        text = auto_memory.load_index(project)
        check("超过 200 行被截断", "已截断" in text)
        check("截断后行数受控", len(text.splitlines()) < 210, str(len(text.splitlines())))
    finally:
        auto_memory.PROJECTS_DIR = original


def test_entries(tmp: Path) -> None:
    print("测试: 记忆条数统计")
    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj4"
        project.mkdir()
        auto_memory.ensure_dir(project)
        root = auto_memory.memory_root(project)

        check("空目录 0 条", auto_memory.count_entries(project) == 0)
        (root / "a.md").write_text("x", encoding="utf-8")
        (root / "b.md").write_text("x", encoding="utf-8")
        (root / "MEMORY.md").write_text("index", encoding="utf-8")
        (root / "notes.txt").write_text("不算", encoding="utf-8")
        check("只数 .md 且排除索引", auto_memory.count_entries(project) == 2, str(auto_memory.count_entries(project)))
        check("列表不含索引", auto_memory.list_entries(project) == ["a.md", "b.md"], str(auto_memory.list_entries(project)))
    finally:
        auto_memory.PROJECTS_DIR = original


def test_render(tmp: Path) -> None:
    print("测试: 注入文本")
    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj5"
        project.mkdir()
        check("没有目录时返回空串", auto_memory.render_for_prompt(project) == "")

        auto_memory.ensure_dir(project)
        text = auto_memory.render_for_prompt(project)
        check("有目录就注入", "## 你的记忆" in text)
        check("给出路径", "memory" in text)
        check("空索引有提示", "还没有任何记忆" in text)
        check("说明怎么用", "file_read" in text and "file_write" in text)

        auto_memory.index_path(project).write_text("- [x](x.md) — 测试", encoding="utf-8")
        text = auto_memory.render_for_prompt(project)
        check("索引内容进注入", "[x](x.md)" in text)
        check("不再显示空提示", "还没有任何记忆" not in text)

        # 到上限时提醒合并
        root = auto_memory.memory_root(project)
        for i in range(auto_memory.MAX_ENTRIES):
            (root / f"m{i}.md").write_text("x", encoding="utf-8")
        text = auto_memory.render_for_prompt(project)
        check("达到上限提醒合并", "上限" in text)
    finally:
        auto_memory.PROJECTS_DIR = original


def test_slugify(tmp: Path) -> None:
    print("测试: 文件名规范化")
    check("中文保留", auto_memory.slugify("用户偏好简洁") == "用户偏好简洁")
    check("空格转连字符", auto_memory.slugify("user prefers concise") == "user-prefers-concise")
    check("非法字符被清理", "/" not in auto_memory.slugify("a/b:c*d"))
    check("连续连字符合并", auto_memory.slugify("a   b") == "a-b")
    check("空标题有兜底", auto_memory.slugify("") == "memory")


def test_permission_boundary(tmp: Path) -> None:
    print("测试: 权限放宽的边界")
    from core.permissions import classify

    original = use_tmp_projects(tmp)
    # permissions.classify 只认"当前工作目录"对应的记忆目录（生产代码就是这么用的），
    # 所以这里用无参数的 memory_root()，而不是造一个临时项目。
    # PROJECTS_DIR 已被指向临时目录，所以不会碰到真实的 ~/.thinker。
    try:
        auto_memory.ensure_dir()
        inside = auto_memory.memory_root() / "note.md"

        check("记忆目录 file_write 免审批", classify("file_write", {"path": str(inside)}).need_approval is False)
        check("记忆目录 file_edit 免审批", classify("file_edit", {"path": str(inside)}).need_approval is False)
        check("记忆目录 file_read 免审批", classify("file_read", {"path": str(inside)}).need_approval is False)

        check("工作区外的普通文件仍要问", classify("file_write", {"path": "C:/Windows/temp.txt"}).need_approval is True)
        check("工作区内的文件仍要问", classify("file_write", {"path": "src/main.py"}).need_approval is True)
        check("缺 path 参数时不放行", classify("file_write", {}).need_approval is True)
        check("cmd_runner 不受影响", classify("cmd_runner", {"command": "dir"}).need_approval is False)
        check("危险命令仍拦截", classify("cmd_runner", {"command": "rm -rf /"}).need_approval is True)
    finally:
        auto_memory.PROJECTS_DIR = original


def test_end_to_end(tmp: Path) -> None:
    print("测试: 端到端（写文件 → 出现在注入里）")
    import asyncio

    original = use_tmp_projects(tmp)
    try:
        project = tmp / "proj7"
        project.mkdir()
        auto_memory.ensure_dir(project)
        root = auto_memory.memory_root(project)

        # 模拟模型写一条记忆
        (root / "user-prefers-conda.md").write_text(
            "---\nname: 用户用 conda\ndescription: 跑 Python 前要 activate\ntype: project\n---\n"
            "用 D:\\Anaconda\\envs\\thinker\\python.exe 直接调。\n",
            encoding="utf-8",
        )
        (root / "MEMORY.md").write_text(
            "- [用户用 conda](user-prefers-conda.md) — 跑 Python 前要 activate\n",
            encoding="utf-8",
        )

        text = auto_memory.render_for_prompt(project)
        check("索引里有这条", "用户用 conda" in text)
        check("条数统计正确", auto_memory.count_entries(project) == 1)
        check("文件真的落盘", (root / "user-prefers-conda.md").is_file())
    finally:
        auto_memory.PROJECTS_DIR = original


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="thinker_automem_") as raw:
        tmp = Path(raw)
        test_slug(tmp)
        test_paths(tmp)
        test_is_memory_path(tmp)
        test_index(tmp)
        test_entries(tmp)
        test_render(tmp)
        test_slugify(tmp)
        test_permission_boundary(tmp)
        test_end_to_end(tmp)

    total = _results["pass"] + _results["fail"]
    print()
    if _results["fail"] == 0:
        print(f"🎉 所有测试通过（{_results['pass']}/{total}）")
    else:
        print(f"❌ {_results['fail']} 项失败（通过 {_results['pass']}/{total}）")
        sys.exit(1)


if __name__ == "__main__":
    main()
