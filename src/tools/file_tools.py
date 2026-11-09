# =============================================================================
# 文件：src/tools/file_tools.py
# 作用：五个"文件专用工具"，让模型不再靠拼 shell 命令去读写文件。
#
#   file_read   读文件（带行号，可分段）
#   file_write  新建/覆盖整个文件
#   file_edit   精确替换文件里的一段文字（带 diff）
#   file_glob   按文件名模式找文件（支持 ** 递归）
#   file_grep   按内容搜索文件（返回 路径:行号:内容）
#
# 【为什么要新增这一组工具】
#   第 8 轮之前，唯一能干活的手是 cmd_runner，读文件靠 `cat -n`、改文件靠
#   `sed -i`、搜索靠 `grep`。但项目跑在 Windows 上，命令走的是 cmd.exe，
#   这三个命令默认都不存在；而且子 agent 的人设里让它"用 Grep / Glob / Read"，
#   工具清单里却根本没有这三个名字，派活几乎必失败。
#   用 Python 自己实现一遍，跨平台一致、输出可控、还能做安全校验。
#
# 【为什么五个类写在同一个文件里】
#   它们共用同一套东西：路径解析、编码探测、忽略目录、"先读后写"记录。
#   拆成五个文件会把这些公共逻辑抄五遍，改一处漏四处。
#   ToolManager 登记的是类，跟放在哪个文件里没有关系。
#
# 【一个安全设计】
#   先读后写：file_write 覆盖已有文件、file_edit 修改文件之前，
#   必须先用 file_read 读过。而且会比对内容的 SHA1——如果读过之后
#   文件被别的程序改过，会拒绝修改，让模型重新读一遍。
#   这样模型就不会拿"记忆里的旧内容"去覆盖磁盘上的新内容。
#
# 【2026-11-08 第 15.2 轮：取消路径边界】
#   以前这里还有一道"只能操作工作区内文件"的边界，越界就返回错误，
#   要靠环境变量 THINKER_ALLOW_OUTSIDE=1 才能放开。现在整套机制删掉了，
#   五个工具都可以直接操作任意路径。原因不是"边界没意义"，而是它没起到作用：
#
#   1. 边界只在 file_* 上有，cmd_runner 里一行检查都没有。
#      模型遇到"路径超出工作区"，改调 cmd_runner 拼一条 Get-ChildItem 就绕过去了
#      （第 15.1 轮的起因就是查简历时它这么干）。对愿意绕的模型，这道门是软的；
#      对老实守规矩的模型，它只是把简单任务变复杂。
#   2. 它挡不住真正的风险。file_write 的危险是"覆盖错文件"，
#      而这个靠"先读后写 + SHA1 校验 + 审批"已经拦住了，跟路径在不在工作区无关。
#   3. 它带来了实实在在的误伤：搜一份桌面上的文档、读一个兄弟项目里的配置，
#      都得先改 .env 再重启，而这本来是 agent 最日常的动作。
#
#   所以现在的分工是：**路径不设限，风险由权限层管**——
#   写文件默认要问、高危命令强制问（`core/permissions.py`）；
#   cmd_runner 仍然能执行任意命令，这一点从第 9 轮起就没变过。
#   如果将来要重新引入限制，正确的做法是在 classify() 里判，让权限层和工具层
#   说同一句话，而不是让工具层单独拒绝、逼模型去找后门。
#
# 【2026-11-06 第 8 轮】新增本文件。
# =============================================================================

import asyncio  # 【第 15 轮】把阻塞的文件 IO 丢进线程池，多个只读工具才能真正并发
import difflib  # 生成"改前 / 改后"的差异文本（就是 git diff 那种 +/- 格式）
import fnmatch  # 按 *.py 这种通配符筛文件名
import glob as globlib  # 按 **/*.py 这种模式找文件；注意别和工具名 file_glob 搞混
import hashlib  # 算文件内容的 SHA1，用来判断"读过之后文件有没有被改过"
import locale  # 读系统默认编码（中文 Windows 上有些老文件是 GBK）
import os  # 读环境变量、取工作目录、遍历文件夹
import re  # file_grep 用的正则表达式
from pathlib import Path  # 路径对象，处理绝对路径 / 相对路径比字符串方便
from tools.base_tool import BaseTool  # 导入父类：所有工具都得有 act / json_schema / get_status


# 【2026-11-06 第 8 轮】所有"魔法数字"集中放在这里，改的时候只改这一处
# 思路和第 1 轮给 cmd_runner 加的 DEFAULT_TIMEOUT / MAX_OUTPUT_CHARS 一样
MAX_READ_LINES = 2000  # 单次最多读多少行（和 cmd_runner 说明书里 "cat -n 默认 2000 行" 对齐）
MAX_LINE_CHARS = 2000  # 单行超过这么多字符就截断，防止一行几万字把上下文撑爆
MAX_GLOB_RESULTS = 100  # 最多返回多少个匹配的文件
MAX_GREP_RESULTS = 100  # 最多返回多少处匹配
MAX_GREP_LINE_CHARS = 300  # grep 结果里每行最多保留多少字符
MAX_DIFF_CHARS = 4000  # file_edit 返回的差异文本最多多长
MAX_GREP_FILE_BYTES = 1024 * 1024  # 超过 1MB 的文件不参与内容搜索（多半是日志或数据文件）

