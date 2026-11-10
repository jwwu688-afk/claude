# =============================================================================
# 文件：src/core/commands.py
# 作用：斜杠命令的注册表与处理函数（2026-11-07 第 14 轮新增）
#
# 【一、为什么需要这一层：有些事根本不该问模型】
#   下面这几句话，发给模型是纯浪费：
#     "/cost"        → 模型不知道你花了多少钱，说了也是编
#     "/resume"      → 要切会话，那是本地文件操作
#     "/memory"      → 问"你记了我什么"，答案在磁盘上的几个 .md 里
#     "/init"        → 生成骨架文件，是写盘，不是对话
#   把它们当普通消息发出去，会连付三次代价：
#     1. 多一次输入 token；
#     2. 换来一个编造的答案；
#     3. 这条命令本身留在上下文里，以后每一轮都在为它付费。
#   所以必须在"消息进历史之前"截下来，本地处理完直接回到输入提示。
#
#   反过来说，只有"本地百分之百能答"的才配当命令。
#   "帮我重构这一段"这类必须交给模型——命令系统不该变成第二套意图路由，
#   那等于用 if/elif 重新实现一遍模型的判断，而且一定更差。
#
# 【二、一次 /cost 的完整时间线（这就是整个机制）】
#   23:41:02  用户敲 "/cost"
#   23:41:02  Conversation._read_user_input() 拿到字符串 "/cost"
#   23:41:02  is_command() → True（行首是 /）
#   23:41:02  handle_command() 被调用：
#               parse("/cost") → ("cost", "")
#               REGISTRY["cost"] → CommandSpec
#   23:41:02  cmd_cost() 读 APIClient / HistoryManager，打印五行
#   23:41:02  回到 _read_user_input 的 while 顶部，**继续等下一句**
#   23:41:30  用户敲 "这个函数什么意思"
#   23:41:30  不是命令 → 原样返回
#   23:41:30  start_conversation 才把这句话 add_message 并发给模型
#
#   注意 23:41:02 那一步"继续等"，而不是"返回空串"：
#   如果返回空串当用户消息，模型会收到一条空消息，还要为它付一次输入费用——
#   而这条命令问的偏偏是"我花了多少钱"。
#
# 【三、改前 vs 改后（为什么要新加一个 _read_user_input）】
#   改前：主 agent 有三处各自 await ui.get_user_input()——
#         开场、Esc 中断之后、一轮说完之后。三处各写一遍判断，加第四处必然漏。
#   改后：三处都调 _read_user_input()，命令的拦截点只有一个。
#         "命令对所有用户输入都生效"变成结构上保证的，不靠人记得。
#
# 【四、为什么用一张表，而不是 if / elif 链】
#   和 ToolManager 登记工具是同一个理由：加命令时只加一条 register()，
#   分发、/help 列表、未知命令提示三处会自动跟着变。
#   第 8.1 轮踩过一个反向的坑：task 工具的说明书里手写了三种 agent 类型，
#   其中两种根本没登记，模型照着示例派活，一直报"类型不存在"。
#   那次之后，凡是"有几项、分别是什么"的东西，都改成从登记表里取。
#
# 【五、边界：分三批落地】
#   第一批（第 14 轮）只做"纯读"：/memory /resume /init /cost。
#   第二批（第 14.5 轮）加了会**改上下文**的两条：
#     /compact 手动压缩 —— 会把旧对话换成摘要；
#     /clear   清空上下文 —— 会换一个新的会话文件。
#   为什么必须分两批：前四条的失败最多是"某个数字不对"，
#   这两条的失败是"历史被改坏，后面每轮请求都 400"。
#   风险等级不一样，混在一批里做，回归挂了没法二分定位是哪一条引起的。
#
#   第三批（第 21 轮）是"技能登记成命令"：
#     磁盘上每有一个技能（.thinker/skills/ 下的 SKILL.md），就登记一条 /技能名。
#     它的 handler 不打印正文、也不直接调模型，只把渲染好的正文放进
#     Conversation 的"待发消息"槽位——斜杠命令"不进历史、不计费"的老规矩
#     （第 14 轮）因此一字不动，而技能正文走的正是普通用户消息那条路。
#
# 【六、为什么技能必须有真命令，不能在 handle_command 里兜底查】
#   /help 和 Tab 补全都遍历 REGISTRY。如果技能只在"未知命令"分支里被兜住，
#   它们既不会出现在 /help 里、也不会被 Tab 补全——用户永远发现不了。
#   第 8.1 轮的教训是"有几项、分别是什么"必须从登记表取；
#   这次要保证"技能也是登记表里的"，就只能真的登记进去。
# =============================================================================

import os  # /resume 按 cwd 过滤"本项目的会话"；/init 要拼 THINKER.md 的路径
from dataclasses import dataclass  # 用一个"只装字段"的小类描述命令，省掉手写 __init__
from typing import Awaitable, Callable, Dict  # handler 是"异步函数"，注册表是字典

from core.session import SessionStore  # /resume 要列会话、读会话


@dataclass(frozen=True)
class CommandSpec:
    """一条斜杠命令的说明书（一条命令 = 一个实例）

    frozen=True 的理由：
      注册表是全模块共享的，一旦某处手滑改掉 usage 或 summary，
      /help 的显示就会在运行中悄悄变样，而且极难查。
      冻结之后，改它的唯一方式就是重新 register 一条，意图明确。

    字段含义：
      name    ：不带斜杠的命令名，比如 "memory"。它是注册表的键，也是分发依据。
                必须是纯 ASCII 小写——用户可能开着中文输入法敲 / ，
                全角斜杠会被 is_command 与 parse 一致地挡在命令系统之外
                （宁可当成普通消息，也不要把 "/cost" 认成命令）。
      usage   ：给人看的用法，比如 "/memory [文件名]"。方括号表示可选，
                和 man page、git 的写法一致，不用另发明一套约定。
      summary ：一句话说明。/help 直接拿它拼列表，所以它必须能独立成句，
                不能写"见上文"这种依赖上下文的表述。
      handler ：真正干活的异步函数，签名 (conversation, args) -> None。
                为什么把 conversation 传进去、而不是让 handler 自己去 Conversation()：
                  命令要在测试里跑（test_commands.py 用的是 FakeConv），
                  写死单例就没法替身；参数传进来，测试才能塞假对象。
    """

    name: str
    usage: str
    summary: str
    handler: Callable[["object", str], Awaitable[None]]


