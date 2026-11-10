# =============================================================================
# 文件：src/tools/skill/frontmatter.py
# 作用：解析 SKILL.md 头部那几行 `key: value`（第 21 轮新增）。
#
# 【为什么自己写，不引 pyyaml】
#   THINKER.md 里有一条硬约定：不引入新依赖。而 pyyaml 不是标准库。
#   更实际的理由是——技能头部用到的 YAML 只有三种写法：
#
#     name: pdf                     → 字符串
#     allowed-tools: [a, b]         → 内联列表
#     allowed-tools:                → 块列表
#       - a
#       - b
#
#   为这三行引一个八千行的库，还要在启动时多一次 import，不划算。
#   写一个 100 多行的解析器，顺便能把"YAML 为什么不好解析"这件事讲清楚，
#   这比多一个依赖更符合本项目的教学定位。
#
# 【明确不支持的写法（遇到会留一条 problem，不会崩）】
#   · 多行字符串（`|` / `>`）：技能的 description 就该是一句话，长描述进的是
#     每轮请求都发的工具说明书，本来就要压到 300 字符（见 core/limits.py）；
#   · 嵌套 map（`metadata:` 下面再缩进一层）：本版不解释 metadata；
#   · 锚点 / 别名 / 行尾注释 / 引号里的 `: `：这些是 YAML 的坑，
#     一个"故意很笨"的解析器宁可说"我不认"，也不要猜错。
#
# 【一个必须照抄 Claude Code 的细节】
#   `---` 必须是文件第一行（允许开头有 UTF-8 BOM）。
#   很多人习惯先写一句 `# 标题` 再写 frontmatter，然后困惑为什么字段不生效——
#   这里不猜，整份文件当正文，并且由上层给一条警告（见 skill_spec.build_spec）。
# =============================================================================

from typing import Dict, List, Optional, Tuple

# UTF-8 文件开头可能有一个 BOM（字节序标记）。Windows 记事本另存为 UTF-8
# 时默认会加上它。不去掉的话，第一行会变成 "\ufeff---"，
# `== "---"` 判断失败，用户会看到"我的 frontmatter 明明写对了却不生效"。
BOM = "\ufeff"

# 分隔线。只认三根横线，不认 `***` / `___`（YAML 里它们是等价的分隔符，
# 但技能文件是给人手写的，多认两种写法只会让"到底哪行才算开头"更难解释）。
DELIMITER = "---"


def strip_bom(text: str) -> str:
    """去掉开头的 BOM（没有就原样返回）

    单独抽成函数，是因为"有没有 BOM"和"有没有 frontmatter"是两件事：
    上层想警告"你第一行不是 ---"的时候，需要先去掉 BOM 再判断，
    否则会把一个 BOM 引起的失败说成"你写错了"，用户会一头雾水。
    """
    return text[1:] if text.startswith(BOM) else text


def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    """把一份技能文件切成 (头部原文, 正文)

    返回：
      · 有 frontmatter → (两行 --- 之间的原文, 正文)。正文是**原样**返回的，
        连开头那个换行都不动——正文里可能有代码块，多删一个空行就可能
        改变 markdown 渲染结果。
      · 没有 frontmatter → (None, 全文)。注意：`---` 必须是**第一行**；
        文件开头先有一句 `# 标题` 的，一律按"没有头部"处理。

    边界（三种都不抛异常）：
      · 空文件 → (None, "")
      · 只有一行 `---` → (None, 全文)。没闭合就不知道该把哪儿算正文，
        宁可退化成"整份都是正文"，也不能猜一个位置然后截掉用户的内容。
      · 闭合线前后有空白（`  ---  `）→ 认。手写文件里太常见了，
        为此报错属于没事找事。
    """
    body = strip_bom(text)

    # keepends=True：保留每行末尾的换行符，最后用 "".join 原样拼回正文。
    #   为什么不用 splitlines() 的默认行为：那样会把 "\r\n" 也吃掉，
    #   拼回去时正文的换行风格就变了（Windows 用户写的 CRLF 会变成 LF），
    #   正文里的代码块一旦被改写，diff 会显示整段变动，排查时没法看。
    lines = body.splitlines(keepends=True)
    if not lines:
        return None, body

    if lines[0].strip() != DELIMITER:
        return None, body  # 第一行不是 ---，整份当正文

    # 从第二行开始找闭合线。注意从 1 开始：第 0 行是开头那条分隔线。
    for index in range(1, len(lines)):
        if lines[index].strip() != DELIMITER:
            continue
        raw = "".join(lines[1:index])  # 两条分隔线之间的原文
        rest = "".join(lines[index + 1:])  # 闭合线之后的正文
        return raw, rest

    return None, body  # 有开头没结尾：当没有 frontmatter 处理（上层会给警告）