# 工作区根目录：程序启动时固定下来，整个会话都不变
# 【第 15.2 轮】它不再是"允许访问的范围"，只剩两个用途：
#   1. 把相对路径补全成绝对路径（模型说 "src/main.py" 时补成 WORKSPACE_ROOT/src/main.py）；
#   2. file_glob / file_grep 不传 path 时的默认搜索起点。
# 为什么仍要 resolve()：把 .. 和符号链接展开，相对路径补全才不会补出个半真半假的路径。
WORKSPACE_ROOT = Path(os.getcwd()).resolve()

# 搜索时要跳过的目录。这些目录里要么是缓存、要么是依赖、要么是编辑器配置，
# 搜出来只会污染结果，还特别慢
_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".idea", ".vscode",
    "venv", ".venv", "dist", "build", ".pytest_cache",
}

# 解码文件时按这个顺序试。UTF-8 优先（现在绝大多数源码都是），
# 其次 GBK（中文 Windows 上的老文件），最后系统默认编码
_ENCODINGS = ("utf-8", "gbk", locale.getpreferredencoding(False))

# 记录"本会话读过哪些文件、读的时候内容是什么"
# 字典：绝对路径字符串 -> 当时内容的 SHA1
# 为什么要记内容而不只是记路径？见文件头"先读后写"第 2 点
_read_files = {}


# -----------------------------------------------------------------------------
# 一、五个工具共用的小函数
# -----------------------------------------------------------------------------

def _resolve_path(raw_path):
    """把模型给的路径统一成绝对路径（2026-11-06 第 8 轮新增，第 15.2 轮去掉越界检查）

    返回两个值：(路径对象, 错误文字)。
      成功时错误文字是空字符串；失败时路径是 None，错误文字可以直接交给模型看。
    为什么不直接抛异常？项目里其他工具都是"返回一句中文提示让模型自己改"，
    统一返回提示比抛异常更可控。

    【第 15.2 轮改动】原来这里末尾还有一道 `_within_workspace()` 检查，
    越界就返回"路径超出工作区"。删掉之后，这个函数只做一件事：把路径规范化。
    为什么删的是"检查"而不是把开关默认改成 True（见文件头的详细说明），
    一句话：那道边界对 cmd_runner 完全无效，留着只能拦住守规矩的做法。

    三个仍然保留的规范化动作，各自有原因：
      · 反斜杠转正斜杠：Windows 上模型常常写成 C:\\a\\b，
        Path 能吃，但后面和 glob 模式拼接时混用两种分隔符容易出错；
      · 相对路径补 WORKSPACE_ROOT：模型说 "src/main.py" 时得有个基准，
        统一补成绝对路径，后续所有判断和回执才一致；
      · resolve()：展开 .. 和符号链接。看着麻烦，但它保证了
        "同一个文件用不同写法指过来，拿到的是同一个字符串"，
        而"先读后写"就是靠路径字符串做键来比对的，不做这一步会漏判。
    """
    if not raw_path or not isinstance(raw_path, str):  # 没给路径，或者给的不是字符串
        return None, "路径参数无效：请提供文件路径字符串。"
    try:
        path = Path(raw_path.replace("\\", "/"))  # Windows 反斜杠统一成正斜杠
        if not path.is_absolute():  # 相对路径：补上工作区根目录
            path = WORKSPACE_ROOT / path
        path = path.resolve()  # 展开 .. 和符号链接，得到真实位置
    except Exception as e:
        # 走到这儿通常是路径里有非法字符，或者盘符不存在。
        # 与其让异常蹦到界面上，不如回一句话让模型换个写法重试。
        return None, f"路径解析失败：{e}"
    return path, ""


def _read_text(path: Path):
    """读取文件内容并解码成文字（2026-11-06 第 8 轮新增）

    返回 (原始字节, 文字, 错误文字)。
    为什么要自己解码而不用 open(encoding="utf-8")？
      cmd_runner 第 4 轮踩过这个坑：中文 Windows 上 GBK 文件按 UTF-8 读会直接抛异常，
      强行 errors="replace" 又会把中文全变成问号。这里按顺序试几种编码。
    """
    try:
        raw = path.read_bytes()  # 先按字节读，解码的事后面再说
    except Exception as e:
        return b"", "", f"读取文件失败：{e}"

    tried = set()  # 记下试过哪些编码，避免重复（列表里可能有重复项）
    for encoding in _ENCODINGS:
        if not encoding or encoding.lower() in tried:  # 空值或重复就跳过
            continue
        tried.add(encoding.lower())
        try:
            return raw, raw.decode(encoding), ""  # 严格解码，成功就直接返回
        except (UnicodeDecodeError, LookupError):
            continue  # 这个编码不行，换下一个
    # 都不行：至少把能认的字符显示出来，别让整个工具挂掉
    return raw, raw.decode("utf-8", errors="replace"), ""


