# =============================================================================
# 文件：src/tools/mcp_cli.py
# 作用：`thinker mcp ...` 子命令（2026-11-12 新增，第 30 轮）
#   对齐 Claude Code 的那套用法，只做 stdio 类型的 MCP：
#     thinker mcp add 12306 -- npx -y 12306-mcp
#     thinker mcp remove 12306
#     thinker mcp list
#     thinker mcp get 12306
#
# 【为什么值得做：手写 JSON 是这个功能最大的门槛】
#   原来接一个服务器要：找到 ~/.thinker/mcp.json → 自己开 } [ ] → 记清 type/command/args
#   → 少一个逗号整个文件就废（运行时只打一行提示，外部工具全消失）。
#   而接服务器这件事本质上只有三个信息：名字、用什么命令、带什么参数。
#   命令行把这三样按顺序写下来就完事，剩下的格式、转义、备份由程序负责。
#
# 【一次 add 的完整时间线（整条链路只有这一条）】
#   12:00:01  用户敲 thinker mcp add 12306 -- npx -y 12306-mcp
#   12:00:01  main.cli() 看到 argv[0] == "mcp"，转给 run_mcp_cli(["add","12306","--","npx","-y","12306-mcp"])
#   12:00:01  _parse_add_args 切出 ("12306", "stdio", "npx", ["-y","12306-mcp"])
#   12:00:01  _validate_name 过一遍（名字里有斜杠、空格就在这一步退回去）
#   12:00:01  resolve_config_path() 定位 ~/.thinker/mcp.json
#   12:00:01  load_config_dict() 严格读整份 → 读不懂就抛错，一个字都不写
#   12:00:02  servers["12306"] = {...} → save_config_dict() 备份 + 原子替换
#   12:00:02  打印「已添加」，返回 0，进程退出（不进对话、不连服务器）
#   12:00:05  用户重新敲 thinker → MCPClient 启动时读到新条目 → 连上 → 工具出现
#
#   注意最后两步之间**没有**任何热加载：命令只负责把文件写对。
#   这是有意的，理由见下面第三条。
#
# 【为什么只有 stdio】
#   客户端的连接代码本来就是 stdio 专用的（client.py 里直接 stdio_client + StdioServerParameters），
#   server_config 也会跳过 http/sse。命令里如果允许写 --transport http，
#   等于让用户以为配上了、运行时却被静默忽略——那是最坏的一种「成功」。
#   所以这里只放行 stdio，其他类型明确报错并说明原因。
#
# 【为什么命令跑完就退出，不进入对话】
#   改配置是「一次性动作」，不是聊天。跑完直接返回，用户接着敲 thinker 启动，
#   新配置在启动时读取——不需要热加载，也不存在「当前会话里的工具表要不要重建」
#   这类麻烦事（MCP 会话已经建立，临时插一条进去很容易把工具表搞脏）。
#
# 【为什么错误分两类（_CliError / MCPConfigError）】
#   对用户来说两者都是「命令失败」，但对排查来说是两件事：
#     _CliError      —— 你敲错了，改一下参数再试，文件从头到尾没被碰过；
#     MCPConfigError —— 文件本身有问题（坏了、读不了、写不进去），
#                       提示里必须明确说「已中止，没有修改」，否则用户不敢再动它。
#   混成一条提示，「失败了但文件到底动没动」就得靠猜。
#
# 【这个文件不碰什么（边界）】
#   不连服务器、不解析 MCP 协议、不判断权限：它只负责改 JSON。
#   连接在 tools/mcp_client/client.py，配置对象化在 server_config.py，
#   外部工具包装在 mcp_tool.py。
#   守住这条边界的直接好处：`thinker mcp list` 是毫秒级的（不 import mcp 库、
#   不启动子进程），而且「配置写错了」和「服务器连不上」变成两个能分别排查的问题——
#   前者这条命令当场就能告诉你，后者要去启动日志里看。
#
# 【为什么不顺便帮用户装依赖】
#   `claude mcp add` 也只写配置，装包是用户自己的事。如果这里替用户跑
#   `npm install -g`，就要处理权限、镜像源、版本冲突、卸载残留——那是一个
#   安装器该干的活，长在配置命令里会让这条命令变得既不可预测又难排查。
#   所以只在命令找不到时给一句黄字提醒（_command_hint），把决定权留给用户。
# =============================================================================

