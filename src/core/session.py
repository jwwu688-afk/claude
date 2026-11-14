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

import gzip  # 【第 35 轮】会话归档：老会话压成 .jsonl.gz，读的时候要能解开
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
        # 【第 35 轮】改走 _read_text：会话被归档（.jsonl.gz）之后也可能走到这里
        #   （比如统计、或者压缩落盘前恰好 resume 了归档会话）。
        #   统一一处读法，不在这里重复一遍"按后缀分支"的判断。
        text = _read_text(self.path)
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

        【第 35 轮】归档文件（.jsonl.gz）在这里直接拒写。
          往 gzip 里 open("a") 追加普通文本，写出来的是一个从中间开始就坏掉
          的文件——连前半段都解不出来，等于把整份历史毁了。
          正常路径不会走到这（open_archive 会先解压），但 resume 那一步
          万一解压失败（磁盘满），path 就还是 .gz；这时宁可丢这几条新存档，
          也不能把旧历史一起弄坏。打一行 stderr 是因为这是异常路径，
          静默的话用户永远不知道自己的对话没被记住。
        """
        if _is_archive(self.path):
            print(
                f"[会话存档] {self.path.name} 是归档文件，本轮消息没有写入。"
                f"（归档不支持追加；重新打开该会话时会自动解压）",
                file=sys.stderr,
            )
            return
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

        # 【第 35 轮】改走 _session_files：它同时认 .jsonl 和 .jsonl.gz，
        #   并且已经按文件名倒序排好（理由见那边的注释）。
        #   为什么归档也要出现在列表里：归档≠删除。用户 /resume 时应该看到
        #   "这个会话还在，只是被压起来了"，选中它会自动解压回来
        #   （见 open_archive），而不是以为记录丢了。
        #   目录不存在时返回空列表，和原来一致。
        files = _session_files(directory)
        if not files:
            return []  # 从没开过会话，或者目录被清空了

        if cwd is None:
            return files  # 不筛项目，全都返回

        want = str(Path(cwd).resolve()).lower()
        # 两边都转小写比较：Windows 路径大小写不敏感，
        # D:\Code\Thinker 和 d:\code\thinker 应该被当成同一个项目。
        matched = []
        for path in files:
            # 【第 36 轮】改用 read_header：只读第一行，不再为了拿 cwd
            #   把整份会话读进来。改前这里调 cls.load()，一个 1.5 MB 的会话
            #   就为开头一百来字节整份读一遍；归档更贵，还要整份解压。
            #   header 就是第一行，读一行的代价与文件大小无关。
            header = read_header(path)
            if str(header.get("cwd", "")).lower() == want:
                matched.append(path)
        return matched
        # 说明：读一行虽然便宜，但仍是"每个文件一次"，文件极多时
        # （上千个）会有几十毫秒开销。真要再快，得把 cwd 编进文件名——
        # 那样名字会变得很难看，不值得。

    @classmethod
    def latest(cls, cwd: Optional[str] = None) -> Optional[Path]:
        """最近一次会话的文件（2026-11-07 第 13 轮）

        没找到返回 None——调用方据此决定是"提示没有历史"还是"直接恢复"。
        不抛异常，因为"第一次用"是完全正常的状态。
        """
        found = cls.list_sessions(cwd)
        return found[0] if found else None

    # -------------------------------------------------------------------------
    # 3.4 清理与归档（第 35 轮）
    #
    # 为什么"清理"要做成"先压缩、真要删得再点一次"：
    #   会话记录是这个工具唯一的外部记忆。用户过两个月想翻回来看
    #   "上次那个报错是怎么解决的"，靠的就是这些文件。
    #   JSONL 是纯文本，gzip 之后通常只剩 8%~15%——用这点空间换"一条不丢"很划算。
    #   删除能力当然要有（有些人就是不想留），但要用户明确选、且二次确认。
    # -------------------------------------------------------------------------
    @classmethod
    def usage_report(cls, days: int = 30) -> dict:
        """统计会话目录占了多少、有几个算"旧"（第 35 轮新增）

        【为什么先统计再动刀】
          清理是不可逆的（删除尤其）。用户得先看到"总共多大、要动哪几个、
          大概能省多少"，而不是敲一条命令然后少一批文件。这个函数只读。

        【"旧"按 mtime 判，不按文件名里的时间】
          文件名是会话**创建**时间，mtime 是最后一次写入。按创建时间算的话，
          一个开了三天还在用的会话会被判成"三天前的旧会话"压起来——那恰好
          是用户最舍不得的那份。会话是越聊越长的东西，最后活动时间才代表
          还有没有人用。
          代价：复制文件、从备份恢复会重置 mtime，让老会话看起来是新的。
            对清理来说这个方向的错误是安全的（少清一个），可以接受。

        返回字典而不是元组：六个字段，用元组调用方记不住顺序。
        """
        directory = SESSIONS_DIR.expanduser()
        files = _session_files(directory)
        cutoff = time.time() - days * 86400  # 从「当前时刻」往前推 days 天

        total_bytes = 0
        archived_count = 0
        old_paths: List[Path] = []
        old_bytes = 0

        for path in files:
            try:
                stat = path.stat()  # 获取该路径对应文件/目录的元数据（状态信息）
            except OSError:
                continue  # 刚被别的进程删了、或权限不对：跳过这一个，不影响其余统计
            total_bytes += stat.st_size

            if _is_archive(path):
                archived_count += 1
                continue  # 已经压过了：既不用再压，也不算"待清理"

            if stat.st_mtime < cutoff:
                old_paths.append(path)
                old_bytes += stat.st_size

        return {
            "count": len(files),
            "total_bytes": total_bytes,
            "archived_count": archived_count,
            "old_count": len(old_paths),
            "old_bytes": old_bytes,
            "old_paths": old_paths,
            # files 是倒序（新的在前），所以最后一个就是最早创建的那个
            "oldest_name": files[-1].name if files else None,
            "days": days,
        }

    @classmethod
    def compress_sessions(cls, paths) -> dict:
        """把会话文件压成 .jsonl.gz，成功后才删原文件（第 35 轮新增）

        【为什么不直接删】
          见本节开头的说明：用 8%~15% 的空间换"一条都不丢"。
          真要彻底删，用 delete_sessions，但要用户明确选。

        【为什么必须"临时文件 + 原子替换"】
          直接往目标名写的话，压缩途中断电就留下半个 .gz，而原文件已经删了
          ——那才是真的数据没了。现在的顺序是：
            读原文 → 写 .gz.tmp → os.replace 成 .gz → 删原文
          任何一步崩掉，原文都还在。

        返回：{"count", "before_bytes", "after_bytes", "skipped", "failed": [说明, ...]}
          失败的文件**不删**，留在原地并在 failed 里说明原因。
          skipped 是"压完反而没变小"的那几个——见下面那段判断。
        """
        report = {"count": 0, "before_bytes": 0, "after_bytes": 0, "skipped": 0, "failed": []}

        for raw in paths:
            path = Path(raw)
            if _is_archive(path):
                continue  # 已经是归档：重复调用不报错，直接跳过

            try:
                before = path.stat().st_size
                text = _read_text(path)
                if not text:
                    # 读不出内容（文件坏了 / 没权限）：不压也不删。
                    # 压一个空归档会让人以为"这个会话本来就是空的"。
                    report["failed"].append(f"{path.name}（读不出内容，已跳过）")
                    continue

                target = path.with_name(path.name + ".gz")
                tmp = target.with_name(target.name + ".tmp")
                with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                    handle.write(text)
                os.replace(tmp, target)  # 这一刻起归档是完整可读的
                after = target.stat().st_size

                # 【第 35 轮】压完没变小就撤销。
                #   这不是理论情况：gzip 光头部固定开销就二十来字节，而
                #   "开一次、问一句就退出"的小会话只有几百字节，压完可能反而更大
                #   （实测 263 B 的会话压出 262 B）。留着这种归档没有任何好处：
                #   空间没省、/resume 还要多一步解压。所以把刚写的归档删掉、原文留着。
                if after >= before:
                    target.unlink()
                    report["skipped"] += 1
                    continue

                path.unlink()            # 归档就位之后才删原文

                report["count"] += 1
                report["before_bytes"] += before
                report["after_bytes"] += after
            except OSError as e:
                # 磁盘满、文件被占用、权限不足……都不该让整批停下来：
                # 能清几个算几个，把失败的报给用户。
                report["failed"].append(f"{path.name}（{type(e).__name__}: {e}）")
                # 顺手清掉可能残留的半截临时文件，免得下次 ls 看到奇怪的东西。
                # 这里不用变量 tmp：它可能在异常发生前还没赋值。
                try:
                    leftover = path.with_name(path.name + ".gz.tmp")
                    if leftover.exists():
                        leftover.unlink()
                except OSError:
                    pass

        return report

    @classmethod
    def delete_sessions(cls, paths) -> dict:
        """删掉指定的会话文件，删不掉的报出来（第 35 轮新增）

        【为什么这个函数自己不弹确认】
          它只管"删"，不问"要不要删"。确认放在命令层（/clean 里问两次），
          因为"怎么问、问几次"是交互设计，会话层不该知道终端长什么样。
          测试也能直接调它删临时文件，不用造一堆假回答。

        返回：{"count", "freed_bytes", "failed": [...]}
        """
        report = {"count": 0, "freed_bytes": 0, "failed": []}
        for raw in paths:
            path = Path(raw)
            try:
                size = path.stat().st_size
                path.unlink()
                report["count"] += 1
                report["freed_bytes"] += size
            except OSError as e:
                report["failed"].append(f"{path.name}（{type(e).__name__}: {e}）")
        return report

    @classmethod
    def open_archive(cls, path) -> Path:
        """resume 之前调用：归档会话先解压回 .jsonl（第 35 轮新增）

        【为什么必须解压，不能直接往 .gz 里追加】
          SessionStore._write_line 用的是 open("a") —— 往 gzip 文件里追普通文本，
          得到的是一个从中间开始就坏掉的文件，连它前半段都解不出来。
          gzip 确实支持"多成员追加"（gzip.open("at")），但一旦某次写到一半崩了，
          整个归档都读不了；而且"边聊边压"本身没有任何收益。

        【解压后为什么删掉归档】
          两份同时留着的话，list_sessions 会把同一个会话列两次
          （一次 .jsonl、一次 .jsonl.gz），用户选哪个都会困惑。
          数据没丢——全在新解出来的 .jsonl 里；等它再次变旧，
          compress_sessions 会重新压一份。

        【顺序：先写成功，再删归档】
          反过来的话，中途失败就是"归档没了、新的还没写完"，真的丢数据。

        不是归档（普通 .jsonl）就原样返回，调用方不用先判断类型。
        """
        source = Path(path)
        if not _is_archive(source):
            return source

        target = source.with_name(source.name[: -len(".gz")])  # x.jsonl.gz → x.jsonl
        try:
            text = _read_text(source)
            # 先写临时文件再替换：同名 .jsonl 有可能已经存在（用户手动恢复过），
            # 直接覆盖写一半就坏了。
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.replace(tmp, target)
            source.unlink()  # 新的已经落地，归档可以走了
            return target
        except OSError:
            # 解压失败（磁盘满、目录只读）：原样返回归档。
            # 拿到的是一个**能读**的路径，resume 照样能看到历史；
            # 只是接下来往里追加会被 _write_line 拦下（那里也有防御）。
            return source


# =============================================================================
# 四、恢复：把存档变成"能直接用"的消息列表
# =============================================================================

def _is_archive(path) -> bool:
    """这个会话文件是不是 gzip 归档（第 35 轮新增）

    单独抽一个判断函数，是因为"怎么认出归档"这件事有四处要一致：
    读文本、读 header、列表展示、压缩时跳过。四处的 `.suffix == ".gz"`
    各写一遍，将来改成 .zip 或加个 .zst 时必然漏一处。
    """
    return Path(path).suffix.lower() == ".gz"


def read_header(path) -> dict:
    """只读会话文件的第一行（header），不解全量（第 36 轮新增）

    【为什么要单独一个函数】
      `list_sessions(cwd)` 要按"这条会话是哪个项目开的"过滤，而 cwd 记在
      header 里、header 就是文件第一行。原来为了这一个字段对每个文件调
      `load()` —— 把整份读进来逐行解析。一个 1.5 MB 的会话只为取开头
      一百来字节，整份读一遍；`/resume`、`--continue` 每次都要付这个代价。

      归档（.jsonl.gz）更贵：要走 gzip 解压。虽然 gzip 是流式压缩、
      读第一行只需要解压开头那个块（几十 KB），但前提是**只读到第一行**
      —— 用 _read_text 就等于把整份解压回来了，归档省的磁盘又白费。

    【第一行不一定是 header】
      老版本文件、或者用户手改过的文件，第一行可能是消息。
      这时返回空字典，调用方按"不知道属于哪个项目"处理——
      在按 cwd 过滤时它就会被排除，和原来 load() 拿不到 header 的结果一致。

    【读失败一律返回空字典】
      口径与 _read_text 一致：坏文件当"没有 header"，不让一条坏归档
      把整个会话列表弄崩。
    """
    target = Path(path)
    try:
        if _is_archive(target):
            with gzip.open(target, "rt", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
        else:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
    except (OSError, EOFError, gzip.BadGzipFile):
        # EOFError / BadGzipFile：归档被截断（压缩到一半断电）。
        # 它们不是 OSError 的子类，必须单独接住。
        return {}

    line = first.strip()
    if not line:
        return {}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) and obj.get("type") == "header" else {}


def _read_text(path) -> str:
    """把会话文件整份读成文本，自动识别 gzip 归档（第 35 轮新增）

    【为什么按后缀判断，不读魔数】
      后缀是显式的：`ls ~/.thinker/sessions` 一眼就能看出哪些会话被归档过。
      读魔数要先按二进制读两个字节、再回退重来，两段逻辑都在猜，
      出问题（比如半截文件）时更难判断到底走了哪条路。
      代价是后缀必须由我们自己写出来——只有 compress_sessions 会产出 .jsonl.gz。

    【读失败统一返回空串】
      和调用方原来的口径一致：坏文件当空文件。
        · _read_session 拿到空串 → 没有消息、没有 header；
        · count_messages 拿到空串 → 0。
      两种后果都是"这个会话看起来是空的"，比抛异常让整个 /resume 列表崩掉好。
    """
    target = Path(path)
    try:
        if _is_archive(target):
            with gzip.open(target, "rt", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        return target.read_text(encoding="utf-8", errors="replace")
    except (OSError, EOFError, gzip.BadGzipFile):
        # EOFError / BadGzipFile：归档写坏了（压缩到一半断电、或者有人用
        #   git checkout 把二进制文件当文本处理过）。这两个都不是 OSError 的子类，
        #   必须单独接住，否则一条坏归档就能让整份会话列表打不开。
        return ""


def _session_files(directory: Path) -> List[Path]:
    """列出目录里所有会话文件（含归档），新的在前（第 35 轮新增）

    为什么要在 list_sessions 之外单独抽一层：
      `list_sessions` 还要按 cwd 过滤、要读 header；而清理、统计只需要
      "有哪些文件"。两处共用这一份"什么算会话文件"的定义，
      以后加一种格式（比如 .jsonl.zst）只改这里。
    """
    if not directory.is_dir():
        return []
    files = list(directory.glob("*.jsonl")) + list(directory.glob("*.jsonl.gz"))
    # 按文件名排序而不是 mtime：文件名以时间开头，天然有序；
    #   mtime 在复制、从备份恢复时会被重置，会把老会话排到最前面。
    return sorted(files, key=lambda p: p.name, reverse=True)


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

    # 【第 35 轮】改走 _read_text：归档（.jsonl.gz）和普通 .jsonl 都从这一处进。
    #   为什么不在这里自己 try OSError：读失败的各种情形（文件没了、归档坏了、
    #   压缩包被截断）统一在 _read_text 里收口，那边返回空串，这里自然得到
    #   "空会话"，少一层分支也少一处漏 catch。
    text = _read_text(path)

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