def _write_text(path: Path, text: str):
    """把文字写回文件，且不做任何换行转换（2026-11-06 第 8 轮新增）

    为什么不能直接用 path.write_text(text, encoding="utf-8")？
      Windows 上 open(..., "w") 默认 newline=None，会把每个 \n 转成 \r\n。
      而 _read_text 是"读原始字节再解码"，拿到的内容里本来就带着 \r\n，
      再写回去就变成 \r\r\n（双重回车），文件里凭空多出一堆空行。
      真机验证里改一个 CRLF 文件，磁盘字节确实从 x\\r\\n 变成了 y\\r\\r\\n。

    newline="" 的意思是"原样写入，别替我转换"。
    这样写进去的字节和 text.encode("utf-8") 完全一致，
    _read_files 里记的内容指纹也才能和磁盘对得上。
    """
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _is_binary(raw: bytes) -> bool:
    """粗略判断是不是二进制文件（前 1024 字节里有 \x00 就当二进制）"""
    return b"\x00" in raw[:1024]


def _sha1(text: str) -> str:
    """算一段文字的 SHA1，用来判断"读完之后文件有没有被改过" """
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _remember(path: Path, text: str):
    """记下"这个文件现在的内容"，供后面的 write / edit 校验"""
    _read_files[str(path)] = _sha1(text)


def _shorten(text: str, limit: int) -> str:
    """把过长的一行截断，并在末尾说明截断了多少字符"""
    if len(text) <= limit:  # 没超限就原样返回
        return text
    return text[:limit] + f" …[本行过长，已截断 {len(text) - limit} 个字符]"


def _relative(path: Path) -> str:
    """把绝对路径显示成相对工作区的短路径，读起来清爽

    只在"文件确实在工作区里"时才会变短。工作区外的路径原样返回绝对路径——
    这一点在第 15.2 轮取消边界后变得更常见：模型搜桌面、搜兄弟项目时，
    回执里就是完整路径，正好方便它下一次直接拿这个路径去 file_read。
    """
    try:
        return str(path.resolve().relative_to(WORKSPACE_ROOT)).replace("\\", "/")
    except ValueError:  # 不在工作区里，显示绝对路径（不是出错，是正常分支）
        return str(path).replace("\\", "/")


def _approval_param(default: bool, description: str) -> dict:
    """生成 need_user_approve 这个参数的说明书（2026-11-06 第 8 轮新增）

    五个工具都要带这个参数，抽成一个函数避免抄五遍、抄走样。
    注意：这个参数是给 Conversation 看的（决定要不要弹确认），
    工具自己用不到；_execute_tool 会在调用前把它从参数里过滤掉。
    """
    return {
        "type": "boolean",
        "description": description,
        "default": default,
    }


# -----------------------------------------------------------------------------
# 二、file_read：读文件
# -----------------------------------------------------------------------------

