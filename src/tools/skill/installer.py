# =============================================================================
# 文件：src/tools/skill/installer.py
# 作用：从「本地目录」或「GitHub 仓库地址」把一个/一批技能装进 ~/.thinker/skills/
#       （2026-11-10 第 21.5 轮新增，由 /skills install 调用）
#
# 【为什么不放在 commands.py】
#   命令层只该做"解析参数 + 打印结果"；下载、解压、路径校验、冲突处理是另一类活，
#   而且它们有边界条件（压缩包里的 ../、同名覆盖、半个技能目录），
#   单独一个文件才好写测试。commands.py 已经一千行了，再塞这些会变成第二个 conversation.py。
#
# 【一次 /skills install 的时间线】
#   /skills install https://github.com/anthropics/skills
#     → 识别成 GitHub 地址，改写成 codeload 的 tar.gz（见 _github_archive_url 的说明）
#     → 下载到临时目录（urllib，stdlib，不加依赖）
#     → 安全解压（逐条检查成员路径，挡掉 ../ 和绝对路径）
#     → 递归找所有含 SKILL.md 的目录（上限 50 个，防止有人指了个巨型仓库）
#     → 逐个 build_spec 拿真名 → 拷到 ~/.thinker/skills/<name>
#     → 返回报告：装了几个、跳过几个（为什么）、失败了什么
#   命令层拿到报告后：重扫 + 重新登记斜杠命令 + 必要时补登记 skill 工具。
#
# 【和别的模块的分工】
#   skill_spec.py    —— 单个技能"怎么读、怎么渲染"（纯函数，无 IO 意外）
#   skill_manager.py —— "磁盘上现在有哪些技能"（扫描 + 清单）
#   installer.py     —— 本文件："技能怎么进来"（网络 + 文件系统）
#   skill_tool.py    —— "模型怎么加载它"
#   四层各自只依赖下一层的接口，所以安装器坏了不影响已有技能的使用，
#   反过来也一样。这也是为什么它敢直接用 urllib、tarfile 这些"重"东西——
#   它们只在 /skills install 这一条路径上被 import。
#
# 【安全边界（这三条是刻意的）】
#   1. 只拷「含 SKILL.md 的目录」，仓库里其它文件（.git、node_modules、图片）一概不碰；
#   2. 绝不执行任何脚本——安装只做文件复制，跑不跑是以后模型+审批的事；
#   3. 同名不覆盖，除非用户显式加 --force。技能是"写进每轮上下文的指令"，
#      悄悄覆盖掉用户改过的版本，比装不上更糟。
# =============================================================================

import re  # 解析 GitHub 地址
import shutil  # 拷目录、清临时目录
import tarfile  # .tar.gz
import tempfile  # 临时目录
import urllib.request  # 下载（stdlib，不引 requests）
import zipfile  # .zip
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from tools.skill.skill_manager import global_skills_dir  # 装到哪里
from tools.skill.skill_spec import SKILL_FILENAME, build_spec  # 识别技能、取真名

# 一次最多装多少个技能。为什么要有：用户可以随手贴一个 monorepo 地址，
# 里面可能有几百个 SKILL.md；没有上限既会写爆磁盘，也会让工具说明书直接爆掉。
MAX_INSTALL = 50

# 下载超时（秒）。github 在国内经常慢，60 秒对几十兆的包够用；
# 卡住时用户按 Esc 也打断不了（这是同步 urllib），所以宁可让它早点失败。
DOWNLOAD_TIMEOUT = 60


@dataclass
class InstallReport:
    """一次安装的结果（命令层只负责把它打印出来）

    三个列表分别对应三种结局，为什么要分开：
      · installed —— 成功了，要告诉用户"这些东西从现在开始对模型可见"；
      · skipped   —— 名字冲突被跳过，用户需要知道加 --force 能覆盖；
      · errors    —— 下载/解压/拷贝失败，是"哪里坏了"而不是"你选错了"。
    混成一个字符串列表的话，命令层就没法决定用绿色还是红色打印。
    """

    installed: List[Tuple[str, Path]] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """装成功了几个（命令层用它决定"要不要提示重扫"）

        为什么单独给个属性而不让调用方写 len(report.installed)：
          "装成功"的判据以后可能会变（比如把 skipped 里 --force 覆盖的算进去），
          只改一处比改三个调用点安全。
        """
        return len(self.installed)


