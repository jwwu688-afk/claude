# =============================================================================
# 文件：src/tools/mcp_config.py
# 作用：MCP 配置文件的「定位 / 读取 / 写入」三件事，供两处共用：
#   · tools/mcp_client/client.py —— 运行时加载（容错优先：坏一条跳一条，程序要能起来）
#   · tools/mcp_cli.py           —— `thinker mcp add/remove` 命令（严格优先：读不懂就停手）
#
# 【为什么单独抽一个文件，而不是塞进 mcp_client 包】
#   1. 「配置在哪个路径」原来只写在 MCPClient._resolve_config_path() 里。
#      现在命令行也要改同一个文件，如果两边各写一份路径逻辑，
#      迟早出现「命令加到了 A 文件、程序却读 B 文件」这类要查半天的问题。真源只留一个。
#   2. mcp_client/__init__.py 会 import client → import mcp、anthropic。
#      命令行只是想改几行 JSON，没必要为此把这套重依赖全拉起来。
#      放在 tools/ 下、只依赖标准库，`thinker mcp list` 就永远是毫秒级的。
#
# 【为什么编辑路径必须「严格读」】
#   改配置是「读整份 → 改一项 → 整份写回」。如果文件本来就不是合法 JSON
#   （少个逗号、手写了 // 注释），却像运行时那样「当作空配置」继续，
#   写回时就会把用户手写的服务器全删掉。丢配置是不可逆的，
#   所以这里宁可报错退出、一个字都不写。
#
# 【一次 add 的落盘过程（对照下面三个函数）】
#   add 命令拿到用户输入
#     → resolve_config_path()  决定改哪个文件（新路径 / 老路径 / 环境变量覆盖）
#     → load_config_dict()     严格读整份，坏就抛错（此时磁盘还没动）
#     → 在内存里改 data["mcpServers"]
#     → save_config_dict()     copy2 备份 → 写 .tmp → os.replace 原子替换
#   任何一步失败，磁盘上要么是「改动前的那份」，要么是「改动后被完整替换的那份」，
#   不存在「写了一半」的中间状态。
#
# 【三个函数的边界】
#   resolve_config_path —— 只回答「在哪」，不碰文件内容；
#   load_config_dict    —— 只回答「现在是什么」，不写；
#   save_config_dict    —— 只负责「安全写回」，不做校验（校验在读的时候做完了）。
#   这样一个函数只改一件事，测试里也容易单独钉住。
# =============================================================================

import json  # 读写 JSON
import os  # 环境变量覆盖、原子替换
import shutil  # 复制备份
from pathlib import Path  # 路径处理
from typing import Any, Dict, Optional  # 类型标注


class MCPConfigError(Exception):
    """配置文件读不懂 / 写不进去时抛这个（表示「已中止，磁盘没被改动」）"""


def resolve_config_path() -> Path:
    """决定当前该用哪个 mcp.json（唯一真源）

    优先级：
      1. 环境变量 THINKER_MCP_CONFIG —— 给测试和「把配置放到别处」用；
      2. ~/.thinker/mcp.json         —— 正常情况；
      3. ~/.quickstar/mcp.json       —— 项目从 QuickStar 改名 Thinker 前的老路径，
         只在「新路径不存在、老路径存在」时兜底，避免老用户升级后外部工具全消失；
      4. 都没有 → 返回新路径（调用方按「文件不存在」处理，程序照常启动）。

    注意第 3 条的判断顺序不能反：一旦新手路径存在，就绝不回退到老路径，
    否则用户在新文件里加了服务器却看不到效果。
    """
    override = os.environ.get("THINKER_MCP_CONFIG")
    if override:  # 显式指定优先，路径可以是相对的（按当前目录解析）
        return Path(override).expanduser()

    new_path = Path("~/.thinker/mcp.json").expanduser()
    if not new_path.exists():  # 只有新文件不存在时才看老文件
        legacy_path = Path("~/.quickstar/mcp.json").expanduser()
        if legacy_path.exists():
            return legacy_path
    return new_path