class FileRead(BaseTool):
    """读文件，输出带行号，可分段读取"""

    is_repeatable = True  # 读操作，重跑拿到（甚至更新鲜的）相同内容，L3 压缩可安全折叠

    def __init__(self):
        super().__init__()  # 父类初始化（目前是空的，但保持和其他工具一致的写法）

    @staticmethod
    def get_tool_name():
        return "file_read"  # 在模型眼里的名字

    async def act(self, path="", offset=0, limit=MAX_READ_LINES, need_user_approve=False):
        """读文件（第 15 轮：整体丢到线程池里执行）

        【为什么要绕这一层 to_thread】
          这个方法的函数体全是同步 IO（read_text、splitlines），一个 await 都没有。
          单条调用时无所谓；但第 15 轮会把多个只读工具并发执行，
          而"async 外壳 + 同步内核"的协程之间没有让出点，
          asyncio.gather 会退化成串行——看起来改了并发，实际一点没快，
          而且这种"没生效的优化"极难发现（结果正确、只是没变快）。
          to_thread 把阻塞段放进线程池：磁盘 IO 期间 GIL 会释放，多个读取才真的重叠。

        【为什么参数要一个个显式摆好】
          to_thread 只接受位置参数。关键字参数得自己按顺序传，
          传乱了不会报错、只会静默错位（比如把 limit 当 offset 用），
          所以这里宁可写长一点。
        """
        return await asyncio.to_thread(self._act, path, offset, limit)

    def _act(self, path, offset, limit):
        # path：文件路径（相对工作区或绝对路径都行）
        # offset：从第几行开始读（0 表示从头），limit：最多读多少行
        # need_user_approve 只留在 act 的签名里（工具清单要它），函数体用不到
        resolved, error = _resolve_path(path)  # 统一成绝对路径（第 15.2 轮起不再检查越界）
        if error:  # 路径有问题，直接把中文提示交回给模型
            return error

        if not resolved.exists():  # 文件不存在
            return f"文件不存在：{resolved}"
        if not resolved.is_file():  # 给的是目录（或者别的特殊文件）
            return f"这不是一个文件：{resolved}。如果是想看目录里有什么，请用 file_glob。"

        try:  # 参数清洗：模型偶尔会传字符串、负数或者特别大的数
            offset = int(offset)
            limit = int(limit)
        except (TypeError, ValueError):
            return "参数错误：offset 和 limit 必须是整数。"
        offset = max(0, offset)  # 负数当成 0
        limit = max(1, min(limit, MAX_READ_LINES))  # 夹在 1 到 2000 之间

        raw, text, error = _read_text(resolved)  # 按编码顺序解码
        if error:  # 读失败了（权限、被占用等）
            return error
        if _is_binary(raw):  # 二进制文件不该按文本读，读出来是一屏乱码
            return f"这是二进制文件，无法按文本读取：{resolved}"

        _remember(resolved, text)  # 记下内容，后面的 file_edit 要用来做"先读后写"校验

        lines = text.splitlines()  # 按行切开（splitlines 自动处理 \r\n 和 \n）
        total = len(lines)  # 总行数
        if total == 0:  # 空文件也要给一句明确的话，别让模型以为读取失败
            return f"文件 {_relative(resolved)} 是空文件（0 行）。"
        chunk = lines[offset:offset + limit]  # 取本次要显示的这一段
        if not chunk:  # offset 超出文件长度了
            return (f"文件 {_relative(resolved)} 共 {total} 行，"
                    f"从第 {offset} 行开始没有内容。请把 offset 调小，或用 file_read 从头读。")

        numbered = []  # 攒带行号的输出
        for index, line in enumerate(chunk):
            line_number = offset + index + 1  # 行号从 1 开始（和 cat -n 一致）
            numbered.append(f"{line_number:>6}\t{_shorten(line, MAX_LINE_CHARS)}")

        end_line = offset + len(chunk)  # 本次显示的最后一行行号
        return ("\n".join(numbered)
                + f"\n\n（文件 {_relative(resolved)} 共 {total} 行，本次显示第 {offset + 1}-{end_line} 行。"
                + "如需更多内容，请调整 offset / limit 分段读取。）")

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # file_read
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": _approval_param(
                            False, "读文件是否需要用户先明确同意。只读操作，一般填 false。"),
                        "path": {
                            "type": "string",
                            "description": "要读取的文件路径，相对工作区或绝对路径都可以",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "从第几行开始读（0 表示第一行），默认 0",
                            "default": 0,
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"最多读多少行，默认 {MAX_READ_LINES}，上限 {MAX_READ_LINES}",
                            "default": MAX_READ_LINES,
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    def get_status(self):
        return ""  # 没有状态要汇报

    def _tool_description(self):
        return """
读取文件内容，输出带行号，可用 offset / limit 分段读取。

使用要点：
- 第一次读某个文件时直接传 path，会看到带行号的内容；修改文件前必须先读。
- 文件很长时不要一次全读，用 offset / limit 分段看，省上下文。
- 输出超过 2000 行会被自动截断，长行也会被截断并标明截了多少字符。
- 路径可以是相对工作区的写法，也可以直接给绝对路径（如 C:/Users/me/Desktop/a.txt）。
- 想按文件名找文件用 file_glob，想按内容搜索用 file_grep，不要用这个工具逐个试。
        """.strip()


# -----------------------------------------------------------------------------
# 三、file_write：新建 / 覆盖整个文件
# -----------------------------------------------------------------------------

class FileWrite(BaseTool):
    """把一段完整内容写进文件（新建或整体覆盖）"""

    def __init__(self):
        super().__init__()

    @staticmethod
    def get_tool_name():
        return "file_write"  # 在模型眼里的名字

    async def act(self, path="", content="", need_user_approve=True):
        resolved, error = _resolve_path(path)  # 路径校验
        if error:
            return error
        if not isinstance(content, str):  # 模型有时会把内容包成对象或数组
            return f"参数类型错误：content 应该是字符串，实际是 {type(content).__name__}。"

        if not resolved.parent.exists():  # 父目录不存在就别硬造（工具说明书里也要求先确认）
            return (f"父目录不存在：{resolved.parent}。"
                    "请先用 cmd_runner 确认或创建目录，再写文件。")
        if not resolved.parent.is_dir():
            return f"父路径不是目录：{resolved.parent}"

        if resolved.exists():  # 覆盖已有文件，属于有风险的操作
            if not resolved.is_file():
                return f"目标不是普通文件，无法覆盖：{resolved}"
            if str(resolved) not in _read_files:  # 没读过就想覆盖
                return ("覆盖已有文件之前，必须先用 file_read 读过它。"
                        f"请先读：{_relative(resolved)}。"
                        "如果只想改其中一小段，请改用 file_edit。")
            _, current, read_error = _read_text(resolved)  # 再读一次，看有没有被别的程序改过
            if read_error:
                return read_error
            if _sha1(current) != _read_files[str(resolved)]:  # 内容变了
                return ("文件在你读取之后被改动过，为防覆盖新内容已拒绝写入。"
                        "请用 file_read 重新读一遍再写。")

        try:
            # 统一按 UTF-8 写。项目里的源码本来就是 UTF-8（含中文注释），
            # 固定一种编码比"猜着写"更不容易把文件写坏。
            # _write_text 会关掉 Windows 的换行转换，见它的说明
            _write_text(resolved, content)
        except Exception as e:
            return f"写入文件失败：{e}"

        _remember(resolved, content)  # 内容是我们刚写进去的，直接记下来，不必再读一遍
        line_count = len(content.splitlines())  # 给模型一个简短的回执
        return f"已写入 {_relative(resolved)}（{line_count} 行，{len(content)} 个字符，UTF-8）。"

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # file_write
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": _approval_param(
                            True, "写文件前是否需要用户先明确同意。新建或覆盖文件建议填 true。"),
                        "path": {
                            "type": "string",
                            "description": "要写入的文件路径，相对工作区或绝对路径都可以",
                        },
                        "content": {
                            "type": "string",
                            "description": "要写入的完整文件内容（注意是整体覆盖，不是追加）",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        }

    def get_status(self):
        return ""

    def _tool_description(self):
        return """
把一段完整内容写入文件。适合新建文件，或者整体重写一个文件。

使用要点：
- 这是"整体覆盖"，不是追加。只想改一小段请用 file_edit。
- 父目录必须已经存在；不存在时会拒绝写入并提示你先确认目录。
- 覆盖已有文件之前必须先用 file_read 读过它，否则会拒绝。
- 如果读完之后文件被别的程序改过，也会拒绝写入，需要重新读一遍。
- 内容统一按 UTF-8 写入。
- 路径可以是相对工作区的写法，也可以直接给绝对路径。
        """.strip()