def install_from(target: str, force: bool = False) -> InstallReport:
    """安装技能；target 可以是本地目录，也可以是 GitHub 仓库地址

    参数 target：用户敲在 /skills install 后面的那串东西。
    参数 force ：True = 同名覆盖。

    返回 InstallReport，**不抛异常**：命令层要把失败原因原样打给用户，
    抛出去会被 handle_command 统一包成"/skills 执行失败：…"，细节就丢了。

    返回值的三种状态可以同时出现（装了 2 个、跳过 3 个、失败 1 条），
    所以报告是三个列表而不是"成功或失败"的二选一——
    一个仓库里混着好坏技能是常态，二选一会逼调用方问"那另一个呢"。
    """
    report = InstallReport()
    target = (target or "").strip()
    if not target:
        report.errors.append("缺少地址。用法：/skills install <GitHub 地址 | 本地目录>")
        return report

    # 整段包在 try/finally 里，是为了保证"临时目录一定被清掉"：
    #   下载中断、解压报错、拷贝到一半失败，任何一种都不该在 %TEMP% 里
    #   留一个几十兆的解压目录。用户清理临时目录时不会想到是 thinker 留下的。
    temp_root: Optional[Path] = None
    try:
        sources, temp_root = _collect_sources(target, report)
        if not sources:
            # 没找到技能不算异常：报告里已经有"为什么没找到"（路径不存在 / 包是空的 /
            # 名字全不合法），命令层照着打印即可，这里不必再补一句。
            return report

        # 目标目录：~/.thinker/skills。parent 不存在也一起建——
        #   全新机器上 ~/.thinker 可能都还没有，不能假设它存在。
        destination_root = global_skills_dir()
        destination_root.mkdir(parents=True, exist_ok=True)

        # 再截一次上限：_find_skill_dirs 已经截过，但本地目录那条路没走它。
        #   两处都截是刻意的——上限是安全属性，不能依赖某一条分支记得处理。
        for source in sources[:MAX_INSTALL]:
            _install_one(source, destination_root, force, report)
    finally:
        # 临时目录一定要清：一个 tar.gz 解压出来可能几十兆，留着纯属占地方。
        # 放在 finally 里是因为中途任何一步 return / 抛异常都不该漏掉它。
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)
    return report


def _install_one(source: Path, destination_root: Path, force: bool, report: InstallReport) -> None:
    """装单个技能目录：解析出真名 → 检查冲突 → 拷贝

    为什么"真名"要 build_spec 来算，而不是直接用目录名：
      frontmatter 里的 `name` 可以和目录名不同（比如目录叫 pdf-skill、name 写 pdf），
      而配置文件决定的是"命令叫什么"。直接用目录名会出现"装好了但命令名对不上"。
    """
    # build_spec 只读前 4KB，很便宜；但它会跑一遍名字校验，
    #   所以"名字不合法的目录"会在这里被挡掉，而不是装进去之后再被扫描器拒载——
    #   后者的表现是"报告说装成功了，/skills 里却没有"，最容易让人以为是缓存问题。
    spec, problems = build_spec(source / SKILL_FILENAME, "global")
    if spec is None:
        # 名字不合法这类问题：跳过但要说清，否则用户会以为"装完了却没生效"。
        report.skipped.append((source.name, "；".join(problems) or "不是合法的技能目录"))
        return

    # 冲突判定用"目标目录是否存在"，不是"同名技能是否已加载"：
    #   后者受扫描时机影响（刚装完还没 reload 时是空的），拿它判断会漏掉覆盖风险。
    destination = destination_root / spec.name
    if destination.exists() and not force:
        report.skipped.append((spec.name, "已存在（加 --force 覆盖）"))
        return

    try:
        # dirs_exist_ok=True 让 --force 能直接覆盖；不带 force 时上面已经拦掉了。
        #   为什么用 copytree 而不是"逐个文件 copy"：技能目录里的子结构
        #   （scripts/、examples/、assets/）必须原样保留，copytree 一次搞定。
        #   代价是它不认"部分失败"——拷到一半磁盘满会留下半个目录，
        #   所以下面把 OSError 记进 errors，让用户知道要手动清理。
        shutil.copytree(source, destination, dirs_exist_ok=True)
    except OSError as error:
        # 只捕 OSError：那是文件系统的失败（占用、权限、磁盘满），
        #   而编程错误（比如类型写错）应该照常抛出来暴露，不该被伪装成"安装失败"。
        report.errors.append(f"{spec.name}：拷贝失败（{type(error).__name__}: {error}）")
        return
    report.installed.append((spec.name, destination))