def load_config_dict(path: Path) -> Dict[str, Any]:
    """严格读取配置文件，返回可直接修改的 dict（保证有 mcpServers 对象）

    与运行时 MCPConfig 的「容错加载」分工不同：
      · MCPConfig：坏一条跳过一条，目标是「程序必须能启动」；
      · 这里：只要有一处读不懂就抛 MCPConfigError，目标是「绝不写坏用户文件」。

    两者为什么不能合并成一份：
      合并只能二选一。选容错，则编辑时会把坏文件当空文件、写回时清空用户配置；
      选严格，则用户配置里一个逗号写错，整个 CLI 都起不来（这正是第 1 轮修过的坑）。
      「要能启动」和「不能写坏」是相反方向的诉求，只能按场景分开。

    文件不存在时返回 {"mcpServers": {}}，让 add 可以顺手创建它。
    """
    if not path.exists():  # 首次使用：不是错误，add 会把它建出来
        return {"mcpServers": {}}

    try:
        # utf-8-sig：Windows 记事本保存带 BOM 的文件时，普通 utf-8 读出来首字符会是
        # "\ufeff"，json.loads 直接报 "Expecting value"。用 utf-8-sig 能同时吃下
        # 带 BOM 和不带 BOM 两种文件，而普通 utf-8 文件用 utf-8-sig 读也没问题。
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:  # 文件存在但读不了（被占用、权限不足）
        raise MCPConfigError(f"配置文件读不了（{error}）") from error

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        # 把行号列号带出来：手写 JSON 出错时，只说「解析失败」等于让人自己逐行数。
        raise MCPConfigError(
            f"配置文件不是合法 JSON（第 {error.lineno} 行第 {error.colno} 列：{error.msg}）"
        ) from error

    if not isinstance(data, dict):  # 最外层必须是 {}
        raise MCPConfigError("配置文件最外层应该是 { } 包裹的对象")

    servers = data.get("mcpServers")
    if servers is None:  # 没有这一栏就补一个空的，后面写回时结构才完整
        data["mcpServers"] = {}
    elif not isinstance(servers, dict):  # 有但不是对象
        raise MCPConfigError("配置里的 mcpServers 应该是 { } 包裹的对象")
    return data


def _detect_indent(path: Path):
    """猜原文件用的缩进（返回 "\t" 或空格数），用于写回时保持一致

    为什么要在意这个：配置文件常被放进 dotfiles 仓库或手动 diff 比对。
    如果用户用 tab 缩进，而我们每次写回都统一成 2 空格，那么改一个服务器，
    diff 里会出现「整份文件重写」——真正改动的那两行淹没在里面，
    以后想 review 或回滚都变难。只改该改的那几行，是「不弄坏用户文件」的一部分。

    猜不出来（空文件、单行 JSON、读不了）时退回 2 空格：json.dumps 的默认风格。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:  # 读不了就按默认来，反正写回时会抛 MCPConfigError
        return 2
    for line in text.splitlines():
        if line.startswith("\t"):  # tab 缩进最常见于手写/编辑器默认配置
            return "\t"
        stripped = line.lstrip(" ")
        leading = len(line) - len(stripped)
        # 至少两个空格才认：单空格缩进不合法（JSON 没这规矩），多半是正文里的空格
        if stripped and leading >= 2:
            return leading
    return 2


def save_config_dict(path: Path, data: Dict[str, Any]) -> Optional[Path]:
    """把 dict 写回配置文件，返回备份文件路径（没有备份时为 None）

    两个保护动作：
      1. 覆盖前先备份成 mcp.json.bak —— 用户手改过的内容，出问题时还能捞回来；
      2. 先写同目录的 .tmp 再 os.replace 原子替换 —— 写到一半断电/被杀进程时，
         原文件还是完整的那一份，不会变成半截 JSON。

    为什么不直接删掉再写：删了再写有个「删除成功、写入失败」的窗口期，
    那段时间里配置文件是不存在的，用户一启动 thinker 就会以为外部工具全丢了。

    .tmp 为什么放在**同一个目录**：os.replace 只有在同一个文件系统内才是原子的。
    放到系统临时目录（可能跨盘）就成了「先复制再删」，断电时两边都可能残缺。
    """
    path.parent.mkdir(parents=True, exist_ok=True)  # ~/.thinker 可能还不存在

    indent = _detect_indent(path) if path.exists() else 2  # 沿用原文件缩进，见 _detect_indent 的注释

    backup_path: Optional[Path] = None
    if path.exists():  # 只有「本来就有文件」才需要备份；首次创建没什么可备份的
        backup_path = path.with_name(path.name + ".bak")
        try:
            shutil.copy2(path, backup_path)  # copy2 连修改时间一起保留
        except OSError as error:
            # 备份失败就不继续了：宁可这次改不成，也不能在没有退路的情况覆盖用户文件。
            raise MCPConfigError(f"备份原文件失败（{error}），已中止，未做任何修改") from error

    tmp_path = path.with_name(path.name + ".tmp")  # 同目录，保证 os.replace 是原子操作
    try:
        # ensure_ascii=False：中文、emoji 原样写进去，人眼看文件时不至于全是 \uXXXX
        # indent 沿用原文件（tab 还是几个空格），避免一次改动让整份文件在 diff 里全变
        # 末尾补 \n：POSIX 惯例，也让 git diff 不会显示「\ No newline at end of file」
        tmp_path.write_text(
            json.dumps(data, indent=indent, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)  # 同盘替换是原子操作，Windows / Linux / macOS 都支持
    except OSError as error:
        # 这里包括「目标文件被别的程序独占」（Windows 常见）：原文件仍然完好
        raise MCPConfigError(f"写入配置文件失败（{error}），原文件未受影响") from error
    finally:
        # 成功时 os.replace 已经把 tmp 移走；失败时会留下半截文件，这里清掉免得下次误读。
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass  # 清不掉也不该盖住上面的真实错误
    return backup_path