import json  # get 子命令要原样输出 JSON（不重新拼，保留用户手写的未知字段）
import shutil  # 判断命令在不在 PATH 里（which）
from pathlib import Path  # 判断绝对路径形式的命令是否存在
# 只用到这三个：参数是 list、name 在解析完成前可能是 None、解析结果是一个四元组。
# 项目下限是 3.9，`list[str]` 能用但 `str | None` 不能，所以 Optional 不能省。
from typing import Dict, List, Optional, Tuple

from rich import box  # 表格边框样式（SIMPLE：只有表头一条横线，最不打扰）
from rich.table import Table  # 列对齐的表格：手写空格对齐在中文宽度下一定错位
from rich.text import Text  # 富文本：给一段文字的不同部分上不同颜色

from tools.mcp_config import (  # 配置定位/读写都走这一个模块，路径逻辑不重复
    MCPConfigError,  # 读不懂 / 写不进去：提示里必须说明「磁盘没动」
    load_config_dict,  # 严格读整份配置（区别于运行时的容错读）
    resolve_config_path,  # 决定改哪个文件（新路径 / 老路径 / 环境变量覆盖）
    save_config_dict,  # 备份 + 原子写回
)
from ui.theme import ACCENT, CHECK_ON, CROSS, DIM, ERR, OK, WARN, console  # 与主界面同一套配色

# 名字里不允许出现的字符：
#   空白     —— 命令行本身按空白分词，名字里带空格时用户没法不打引号地写对；
#   斜杠/冒号 —— 会让名字在日志和提示里看着像路径（mcpServers 的键只用于展示和删除，
#              不参与工具名，所以这里不需要更严格的标识符规则）。
# 用 set 而不是正则：报错时要能把「具体哪几个字符不行」列出来，set 好取差集。
_FORBIDDEN_NAME_CHARS = set(" \t\r\n/\\:：")

# 帮助文本单独放常量：run_mcp_cli 的三条分支（无参数、help、子命令里 -h）
# 都要打印同一份内容，写三遍必然有一天改了其中两处。
#
# 顺序是按「用户此刻想知道什么」排的，不是按重要性：
#   ① 子命令表最先 —— 敲 `thinker mcp` 的人多半是忘了子命令叫什么；
#   ② add 的选项第二 —— 只有 -t 一个，而且默认值就对，扫一眼即可；
#   ③ 示例第三 —— 给「不想读说明、只想抄一行」的人（占多数）；
#   ④ 配置文件路径最后 —— 属于出问题或想深究时才看的细节。
_HELP_TEXT = """\
用法：thinker mcp <子命令> [参数]

  add <名字> -- <命令> [参数...]    添加（或覆盖）一个 stdio 服务器
  remove <名字>                     删除一个服务器（别名 rm）
  list                              列出已配置的服务器（别名 ls）
  get <名字>                        查看某个服务器的配置原文
  help                              显示这份帮助

add 的选项：
  -t, --transport <类型>   连接类型，目前只有 stdio（默认就是 stdio）
  -e, --env KEY=VALUE      给这个服务器单独设环境变量，可写多次
                           最常见的用法是配代理：
                           -e HTTPS_PROXY=http://127.0.0.1:7890

示例：
  thinker mcp add 12306 -- npx -y 12306-mcp
  thinker mcp add duckduckgo -e HTTPS_PROXY=http://127.0.0.1:7890 -- C:/tools/duckduckgo-mcp.exe
  thinker mcp list
  thinker mcp remove 12306

配置文件：~/.thinker/mcp.json
  每次改动前会自动备份成 mcp.json.bak；改完下次启动 thinker 生效。"""


class _CliError(Exception):
    """用法错误（打错命令、少参数、名字非法）——只影响这一次命令，磁盘不动"""