def looks_like_frontmatter(text: str) -> bool:
    """文件是不是"看起来想写 frontmatter"（用来决定要不要给警告）

    为什么不直接用 split_frontmatter 的返回值判断：
      它把"没写"和"写坏了"都返回 None，上层没法区分。
      而这两种情况的提示完全不同——
        · 没写：正常，Claude Code 也允许没有头部的技能；
        · 写坏了（有开头没结尾）：必须警告，否则用户会以为字段生效了。
    """
    body = strip_bom(text).lstrip()
    return body.startswith(DELIMITER)


def parse_kv(raw: str) -> Tuple[Dict[str, object], List[str]]:
    """把头部原文解析成字典；解析不了的行收集成 problem，不抛异常

    参数 raw：split_frontmatter 返回的头部原文（不含两条 --- 本身）。

    返回 (fields, problems)：
      fields   —— 解析成功的键值。**能解析多少算多少**：
                  某一行写坏了，不该把同一份文件里写对的 name 也一起丢掉。
      problems —— 每条是一句中文说明（带行号），交给 /skills 展示。
                  为什么一定要留：第 16.1 轮的教训是"静默降级 = 把 bug 藏起来"，
                  解析器悄悄吞掉一行，用户会永远不知道为什么字段不生效。

    支持的行：
      key: value            标量（会去掉包裹的成对引号）
      key: [a, b]           内联列表
      key:
        - a
        - b                 块列表
      # 整行注释            跳过（行尾注释不支持，见文件头）
    """
    fields: Dict[str, object] = {}
    problems: List[str] = []

    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1  # 先加一，后面所有分支都用"当前 index"指向下一行
        #  ↑ 这个顺序有讲究：行号在提示里是给人看的（从 1 开始），
        #    而处理块列表时还要往后读若干行。先自增，循环内部统一用
        #    `lines[index]` 表示"下一行"，就不会出现"到底该 +1 还是 -1"的混乱；
        #    真写错了的表现是提示里的行号差一，虽然不影响功能，但会浪费排查时间。

        if not stripped or stripped.startswith("#"):
            continue  # 空行 / 整行注释

        if stripped.startswith("- "):
            # 没有归属键的列表项。常见于"块列表缩进写错"，
            # 直接忽略并留一条说明，比硬塞给上一个键安全。
            problems.append(f"第 {index} 行的列表项没有归属的键，已忽略：{stripped}")
            continue

        if ":" not in stripped:
            # 这一行既不是空行、不是注释、也不是列表项，还没有冒号——
            # 多半是手写时少打了个冒号，或者把说明文字写进了头部。
            # 记一条带行号的 problem：用户照着行号看过去，一眼就知道改哪儿。
            problems.append(f"第 {index} 行不是 key: value 形式，已忽略：{stripped}")
            continue

        key, _, value = stripped.partition(":")  # 只按第一个冒号切
        key = key.strip()
        value = value.strip()
        if not key:
            problems.append(f"第 {index} 行缺少键名，已忽略：{stripped}")
            continue

        if not value:
            # 值写在后面几行：块列表，或者（不支持的）嵌套 map。
            items: List[str] = []
            while index < len(lines):
                nxt = lines[index]
                nxt_stripped = nxt.strip()
                if not nxt_stripped:
                    index += 1  # 块中间的空行：跳过，不终止收集
                    continue
                if nxt_stripped.startswith("- "):
                    items.append(_scalar(nxt_stripped[2:]))
                    index += 1
                    continue
                if nxt[:1] in (" ", "\t"):
                    # 缩进但不是列表项 → 嵌套 map。本版不解释，吞掉并留说明。
                    problems.append(f"不支持嵌套字段（第 {index + 1} 行），已忽略：{nxt_stripped}")
                    index += 1
                    continue
                break  # 回到顶层，交给外层继续解析
            # 空块（写了 key: 下面什么都没写）存成空字符串，**不是**不存这个键。
            #   区别在于语义：空字符串 = "用户明确写了这个键、值是空"，
            #   缺键 = "用户没提这个字段"，上层回退默认值的逻辑要靠这个区别。
            #   比如 `description:` 下面空着，应该得到"有键无值"（技能没有描述），
            #   而不是悄悄变成"没写 description"（那在 /skills 的展示里是另一句话）。
            fields[key] = items if items else ""
            continue

        if value[0] in "|>":
            # 多行字符串。吞掉后面所有缩进行，免得它们的 `xxx:` 被当成顶层键。
            problems.append(f"不支持多行字符串（{key}: {value[0]}），已忽略这一段")
            while index < len(lines) and lines[index][:1] in (" ", "\t") and lines[index].strip():
                index += 1
            continue

        if value.startswith("["):
            parsed_list = _inline_list(value, key, problems, index)
            if parsed_list is not None:
                fields[key] = parsed_list
            continue

        fields[key] = _scalar(value)

    return fields, problems


