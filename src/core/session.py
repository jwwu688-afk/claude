# =============================================================================
# 文件：src/core/session.py
# 作用：把每一轮消息写进磁盘，支持 `thinker --continue` 接着上次继续聊。
#
# 【一、为什么需要】
#   现在杀掉进程（或者电脑崩了、终端被关了），这次对话的全部上下文就没了。
#   用了半小时让它读代码、改文件、跑测试，一句 Ctrl+C 全清零——
#   这是"能不能真正拿来干活"的门槛，比任何锦上添花的功能都重要。
#
# 【二、格式：JSONL，一行一个对象】
#   第一行是 header，之后每行一条消息。选 JSONL 而不是"一个大 JSON"的理由：
#     · 追加写：新消息直接 append 一行，不用把整个文件读出来再写回去
#     · 崩溃安全：写到一半断电，前面的行还是完整的，最多丢最后一行
#     · 人工可读：出问题时能直接 tail 看最后几条
#
#   实际长这样（沿用之前实验留下的 v1 格式，老文件也能读）：
#     {"type":"header","v":1,"id":"20261106-221248-ee59","started_at":"...","cwd":"...","model":"spark-x2.6"}
#     {"type":"message","ts":1793974368.95,"message":{"role":"user","content":[...]}}
#     {"type":"message","ts":1793974370.12,"message":{"role":"assistant","content":[...]}}
#
# 【三、一次典型的时间线】
#   18:30:24  用户敲 thinker
#               → SessionStore.create() 建文件，只写一行 header
#   18:30:26  用户输入"帮我看看 main.py"
#               → Conversation.add_message() → store.append_message()
#               → 文件里多一行（此时磁盘上是 header + 1 条消息）
#   18:30:29  模型回复 → 文件里再多一行
#   18:30:31  模型要调 file_read → 消息 3 行
#             ...
#   18:31:40  用户按 Ctrl+C 直接关窗口
#               → 什么收尾代码都没跑，但磁盘上已经有完整的 12 行
#   第二天    thinker --continue
#               → list_sessions 找到这个文件 → load 读回 11 条消息
#               → 换成今天的系统提示词 → 接着聊
#
# 【四、什么时候写】
#   每条消息进历史的那一刻就写，不是等这一轮结束才写。
#   原因：崩溃往往发生在"正在跑工具"的时候，等轮次结束再落盘等于白存。
#   代价是每写一条都开关一次文件——一轮对话也就几十条，这个开销可以忽略。
#
# 【五、恢复时怎么处理 system 提示词】
#   不直接用存档里的 system。因为系统提示词里带着"当前日期""当前目录""项目记忆"，
#   隔天再开会变成过时信息。所以恢复时丢掉旧的 system，用新生成的替换。
#   user / assistant / tool 三类消息原样恢复——那才是对话本身。
# =============================================================================

import json  # 读写 JSONL：每行是一个 JSON 对象
import os  # 取当前工作目录，写进 header
import secrets  # 生成会话 id 的随机后缀，避免同一秒内开两个会话撞名
import sys  # 【第 14.2 轮】序列化失败时把警告写到 stderr，不打断正文显示
import time  # 记录每条消息的写入时刻（浮点秒，比字符串省事）
from datetime import datetime  # 生成可读的会话 id（年月日-时分秒）
from pathlib import Path  # 路径操作
from typing import Dict, List, Optional, Tuple  # 类型标注（兼容 Python 3.8，不能用 list[str] 写法）

# =============================================================================
# 一、常量
# =============================================================================

SESSIONS_DIR = Path("~/.thinker/sessions")
# 会话文件都放这里，和 Claude Code 的 ~/.claude/projects 一个思路：
# 集中在一个已知位置，方便清理，也不会污染任何项目目录。
#
# 为什么不像 Claude Code 那样按项目分子目录：
#   它按项目分会话是为了"一个项目一堆历史"，但我们已经有 header 里的 cwd 字段，
#   按 cwd 过滤同样能实现"只看本项目的会话"，没必要多一层目录。
#   平铺还有个好处：`ls ~/.thinker/sessions` 直接按时间排序，一眼看到全部历史。