# -----------------------------------------------------------------------------
# 四、file_edit：精确替换一段文字
# -----------------------------------------------------------------------------

class FileEdit(BaseTool):
    """把文件里的 old_string 换成 new_string，并返回 diff"""

    def __init__(self):
        super().__init__()

    @staticmethod
    def get_tool_name():
        return "file_edit"  # 在模型眼里的名字

    async def act(self, path="", old_string="", new_string="", replace_all=False,
                  need_user_approve=True):
        resolved, error = _resolve_path(path)  # 路径校验
        if error:
            return error
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return "参数类型错误：old_string 和 new_string 都必须是字符串。"
        if not old_string:  # 空串会匹配到所有位置，没有任何意义
            return "old_string 不能为空：请写出要被替换的那段原文。"
        if old_string == new_string:  # 没变化，白写一次还浪费一次确认
            return "old_string 和 new_string 完全一样，不需要修改。"

        if not resolved.exists() or not resolved.is_file():
            return f"文件不存在或不是普通文件：{resolved}。请先用 file_read 确认路径。"

        if str(resolved) not in _read_files:  # 门禁一：必须先读
            return ("修改文件之前，必须先用 file_read 读过它。"
                    f"请先读：{_relative(resolved)}。")

        _, text, read_error = _read_text(resolved)  # 重新读磁盘上的最新内容
        if read_error:
            return read_error
        if _sha1(text) != _read_files[str(resolved)]:  # 门禁二：读过之后没被改过
            return ("文件在你读取之后被改动过，为防覆盖新内容已拒绝修改。"
                    "请用 file_read 重新读一遍，再重新组织 old_string。")

        # 【2026-11-06 第 8 轮】换行适配：
        #   模型从 file_read 输出里抄来的 old_string 用的是 \n，
        #   但 Windows 上的文件很可能是 \r\n，直接匹配会一处都找不到。
        #   先按原文找；找不到就把两边的 \n 都换成 \r\n 再试一次。
        #   替换时也用同一套换行，免得改完文件里混着两种换行符。
        actual_old = old_string  # 真正拿去匹配的原文
        actual_new = new_string  # 真正写进去的新内容
        count = text.count(actual_old)  # 数一数这段原文出现了几次
        if count == 0 and "\n" in old_string:  # 可能是换行风格不一致
            crlf_old = old_string.replace("\n", "\r\n")
            if text.count(crlf_old) > 0:  # CRLF 版本能匹配上
                actual_old = crlf_old
                actual_new = new_string.replace("\n", "\r\n")
                count = text.count(actual_old)
        if count == 0:
            return ("文件里没有找到 old_string 这段原文。"
                    "常见原因：空格/缩进/换行不一致，或者文件被改过。"
                    "请用 file_read 重新确认，注意行号输出里的制表符不属于文件内容。")
        if count > 1 and not replace_all:  # 不唯一就别乱改，让模型补上下文
            return (f"old_string 在文件里出现了 {count} 次，无法判断要改哪一处。"
                    "请把上下文写长一点使它唯一，或者确认确实要全部替换时设 replace_all=true。")

        if replace_all:  # 明确要求全替换：不限制次数
            new_text = text.replace(actual_old, actual_new)
        else:  # 默认只替换第一处（此时 count 已经确认是 1）
            new_text = text.replace(actual_old, actual_new, 1)
        # replace 的第三个参数是"最多替换几次"，不传就是全部替换

        try:
            _write_text(resolved, new_text)  # 关掉换行转换，见 _write_text 的说明
        except Exception as e:
            return f"写入修改失败：{e}"

        _remember(resolved, new_text)  # 更新指纹，后续还能继续 edit

        # 生成 unified diff：让模型和用户都能一眼看出改了哪几行
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True),  # 改前（保留行尾换行，diff 才不会错位）
            new_text.splitlines(keepends=True),  # 改后
            fromfile=_relative(resolved),  # diff 头里显示的文件名
            tofile=_relative(resolved),
            n=2,  # 每处改动前后各带 2 行上下文
        )).replace("\r\n", "\n")  # CRLF 文件里 diff 会带 \r，显示前统一成 \n，读起来干净
        if len(diff) > MAX_DIFF_CHARS:  # diff 太长就截断，别把上下文吃满
            diff = diff[:MAX_DIFF_CHARS] + f"\n…[差异过长，已截断 {len(diff) - MAX_DIFF_CHARS} 个字符]"
        replaced = count if replace_all else 1
        return f"已修改 {_relative(resolved)}（替换 {replaced} 处）。改动如下：\n{diff}"

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # file_edit
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": _approval_param(
                            True, "改文件前是否需要用户先明确同意。修改已读过的文件可填 false，新建/删除类操作填 true。"),
                        "path": {
                            "type": "string",
                            "description": "要修改的文件路径",
                        },
                        "old_string": {
                            "type": "string",
                            "description": "要被替换的原文，必须与文件内容完全一致（含缩进和换行），且默认要求唯一",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "替换后的新内容",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "old_string 出现多次时是否全部替换，默认 false",
                            "default": False,
                        },
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        }

    def get_status(self):
        return ""

    def _tool_description(self):
        return """
精确替换文件里的一段文字，并返回改动前后的差异（unified diff）。

这是修改已有文件的首选工具：只传要改的那一小段，不用把整个文件重发一遍。

使用要点：
- 修改前必须先用 file_read 读过这个文件，否则会拒绝。
- old_string 必须与文件内容完全一致，包含缩进和换行；它是纯文本，不要带行号。
- old_string 默认必须在文件里唯一。出现多次时要么写长一点使它唯一，要么设 replace_all=true。
- 读完之后文件若被别的程序改过，会拒绝修改并要求重新读，避免覆盖新内容。
- 想整体重写或新建文件请用 file_write；删文件请用 cmd_runner 并请求用户同意。
        """.strip()