def _scalar(value: str) -> str:
    """去掉两端成对的引号，返回纯文本

    只处理"整段被一对引号包住"的情况（`"abc"` / `'abc'`）。
    不做转义还原：技能头部里出现 `\\n` 这类转义的概率极低，
    而真去处理它就得顺带处理单引号里的双引号、反斜杠转义……
    这个解析器的目标是"够用且看得懂"，不是"复刻 YAML"。
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _inline_list(value: str, key: str, problems: List[str], line_no: int) -> Optional[List[str]]:
    """解析 `[a, b, c]`；括号没闭合时留一条 problem 并返回 None

    返回 None 而不是空列表，是为了让调用方"不设置这个键"——
    设置成空列表会让 `if spec.tools:` 之类的判断拿到"用户明确要求空"
    这个错误语义，而实际情况是"这一行没写对"。
    """
    if not value.endswith("]"):
        problems.append(f"{key} 的内联列表缺少右括号（第 {line_no} 行），已忽略")
        return None
    inner = value[1:-1].strip()
    if not inner:
        # `[]` 是合法写法：明确表示"空列表"。
        #   这里返回 [] 而不是 None，是因为它和"括号没闭合"是两回事——
        #   前者是用户写清楚了，后者是没写完。混成一个结果，上层就没法判断要不要告警。
        return []
    # 只按逗号切，不考虑"引号里的逗号"——技能头部不会出现这种值。
    return [_scalar(piece) for piece in inner.split(",") if _scalar(piece)]


def as_bool(value, default: bool = False) -> bool:
    """把 frontmatter 里的布尔值转成 True / False（认不出来就用默认值）

    认的写法（大小写不敏感）：true/false、yes/no、on/off、1/0，对齐 Claude Code。
    为什么要这么宽：用户是从 Claude Code 那边拷技能过来的，那边同一字段
    可能写成 `true`、也可能写成 `yes`。只认 `true` 会让人以为"这个开关没用"。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    # 认不出来（写了 "maybe"、或者写成了列表）就退回默认值，不报错。
    #   为什么这里可以静默：布尔字段都有安全的默认值（关闭 = 更保守），
    #   猜错的后果只是"一个开关没生效"，而报错会让整个技能加载失败——
    #   用一次加载失败去换一个开关，代价不对等。真正的"没生效"由 /skills 的
    #   字段标注兜底，用户不至于完全蒙在鼓里。
    return default
