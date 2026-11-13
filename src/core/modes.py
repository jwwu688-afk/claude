# =============================================================================
# 文件：src/core/modes.py
# 作用：定义"工作模式"这一层的全部静态数据与纯函数（2026-11-12 第 33 轮新增）
#
# 【这一层为什么单独存在，而不是塞进 permissions.py】
#   权限层回答的是"这一次调用要不要问"，模式回答的是"这一整轮按哪种口径问"。
#   两者会一起用（classify 的第三个参数就是模式），但生命周期完全不同：
#     · 模式是会话级状态，用户随时按 Shift+Tab 就变；
#     · 权限判定是单次调用的瞬时结果，判完就扔。
#   把模式的定义（几档、叫什么、什么颜色、切换顺序）和判定规则放一个文件里，
#   以后加第五档就得在"一堆正则和危险命令"中间找位置，很容易改错。
#   所以这里只放**纯数据 + 纯函数**，不 import 项目里任何其它模块——
#   permissions.py 反过来 import 这里的常量，依赖方向永远是单向的，不会成环。
#
# 【四档的语义，以及为什么是这四档】
#   这是照 Claude Code 的权限模式做的，四档按"从最严到最松"排：
#     plan   只读：只能看和查，一个字节都不许写。用来出方案。
#     default 默认：每次写文件、每条命令都问一句（项目原来的行为）。
#     accept  接受编辑：改文件不问了，命令照问。
#     auto    自动：什么都不问，直接执行。
#   Claude Code 里 auto 这一档叫 bypassPermissions，默认不在 Shift+Tab 循环里；
#   这里按用户的要求把它放进了循环，所以界面上必须给足颜色警示（金黄色）。
#
# 【循环顺序为什么是 default → plan → accept → auto】
#   从"标准状态"出发，先收紧到只读，再放开到能改文件，最后全放开。
#   四档像一把开关沿一个方向推到底，用户按 Shift+Tab 时能预期"再按一下就换一档"，
#   不需要记"刚才从哪来"。
#
# 【为什么当前模式不写进系统提示词】
#   系统提示词在请求的最前面，改一个字整段前缀缓存就失效（/cost 里的命中率会掉）。
#   所以这里只提供一段**永远不变**的静态说明（MODE_SECTION），
#   当前是哪一档通过每轮末尾的 reminder 注入（见 mode_reminder）。
# =============================================================================

from dataclasses import dataclass  # 装"一档模式有哪些字段"的小容器
from typing import Optional  # normalize_mode 可能返回 None


# --- 四个模式名 ---------------------------------------------------------------
# 用模块级常量而不是字符串字面量：模式名会出现在 classify、conversation、
# commands、ui 四个地方，散着写迟早出现 "plan" 和 "Plan" 两种拼法。
MODE_DEFAULT = "default"
MODE_PLAN = "plan"
MODE_ACCEPT = "accept"
MODE_AUTO = "auto"


@dataclass(frozen=True)
class ModeSpec:
    """一档模式给人看的那部分（2026-11-12 第 33 轮新增）

    frozen=True：这张表是全局共享的，任何地方手滑改掉 label，
    底栏和 /mode 的显示会在运行中悄悄变样，而且几乎没法排查。冻结之后
    想改只能改源码，意图明确。

    字段：
      name      ：内部标识，也是 classify() 认的那个值。纯小写 ASCII。
      label     ：底栏和 /mode 列表里显示的中文名，控制在 4 个字以内，
                  太长了底栏会被挤到换行。
      summary   ：一句话说明，只给 /mode 列表用。
      status    ：切模式时那行状态里的极短说明（2026-11-12 第 33.2 轮新增）。
                  为什么不复用 summary：summary 是"给没试过的人解释这一档"，
                  写在列表里可以长一点；status 是"给已经在用的人确认现在在哪"，
                  每一帧都会出现在输入行上方，必须短。两者受众不同。
      model_hint：注入给模型的说明（走 reminder，不进系统提示词）。
                  写"能做什么/不能做什么"，不写"你现在很谨慎"这种空话——
                  模型需要的是可执行的约束，不是角色扮演。
    """

    name: str
    label: str
    summary: str
    status: str
    model_hint: str