# -----------------------------------------------------------------------------
# 五、file_glob：按文件名模式找文件
# -----------------------------------------------------------------------------

class FileGlob(BaseTool):
    """按通配符找文件，结果按修改时间从新到旧排序"""

    is_repeatable = True  # 只读查找，重跑拿到相同文件列表，L3 压缩可安全折叠

    def __init__(self):
        super().__init__()

    @staticmethod
    def get_tool_name():
        return "file_glob"  # 在模型眼里的名字

    async def act(self, pattern="", path="", need_user_approve=False):
        """按通配符找文件（第 15 轮：整体丢到线程池里执行）

        为什么要绕 to_thread：glob 扫目录是同步阻塞的，
        和 file_read / file_grep 一起并发时，没有让出点就会退化成串行。
        详见 FileRead.act 的注释。
        """
        return await asyncio.to_thread(self._act, pattern, path)

    def _act(self, pattern, path):
        if not pattern or not isinstance(pattern, str):  # 没给模式就没法找
            return "请提供文件名模式（pattern），例如 **/*.py 或 src/**/*.md。"

        if path:  # 允许指定从哪个子目录开始找
            root, error = _resolve_path(path)
            if error:
                return error
        else:
            root = WORKSPACE_ROOT  # 不指定就从工作区根目录找
        if not root.is_dir():
            return f"搜索起点不是目录：{root}"

        normalized = pattern.replace("\\", "/")  # 统一分隔符，Windows 上也能用 **/*.py
        full_pattern = str(root / normalized)  # 拼成绝对路径再交给 glob
        # 注意：如果 normalized 本身是绝对路径，Path 的 / 运算会直接采用它

        try:
            matches = globlib.glob(full_pattern, recursive=True)  # recursive=True 才支持 **
        except Exception as e:
            return f"文件名模式无效：{e}"

        results = []  # 攒通过检查的结果
        for item in matches:
            candidate = Path(item)
            if not candidate.is_file():  # 只要文件，不要目录
                continue
            # 【第 15.2 轮】这里原来还有一道 `_within_workspace(candidate)` 过滤，
            # 因为通配符（尤其是 **）可能一路匹配到工作区外面去。边界取消后一并删掉。
            # 注意别把它"简化"成一行空判断：现在这个循环里剩下的两步
            # （只要文件、跳过忽略目录）是在筛**结果质量**，跟权限无关，必须留着。
            parts = set(candidate.parts)  # 路径的每一段
            if parts & _IGNORE_DIRS:  # 命中忽略目录（.git、__pycache__ 等）就跳过
                continue
            results.append(candidate)

        # 按修改时间倒序：最近动过的文件排前面，通常也是模型最想看的
        results.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total = len(results)  # 总数（截断前）
        shown = results[:MAX_GLOB_RESULTS]  # 只展示前 100 个
        lines = [_relative(p) for p in shown]  # 转成相对路径，短一点好读

        if not lines:  # 一个都没找到
            return f"没有找到匹配 {pattern} 的文件。请检查模式，或先用较宽的模式（如 **/*）确认目录结构。"
        header = f"找到 {total} 个文件"
        if total > MAX_GLOB_RESULTS:  # 被截断了要说清楚，免得模型以为只有这些
            header += f"，只显示最近修改的 {MAX_GLOB_RESULTS} 个。请把模式写得更具体一些。"
        return header + "：\n" + "\n".join(lines)

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # file_glob
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": _approval_param(
                            False, "查找文件是否需要用户先明确同意。只读操作，一般填 false。"),
                        "pattern": {
                            "type": "string",
                            "description": "文件名通配符，例如 **/*.py、src/**/*.md、test_*.py",
                        },
                        "path": {
                            "type": "string",
                            "description": "从哪个目录开始找，留空表示工作区根目录",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }

    def get_status(self):
        return ""

    def _tool_description(self):
        # 【第 15.3 轮】这段说明重写过。改之前它只有 4 行，且第一句是
        # "**/*.py 会找出**工作区**里所有 Python 文件"——边界虽然已经删了，
        # "工作区"这三个字还留在说明书里，等于继续暗示"它只能搜项目目录"。
        # 同时它没回答模型最常见的那个问题：**不知道文件在哪时该填什么**。
        # 于是模型转身去用 dir /s —— 它更熟，而且一条命令能同时扫好几个目录。
        # 新版按"什么时候用 / 怎么用"组织，专门补上这两个缺口。
        return """
按文件名通配符查找文件，支持 ** 递归，结果按修改时间从新到旧排序。

什么时候用它：
- 用户说"找一下我的某某文件"但没说在哪个目录时，这就是本工具的典型场景。
- 不要用 dir /s、Get-ChildItem -Recurse、find 去拼 shell：前两个会扫全盘、经常超时，
  在 Windows 的 cmd.exe 里还容易写成两条不同语法的混合体导致报错。

用法：
- pattern 里 ** 表示"任意层子目录"，例如 **/*简历* 会一路往下找出所有名字带"简历"的文件。
- path 是搜索起点，可以给任意绝对路径（如 C:/Users/用户名/Desktop、D:/项目）；留空从当前目录开始。
- 不确定在哪儿时，先挨个搜几个常见目录（桌面、下载、文档），每次 pattern 用 **/*关键词*：
  这三个目录都是毫秒级返回，而且能分几次调用、并发执行，加起来比一条 shell 命令还快。
- 不要一上来就拿 C:/Users/用户名 或盘符根（D:/）当 path 递归：那要十几秒，还可能超时。
  常见目录里没找到，再往上一层扩大范围。
- 自动跳过 .git、__pycache__、node_modules 等缓存和依赖目录；最多返回 100 个结果，
  太多时把模式写具体一点。
- 找的是"文件名"；搜"文件内容"请用 file_grep，读某个具体文件用 file_read。
        """.strip()