class _HelpRequested(Exception):
    """在子命令里写了 -h/--help，需要退回去打印总帮助

    为什么用异常而不是让每个子命令 return：
      打印帮助是「所有子命令都可能有」的行为，用异常就能在 run_mcp_cli 里
      统一兜一次；否则 4 个子命令各自写一遍 if，将来加第 5 个必然漏。
    """


def run_mcp_cli(argv: List[str]) -> int:
    """`thinker mcp` 的总入口，返回进程退出码（0 成功 / 1 失败）

    argv 是 `mcp` 之后的参数，例如 ["add", "12306", "--", "npx", "-y", "12306-mcp"]。
    返回码而不是直接 sys.exit：方便测试里直接调用（sys.exit 会把测试进程带走），
    也方便以后被别的地方复用（比如将来做个交互式配置界面）。

    为什么不用 argparse：
      `add <名字> -- <命令> [参数...]` 的核心诉求是「-- 之后一个 token 都不能动」，
      而这正是 argparse 最不擅长的部分（REMAINDER 与位置参数的相对顺序很脆，
      一旦命令里出现 -h，argparse 会抢过去当自己的帮助）。
      这里总共 4 个子命令、3 个选项，手写解析反而能把边界写清楚：
      哪些 token 归我们、哪些归子进程，`_parse_add_args` 里一眼能看完。
      顺带的好处是报错文案能保持中文、和界面其余部分一致。
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        # 裸敲 `thinker mcp` 不报错、直接给用法：这是「我想看看有什么」的意图，
        # 不是「我敲错了」，用退出码 1 会让脚本误判。
        _print_help()
        return 0

    subcommand, rest = argv[0], argv[1:]

    # 用 dict 而不是 if/elif 链：别名（rm/ls）天然是同一个键指向同一个函数，
    # 加子命令时只加一行，分发和「未知子命令」的兜底都不用跟着改。
    handlers = {
        "add": _cmd_add,
        "remove": _cmd_remove,
        "rm": _cmd_remove,  # 别名：和 git / npm 的习惯一致，少打几个字
        "list": _cmd_list,
        "ls": _cmd_list,  # 同上
        "get": _cmd_get,
    }

    handler = handlers.get(subcommand)
    if handler is None:
        console.print(Text(f"  {CROSS} 不认识的子命令：{subcommand}", style=ERR))
        _print_help()  # 顺手把用法打出来，比只说「不认识」有用
        return 1

    try:
        return handler(rest)
    except _HelpRequested:
        _print_help()
        return 0
    except _CliError as error:  # 用法错误：告诉用户怎么改，别甩堆栈
        console.print(Text(f"  {CROSS} {error}", style=ERR))
        console.print(Text("  用法见：thinker mcp --help", style=DIM))
        return 1
    except MCPConfigError as error:  # 配置读/写失败：必须明确「磁盘没动」
        console.print(Text(f"  {CROSS} {error}", style=ERR))
        console.print(Text("  已中止，配置文件没有被修改。", style=DIM))
        return 1


# =============================================================================
# add

def _cmd_add(tokens: List[str]) -> int:
    """添加（或覆盖）一个 stdio 服务器

    顺序是刻意的：**解析 → 校验 → 读文件 → 改内存 → 写盘**。
    任何一步失败都发生在写盘之前，所以「报错了但文件被改了一半」在结构上不可能出现。
    反过来写（先读文件、边解析边改）就得靠人记得在每个 return 前回滚。
    """
    name, transport, command, args, env = _parse_add_args(tokens)

    if transport != "stdio":
        # 这条不是「暂不支持，敬请期待」的客套话：客户端代码里根本没有 http/sse 的通道，
        # 写进配置只会被 server_config 静默跳过，用户会以为是服务器那边的问题。
        raise _CliError(f"只支持 stdio，{transport} 类型的 MCP 暂不支持（写进配置也不会被连接）")

    _validate_name(name)

    path = resolve_config_path()
    data = load_config_dict(path)  # 读不懂会抛 MCPConfigError → 一个字都不写
    servers = data["mcpServers"]
    replaced = name in servers  # 记住是不是覆盖，输出提示要说清楚（「已添加」和「已更新」不是一回事）

    # 写三项必填 + 可选的 env（第 33.5.2 轮）：
    #   type 显式写 "stdio"，是为了让人直接看文件时也知道这是 stdio（缺省虽然也能跑）；
    #   env 只在用户真的写了 -e 时才写进去，避免每台服务器都挂一个空对象。
    # list(args) / dict(env) 复制一份：调用方的对象和这里存进去的不该共享引用。
    entry = {"type": "stdio", "command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    servers[name] = entry
    backup_path = save_config_dict(path, data)  # 备份 + 原子写；失败会抛 MCPConfigError

    # 下面四行输出各管一件事，缺一行用户就得多做一步：
    #   ① 结果（已添加 / 已更新）—— 一眼知道成没成；
    #   ② 刚写进去的完整命令 —— 核对参数有没有漏（漏参数是最常见的「配上了但不工作」）；
    #   ③ 文件路径 —— 多用户 / 多机器时最容易搞混「改的到底是哪个文件」；
    #   ④ 备份文件名 —— 退路在哪（只在确实有旧文件时才打）。
    verb = "已更新" if replaced else "已添加"
    console.print(Text(f"  {CHECK_ON} {verb} {name}", style=OK))
    console.print(Text(f"    命令：{_join_command(command, args)}", style=DIM))
    if env:
        # 只打 key 不打 value：env 里通常是代理地址或 API key，
        # 打出来会被截图、日志、聊天记录带走。
        console.print(Text(f"    环境变量：{'、'.join(sorted(env))}（值已隐藏）", style=DIM))
    console.print(Text(f"    配置：{path}", style=DIM))
    if backup_path is not None:  # 首次创建文件时没有旧文件，也就没有备份，不打印这一行
        console.print(Text(f"    旧文件已备份：{backup_path.name}", style=DIM))

    hint = _command_hint(command)  # 只是黄字提醒，不影响写入结果
    if hint:
        console.print(Text(f"  {WARN} {hint}", style=WARN))

    # 这句必须打：用户刚敲完命令，最容易的误解就是「现在就生效了吧」，
    # 然后打开 thinker 发现工具没出来，以为命令没写成功。
    console.print(Text("    下次启动 thinker 时生效。", style=DIM))
    return 0


def _parse_add_args(tokens: List[str]) -> Tuple[str, str, str, List[str], Dict[str, str]]:
    """把 add 后面的 token 折成（名字, 连接类型, 命令, 参数）

    支持两种写法：
      thinker mcp add 12306 -- npx -y 12306-mcp     ← 推荐，和 Claude Code 一致
      thinker mcp add 12306 npx -y 12306-mcp        ← 省略 -- 的顺手写法

    选项（-t/--transport）放在名字前后都行，只要在 -- 之前：
      thinker mcp add -t stdio 12306 -- npx ...
      thinker mcp add 12306 -t stdio -- npx ...
    后者是照 Claude Code 的 `claude mcp add name -e K=V -- cmd` 来的：
    选项紧跟在名字后面是很常见的顺序，只认「名字前」会让人白挨一次报错。

    分隔符 -- 的意义：一旦出现，后面的 token 全部原样交给子进程。
    没有它的话，-y / -t 这种「长得像我们自己的选项」就无法区分是谁的，
    只能靠猜——而猜错的后果是把参数吞掉，用户看到的是「服务器起来了但不干活」。

    三种典型输入的走法（对照下面的 while 循环看）：

      输入 tokens                         name     停在哪   命令部分
      ["-t","stdio","12306","--","npx"]   12306    跳过 --   ["npx"]
      ["12306","npx","-y","x"]            12306    第二个非选项 ["npx","-y","x"]
      ["12306"]                           12306    数组末尾    [] → 报「缺少命令」
    """
    name: Optional[str] = None
    transport = "stdio"  # 默认 stdio：这是唯一支持的类型，也就没必要强制用户写 -t
    # 【第 33.5.2 轮】-e/--env 收集到的环境变量。只在 -- 之前有效——
    #   -- 之后的东西一律属于子进程，写 `-e` 也不会被我们吃掉。
    env: Dict[str, str] = {}
    index = 0

    # 用 while + 手写游标，而不是 for + enumerate：
    #   `-t stdio` 这种「一个选项吃掉两个 token」的情形，for 无法跳过一个元素。
    while index < len(tokens):
        token = tokens[index]

        if token == "--":  # 分隔符：命令部分从下一个开始（跳过它本身）
            index += 1
            break  # 不用再解析了，剩下全是命令

        if token in ("-t", "--transport"):
            if index + 1 >= len(tokens):  # 只写了选项没写值，直接指出缺什么
                raise _CliError(f"{token} 后面要跟类型，例如 --transport stdio")
            transport = tokens[index + 1]
            index += 2  # 一次吃掉「选项 + 它的值」两个 token
            continue

        if token.startswith("--transport="):
            # --transport=http 这种等号写法：Claude Code 两种都吃，这里也一起支持，
            # 否则用户按习惯写等号会得到「不认识的选项」。
            transport = token.split("=", 1)[1]
            if not transport:  # "--transport=" 后面空着
                raise _CliError("--transport= 后面要跟类型")
            index += 1
            continue

        if token in ("-e", "--env"):
            # 【第 33.5.2 轮】给这个服务器单独设环境变量，最典型的用法是配代理：
            #   thinker mcp add duckduckgo -e HTTPS_PROXY=http://127.0.0.1:7890 -- C:/.../duckduckgo-mcp.exe
            # 为什么必须在写盘前校验格式：KEY=VALUE 写错（少等号、key 为空）
            #   会让子进程拿到一个空名字的变量，报错却是"服务器启动失败"，
            #   方向和真正的原因差得很远。这里直接指出是命令写错了。
            if index + 1 >= len(tokens):  # 只写了 -e 没写值
                raise _CliError(f"{token} 后面要跟 KEY=VALUE，例如 -e HTTPS_PROXY=http://127.0.0.1:7890")
            pair = tokens[index + 1]
            key, separator, value = pair.partition("=")
            if not separator or not key.strip():
                raise _CliError(f"环境变量要写成 KEY=VALUE，收到的是：{pair}")
            env[key.strip()] = value
            index += 2  # 一次吃掉「选项 + 它的值」两个 token
            continue

        if token.startswith("--env="):
            # 等号写法：--env=KEY=VALUE。只切第一个等号后面的都留给值，
            # 因为值本身可能带等号（base64、查询串都会出现）。
            pair = token.split("=", 1)[1]
            key, separator, value = pair.partition("=")
            if not separator or not key.strip():
                raise _CliError(f"环境变量要写成 --env=KEY=VALUE，收到的是：{token}")
            env[key.strip()] = value
            index += 1
            continue

        if token in ("-s", "--scope") or token.startswith("--scope="):
            # 【2026-11-12 第 30.2 轮】按 Claude Code 的习惯，很多人会顺手写
            #   `add 12306 -s user -- npx -y 12306-mcp`。
            #   Thinker 只有一份配置（~/.thinker/mcp.json，相当于 claude 的 user 档），
            #   没有 local / project 之分。所以这里既不静默忽略，也不落到下面通用的
            #   "不认识的选项"——那句提示（"命令自己的参数请写在 -- 后面"）会把人带偏，
            #   因为 -s 确实是配置选项，只是我们没有这一维。
            raise _CliError(
                # 拆成两行：单行 80 列装不下，rich 会在词中间折断（"--scope" 被劈开很难看）。
                # 第一行说原因，第二行直接给能复制的命令——用户此刻要的是后者。
                "Thinker 只有一份全局配置（~/.thinker/mcp.json），没有 --scope\n"
                "    去掉它即可：thinker mcp add 12306 -- npx -y 12306-mcp"
            )

        if token in ("-h", "--help"):
            raise _HelpRequested()  # 交回总入口统一打印，不在这里重复一份帮助文本

        if token.startswith("-"):  # -- 之前只允许我们自己认识的选项
            raise _CliError(f"不认识的选项：{token}（命令自己的参数请写在 -- 后面）")

        if name is None:  # 第一个非选项 token 就是名字
            name = token
            index += 1
            continue

        # 名字之后又遇到非选项 token：说明用户省掉了 --，
        # 从它开始全部是命令（含 -y 这类参数），到这里就不再解析我们的选项。
        break

    if name is None:
        raise _CliError("缺少服务器名字，例如：thinker mcp add 12306 -- npx -y 12306-mcp")

    # 走到这里 index 已经越过 --（如果有），剩下的一律是命令，原样保留
    command_tokens = tokens[index:]
    if not command_tokens:
        raise _CliError("缺少启动命令，例如：thinker mcp add 12306 -- npx -y 12306-mcp")

    # 第一个 token 是可执行文件，其余是它的参数；两者分开存，因为配置文件里
    # command / args 是两个键（stdio_client 也按这个结构启动子进程）。
    # env 是第 33.5.2 轮加的：只在子进程里生效，不污染全局环境。
    return name, transport, command_tokens[0], command_tokens[1:], env


def _validate_name(name: str) -> None:
    """名字的合法性检查（名字只用于展示和 remove，不参与工具名）

    为什么还要管：
      名字会成为 JSON 的键和提示里的一句话。带空格的名字在命令行里必须加引号，
      带斜杠的名字在提示里看着像路径，用户回头复制粘贴就会出错。
      工具名本身来自服务器（tool.name），跟这个名字无关，所以不用按标识符规则卡死。
    """
    if not name:
        raise _CliError("服务器名字不能为空")
    if name.startswith("-"):
        # 以 - 开头的名字会被解析器当成选项，写进去之后 remove 都删不掉它
        raise _CliError(f"名字不能以 - 开头：{name}")
    bad = sorted({char for char in name if char in _FORBIDDEN_NAME_CHARS})
    # 用集合 + 排序而不是「找到第一个就报」：名字里同时有空格和斜杠时，
    # 一次把问题字符列全，用户改一次就够了，不用来回试三遍。
    if bad:
        # 只报告真实出现的字符，而不是把整张禁用表列一遍——用户才知道该删哪个
        shown = " ".join(repr(char) for char in bad)
        raise _CliError(f"名字里不能有空白、斜杠或冒号（发现 {shown}）")


def _command_hint(command: str) -> Optional[str]:
    """启动命令找不到时给一句提醒（不阻止写入）

    为什么只提醒、不报错：
      配 MCP 的常见顺序是先写好配置、再去装那个包；而且 Windows 上
      PATH 是「开着的终端才更新」的，新装的 npx 在当前进程里可能还找不到。
      当场拒绝写入会把这两种正常情况都堵死，所以只给黄字提示。

    为什么用 which 之外还要判路径：
      which 只查 PATH，绝对路径（C:/Python312/python.exe）不归它管。
      含斜杠/反斜杠的就当路径处理，直接看文件在不在。
    """
    looks_like_path = "/" in command or "\\" in command
    if looks_like_path:
        if not Path(command).expanduser().exists():  # expanduser 让 ~/bin/x 这种写法也能判
            return f"找不到可执行文件：{command}（路径写错的话，启动时会连接失败）"
        return None
    if shutil.which(command) is None:  # PATH 里没有
        return f"PATH 里没找到 {command}（没装或者拼错了？启动时会连接失败）"
    return None


def _join_command(command: str, args: List[str]) -> str:
    """把命令和参数拼成一行给人看（参数含空格时加引号，避免看起来像两个参数）

    只是「显示用」的拼接，不参与实际执行——真正启动子进程时，
    command 和 args 是分开传给 stdio_client 的，不存在二次转义问题。
    """
    parts = [command] + [f'"{arg}"' if " " in arg else arg for arg in args]
    return " ".join(parts)


# =============================================================================
# remove

def _cmd_remove(tokens: List[str]) -> int:
    """删除一个服务器（别名 rm）

    和 add 一样：所有校验都在写盘之前；写盘只有 save_config_dict 这一次。
    """
    if not tokens:
        raise _CliError("缺少服务器名字，例如：thinker mcp remove 12306")
    if tokens[0] in ("-h", "--help"):
        raise _HelpRequested()
    # 明确拒绝「一次删多个」：批量删是高风险动作，真要做得配确认，
    # 而这条命令设计成谁都敢随手敲，所以宁可让用户敲两次。
    if len(tokens) > 1:
        raise _CliError(f"只能一次删一个，多出来的参数：{' '.join(tokens[1:])}")

    name = tokens[0]
    path = resolve_config_path()
    if not path.exists():
        # 文件都没有，说明从来没配过；报「没有可删的」比抛 MCPConfigError 更贴事实
        raise _CliError(f"还没有配置文件 {path}，没有可删的服务器")

    data = load_config_dict(path)
    servers = data["mcpServers"]
    if name not in servers:
        # 把现有名字列出来：八成的「不存在」是名字敲错了（比如 12306 写成 12360），
        # 列出来用户一眼就能对上，不用再去开文件。
        existing = "、".join(servers) if servers else "（空）"
        raise _CliError(f"没有叫 {name} 的服务器。现有：{existing}")

    removed = servers.pop(name)  # 从内存里删掉，下面的保存才会落盘
    backup_path = save_config_dict(path, data)

    console.print(Text(f"  {CHECK_ON} 已删除 {name}", style=OK))
    if isinstance(removed, dict) and removed.get("command"):
        # 顺手回显被删的那条，方便用户发现「删错了」时照着补回来
        console.print(
            Text(
                f"    原来是：{_join_command(str(removed.get('command')), list(removed.get('args') or []))}",
                style=DIM,
            )
        )
    if backup_path is not None:
        # 备份文件就是最省事的撤销手段，这句提示比「已备份」四个字有用得多
        console.print(Text(f"    旧文件已备份：{backup_path.name}（想撤销就把它复制回 mcp.json）", style=DIM))
    console.print(Text("    下次启动 thinker 时生效。", style=DIM))
    return 0


# =============================================================================
# list / get

def _cmd_list(tokens: List[str]) -> int:
    """列出已配置的服务器：名字 / 类型 / 完整命令

    为什么用表格而不是逐行打印：
      MCP 的启动命令动辄六七段（解释器 + 脚本 + 一堆参数），逐行打印时
      「名字」和「命令」贴在一起，扫一眼分不清哪段是名字。
      列对齐之后，用户找的是「哪一行」，而不是「哪一段」。
    """
    if tokens:
        if tokens[0] in ("-h", "--help"):
            raise _HelpRequested()
        raise _CliError(f"list 不带参数，多出来的：{' '.join(tokens)}")

    path = resolve_config_path()
    data = load_config_dict(path)
    servers = data["mcpServers"]

    if not servers:
        # 空清单时直接给一条能复制的命令：这是用户此刻最需要的东西，
        # 只说「没有配置」等于让他自己回去翻文档。
        console.print(Text("  还没有配置任何 MCP 服务器。", style=DIM))
        console.print(Text("  添加一个：thinker mcp add 12306 -- npx -y 12306-mcp", style=DIM))
        console.print(Text(f"  配置文件：{path}", style=DIM))
        return 0

    table = Table(
        box=box.SIMPLE,  # 只有表头一条横线，和界面上其他输出一样克制
        show_header=True,
        header_style=DIM,  # 表头不该比数据更抢眼
        pad_edge=False,  # 不要左右留白，让表格贴齐其他输出的两格缩进
        expand=False,  # 不撑满终端宽度：两三条服务器时撑满会显得很空
        # 不锁死宽度：窄终端下交给 rich 自己压缩（它会按列 content 折行），
        # 锁成 WIDTH=72 反而会在更窄的窗口里横向溢出。
    )
    table.add_column("#", style=DIM, justify="right", width=3)  # 序号，便于和别的输出对照
    table.add_column("名字", style=ACCENT, no_wrap=True)  # 名字不折行，折了就看不清是哪个
    table.add_column("类型", style=DIM, no_wrap=True)  # 只有 stdio 一种正常值，用最暗的颜色
    table.add_column("启动命令", overflow="fold")  # 命令可以长，折行总比截断好（截断会藏起参数）

    for number, (name, info) in enumerate(servers.items(), start=1):
        if not isinstance(info, dict):  # 手写文件里可能出现 "foo": "bar" 这种
            table.add_row(str(number), name, "-", Text("配置格式不对（应为对象）", style=ERR))
            continue

        type_ = info.get("type") or "stdio"  # 不写 type 时 server_config 按 stdio 处理，这里保持一致
        command = info.get("command") or ""
        args = info.get("args") or []
        # args 理论上一定是列表，但用户手写时可能写成字符串。这里统一成列表再拼，
        # 免得 str + list 直接抛 TypeError——展示命令而已，不该因为格式瑕疵崩掉。
        args = [str(arg) for arg in args] if isinstance(args, list) else [str(args)]

        if type_ != "stdio":
            # 明确标出来：这类服务器躺在文件里，但本版本根本不会去连它。
            # 不标的话，用户会照着 list 的结果去排查「为什么工具没出现」。
            table.add_row(
                str(number),
                name,
                Text(f"{type_}（忽略）", style=WARN),
                Text(_join_command(command, args) or "-", style=DIM),
            )
            continue

        table.add_row(
            str(number),
            name,
            "stdio",
            # 缺 command 的条目运行时会被静默跳过，这里提前标红，省得用户去猜
            _join_command(command, args) or Text("（缺 command，运行时会被跳过）", style=ERR),
        )

    console.print(table)
    console.print(Text(f"  配置文件：{path}", style=DIM))  # 永远带上路径：多台机器/多用户时最容易搞混的就是「改的是哪个文件」
    return 0


def _cmd_get(tokens: List[str]) -> int:
    """查看某个服务器的配置原文（JSON）

    和 list 的分工：list 回答「我配了哪些」，get 回答「这一条到底写了什么」。
    后者在排查「为什么它连不上」时才是真正要看的——比如别人手写时加了 env、cwd。
    """
    if not tokens:
        raise _CliError("缺少服务器名字，例如：thinker mcp get 12306")
    if tokens[0] in ("-h", "--help"):
        # 注意顺序：先查「有没有参数」再查「是不是 -h」。
        # 反过来的话 `thinker mcp get` 会因为 tokens 为空而 IndexError。
        raise _HelpRequested()
    if len(tokens) > 1:
        # 和 remove 同理：多给的名字一定表示用户理解错了命令，静默忽略只会让他以为生效了
        raise _CliError(f"只能一次看一个，多出来的参数：{' '.join(tokens[1:])}")

    name = tokens[0]
    path = resolve_config_path()
    data = load_config_dict(path)
    servers = data["mcpServers"]
    if name not in servers:  # 同 remove：列出已有名字，敲错的概率远高于真的没有
        existing = "、".join(servers) if servers else "（空）"
        raise _CliError(f"没有叫 {name} 的服务器。现有：{existing}")

    console.print(Text(f"  {name}", style=ACCENT))
    # 用 indent=2 而不是压成一行：用户是来读的，不是来解析的。
    # 一条 stdio 配置至少三行（type/command/args），压成一行在窄终端里会折得看不清。
    # 原样输出 JSON 而不是重新拼一行命令：用户常常是想确认「文件里到底写了什么」，
    # 这时候把未知字段（别人手工加的 env、cwd）藏起来反而添乱。
    console.print(Text(json.dumps(servers[name], indent=2, ensure_ascii=False), style=DIM))
    console.print(Text(f"  配置文件：{path}", style=DIM))
    return 0


def _print_help() -> None:
    """打印用法（三处入口共用一份文本，见 _HELP_TEXT 的注释）"""
    console.print(Text(_HELP_TEXT, style=DIM))