# 注册表：键是命令名（小写），值是说明书。
# 用模块级字典而不是类，是因为它完全无状态——
# 不像 ToolManager 要持有工具实例，这里只有"名字 → 函数"，没有实例可管。
# 也正因为是模块级，import 一次就全局可用，不需要谁去初始化。
REGISTRY: Dict[str, CommandSpec] = {}


def register(spec: CommandSpec) -> None:
    """把一条命令登记进表里

    重复登记会直接覆盖，不做冲突检查。理由：
      调试时经常要临时替换某个命令（比如把 /resume 换成只打印、不真切会话），
      每次先 unregister 再 register 太啰嗦。
      真出问题时，"后注册的赢"也是最好理解的规则。
    """
    REGISTRY[spec.name] = spec


# 【2026-11-10 第 21 轮】技能命令名台账。
#   为什么要记：技能是磁盘上的文件，会增会删。/skills reload 时如果只"补登记新的"，
#   已经删掉的那个技能仍留在 REGISTRY 里——用户敲 /old-name 会得到
#   "技能已经不在了"，看起来像 reload 根本没生效。
#   所以重新登记前，先按这个集合把上一轮登记的技能命令整体撤掉。
#
#   为什么不用"扫一遍 REGISTRY，凡是 handler 是技能的就删"：
#     那要靠函数的 __name__ 或某种标记来认人，脆弱且难读。
#     一份显式的名字集合，读代码的人一眼就知道它管什么。
_skill_command_names: set = set()


def builtin_command_names() -> set:
    """内置命令名（= 注册表里除去技能登记的那些）

    谁在问：SkillManager 扫描时用来拦"技能和内置命令重名"（比如一个叫 cost 的技能）。
    为什么要减掉 _skill_command_names：
      否则同名判断会把"上一轮自己登记过的技能命令"当成内置命令，
      于是 reload 之后一个技能会因为跟它自己的旧命令重名而被拒载——
      表现为"第一次能用，reload 一次就消失了"。
    """
    return {name for name in REGISTRY if name not in _skill_command_names}


def is_command(text: str) -> bool:
    """判断这一行是不是命令（True = 该由本地处理，别发给模型）

    参数 text：用户刚敲的那一行，可能带前导空格（从别处复制粘贴过来时很常见）。

    为什么要求"第一个非空白字符就是 /"：
      正文里出现 / 太多了——
        · Windows 路径：D:/code/thinker
        · 除法：3/4
        · 日期：2026/11/08
        · 网址：https://example.com
      如果按"含 /"判断，上面这些会被误当成命令吞掉，
      用户会觉得"我打个路径它就没反应"。
      只认行首，和 shell、Claude Code 的习惯一致：命令必须在开头。

    边界：
      · 空串 → False（用户直接回车）
      · "/" → True（交给 handle_command，它会打印帮助）
        为什么不让它判 False：那样用户打一个斜杠回车，这行会被当成普通消息
        发给模型，模型只能回"我不明白"。打印帮助更有用。
    """
    return bool(text) and text.lstrip().startswith("/")


def parse(text: str):
    """把 "/memory  foo.md" 拆成 ("memory", "foo.md")

    返回 (命令名, 参数串)；拆不出命令名时返回 (None, "")。

    三个细节：
      1. 命令名转小写：/COST 和 /cost 应该等价，用户不会记得大小写。
         参数**不动**：/memory 后面可能是 README.md，转小写就找不到文件了。
      2. split(None, 1) 只切一刀：命令名之后再出现的空格，全部算参数。
         这样 /init --global --force 的 args 是 "--global --force" 一整串，
         由 handler 自己按空格再切——不同命令的开关规则不一样，
         在这里统一切成列表只会限制它们。
      3. 只有一个 "/" 时返回 (None, "")：让上层走"打印帮助"那条路，
         而不是报"未知命令 /"，那样没有任何信息量。

    边界：
      · "/ "（斜杠加空格）→ body 是 " "，strip 后为空 → (None, "")
      · "/cost "（尾部空格）→ args 是 ""，不是 " "
    """
    body = text.lstrip()[1:]  # 去掉行首空白，再砍掉那个斜杠
    if not body.strip():  # 只有斜杠，或者斜杠加空白
        return None, ""

    parts = body.strip().split(None, 1)  # 最多切一刀：命令名 + 其余全部
    name = parts[0].lower()  # 只有命令名转小写，参数保持原样（见上面第 1 点）
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


async def handle_command(text: str, conversation) -> None:
    """执行一条命令；调用方负责保证 text 确实以 / 开头

    为什么返回 None 而不是 bool：
      不管成功、失败、未知命令，对调用方来说结果都一样——
      "这一行已经被消费掉了，不要发给模型"。
      返回 True/False 反而会诱惑调用方在 False 时把命令发给模型，
      那就绕回了我们想避免的路径。

    未知命令为什么不往模型发，而是就地给清单：
      用户打错一个字母（/memroy），发给模型只会得到"我无法执行斜杠命令"，
      然后他还要自己猜正确拼写。直接列出可用命令，一次就能改对。
      这个教训来自第 8.1 轮：task 工具当时抛异常，异常被上层吞成
      "运行工具时出错"，模型看不到"正确的有哪些"，于是再猜一个继续撞。

    参数：
      text         ：用户输入的整行，形如 "/cost" 或 "/memory foo.md"
      conversation ：对话总管。命令要读它的 api_client / history_manager /
                     _session 等实时状态，所以必须传进来，不能自己去 new 单例。
    """
    name, args = parse(text)
    ui = conversation._ui_manager  # 所有输出都走这个门面，不直接 print

    if not name:  # 用户只打了一个 "/"
        _print_help(conversation)
        return

    spec = REGISTRY.get(name)
    if spec is None:  # 打错了，或者这条命令还没实现
        ui.print_error(f"未知命令：/{name}")
        # sorted 是为了让每次提示的顺序一致：注册顺序取决于 import 次序，
        # 用户看到的清单不该因为改代码而重排。
        names = "、".join("/" + n for n in sorted(REGISTRY))
        ui.print_info(f"可用命令：{names}（/help 看详细说明）")
        return

    try:
        await spec.handler(conversation, args)
    except Exception as e:
        # 命令出错绝不能把整个对话带崩。
        # 注意它和工具执行是两种处理：工具失败要变成一条消息交给模型自我纠正，
        # 命令失败只影响用户自己这一下，跟模型无关，所以就地报错即可。
        # 带上异常类型：只打 {e} 的话，遇到消息为空的异常（比如被取消）
        # 会打印成"执行失败：:"，什么线索都没有。
        ui.print_error(f"/{name} 执行失败：{type(e).__name__}: {e}")