def _collect_sources(target: str, report: InstallReport) -> Tuple[List[Path], Optional[Path]]:
    """把 target 变成「含 SKILL.md 的目录列表」，外加一个需要事后清理的临时目录

    返回 (sources, temp_root)：
      temp_root 只有在走网络时才不为 None。为什么由调用方清理、而不是在这里 try/finally：
      解压出来的目录就是 sources 本身，清早了就拷不到东西。调用方拷完再清。

    两种来源：
      · 本地目录：若它本身就是技能目录（有 SKILL.md）就只有一个，否则递归找；
      · 网络地址：下载 → 解压 → 递归找。
    """
    # 只认 http/https 前缀，不认 "git@github.com:..." 这种 SSH 写法：
    #   那需要本机有 git 和密钥，而本功能的定位就是"不装 git 也能装技能"。
    if target.startswith(("http://", "https://")):
        return _sources_from_url(target, report)

    # expanduser：允许 `~/my-skills` 这种写法。Windows 上 ~ 也认（映射到用户目录），
    #   不展开的话 Path 会把它当成字面量 "~"，exists() 永远是 False。
    local = Path(target).expanduser()
    if not local.exists():
        report.errors.append(f"路径不存在：{local}")
        return [], None
    if local.is_file():
        # 传了个 .md 文件：单文件形态也支持，但这里不做——拷一个文件到
        # ~/.thinker/skills/<名>.md 的语义和"装一个技能目录"不一样，
        # 让用户手动放更清楚，也不会让人以为装完就带了支撑文件。
        report.errors.append("请给「目录」或「GitHub 地址」；单文件技能请手动放进 ~/.thinker/skills/")
        return [], None
    if (local / SKILL_FILENAME).is_file():
        return [local], None  # 用户指的正好是某个技能目录

    found = _find_skill_dirs(local)
    if not found:
        report.errors.append(f"这个目录里没有找到 {SKILL_FILENAME}：{local}")
    return found, None


def _sources_from_url(url: str, report: InstallReport) -> Tuple[List[Path], Optional[Path]]:
    """下载并解压一个仓库压缩包，返回其中所有技能目录

    为什么不直接 `git clone`：
      1. 有的机器上 git 没有、或者公司网络把 github.com 拦了（本机就是：schannel 握手失败），
         而 codeload/api 反而通；
      2. 我们只要文件，不要历史——tar.gz 比 clone 快一个数量级。
    """
    # 能改写就改写（github 页面 → codeload 包），改不了就原样下——
    #   用户可能直接给了某个 .tar.gz 的直链，那种情况我们不该拦。
    archive_url = _github_archive_url(url) or url
    temp_root = Path(tempfile.mkdtemp(prefix="thinker_skill_"))
    try:
        archive = _download(archive_url, temp_root)
        # 下载物和解压物分开放：有些包解出来是 `repo-main/`，和 archive.bin 同级时
        # 后面 rglob 会连压缩包本身一起扫，白费时间。
        extract_root = temp_root / "src"
        extract_root.mkdir()
        _safe_extract(archive, extract_root)
        found = _find_skill_dirs(extract_root)
        if not found:
            report.errors.append(f"压缩包里没有找到 {SKILL_FILENAME}：{url}")
        # 返回临时目录让调用方在**拷贝完之后**清理：这里清掉的话，
        # found 指向的路径就全没了，后面 copytree 必然失败。
        return found, temp_root
    except Exception as error:  # 网络/解压的失败都要变成一行说明，而不是崩掉命令
        # 失败分支自己清临时目录，然后把 temp_root 置 None 返回——
        #   否则调用方的 finally 会对着一个已经删掉的路径再 rmtree 一次（虽然无害，但没必要）。
        report.errors.append(f"下载或解压失败（{type(error).__name__}: {error}）：{archive_url}")
        shutil.rmtree(temp_root, ignore_errors=True)
        return [], None


def _github_archive_url(url: str) -> Optional[str]:
    """把 github.com 的仓库页地址换成 codeload 的 tar.gz 地址

    为什么必须换：本机 `github.com` 的 TLS 握手会被中断（schannel 报
    "server closed abruptly"），而 `codeload.github.com` 正常。换完还能省掉 git 依赖。

    支持的形状：
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      https://github.com/owner/repo/tree/<branch>
    不认识的（比如已带 .tar.gz、或者是别的站点）返回 None，让调用方原样下载。
    """
    match = re.match(
        r"^https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:/tree/([^/?#]+))?/?$",
        url.strip(),
    )
    if not match:
        return None
    owner, repo, branch = match.group(1), match.group(2), match.group(3) or "main"
    return f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"