FORMAT_VERSION = 1
# 格式版本。将来字段变了（比如加 token 统计、加分支信息），靠这个区分老文件：
#   读到 v=1 就用老逻辑解析，读到 v=2 再走新逻辑。
# 现在只有 v1——它是之前那次实验定的，本文件保持兼容，所以目录里那个
# 20261106 的孤儿文件可以直接读。

_ID_BYTES = 2
# id 随机后缀用几个字节 → 2 字节 = 4 个十六进制字符 = 65536 种可能。
# 为什么不是更多：同一秒内开两个会话的概率极低，4 位够用；
# 名字短一点，ls 的时候更容易看全。

# =============================================================================
# 二、id 与时间
# =============================================================================

def new_session_id() -> str:
    """生成会话 id：20261107-183024-a1b2 这种形式（2026-11-07 第 13 轮）

    为什么用"时间 + 随机"而不是纯 UUID：
      时间在前，目录里按名字排序就是按时间排序，人一眼能看出先后；
      加随机后缀是为了同一秒内开两个会话也不会撞名。
        （比如同时开两个终端各跑一个 thinker，时间戳可能完全相同）

    为什么不用纳秒级时间戳保证唯一：
      名字会变长到 20 多位，肉眼没法读。4 位随机更划算。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # %Y 四位年、%m 两位月、%d 两位日，中间用 - 分开读着舒服；
    # 时分秒也是两位，所以整个时间部分是定长的，排序不会错乱。
    return f"{stamp}-{secrets.token_hex(_ID_BYTES)}"
    # secrets 而不是 random：虽然这里不需要密码学强度，
    # 但 secrets 的实现保证在任何平台上都不会因为种子相同而产生重复序列，
    # 而且它本来就是标准库推荐的"生成不可预测随机"的方式，用它不会错。


def _now_iso() -> str:
    """当前时间的 ISO 字符串，精确到秒（2026-11-07 第 13 轮）

    为什么 timespec="seconds" 而不是默认的微秒：
      header 里的开始时间是给人看的（"这次对话是几点开始的"），
      微秒那一串 .123456 完全是噪音。
    为什么不带时区：
      本项目只在本机用，同机所有时间都是同一个时区，加 +08:00 反而更长。
      真要跨时区分析，ISO 格式本身也能被标准工具猜出来。
    """
    return datetime.now().isoformat(timespec="seconds")


# =============================================================================
# 三、核心：一个会话文件的读写句柄
# =============================================================================

class SessionStore:
    """一个会话文件的读写句柄（2026-11-07 第 13 轮）

    用起来是这样：
        store = SessionStore.create(cwd, model)   # 新建（写 header）
        store.append_message(msg)                 # 每加一条消息就调一次
        ...
        messages, header = SessionStore.load(store.path)   # 恢复时读回来

    为什么做成"类"而不是一堆函数：
      需要记住"当前在写哪个文件"这件事。用类把这个路径存在 self 上，
      调用方就不用每次都传一遍（传多了迟早传错）。
    """

    def __init__(self, path: Path):
        # 参数是"要读写的那个 jsonl 文件"。
        # 注意这里**不检查文件是否存在**——恢复场景下它当然存在；
        # 万一不存在，append 会在 _write_line 里被 OSError 兜住，不会崩。
        self.path = Path(path)  # 统一转成 Path，调用方传字符串也能用

    # -------------------------------------------------------------------------
    # 3.1 创建与写入
    # -------------------------------------------------------------------------
    @classmethod
    def create(cls, cwd: Optional[str] = None, model: str = "", messages: Optional[List[dict]] = None) -> "SessionStore":
        """开一个新会话文件，写好 header（2026-11-07 第 13 轮）

        参数：
          cwd      项目目录，写进 header，--continue 时靠它找"本项目的会话"
          model    模型名，纯记录用途（排查问题时能看出当时用的哪个模型）
          messages 已经存在的消息。一般新建时传 None；
                   如果调用方想把一批旧消息归档进新文件，可以传进来。

        返回：一个指向新文件的 SessionStore。
        """
        directory = SESSIONS_DIR.expanduser()
        # expanduser() 把开头的 ~ 换成真实家目录（C:\Users\23889）。
        # 不展开的话 Path("~/x").exists() 永远 False——~ 在文件系统里不是魔法字符。
        directory.mkdir(parents=True, exist_ok=True)
        # parents=True：中间层不存在也一起建（全新机器上 ~/.thinker 可能还没有）
        # exist_ok=True：目录已存在不算错（绝大多数情况下它确实已经存在）

        session_id = new_session_id()
        store = cls(directory / f"{session_id}.jsonl")
        # 文件名就是 id，两者保持一致——排查时看到 header 里的 id，
        # 直接就能在目录里定位到对应文件。

        store._write_line({
            "type": "header",  # 行类型：头信息（整个文件只有这一行是 header）
            "v": FORMAT_VERSION,  # 格式版本，将来兼容老文件用
            "id": session_id,  # 会话 id，和文件名一致
            "started_at": _now_iso(),  # 开始时间，给人看的
            "cwd": str(cwd or os.getcwd()),  # 项目目录，--continue 按这个过滤
            "model": model,  # 当时用的模型
        })

        for message in (messages or []):
            store.append_message(message)
            # 有历史消息的话按顺序补写进去。
            # 用 append_message 而不是直接拼字符串，是为了走同一条写入路径，
            # 时间戳、格式处理完全一致。

        return store

    def append_message(self, message: dict) -> None:
        """追加一条消息（2026-11-07 第 13 轮）

        注意：这里**不做任何过滤**，system / user / assistant / tool 全写。
        恢复时再决定哪些要、哪些丢（system 会被替换掉）。

        为什么"存"和"用"要分开：
          存档的职责是"当时发生了什么就记什么"，越忠实越好；
          恢复的职责是"哪些还有用"，那是业务判断。
          两个职责混在写入端，会导致"存档看起来完整，但其实已经悄悄丢过东西"——
          出问题时根本没法回溯当时到底给模型喂了什么。
        """
        self._write_line({
            "type": "message",
            "ts": time.time(),  # 写入时刻（浮点秒）。注意是"写的时候"而不是"说的时候"，
                                # 两者通常只差几毫秒，不值得为此多传一个参数
            "message": message,  # 完整消息对象，原样存
        })

    def count_messages(self) -> int:
        """这个文件里已经写了多少条 message 行（2026-11-13 第 34 轮新增）

        谁在用：压缩落盘时要记"保留区从文件第几条开始"（keep_from），
        这个坐标必须基于**文件里已有的条数**，不能基于内存里的条数——
        resume 之后内存会多一条新生成的 system，两边永远差一条。

        为什么现读文件而不是维护一个计数器：
          SessionStore 是"打开就写"的薄封装，resume 之后重新
          `SessionStore(path)` 时计数器从 0 开始，和文件实际条数对不上。
          要在构造函数里数一遍就得每次 resume 多读一次文件，不如需要时再读。
          压缩是低频操作（一个会话几次），读一次 1~2MB 完全可以接受。

        坏行跳过，与 load() 的口径一致：数少了只会让 keep_from 偏小
        （保留区多留几条），是安全方向；数错了宁可多留不可少留。
        """
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        total = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("type") == "message":
                    total += 1
            except json.JSONDecodeError:
                continue
        return total

    def append_compaction(self, keep_from: int, summary: str,
                          before: int = 0, after: int = 0) -> None:
        """记一条"这里发生过压缩"（2026-11-13 第 34 轮新增）

        【为什么需要这条记录】
          压缩只改内存里的 messages_history，文件是 append-only、从不重写。
          后果是：resume 永远读到压缩前的完整历史 → 估算必然超高 → 每次
          resume 都要重新压一次、重新花一次摘要调用，而且反复把同一段历史
          摘要来摘要去。
          有了这条记录，resume 就能重建"压缩后的视图"，与内存保持一致。

        参数：
          keep_from  压缩时，保留区在**文件**里的起始条数（0 基，含 system 行）。
                     注意是文件坐标不是内存坐标：resume 时内存会多一条新生成的
                     system，两者永远差一条，用内存坐标会整体错位。
          summary    这次生成的摘要正文（不含公告抬头）。
          before/after 压缩前后的条数，纯记录用，排查时能看出压了多少。

        为什么单独一种行类型而不是复用 message：
          它描述的是"如何解释前面的消息"，不是对话的一部分。
          load() 里 type 不认识就跳过，所以老版本读到新文件不会崩，
          新版本读到老文件（没有这种行）也只是"没有压缩记录"，行为不变。
        """
        self._write_line({
            "type": "compaction",
            "ts": time.time(),
            "keep_from": int(keep_from),
            "summary": summary or "",
            "before": int(before),
            "after": int(after),
        })

    def _write_line(self, obj: dict) -> None:
        """写一行 JSON 到文件末尾（2026-11-07 第 13 轮）

        为什么每次 open/close 而不是一直持有文件句柄：
          · Windows 上长期占着文件会让"用别的程序打开看"失败（文件锁）
          · 进程被强杀时，缓冲区里的内容会丢；每次写完就关保证真的落盘
          代价是每轮多几十次系统调用，相对于网络请求的耗时完全可以忽略。

        ensure_ascii=False：让中文以原样存进去，不要变成 \\u4f60\\u597d。
        否则出问题时 tail 一眼看过去全是转义码，没法排查。

        为什么吞掉 OSError：
          磁盘满、无权限、路径失效……会话记录失败不该打断正在进行的对话。
          用户最多丢了这次存档，但当前对话还能继续——这个取舍明显更划算。
        """
        try:
            with self.path.open("a", encoding="utf-8") as f:
                # "a" 追加模式：文件不存在就创建，存在就写到末尾。
                # encoding 必须显式指定：Windows 默认 GBK，中文会写坏。
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                # 一行一个对象，末尾必须有换行——否则下一条会和它粘成一行，
                # 下次读的时候整行都解析失败。
        except OSError:
            # 静默跳过。这里**故意不打印警告**：
            # 存档失败往往是因为磁盘满，此时屏幕上再刷一行错误，
            # 用户既没法处理，又会被打断注意力。
            pass
        except TypeError as e:
            # 【2026-11-08 第 14.2 轮新增】消息里有 json 不认识的类型。
            #   真实踩到的例子：assistant 消息的 tool_calls 是 openai SDK 的
            #   pydantic 对象，json.dumps 直接抛 TypeError，而当时这里只捕 OSError，
            #   于是异常一路冒到 _run_one_iteration，把**整轮对话打断**——
            #   模型已经答完、工具还没执行，用户看到的是半截回答加一段红字。
            #   根因已经在 Conversation.add_message 里用 normalize_message 修掉了，
            #   这里加一道兜底：万一将来又有什么字段没规范化，宁可丢这一条存档，
            #   也不能让正在进行的对话崩掉。
            #
            #   为什么只跳过、不写个"残缺版"：
            #     assistant(tool_calls) 和后面的 tool 结果是成对的，写一半出去
            #     会让恢复时 400（role=tool 找不到对应的 tool_calls）。
            #     少一条消息只是"恢复时少一件事"，写坏配对是"恢复后直接不能用"。
            #
            #   为什么这条**要**打印而 OSError 不打印：
            #     OSError 是环境问题（磁盘满），用户自己知道该怎么办；
            #     这条是代码缺陷，不说出来就永远没人修。写 stderr 是为了不打断
            #     rich 正在画的正文区域。
            print(f"[会话存档] 这条消息没能写入（{type(e).__name__}: {e}）。"
                  f"当前对话继续，但 --continue 会缺少这一条。", file=sys.stderr)

    # -------------------------------------------------------------------------
    # 3.2 读取
    # -------------------------------------------------------------------------
    @classmethod
    def load(cls, path) -> Tuple[List[dict], dict]:
        """读一个会话文件，返回 (消息列表, header)（2026-11-07 第 13 轮）

        坏行直接跳过，不抛异常。理由：
          会话文件是"尽力保存"的产物，可能因为断电留下半行。
          为了半行坏数据让整个恢复失败，不如丢掉那一行继续。
          反正最多丢一条消息，比"完全打不开"好得多。

        返回的是元组而不是只返回消息：
          调用方经常需要 header 里的 cwd 来判断"这是不是本项目的会话"，
          多返回一个字段比让它自己再读一遍文件划算。
        """
        # 【2026-11-13 第 34 轮】解析逻辑抽到模块级 _read_session()：
        #   resume 还需要同一文件里的"压缩记录"，分两次读 1~2MB 不划算。
        #   这里只要前两项，行为与以前完全一致。
        messages, header, _ = _read_session(path)
        return messages, header

    # -------------------------------------------------------------------------
    # 3.3 找历史会话
    # -------------------------------------------------------------------------
    @classmethod
    def list_sessions(cls, cwd: Optional[str] = None) -> List[Path]:
        """列出会话文件，新的在前（2026-11-07 第 13 轮）

        参数 cwd 给了就只返回这个目录下的会话——在不同项目里
        `--continue` 应该接着本项目的上一次，而不是另一个项目的话题。
        """
        directory = SESSIONS_DIR.expanduser()
        if not directory.is_dir():
            return []  # 目录都还没有，说明从没开过会话

        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.name, reverse=True)
        # 只认 .jsonl，其他文件（用户手动放的说明文档之类）不算
        # 按**文件名**排序而不是 mtime：
        #   文件名以时间开头，天然有序；而 mtime 在复制文件、从备份恢复时会被重置，
        #   会导致"老会话排到最前面"，--continue 就接到错的地方去了。
        # reverse=True：新的在前。

        if cwd is None:
            return files  # 不筛项目，全都返回

        want = str(Path(cwd).resolve()).lower()
        # 两边都转小写比较：Windows 路径大小写不敏感，
        # D:\Code\Thinker 和 d:\code\thinker 应该被当成同一个项目。
        matched = []
        for path in files:
            _, header = cls.load(path)
            if str(header.get("cwd", "")).lower() == want:
                matched.append(path)
        return matched
        # 说明：这里每个文件都要读一遍才能拿到 header，文件多时会慢。
        # 但会话文件一般只有几十个、每个也就几十 KB，实测无感。
        # 真要优化，可以把 cwd 编进文件名，但那样名字会变得很难看，不值得。

    @classmethod
    def latest(cls, cwd: Optional[str] = None) -> Optional[Path]:
        """最近一次会话的文件（2026-11-07 第 13 轮）

        没找到返回 None——调用方据此决定是"提示没有历史"还是"直接恢复"。
        不抛异常，因为"第一次用"是完全正常的状态。
        """
        found = cls.list_sessions(cwd)
        return found[0] if found else None


# =============================================================================
# 四、恢复：把存档变成"能直接用"的消息列表
# =============================================================================

def _read_session(path) -> Tuple[List[dict], dict, dict]:
    """读会话文件，返回 (消息列表, header, 最后一条压缩记录)（2026-11-13 第 34 轮新增）

    【为什么"压缩记录"要和消息一起读出来】
      压缩只改内存里的 messages_history，文件是 append-only、从不重写。
      所以文件里必须留一条"这里压缩过"的痕迹，否则 resume 每次都读到压缩前的
      完整历史：估算必然超高 → 每次都重新压一遍 → 每次都真花一次摘要调用，
      而且反复把同一段历史摘要来摘要去（第二次开始就是在摘要"上一次的摘要"）。

    三个东西同一次读盘返回，是因为 resume 两条路都要用：
      · 有压缩记录 → 用消息 + 记录重建"压缩后的视图"；
      · 没有 → 只返回消息，行为和以前完全一样（老会话文件天然走这条路）。

    坏行直接跳过、不抛异常：会话文件是"尽力保存"的产物，可能因为断电留下半行，
    为了半行坏数据让整个恢复失败不值得。
    """
    messages: List[dict] = []
    header: Dict = {}
    compaction: Optional[dict] = None
    target = Path(path)

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return messages, header, compaction

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = obj.get("type")
        if kind == "header":
            header = obj
        elif kind == "message" and isinstance(obj.get("message"), dict):
            messages.append(obj["message"])
        elif kind == "compaction":
            # 后来覆盖前面：多次压缩只认最后一条。
            # 它描述的保留区已经把之前几次压缩的结果包含在内了，
            # 用更早的记录重建会退回到"压缩到一半"的状态。
            compaction = obj

    return messages, header, compaction


def load_messages_for_resume(path, keep_system: bool = False) -> List[dict]:
    """读会话文件并整理成"可以直接塞进历史"的消息列表（2026-11-07 第 13 轮）

    默认丢掉旧的 system 消息。为什么：
      系统提示词里带着当前日期、当前目录、项目记忆、可用工具清单。
      昨天存的那份今天已经不准了，用它等于给模型灌过期信息
      （最典型的症状：它张口就说"今天是 11 月 6 日"，而你明明在 7 号问它）。

      所以调用方会重新生成一份新的 system 放在最前面，
      这里只负责把"对话本身"（user / assistant / tool）捞出来。

    keep_system=True 只在排查问题时用：
      想看看当时到底给模型喂了什么提示词，这是唯一的入口。

    为什么不在这里顺便做裁剪/压缩：
      恢复是"忠实还原"，压缩是"按当前上下文窗口做取舍"，
      两件事发生在不同的时间点，混在一起会让行为难以预测。
      压缩交给 HistoryManager 在下一轮请求前自己判断。

    但有一件事必须在这里做（第 26 轮）：**应用已有的压缩记录**。
      那不是"现在做一次压缩"，而是"把上次已经压过的结果还原出来"——
      和"忠实还原"是同一件事，只是还原的是压缩后的那份视图。
      不做的话，resume 每次都退回压缩前，于是每次都重压、每次都花钱。
    """
    messages, _header, compaction = _read_session(path)

    # 排查模式：原样返回，不做任何重建——这时要看的正是"当时到底存了什么"
    if keep_system:
        return messages

    # 【2026-11-13 第 34 轮】有压缩记录就先重建"压缩后的视图"。
    #   不重建的话，resume 拿回的是压缩前的完整历史，账本一估就超标，
    #   于是每次 resume 都要重新压一次、重新花一次钱。
    if compaction:
        rebuilt = _apply_compaction(messages, compaction)
        if rebuilt is not None:
            return rebuilt

    return [m for m in messages if m.get("role") != "system"]
    # 用列表推导而不是在 load 里跳过：load 保持"忠实读取"的职责，
    # 筛不筛由调用方决定


def inspect_session(path) -> dict:
    """列表展示用：一次读盘，返回"原始"和"有效"两份消息（2026-11-13 第 34 轮新增）

    为什么需要两份：
      · raw       —— 文件里原样的消息。用于取预览（第一条用户说了什么）。
                     压缩后恢复出来的第一条是"历史已压缩"的公告，拿它做预览
                     会显示成"[为节省上下文空间…"，完全看不出这个会话是干嘛的。
      · effective —— 应用压缩记录后的消息。用于显示"这条会话现在有多大"。
                     用户看到列表写 803 条、选中后却只恢复 12 条，第一反应是
                     "压缩是不是没生效/数据丢了"——两处口径必须一致。

    为什么不让调用方自己调两个函数：
      列表一次要列 10 个会话，各读两遍 1~2MB 的文件，加起来就是几十 MB。
      这里同一次读盘把两份都算出来，代价只有一次解析。

    返回 dict 而不是元组：四个字段（raw / effective / header / compacted），
      用元组的话调用方得记住顺序，可读性太差。
    """
    messages, header, compaction = _read_session(path)

    compacted = False
    effective = None
    if compaction:
        rebuilt = _apply_compaction(messages, compaction)
        if rebuilt is not None:
            effective = rebuilt
            compacted = True
    if effective is None:
        # 没有记录、或记录不可用：行为和以前一致（去掉 system 就是有效消息）
        effective = [m for m in messages if m.get("role") != "system"]

    # 四个字段各对应列表里的一项展示，缺一个调用方就得再读一次盘：
    #   raw 取预览、effective 显示条数、header 显示时间、compacted 决定加不加"（压缩后）"。
    # 为什么一个都不能省：
    #   只给 effective → 预览会变成"[为节省上下文空间…"，看不出会话主题；
    #   只给 raw → 条数又回到 805，和"已恢复 14 条"打架，用户以为压缩没生效。
    return {
        "raw": messages,         # 文件原文：只用来取预览（第一条用户到底说了什么）
        "effective": effective,  # 压缩后实际会恢复的那份：条数必须与"已恢复 N 条"一致
        "header": header,        # 会话元信息（开始时间、cwd、模型名）
        "compacted": compacted,  # 是否应用过压缩记录 → 列表里标"（压缩后）"
    }


def _apply_compaction(messages: List[dict], record: dict) -> Optional[List[dict]]:
    """按压缩记录重建"压缩后的视图"（2026-11-13 第 34 轮新增）

    重建规则：公告（含摘要）+ 文件里 keep_from 之后的那些消息。
    新的 system 由调用方（Conversation）重新生成后插在最前面，这里不管。

    参数 keep_from 是**文件坐标**：压缩那一刻文件里已经有几条 message 行。
    它之后的消息就是"压缩后新聊的内容"，必须原样保留——否则压缩过的那次
    会话一旦 resume，后面的对话就全丢了。

    返回 None 表示这条记录不可用（字段缺失、类型不对、坐标是负数）。
    调用方会退回"返回完整历史"：宁可让用户多压一次，也不能因为一条坏记录
    把整段对话弄丢。这是所有"尽力恢复"路径的共同取舍。

    公告文案必须和 HistoryManager._create_compression_notice() 逐字一致：
    那边靠 `[为节省上下文空间` 这个前缀识别"这是上次压缩留下的"，
    前缀对不上，防套娃判断就失效（症状是"压了没效果"，不报错）。
    """
    try:
        keep_from = int(record.get("keep_from", -1))
    except (TypeError, ValueError):
        return None
    if keep_from < 0:
        return None

    summary = record.get("summary") or ""
    kept = messages[keep_from:] if keep_from < len(messages) else []
    # 压缩时 system 是单独拎出去的，保留区理论上不含 system；
    # 但手工改过的文件可能有，过滤掉——否则会出现两个 system 抢规则。
    kept = [m for m in kept if isinstance(m, dict) and m.get("role") != "system"]

    notice_text = "[为节省上下文空间，之前的对话已被压缩。以下是被压缩部分的摘要：]\n" + summary
    return [{"role": "user", "content": [{"type": "text", "text": notice_text}]}] + kept