# =============================================================================
# 一、/help
# =============================================================================

def _display_width(text: str) -> int:
    """算一段文字在等宽终端里占几列（中文按 2 列算）

    为什么不能直接用 len()：
      终端里中文是双宽字符。"/memory [文件名]" 是 15 个字符，但显示占 18 列。
      按 len() 补空格的话，含中文的那几行会明显比别的行短一截，
      看起来像"漏了个空格"——明明是对齐代码，却让人以为坏了。

    三个区间分别是：
      0x4E00-0x9FFF  CJK 统一表意文字（汉字主体）
      0x3000-0x303F  中文标点（、。「」之类）
      0xFF00-0xFFEF  全角符号（，：（））
    这三段覆盖了中文正文的绝大部分。用区间判断比引入 unicodedata 快得多，
    也不需要额外依赖——项目明确不加新依赖。
    """
    width = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            width += 2  # 双宽
        else:
            width += 1  # 半宽（ASCII、数字、符号）
    return width


def _print_help(conversation) -> None:
    """列出所有命令

    列宽按最长的那条 usage 动态算，而不是写死一个数字：
      `/init [--global] [--force]` 比 `/cost` 长一倍多。
      写死小值，长命令会把摘要顶到紧贴，看起来像"少了个分隔符"；
      写死大值，短命令后面又会拖一大段空白。
      动态取最大值，两头都不难看。

    排序：按命令名排，不按注册顺序。
      注册顺序取决于 import 时的执行次序，是实现的偶然结果；
      用户看到的顺序必须稳定，否则每次改代码 help 列表就重排一遍。
    """
    ui = conversation._ui_manager
    specs = sorted(REGISTRY.values(), key=lambda s: s.name)
    width = max(_display_width(spec.usage) for spec in specs)

    ui.print_info("可用命令：")
    for spec in specs:
        padding = " " * (width - _display_width(spec.usage))  # 按显示宽度补，不按字符数补
        ui.print_info(f"  {spec.usage}{padding}  {spec.summary}")

    ui.print_info("")
    # 这句不是装饰：用户第一次看到斜杠命令，最想知道的是
    # "它会不会也被记进对话、也被计费"。直接说清楚，省得他去试探。
    ui.print_info("斜杠命令在本地处理，不会发给模型，也不占上下文。")
    # 【2026-11-10 第 21 轮】技能命令是**唯一**的例外，上面那句话对它不成立。
    #   必须写出来：用户看到 /pdf 以为"本地处理不花钱"，结果它把两千字正文
    #   当成用户消息发出去、还按输入计费——不说清楚，这是最容易被当成 bug 的地方。
    #   只在真的登记了技能命令时才提这一句，没技能的用户不该被这个概念打扰。
    if _skill_command_names:
        ui.print_info("技能命令（.thinker/skills 里那些）例外：它把技能正文当成你说的这句话发给模型。")
    # 【第 16 轮】Tab 补全是个"隐形的功能"：不写在任何地方，用户永远不会发现。
    # 而 help 恰好是他会主动打开的地方，放在这里最省事。
    ui.print_info("输入 / 开头之后按 Tab，可以补全命令名（唯一匹配会直接补上）。")


async def cmd_help(conversation, args: str) -> None:
    """/help —— 列出所有命令（忽略参数）

    为什么不支持 "/help memory" 看单条详细说明：
      单条的"详细说明"目前只有一行摘要，拆开也没有更多内容可看。
      等哪条命令的参数复杂到一行写不下（比如以后的 /compact [比例]）再加。
      现在做，只会多一个没人用的分支。
    """
    _print_help(conversation)


# =============================================================================
# 二、/memory —— 我现在记了你什么（兑现 ROADMAP 的 A1）
# =============================================================================

async def cmd_memory(conversation, args: str) -> None:
    """查看自动记忆：目录、条目、索引；带参数可看某条的正文

    为什么这条最该先做：
      第 12 轮上了自动记忆之后，模型一直在往
      ~/.thinker/projects/<项目>/memory/ 里写文件，但用户**完全看不到**。
      记错了、记重复了、记了不该记的，只能自己开资源管理器一层层翻。
      一个"只写不读"的记忆系统，用户没法信任它——
      他不知道自己上次纠正过的事有没有被记住，也就不会再认真纠正。

    两种模式：
      不带参数：目录 + 条目数 + 条目清单 + 索引全文（回答"你记了什么"）
      带参数  ：某一条的正文（回答"那一条具体写了啥"）

    参数 args：记忆文件名，扩展名可省。
      为什么允许省：目录里全是 .md，让用户每次多打四个字符没有信息量。
      但如果他真打了（/memory a.md），也不能变成 a.md.md——所以先判断再拼。

    边界：
      · 记忆目录不存在 → 提示"还没有"，不报错。全新项目本来就没有，这是正常状态。
      · 文件不存在 → 报错 + 列出已有条目，让用户直接照着打。
      · 路径越界 → 拒绝。理由见下面那段。
    """
    # 放在函数内 import 而不是文件头：auto_memory 会读路径、建目录，
    # 和本模块的"纯注册"职责无关；而且这样导入失败只影响这一条命令，
    # 不会让整个命令系统在启动时就连不上。
    from core.auto_memory import is_memory_path, list_entries, load_index, memory_root

    ui = conversation._ui_manager
    root = memory_root()  # ~/.thinker/projects/<cwd 转成的 slug>/memory

    if not root.is_dir():
        # 目录不存在不是错误：第一次用的人本来就没有记忆。
        # 但要说清"它什么时候会出现"，否则用户会以为功能没做。
        ui.print_warning(f"还没有记忆目录：{root}")
        ui.print_info("等模型第一次记东西时会自动创建。")
        return

    if args:  # 带参数：看某一条的正文
        name = args if args.endswith(".md") else args + ".md"
        target = root / name

        # 越界检查。为什么命令也要查一遍：
        #   命令是用户手打的，理论上他不会害自己；但这条路径最终会喂给 open()，
        #   "/memory ../../.ssh/id_rsa" 是一秒钟就能打出来的。
        #   记忆目录在 ~/.thinker 下，和项目目录完全无关；
        #   混进一个能读任意文件的入口，以后想收紧都没机会。
        #   is_memory_path() 会先 resolve 再按路径段比较，.. 和软链接都绕不过。
        if not is_memory_path(target):
            ui.print_error(f"拒绝访问：{args} 不在记忆目录里。")
            return

        if not target.is_file():
            ui.print_error(f"没有这条记忆：{target.name}")
            available = list_entries()
            if available:  # 空的时候不打"现有条目："，那行会孤零零挂着
                ui.print_info("现有条目：" + "、".join(available))
            return

        # errors="replace"：文件被别的程序写坏、或者不是 UTF-8 时，
        # 用 � 代替非法字节，而不是抛 UnicodeDecodeError 让整条命令失败。
        # 查看记忆是只读操作，读不全比读不出来强。
        text = target.read_text(encoding="utf-8", errors="replace")
        ui.print_info(f"— {target.name} —")
        # 单条记忆正常只有一两百字。超过 4000 基本是把整份文档贴进来了，
        # 截断并明说，免得刷满一屏让人以为程序卡住。
        if len(text) > 4000:
            text = text[:4000] + f"\n…（已截断，完整内容 {len(text)} 字符）"
        ui.print_info(text.rstrip())  # rstrip：文件末尾的换行会多留一个空白行
        return

    entries = list_entries()  # 只数 .md，且排除索引自己，见 auto_memory.list_entries
    index = load_index()  # 超长会被截断并注明，这里不用再处理

    ui.print_info(f"记忆目录：{root}")
    ui.print_info(f"条目数量：{len(entries)}")
    if entries:
        ui.print_info("")
        ui.print_info("条目：")
        for name in entries:  # list_entries 已经排好序，不用再 sort
            ui.print_info(f"  · {name}")
    ui.print_info("")
    ui.print_info("索引（MEMORY.md）：")
    # 没有索引时不显示"（空）"就结束，而是明确说"还没有任何记忆"——
    # 前者像出错了，后者是正常状态。
    ui.print_info(index if index else "  （空。还没有任何记忆。）")
    ui.print_info("")
    ui.print_info("看某一条的正文：/memory <文件名>（扩展名可省）")