# -----------------------------------------------------------------------------
# 六、file_grep：按内容搜索文件
# -----------------------------------------------------------------------------

class FileGrep(BaseTool):
    """在文件里按正则搜索内容，输出 路径:行号:内容"""

    is_repeatable = True  # 只读搜索，重跑拿到相同（甚至更新鲜的）命中，L3 压缩可安全折叠

    def __init__(self):
        super().__init__()

    @staticmethod
    def get_tool_name():
        return "file_grep"  # 在模型眼里的名字

    async def act(self, pattern="", path="", include="", case_sensitive=False,
                  need_user_approve=False):
        """按内容搜索（第 15 轮：整体丢到线程池里执行）

        为什么要绕 to_thread：这个工具最重——要递归遍历目录、逐个读文件、
        再逐行跑正则。它和 file_read 并发时如果不同步让出，等于白并发。
        详见 FileRead.act 的注释。
        """
        return await asyncio.to_thread(self._act, pattern, path, include, case_sensitive)

    def _act(self, pattern, path, include, case_sensitive):
        if not pattern or not isinstance(pattern, str):
            return "请提供要搜索的内容（pattern），支持正则表达式。"

        flags = 0 if case_sensitive else re.IGNORECASE  # 默认不区分大小写，更容易找到
        try:
            regex = re.compile(pattern, flags)  # 先编译，语法错了早点告诉模型
        except re.error as e:
            return f"正则表达式无效：{e}。如果只是想搜普通文字，请避开 . * ( ) [ ] 这些特殊符号。"

        if path:  # 可以限定在某个子目录里搜
            root, error = _resolve_path(path)
            if error:
                return error
        else:
            root = WORKSPACE_ROOT
        if not root.is_dir():
            return f"搜索起点不是目录：{root}"

        hits = []  # 攒命中结果
        scanned = 0  # 扫了多少个文件，用来在回执里说明
        for dirpath, dirnames, filenames in os.walk(root):
            # 这行是"剪枝"：直接把忽略目录从待遍历列表里删掉，
            # os.walk 就不会再往里钻，比事后过滤快得多
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
            for name in filenames:
                if include and not fnmatch.fnmatch(name, include):  # 例如只搜 *.py
                    continue
                file_path = Path(dirpath) / name
                try:
                    if file_path.stat().st_size > MAX_GREP_FILE_BYTES:  # 大文件跳过
                        continue
                except OSError:
                    continue  # 文件可能刚好被删了，跳过就行
                # 【第 15.2 轮】这里原来还有一道 `_within_workspace(file_path)` 过滤，
                # 防止"传了绝对路径的 include"把工作区外的文件也扫进来。边界取消后删掉。
                # 上面的两处跳过（超大文件、stat 失败）是在保结果质量和防崩，别一起删了。

                raw, text, error = _read_text(file_path)  # 读文件并解码
                if error or _is_binary(raw):  # 读不了或二进制文件，跳过
                    continue
                scanned += 1
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):  # 命中
                        hits.append(f"{_relative(file_path)}:{line_number}:{_shorten(line.strip(), MAX_GREP_LINE_CHARS)}")
                        if len(hits) >= MAX_GREP_RESULTS:  # 到上限就停，别把上下文撑爆
                            break
                if len(hits) >= MAX_GREP_RESULTS:
                    break
            if len(hits) >= MAX_GREP_RESULTS:
                break

        if not hits:  # 一处都没找到
            return (f"没有找到匹配 {pattern} 的内容（扫描了 {scanned} 个文件）。"
                    "可以换个关键词、放宽 include，或先用 file_glob 确认有哪些文件。")

        header = f"找到 {len(hits)} 处匹配（扫描了 {scanned} 个文件）"
        if len(hits) >= MAX_GREP_RESULTS:  # 到上限了要说明结果不完整
            header += f"，已达 {MAX_GREP_RESULTS} 处上限。请把关键词或 include 写得更具体。"
        return header + "：\n" + "\n".join(hits)

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.get_tool_name(),  # file_grep
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "need_user_approve": _approval_param(
                            False, "搜索文件内容是否需要用户先明确同意。只读操作，一般填 false。"),
                        "pattern": {
                            "type": "string",
                            "description": "要搜索的内容，支持正则表达式",
                        },
                        "path": {
                            "type": "string",
                            "description": "在哪个目录里搜，留空表示整个工作区",
                        },
                        "include": {
                            "type": "string",
                            "description": "只搜符合这个文件名模式的文件，例如 *.py，留空表示不限",
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "是否区分大小写，默认 false（不区分）",
                            "default": False,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }

    def get_status(self):
        return ""

    def _tool_description(self):
        return """
按内容搜索文件，输出格式为 路径:行号:该行内容。用 Python 正则实现，不依赖系统 grep，Windows 上也能用。

使用要点：
- 想找"某个类/函数定义在哪"、某段报错文字出现在哪些文件里，用这个工具。
- 默认不区分大小写；要精确匹配请设 case_sensitive=true。
- 用 include 限定文件类型（如 *.py）能快很多，也能少看无关结果。
- 自动跳过缓存目录、二进制文件和超过 1MB 的大文件；最多返回 100 处。
- 找"文件名"请用 file_glob，读某个具体文件请用 file_read。
        """.strip()