# 四档的定义。顺序即 Shift+Tab 的循环顺序，也是 /mode 列表的展示顺序。
# 改顺序只需改这一个元组，cycle_mode 会自动跟着变（它读的就是这里）。
MODE_SPECS = (
    ModeSpec(
        name=MODE_DEFAULT,
        label="默认模式",
        summary="每个改动都要你确认（项目原来的行为）",
        status="每步都要确认",
        model_hint=(
            "当前是默认模式：写文件、执行命令之前系统会问用户，"
            "被拒绝时按用户的说明换个做法，不要反复重试同一次调用。"
        ),
    ),
    ModeSpec(
        name=MODE_PLAN,
        label="计划模式",
        summary="只能看不能写，用来调研并产出方案",
        status="只读，用来出方案",
        model_hint=(
            "当前是计划模式（plan）：你只能读取和查询，不能修改任何文件，"
            "也不能执行会写盘或改变仓库状态的命令。"
            "写工具（file_write / file_edit）和执行写操作的命令会被系统拦截；"
            "只读命令（查看、搜索、git log/diff/status、node --check 这类语法检查）可以直接跑。"
            "工具你都能看到、但现在用不了——方案里请直接写清「用 file_edit 改 foo.ts:42 这一段」，"
            "不要因为不能用就绕开工具名。"
            "任务是把现状摸清并给出一份能照着做的方案：改哪些文件、为什么改、"
            "影响面和风险、分几步做。即使用户催你动手，也先把方案交出来。"
        ),
    ),
    ModeSpec(
        name=MODE_ACCEPT,
        label="接受编辑",
        summary="改文件不用确认，执行命令仍要确认",
        status="改文件免确认，命令仍要问",
        model_hint=(
            "当前是接受编辑模式：file_write / file_edit 会直接执行、不再询问，"
            "但执行命令仍然要用户确认。改文件前仍要先用 file_read 读过目标文件，"
            "不要因为免确认就跳过读这一步。"
        ),
    ),
    ModeSpec(
        name=MODE_AUTO,
        label="自动模式",
        summary="不再询问，所有工具直接执行（危险）",
        status="不再询问，全部直接执行",
        model_hint=(
            "当前是自动模式：所有工具调用都会直接执行，不会再有人替你确认。"
            "正因为没有人拦，动手前自己先确认三件事：目标路径对不对、"
            "这条命令会不会删掉不该删的东西、改动能不能回滚。"
            "不确定就先用只读工具核实，不要靠猜。"
        ),
    ),
)

# 名字 → 定义。查询是最高频操作（每轮拼 reminder、每次画底栏），用字典 O(1)。
MODE_MAP = {spec.name: spec for spec in MODE_SPECS}


# --- 别名 ---------------------------------------------------------------------
# 为什么需要别名：用户会照着 Claude Code 的写法敲 /mode acceptEdits，
# 也会有人敲中文。这些都应该落到同一档，而不是报"未知模式"。
# 只做**输入归一**，不反过来输出别名——底栏永远显示规范名。
_ALIASES = {
    "default": MODE_DEFAULT,
    "manual": MODE_DEFAULT,  # Claude Code 里这一档也叫 manual
    "手动": MODE_DEFAULT,
    "默认": MODE_DEFAULT,
    "plan": MODE_PLAN,
    "计划": MODE_PLAN,
    "accept": MODE_ACCEPT,
    "accepts": MODE_ACCEPT,
    "acceptedits": MODE_ACCEPT,  # 去掉连字符/下划线后的小写形式
    "accept-edits": MODE_ACCEPT,
    "accept_edits": MODE_ACCEPT,
    "edits": MODE_ACCEPT,
    "接受编辑": MODE_ACCEPT,
    "auto": MODE_AUTO,
    "automatic": MODE_AUTO,
    "bypass": MODE_AUTO,  # Claude Code 的 bypassPermissions 就是这一档
    "bypasspermissions": MODE_AUTO,
    "自动": MODE_AUTO,
}