# =============================================================================
# 三、/resume —— 在多个历史会话里选一个（兑现 ROADMAP 的 I3）
# =============================================================================

async def cmd_resume(conversation, args: str) -> None:
    """列出本项目的会话，选一个接着聊

    和第 13 轮的 `--continue` 什么关系：
      `--continue` 只能接"最近一次"，而且只能在启动时用。
      这条命令补两个场景：
        1. 这个项目有好几个会话，我想挑其中一个（不是最近那个）；
        2. 聊到一半发现聊岔了，想回到上午那次重新开始。

    为什么只列本项目（按 cwd 过滤）：
      在 A 项目里 /resume 却接上 B 项目的话题，模型会拿着一堆无关上下文，
      反而更糊涂。这个判断和 --continue 一致，两处行为不该有分歧。

    参数 args：暂时不使用。留着是因为以后可能要 "/resume 3" 直接选第三项；
      现在只做交互式选择，少一条没人验证的代码路径。
    """
    ui = conversation._ui_manager

    # 子 agent 也可能被误调到这里（虽然主循环不会给它们读输入的机会）。
    # 明确拒绝，比让它去改主对话的历史安全——那会把主 agent 的上下文搞乱。
    if getattr(conversation, "is_subagent", False):
        ui.print_error("子 agent 里不能用 /resume。")
        return

    sessions = SessionStore.list_sessions(_cwd())  # 新的在前（文件名以时间开头）
    if not sessions:
        ui.print_warning("这个目录下还没有任何历史会话。")
        return

    current_path = getattr(getattr(conversation, "_session", None), "path", None)
    # 为什么是两层 getattr：Conversation.__new__ 出来的对象（测试、子 agent）
    # 可能没有 _session，直接 .path 会 AttributeError。

    # 只列最近 10 个。为什么是 10：
    #   终端一屏大约 20~30 行，10 行会话 + 标题 + 提示符刚好不滚动。
    #   要列更多就得翻页，而翻页需要接管键盘——那是 prompt_toolkit 的活，
    #   项目明确不加依赖（见 THINKER.md「不要做的事」）。
    picked = sessions[:10]

    labels = []
    for path in picked:
        # 每个文件都要读一遍才能拿到 header 和消息数。
        # 为什么敢这么干：会话一般几十个、每个几十 KB，实测无感。
        # 真要优化就把 cwd 编进文件名，但那样名字会变得很难看，不值得。
        messages, header = SessionStore.load(path)  # 坏行会被跳过，不会抛

        started = header.get("started_at", "")[:16].replace("T", " ")
        # "2026-11-07T23:47:12" → "2026-11-07 23:47"。
        # 去掉秒和 T：列表里不需要精确到秒，短一点更好扫。

        preview = _first_user_text(messages)
        mark = "  ← 当前" if current_path and path == current_path else ""
        # 标出"当前"是必要的：用户很可能不知道自己正在哪个会话里，
        # 选了当前这个会看起来"什么都没发生"，然后以为命令坏了。
        labels.append(f"{started}  {len(messages):>3} 条  {preview}{mark}")
        # {len(messages):>3} 右对齐到 3 位：条数会从个位变到三位，
        # 不补齐的话后面的预览会参差不齐。

    idx = await ui.get_numbered_choice("选择要恢复的会话：", labels)
    if idx is None:  # 回车 / 非数字 / 越界，统一当取消
        ui.print_info("已取消。")
        return

    target = picked[idx]

    # 换历史走的是和 --continue 完全相同的那个方法。
    # 为什么不在这里自己写一遍：那段逻辑有四个步骤（读文件、换历史、
    # 补用量账本、换会话句柄），第 13.1 轮就是漏了第三步导致恢复长会话必 400。
    # 两处各写一遍，下次改动只会补在一处。
    count, estimated = conversation._load_session_into_history(target)

    ui.print_success(f"已切换到：{target.name}")
    ui.print_info(f"共 {count} 条消息，按字符估算上下文约 {estimated:,} token")
    # 只报数量，不把历史内容打出来：接上一个 100 条的会话，
    # 全打出来会刷掉整屏，而用户想要的只是"我回来了"。
    ui.print_info("直接说下一句，就接着这个会话聊。")


def _cwd():
    """当前工作目录

    为什么单独包一层函数，而不是各处直接 os.getcwd()：
      /resume 要按 cwd 过滤会话，而测试需要在临时目录里造几个假会话，
      又不能真的 chdir（会污染同一进程里的其他测试）。
      留一个可替换的接缝，测试把它换成固定路径就行。
      生产代码里它就是个转发，没有额外逻辑，多这一层几乎零成本。
    """
    return os.getcwd()