def _download(url: str, directory: Path) -> Path:
    """下载到 directory，返回本地文件路径

    文件名从 URL 末段取；取不出来就叫 archive.bin（_safe_extract 会自己嗅探格式）。
    带一个 User-Agent：某些 CDN 对没有 UA 的请求直接 403。
    """
    # 文件名只用来"让 _safe_extract 能嗅探格式"：真正的格式判断在
    #   zipfile.is_zipfile / tarfile.open 里，扩展名不可信（URL 里常常什么都没有）。
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "archive.bin"
    if not name.endswith((".tar.gz", ".tgz", ".zip")):
        name = "archive.bin"
    destination = directory / name
    # 自定义 UA：GitHub 的 codeload 对空 UA 的请求偶尔回 403，
    #   而 urllib 默认就带 "Python-urllib/3.x"，那种 UA 更容易被风控。
    request = urllib.request.Request(url, headers={"User-Agent": "thinker-skill-installer"})
    # 流式拷贝而不是 response.read()：技能包可能有几十兆（本机实测 anthropics/skills 4MB、
    #   解压后 13MB），一次性读进内存没必要；copyfileobj 默认 64KB 一块，内存占用恒定。
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        with open(destination, "wb") as handle:
            shutil.copyfileobj(response, handle)
    return destination


def _safe_extract(archive: Path, destination: Path) -> None:
    """解压 tar.gz / zip，**逐条校验成员路径**

    为什么不用 `extractall()` 直接解：
      压缩包里的成员名可以是 `../../.ssh/authorized_keys`——解压出来就写到目标目录之外了
      （经典的 zip-slip）。这里对每个成员做两件事：
        1. 拒绝绝对路径和含 `..` 的路径；
        2. 解压后不信任软链接（tar 里的 symlink 可以是绝对路径，等于给出一个跳出目录的通道）。
      只复制普通文件和目录，其它类型一律跳过。代价是极端情况会少解几个文件，
      但这些技能本来只需要 .md/.py/.json。
    """
    if zipfile.is_zipfile(archive):
        # 逐条 extract 而不是 extractall：只有这样才能在解之前筛掉危险成员。
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if not _is_safe_member(info.filename):
                    continue  # 绝对路径 / 含 .. 的成员：直接跳过（不报错，继续解安全的）
                bundle.extract(info, destination)
        return

    # tar 分支同理逐成员处理。除了路径，还要跳过非普通文件：
    #   tar 成员可以是符号链接，而 `linkname` 允许写绝对路径——解出来就是一个
    #   指向任意位置的门，之后往这个门里写文件照样能逃出目标目录。
    #   技能只需要 .md/.py/.json，跳过软链接不影响任何正常安装。
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if not (member.isfile() or member.isdir()):
                continue  # 软链接/设备文件：跳过
            if not _is_safe_member(member.name):
                continue
            bundle.extract(member, destination)


def _is_safe_member(name: str) -> bool:
    """压缩包成员路径是否安全（绝对路径 / 上跳都拒）"""
    candidate = Path(name)
    if candidate.is_absolute():
        return False  # /etc/passwd、C://Windows//... 这一类
    # 逐段比较而不是查字符串 "../"：`a/../b` 和 `a\..\b` 写法不同但都危险，
    #   Windows 上还有反斜杠，按 Path 的段来判才两种都拦得住。
    return ".." not in candidate.parts


def _find_skill_dirs(root: Path) -> List[Path]:
    """递归找出所有含 SKILL.md 的目录（排好序，结果稳定）

    排序是为了让"装哪几个"的顺序可预测：一次装 20 个技能时，
    报告里的顺序每次都变会让人以为漏装了什么。
    """
    found = []
    # rglob 找的是"所有叫 SKILL.md 的文件"，转成它们所在目录：
    #   一个目录里只可能有一个 SKILL.md，所以不需要额外去重逻辑，
    #   但显式判一下 `not in found` 更保险（大小写不敏感的文件系统上 rglob 可能重复命中）。
    # rglob 遍历整棵树：4MB 的官方仓库有 446 个文件，扫一遍几十毫秒，
    #   可以接受；真正的耗时在下载，不在这一步。
    for skill_file in sorted(root.rglob(SKILL_FILENAME)):
        directory = skill_file.parent
        # 上限在这里就拦：没必要扫完一个巨仓库再截断。
        if directory not in found:
            found.append(directory)
        if len(found) >= MAX_INSTALL:
            break
    return found