def normalize_mode(raw) -> Optional[str]:
    """把用户/配置里写的一串文字归一成规范模式名（2026-11-12 第 33 轮新增）

    有效返回四个常量之一，无效返回 None。

    为什么无效返回 None 而不是抛异常、也不是"猜一个最像的"：
      这个函数唯一的调用方是 /mode 命令和模式状态设置。用户敲错模式名时，
      正确反应是"告诉他有哪些可选"，而不是让程序崩，更不是悄悄切成默认——
      后者会让用户以为切换成功了，然后在下一次写文件时才发现模式没变。
      返回 None 把"要不要提示"的决定权留给调用方。
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    return _ALIASES.get(text)


def get_spec(mode: str) -> ModeSpec:
    """取某一档的定义；未知模式退回默认档（2026-11-12 第 33 轮新增）

    为什么未知值不抛异常：这个函数在画底栏、拼 reminder 时都会被调用，
    而那些地方都在"每轮对话"的热路径上。一个拼错的状态值不该让整轮对话崩掉，
    退到默认档最多是显示不准，用户还能继续用。
    （写入侧（normalize_mode）是严格的，两边分工不同。）
    """
    return MODE_MAP.get(mode, MODE_MAP[MODE_DEFAULT])


def cycle_mode(current: str) -> str:
    """按 MODE_SPECS 的顺序取下一档（2026-11-12 第 33 轮新增）

    未知的 current 当作默认档，于是下一次切换从"计划"开始——
    这比随机跳一档好解释，也不会让用户卡在未知状态里。
    """
    names = [spec.name for spec in MODE_SPECS]
    if current not in names:
        return names[1]  # 未知值 → 下一档（names[0] 是 default，所以取 [1]）
    return names[(names.index(current) + 1) % len(names)]


# 切换模式时输入行上方那条状态的图标（2026-11-12 第 33.2 轮新增）。
# 四档共用一个字形：区分度交给颜色和文字，字形只负责"这是一条状态"。
# 为什么用 ▸▸（U+25B8，几何图形区）而不是 ⏵⏵ / ⏸：后者落在 U+23xx 区，
# conhost 会把它交给 emoji 字体渲染成彩色方块，rich 也管不了颜色——
# 第 10 轮在 ⏺ 上踩过一模一样的坑（见 theme.py 文件头）。
MODE_ICON = "▸▸"


def status_line(mode: str) -> str:
    """切模式时显示的一行状态（2026-11-12 第 33.2 轮新增）

    形如 `▸▸ 计划模式 · 只读，用来出方案`。

    为什么做得这么短：这一行会原地刷新在输入行上方，每按一次 Shift+Tab 就换一次。
    第 33 轮第一版打的是 `已进入计划模式 · 只能看不能写，用来调研并产出方案`，
    连按三下就是三行长度相近的日志，看着像刷屏——问题不在颜色，在于把
    "状态变化"当成了"事件日志"。**状态可以覆盖，日志才要追加**，
    这一行按前者设计。
    """
    spec = get_spec(mode)
    return f"{MODE_ICON} {spec.label} · {spec.status}"


def mode_reminder(mode: str) -> str:
    """拼一段给模型看的"当前模式"说明（2026-11-12 第 33 轮新增）

    为什么走 reminder 而不是系统提示词：见文件头【为什么当前模式不写进系统提示词】。
    这段每轮都会附在最后一条工具结果后面，位置在上下文尾部，
    模型对最近内容的注意力最强，正好适合放"这一轮按什么规矩来"。
    """
    spec = get_spec(mode)
    return (
        "<mode>\n"
        f"## 当前工作模式：{spec.label}（{spec.name}）\n"
        f"{spec.model_hint}\n"
        "</mode>"
    )


# --- 系统提示词里的静态说明 ---------------------------------------------------
# 这一段是**固定文本**，任何时候都不变，所以可以安全地进系统提示词而不破坏缓存。
# 它只回答"有几种模式、分别是什么"，不回答"现在是哪一种"——后者由 reminder 给。
#
# 为什么系统提示词里还要说一遍：模型看不到 classify() 的代码，也不知道
# "工具清单被剪过"意味着什么。只说"写工具不在清单里"不够，还要告诉它
# "这不是 bug，是当前模式"——否则它会以为工具没注册，去报告一个不存在的问题。
MODE_SECTION = """## 工作模式

这个工具支持四种工作模式，当前是哪一种会在每轮末尾的 <mode> 提醒里给出：
- 默认（default）：写文件、执行命令前都会询问用户
- 计划（plan）：只能读取和查询，产出方案，不做任何修改
- 接受编辑（accept）：改文件不再询问，执行命令仍要询问
- 自动（auto）：不再询问，所有工具直接执行

按当前模式行事。计划模式下写工具会被拒绝（它们仍在工具清单里，你能看到），
这是模式限制而不是环境缺工具：不要反复尝试、也不要用命令绕过，
直接把方案交给用户，等他切换模式。""".strip()