def _first_user_text(messages) -> str:
    """从一段消息里找第一句用户说的话，压成一行当预览

    为什么用"第一句"而不是"最后一句"：
      会话列表按时间倒序，第一句最能说明"这次聊的是什么"。
      最后一句往往是"嗯"、"继续"、"好的"，看不出任何主题。

    为什么要兼容 str 和 list 两种 content：
      历史里 content 是"内容块列表"（[{"type":"text","text":...}]），
      但测试里、以及早期版本写下的会话文件里可能是纯字符串。
      两种都读，不至于因为一条老数据就让整份列表显示不出来。

    截断到 36 个字符：
      一行要塞下"时间 + 条数 + 预览"，终端宽度减去前面那些只剩这么多。
      中文 36 字已经足够表达一个主题了。
    """
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue  # 只看用户说的；system 是提示词，assistant 是模型的回答

        content = message.get("content")
        if isinstance(content, str):  # 老格式 / 测试数据
            text = content
        elif isinstance(content, list):  # 标准格式：内容块列表
            text = " ".join(
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            continue  # None 或其他奇怪结构，跳过这一条继续找

        text = " ".join(text.split())  # 换行、连续空格压成单个空格，否则列表会错行
        if text:
            return text[:36] + ("…" if len(text) > 36 else "")
    return "（没有用户消息）"  # 只说了一句 /cost 就退出的会话会是这种


# =============================================================================
# 四、/init —— 生成项目记忆骨架（兑现 ROADMAP 的 M2）
# =============================================================================

async def cmd_init(conversation, args: str) -> None:
    """生成 THINKER.md 骨架（会话内唯一的入口）

    【为什么做成斜杠命令，而不是命令行参数】
      原来的 `thinker --init` 必须**启动前**敲。而人往往是聊到一半才发现
      "这个项目的约定该写进 THINKER.md"，这时候退出重开再进来，上下文就断了——
      为了写一份记忆文件把正在进行的对话丢掉，很不划算。
      2026-11-08 清理时把 CLI 那条路删掉了：两个入口意味着主目录守卫、提示文案、
      测试都要维护两份，而"必须先退出"本身就是个没必要的限制。

    参数（按空格切成开关，顺序无关）：
      --global  写全局记忆（~/.thinker/THINKER.md），对所有项目生效
      --force   覆盖已存在的文件
      --here    允许在用户主目录里生成（默认拒绝，见下）

    为什么在主目录里默认拒绝（2026-11-08 第 14.1 轮补）：
      有用户从 `PS C:\\Users\\Jarvis>` 启动了 thinker，再执行 /init，
      于是生成了 `C:\\Users\\Jarvis\\THINKER.md`——一份写着
      "（没检测到常见的项目标志文件）"的骨架，却会成为**所有从主目录启动的
      会话**的项目记忆。它描述的不是任何项目，只是把一条错误的小知识
      反复喂给模型。
      这类错误在终端里看不出来：命令回的是"已生成项目记忆"，看着完全正常。
      所以默认拒绝 + 明确告诉用户"请先 cd 到项目目录"；
      真要写在这儿（少数人确实拿主目录当工作区），加 --here 就行。

    为什么默认不覆盖：
      用户可能已经在里面手写了几百字的项目约定，一条命令全毁掉的代价
      远大于"多打一次 --force"的便利。
      这类"不可逆 + 高价值"的默认值，一律选保守的那边。

    边界：
      文件已存在且没给 --force → 提示"没有覆盖"并返回，不改动任何内容。
    """
    # 同样放函数内 import：只有真的执行 /init 时才需要写盘相关的东西。
    from core.prompt.memory import (
        GLOBAL_MEMORY_PATH,
        MEMORY_FILENAME,
        is_user_home,
        write_init_file,
    )

    ui = conversation._ui_manager
    flags = args.split()  # 开关按空格切；和 parse() 保留整串不同，这里就是要逐项判断
    scope = "global" if "--global" in flags else "project"
    force = "--force" in flags

    # 主目录守卫：只拦"项目"这一档。
    #   --global 写的是 ~/.thinker/THINKER.md，那本来就是跟人走、不跟项目走的，
    #   在主目录里执行它完全正当，不该拦。
    if scope == "project" and is_user_home() and "--here" not in flags:
        ui.print_error(f"当前目录是用户主目录：{os.getcwd()}")
        ui.print_info("在这里生成的 THINKER.md 会成为所有「从主目录启动」的会话的项目记忆，")
        ui.print_info("而它描述的不是任何具体项目。请先 cd 到项目目录再执行 /init。")
        ui.print_info("确实要写在这里，请加 --here。")
        return

    if scope == "global":
        target = GLOBAL_MEMORY_PATH.expanduser()  # ~ 不展开的话 exists() 永远是 False
        what = "全局记忆"
        next_step = "按实际习惯改「说话方式」「动手习惯」，尤其是那几条红线（不要自动提交）。"
    else:
        target = os.path.join(os.getcwd(), MEMORY_FILENAME)
        what = "项目记忆"
        next_step = "补充「代码约定」「不要做的事」两节，模型每轮都会读到。"

    existed = os.path.exists(target)  # 先记下来：write_init_file 跳过了也不会告诉我们
    path = write_init_file(force=force, scope=scope)  # 真正写盘就这一行（项目/全局共用同一函数）

    if existed and not force:
        ui.print_warning(f"{what}已存在，没有覆盖：{path}")
        ui.print_info("想重新生成请加 --force（会覆盖你现在的内容）")
        return

    ui.print_success(f"已生成{what}：{path}")
    ui.print_info(next_step)
    # 这句必须说：记忆是在 start_conversation 里拼进系统提示词的，
    # 也就是"启动时读一次"。改完不重启，模型这一轮还是按老规矩来，
    # 用户会以为命令没生效。
    ui.print_info("注意：记忆是启动时读进系统提示词的，改完要重启 thinker 才生效。")


# =============================================================================
# 五、/cost —— 这次会话花了多少、缓存有没有帮上忙
# =============================================================================

async def cmd_cost(conversation, args: str) -> None:
    """打印成本与上下文占用

    为什么这条必须有：
      第 6 轮加了缓存命中率，但它只出现在每轮结束的状态行里，滚两屏就看不见了。
      而且那是"这一轮"的视角，回答不了"我这个下午一共花了多少"。
      没有这个数字，用户没法判断"要不要换个便宜模型"、
      "缓存到底有没有省到钱"——省钱的机制看不见，就等于不存在。

    口径说明（这几个数字都是累计的，不是这一轮的）：
      prompt_tokens / cached_tokens 由 APIClient._record_usage 每次请求累加；
      total_cost 只在接口返回 usage.model_extra.cost 时才累加。
    """
    ui = conversation._ui_manager
    api = conversation._api_client
    history = conversation._history_manager

    cost = api.total_cost
    # 关键取舍：0 到底是什么意思？
    #   有两种可能——真的一分钱没花，或者接口根本不返回 cost 字段。
    #   从数字上分不清，所以绝不能显示 "$0.00"：那会让人以为免费，然后放开了用。
    #   显示"未提供"才是诚实的。（spark-x2.6 这类中转站大概率不返回 cost。）
    cost_text = f"${cost:.2f}" if cost else "未提供（接口没返回 cost 字段）"

    lines = [
        f"花费：{cost_text}",
        # 千分位：48213 和 48,213 的可读性差得远，而 token 数动辄五位数。
        f"输入 token：{api.prompt_tokens:,}",
        f"命中缓存：{api.cached_tokens:,}（{api.cache_hit_rate:.1f}%）",
        # current_context_window 本身是字符串（"11.8"），因为状态行要直接拼进 f-string。
        f"上下文占用：{history.current_context_window}%",
        # 条数能解释"为什么上下文这么大"：压缩过之后会突然掉一截，
        # 看到条数变少就知道是压缩生效了，而不是哪里出错了。
        f"当前消息数：{len(history.messages_history[-1])}",
    ]

    # 命中率为 0 时补一句解释。为什么值得单独说：
    #   0% 有两个完全不同的原因——接口不支持提示词缓存（换供应商才能解决），
    #   或者消息前缀一直在变（比如系统提示词里带了时间戳，每次请求都不一样）。
    #   不点出来，用户只会觉得"缓存这东西没用"。
    if api.cache_hit_rate == 0 and api.prompt_tokens > 0:
        lines.append("缓存命中为 0：要么接口不支持提示词缓存，要么消息前缀一直在变。")

    for line in lines:
        ui.print_info(line)


# =============================================================================
# 六、/compact —— 立刻压一次，不等 80%（第 14.5 轮）
# =============================================================================

async def cmd_compact(conversation, args: str) -> None:
    """手动压缩上下文，不看有没有过阈值

    【为什么需要手动这一下】
      自动压缩只在超过 80% 时动手，那是"防爆炸"的兜底。
      但有一类情况永远等不到 80%：刚让模型试了七八种写法，过程很长、
      结论只有一句。这时候上下文可能才 30%，压掉之后每一轮都省，
      而没有 /compact 的话用户对此毫无办法。

    【为什么提示里要说"会调用一次摘要模型"】
      压缩不是本地文本处理：它会把被删的那段交给模型总结，真花钱。
      用户按一下 /compact，账单上会多一次请求，不说清楚会以为是 bug。
      这也是项目一贯的取舍：凡是会花钱的动作，先在界面上说一声。

    参数 args：暂时不用。将来可能支持 "/compact 0.5" 指定压掉多少比例，
      现在先只做一个"压一轮"的版本，少一条没人验证的路径。
    """
    ui = conversation._ui_manager
    history = conversation._history_manager

    # 只有系统提示词时没有任何"对话轮次"可压。
    # 为什么要提前拦：压缩是按"用户消息"切段的，一段都找不到时
    # 底层会安静地什么都不做，用户看到"已压缩、省了 0 token"只会更困惑。
    if len(history.messages_history[-1]) <= 1:
        ui.print_warning("当前上下文只有系统提示词，没有可压缩的内容。")
        return

    ui.print_info("正在压缩上下文…（会调用一次摘要模型，产生一次请求）")
    stats = await history.compact_now()

    # 为什么用 token 而不是"条数"判断有没有效果：
    #   压缩不是纯删除——它会在原位置插一条"历史已压缩 + 摘要"的公告。
    #   删掉 3 条、插入 1 条，条数可能只差 2；极端情况下摘要比被删内容还长，
    #   条数变少了、token 反而更多。用户关心的是"省了多少上下文"，不是条数。
    if stats["after_tokens"] >= stats["before_tokens"]:
        ui.print_info("这次压缩没省下 token：摘要本身也要占长度，被删的内容太短时不划算。")
        ui.print_info("可以直接继续；等对话再长一些再压更容易见效。")
        return

    saved = stats["before_tokens"] - stats["after_tokens"]
    percent = (saved / stats["before_tokens"] * 100) if stats["before_tokens"] else 0
    ui.print_success(
        f"已压缩：{stats['before_count']} 条 → {stats['after_count']} 条，"
        f"约 {stats['before_tokens']:,} → {stats['after_tokens']:,} token"
    )
    ui.print_info(f"省了约 {saved:,} token（{percent:.0f}%）。")
    # 这句必须说：压缩是"整体替换消息列表"，前缀从中间断了，
    # 下一轮请求的缓存命中会归零、按原价计费。不讲的话，
    # 用户过一会儿看 /cost 发现花费跳涨，会以为是压缩本身的错。
    ui.print_info("注意：消息列表变了，下一轮的提示词缓存会失效，那一次输入按原价计费。")


# =============================================================================
# 七、/clear —— 清空上下文并开一个新会话（第 14.5 轮）
# =============================================================================

async def cmd_clear(conversation, args: str) -> None:
    """/clear —— 清空上下文，同时换一个新的会话文件

    【为什么不是"把消息列表清空"这么简单】
      只清内存的话，旧内容还在 JSONL 里。用户下次 `--continue`、
      或者用 /resume 选中那个文件，刚清掉的对话会原样回来——
      他会认为命令坏了，而且没法知道是"清了内存、文件还在"。
      所以清空必须同时换文件：新会话从零开始，旧文件留着还能 /resume。
      这样 /clear 才是"开新话题"的正确入口，而不是一个假动作。

    【为什么不顺便重置花费】
      花费是"这个进程到目前为止"的真实开销，清上下文不能让已经花掉的钱消失。
      清零等于骗人。清不了，就说清楚（见下面的提示）。
    """
    ui = conversation._ui_manager
    before_count = len(conversation._history_manager.messages_history[-1])

    old_path, new_path = conversation.reset_conversation()

    ui.print_success(f"已清空上下文（{before_count} 条 → 1 条系统提示词）。")
    ui.print_info(f"新会话文件：{new_path.name}")
    if old_path is not None:
        # 明说旧文件还在：用户可能以为"清空=删除存档"，那样下次想找回就白找了。
        ui.print_info(f"旧会话仍在：{old_path.name}（想回去用 /resume）")
    ui.print_info("累计花费不重置：/cost 显示的是本次进程的总开销。")


# =============================================================================
# 八、注册
#
async def cmd_agents(conversation, args: str) -> None:
    """/agents —— 列出内置的子 agent 类型（第 18 轮新增）

    为什么需要这条命令：
      类型、工具白名单、模型都写在 builtin_agents.py 里。没有它，用户要么去读源码，
      要么只能从 task 说明书里猜。一条命令把"现在有几类、各自能干什么、用什么模型"
      一次说清。

    为什么没有 reload 参数：
      定义是代码的一部分，改完要重启进程——和改其它代码一样。
      简化之后没有"扫目录"这回事，也就没有可重载的东西。
      （前一版有 reload，是因为那时刻意支持从 .thinker/agents 读 md。）
    """
    from tools.subagent.subagent_manager import SubagentManager  # 局部导入：避免命令模块和工具模块在导入期互相牵连

    ui = conversation._ui_manager
    specs = SubagentManager().list_specs()

    ui.print_info(f"内置 {len(specs)} 个子 agent：")
    for spec in specs:
        # 一行摘要 + 一行人设说明。
        #   为什么把工具和模型也打出来：用户能一眼判断"派它会不会弹审批"
        #   （只读工具免审批）、"会不会用到大模型"。
        ui.print_info(f"  {spec.name}  工具：{spec.tools_text()} ｜ 模型：{spec.model_text()}")
        ui.print_info(f"    {spec.description}")


# =============================================================================
# 九、技能（2026-11-10 第 21 轮）
#
# 这一节只做两件事：
#   1. 把磁盘上的技能登记成斜杠命令（register_skill_commands）；
#   2. /skills —— 看清楚现在加载了什么、哪些字段没生效、扫描时出了什么问题。
#
# 技能的正文怎么渲染、同名怎么覆盖、目录在哪，都在 tools/skill/ 里。
# 命令层不重复实现那些规则，只取"已经扫好的清单"。
# =============================================================================

def _one_line_summary(description: str) -> str:
    """把技能描述压成 /help 里能放的一行摘要

    为什么要压：
      /help 是"教用户有哪些命令"的清单，一行一条。描述里要是有换行，
      整个列表的对齐就乱了；太长则会把后面的说明顶出屏幕。
    为什么截 40 字：usage 列通常十几列，终端按 80 列算，剩给摘要的就这么多。
      超出部分在 /skills 里能看到全文——那里才是"了解一个技能"的地方。
    """
    text = " ".join((description or "").split())  # 换行、连续空格压成单个空格
    if not text:
        # 没写描述不是错误（技能仍能用 /名字 手动调用），但摘要位不能空着，
        # 否则 /help 那一行看起来像渲染坏了。
        return "（技能没有写描述）"
    return text[:40] + ("…" if len(text) > 40 else "")


def _make_skill_handler(name: str):
    """造一条技能命令的处理函数

    参数 name：技能名。用闭包固定住，而不是从 args 里解析——
      args 是"用户给的参数"（文件路径之类），技能名是"用户点了哪条命令"，
      两者在 parse() 里已经分开了，再混回去等于把命令名当成参数。

    这个 handler 只做三件事：
      1. 现取一次 spec（可能在扫描之后被删了，所以要判空）；
      2. 渲染正文（现读文件 + 变量替换 + 截断，和 skill 工具走同一个函数）；
      3. 把正文放进 Conversation 的"待发消息"槽位。
    它**不打印正文、不直接调模型**——正文由主循环当用户这一句发出去，
    这样它才会进历史、下一轮模型还记得自己读过。
    """

    async def handler(conversation, args: str) -> None:
        # 局部 import：和本文件其它命令一样，避免命令模块在导入期就拖进工具子包
        # （tools/skill 会读 core.limits，将来也可能反向引用提示词）。
        from tools.skill.skill_manager import SkillManager

        manager = SkillManager()
        ui = conversation._ui_manager
        spec = manager.get(name)

        if spec is None:
            # 扫描之后文件被删掉的情况。不自动 reload：用户可能正在整理技能目录，
            # 悄悄重扫会让"我刚删了它却又能用"更难理解。给一句明确的下一步即可。
            ui.print_error(f"技能 '{name}' 已经不在了（可能刚被删除）。执行 /skills reload 刷新。")
            return

        body = manager.load_body(spec, args)
        # 前缀标一行来源：模型看到这段文字时能区分"这是技能正文"和"这是用户自己写的"，
        # 排查上下文时也一眼能找到技能是从哪一轮进来的。
        conversation.set_pending_input(f"[技能已加载：{name}]\n{body}")

        # 给用户一行回执。为什么不打印正文：正文动辄上千字，刷满屏幕之后
        # 用户既看不到自己的输入，也不知道到底加载成功没有。一行长度就是最好的确认。
        ui.print_info(f"已加载技能 /{name}（正文 {len(body):,} 字符），下面按它的步骤做。")

    return handler


def register_skill_commands() -> int:
    """把磁盘上的技能登记成斜杠命令，返回本次登记了几条

    什么时候调：
      · main.cli() 启动时一次；
      · /skills reload 时再一次。
    两处都走这个函数，是为了避免"启动时和 reload 时行为不一致"——
    那类问题最典型的表现是"重启好了、reload 没好"，而两者本该是同一件事。

    为什么整批撤掉再登记，而不是只补新的：
      技能文件会被删。只补不撤的话，删掉的 /old-name 还在 REGISTRY 里，
      一敲就报"技能不存在"，看起来像 reload 没生效（见 _skill_command_names 的说明）。

    为什么 user-invocable: false 的技能也加载、但不登记：
      它是"只给模型用"的技能（比如一段自动生效的编码约定）。
      出现在斜杠命令里会让用户以为要手动敲它，而它本来就是被模型按需加载的。
    """
    from tools.skill.skill_manager import SkillManager

    manager = SkillManager()
    manager.reload()  # 登记前必须拿到最新清单；重扫的代价是几十个 4KB 读，可以忽略

    for name in list(_skill_command_names):
        REGISTRY.pop(name, None)
    _skill_command_names.clear()

    registered = 0
    for spec in manager.list_specs():
        if not spec.user_invocable:
            continue  # 不进斜杠命令（模型仍能通过 skill 工具调用）
        usage = f"/{spec.name}"
        if spec.argument_hint:
            # 参数提示只影响 /help 的用法显示。没有就不加，免得每行都拖一个空的 [参数]。
            usage += f" {spec.argument_hint}"
        register(
            CommandSpec(
                name=spec.name,
                usage=usage,
                summary=_one_line_summary(spec.description),
                handler=_make_skill_handler(spec.name),
            )
        )
        _skill_command_names.add(spec.name)
        registered += 1
    return registered


async def cmd_skills(conversation, args: str) -> None:
    """/skills —— 列出已加载的技能；/skills reload 重新扫描

    为什么这条必须有：
      技能的目录、优先级、哪些字段没生效、扫描时哪一行写坏了……这些信息
      如果只在启动日志里出现一次，用户改完文件想知道"现在到底加载了什么"
      就只能去翻源码。一个"只写不读"的机制，用户没法信任它——
      和 /memory 那条命令的理由一模一样（第 14 轮）。

    参数 args：
      "reload" → 重扫目录、重新登记斜杠命令、必要时补登记 skill 工具；
      其他/空 → 只看不清单，不做任何改动。
      为什么区分：重扫会改变 tools 清单（技能从 0 变 1 时），
      那会让提示词缓存失效一次。不该在"我只是想看看"的时候发生。
    """
    from tools.skill.skill_manager import SkillManager, global_skills_dir, project_skills_dir

    ui = conversation._ui_manager
    wants_reload = args.strip().lower() == "reload"

    if wants_reload:
        count = register_skill_commands()  # 内部已经 reload 过，不再多扫一次
        # 技能从 0 变 1 时，工具表里还没有 skill，需要补登记（见 ToolManager.refresh_skill_tool）。
        tool_manager = getattr(conversation, "_tool_manager", None)
        added = tool_manager.refresh_skill_tool() if tool_manager is not None else False
        ui.print_success(f"已重新扫描技能目录，登记 {count} 条技能命令。")
        if added:
            # 必须提醒：工具清单在缓存前缀里，变了就会让下一轮缓存命中归零、按原价计费一次。
            # 不说的话，用户过一会儿看 /cost 发现贵了一下，会以为是 reload 的 bug。
            ui.print_info("skill 工具是这次新登记的：下一轮请求的工具清单会变，提示词缓存会失效一次。")

    manager = SkillManager()
    specs = sorted(manager.list_specs(), key=lambda spec: spec.name)  # 按名字排，稳定
    if specs:
        # 顺序固定成"全局在前、项目在后"：和扫描顺序一致，用户容易对上。
        ui.print_info(f"全局目录：{global_skills_dir()}")
        ui.print_info(f"项目目录：{project_skills_dir()}")
        ui.print_info("")
        ui.print_info(f"已加载 {len(specs)} 个技能：")
        for spec in specs:
            # 标记位拼成一段括号，比每个技能打好几行更省屏幕。
            flags = []
            if not spec.user_invocable:
                flags.append("不进斜杠命令")
            if not spec.description.strip():
                # 【第 21.1 轮】没描述不再等于"模型看不到"，只是清单里显示占位文字。
                # 标记保留，是因为"给模型看的那一行信息量很低"仍然值得提醒用户补一句。
                flags.append("没有描述，清单里只显示占位")
            # 【第 21.2 轮】这里原来还有一行"未生效字段：…"（allowed-tools / model / hooks…）。
            #   整套 UNSUPPORTED_FIELDS 机制删掉了：不认识的字段直接忽略、不展示。
            #   少一个概念，就少一处"某人改了字段表、忘了改这里"的可能。
            suffix = f"（{'；'.join(flags)}）" if flags else ""
            description = spec.description or "（无描述）"
            ui.print_info(f"  /{spec.name}  [{spec.source_text()}] {description}{suffix}")
    else:
        ui.print_warning("还没有加载到任何技能。")
        ui.print_info(f"在 {project_skills_dir()} 下建一个 <名字>/SKILL.md 就能用（也可以放 <名字>.md）。")

    # 同名覆盖：必须在列表下面单独说，否则用户会奇怪"我明明改了全局那份"
    for name, path in manager.shadowed:
        ui.print_info(f"同名覆盖：/{name} 用的是项目版本，被盖住的是 {path}")

    warnings = manager.warnings
    if warnings:
        # 扫描问题单独一段。为什么要主动打出来：第 16.1 轮的教训是
        # "静默降级 = 把 bug 藏起来"，一个名字写错、一个 frontmatter 没闭合，
        # 都只会表现为"技能没生效"，不说清楚用户只能靠猜。
        ui.print_warning(f"扫描时有 {len(warnings)} 条问题：")
        for warning in warnings:
            ui.print_info(f"  · {warning}")

    ui.print_info("")
    # 热更新的口径分两半，这里必须说清（见 docs/优化记录/21-… §热更新）：
    #   正文每次调用都现读，改字立即生效；元数据进了工具说明书和 REGISTRY，是快照。
    ui.print_info("只改正文不用 reload（每次都现读）；改了名字/描述/开关要 /skills reload。")


# =============================================================================
# 命令注册（放在文件末尾，理由见本节开头）
# 为什么放在文件末尾，而不是每条命令定义完就注册：
#   读代码的人按"头部说明 → 数据结构 → 工具函数 → 各命令 → 注册表"的顺序看下来，
#   最后一屏就能看到"到底有哪些命令"。散落在中间的话，得来回翻。
# =============================================================================

register(CommandSpec("help", "/help", "列出所有命令", cmd_help))
register(CommandSpec("memory", "/memory [文件名]", "查看模型记了什么（索引与正文）", cmd_memory))
register(CommandSpec("resume", "/resume", "在历史会话里选一个接着聊", cmd_resume))
register(CommandSpec("init", "/init [--global] [--force] [--here]", "生成 / 更新项目记忆骨架", cmd_init))
register(CommandSpec("cost", "/cost", "本次会话的花费与缓存命中", cmd_cost))
register(CommandSpec("compact", "/compact", "立刻压缩上下文（不等 80%）", cmd_compact))
register(CommandSpec("clear", "/clear", "清空上下文并开一个新会话", cmd_clear))
register(CommandSpec("agents", "/agents", "列出子 agent 类型（同时重新扫描目录）", cmd_agents))
# 【2026-11-10 第 21 轮】技能的管理入口。它必须在技能命令之前就登记好：
#   SkillManager 扫描时拿 builtin_command_names() 拦"和内置命令重名"的技能，
#   而 skills 自己也是内置命令——不先登记，一个叫 skills 的技能目录就能把 /skills 顶掉。
register(CommandSpec("skills", "/skills [reload]", "列出已加载的技能；reload 重新扫描", cmd_skills))
