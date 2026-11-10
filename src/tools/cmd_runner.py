# =============================================================================
# 文件：src/tools/cmd_runner.py
# 作用：命令执行工具。负责跑 shell 命令、跑测试、git 操作。
#
# 【2026-11-06 第 8 轮】读/写/改/找/搜文件已经拆到 tools/file_tools.py，
#       本工具不再负责文件操作。现在只有它一个工具会真正启动外部进程。
#
# 它的工作流程：
#   1. 模型说："我想执行 dir，不需要用户同意"
#   2. 这个工具收到命令，真的去跑
#   3. 把命令的输出（或错误信息）当作结果返回给模型
#
# 注意：第 8 轮之前，所有文件操作都靠 cat / sed / grep 这些命令凑，
# 但当前环境是 Windows，命令走 cmd.exe，这三个命令默认都不存在。
# 本轮把说明书里的 Linux 命令指引也一并换成了专用文件工具。
# =============================================================================

import asyncio  # asyncio 库：用来"异步"执行命令（不阻塞界面）
import locale  # locale 库：用来猜当前系统的默认文字编码（中文 Windows 是 GBK）
import re  # 【第 15.3 轮】识别"用 shell 做文件查找"的命令，见 _FILE_SEARCH_PATTERNS
import sys  # sys 库：这里只用来判断"当前是不是 Windows"（决定用哪种方式杀进程）
from core.limits import MAX_TOOL_OUTPUT_CHARS  # 【第 9 轮】输出上限统一到一处，不再各写一个数
from tools.base_tool import BaseTool  # 导入父类


# 【2026-11-06 优化】下面三个常量是这轮新增的，把"魔法数字"集中到一处，改的时候只改这里
DEFAULT_TIMEOUT = 30  # 默认最多等 30 秒
MAX_TIMEOUT = 600  # 上限 10 分钟（防止模型传个几小时把程序挂住）
# 【2026-11-06 第 9 轮】原来这里自己写死 30000，而 conversation.py 里另有一个 12000。
#   同一段输出被砍两刀，12000 那一刀最终生效，所以 30000 等于白写。
#   现在两处共用 core/limits.py 里的同一个数字（默认 30000，可用环境变量覆盖）。
MAX_OUTPUT_CHARS = MAX_TOOL_OUTPUT_CHARS

# 【2026-11-10 新增】输出捕获的字节预算（按下标 2 以下才安全：这条线限制的是"内存"，不是上下文）
#
# 为什么需要这组常量（这是对照 Claude Code 行为补上的一块）：
#   原实现用 process.communicate() 一次性读完全部输出，读到内存里之后才由 _truncate 按字符截断。
#   问题在"读完全部"这一步：跑 `dir /s C:\` 或 `npm install` 这类命令，输出可能有几百 MB——
#   _truncate 是在几百 MB 已经全部进内存之后才执行的，等于防了个寂寞。
#
# 现在的方案：边读边收，每个流最多保留 MAX_STREAM_HEAD_BYTES（开头）+
#   MAX_STREAM_TAIL_BYTES（结尾）两段，中间的字节只计数不保存。
#   为什么保留头和尾而不是只留头：命令输出的"结论"几乎总在结尾（构建结果、测试汇总、报错堆栈），
#   只留头会让模型拿不到最有用的那段。
#
# 数值怎么定的：上下文层每流最多展示 MAX_OUTPUT_CHARS（默认 30000 字符），
#   头尾各 200000 字节（约 40 万字节/流）保证了"上下文要用的部分一定在捕获范围内"，
#   多出来的字节是给"人眼看错误定位"留的余量；单条命令内存占用被钉死在 ~1MB 以内。
#   为什么按字节而不是按字符：管道读出来就是字节，按字节计数不用边读边解码（多字节字符
#   可能跨块，按字符算必须维护增量解码器，复杂度不值当；切缝处最多损失一个字符）。
STREAM_CHUNK_SIZE = 65536        # 每次从管道读 64KB
MAX_STREAM_HEAD_BYTES = 200_000  # 每个流保留开头的字节数
MAX_STREAM_TAIL_BYTES = 200_000  # 每个流保留结尾的字节数


# 【第 15.3 轮】识别"这条命令其实是在按文件名找文件"。
#
# 为什么需要这层：第 15.1 轮改了提示词、第 15.2 轮直接删了路径限制，
# 但实测（会话 20261108-154549）模型仍然连发 6 条 dir / Get-ChildItem 去找一份简历，
# 其中一条全盘递归超时被杀，还有两条 PowerShell 语法写错。它加载的是**新**提示词，
# 说明事前约束（提示词 + 工具说明书）它不一定读进去。
#
# 而工具结果是它每一轮都必读的。所以把引导放在结果末尾：不阻断、不改变行为，
# 只是让它下一次能看见"这件事有专用工具"。
# 这比在工具层直接拒绝温和：拒绝会让模型以为环境坏了，转而用更奇怪的方式绕。
_FILE_SEARCH_PATTERNS = (
    # dir /s ...：递归列目录，是这次实际用到的写法
    re.compile(r"\bdir\b[^|&;]*\s/s\b", re.I),
    # dir ... *关键词*：带通配符的列目录，等价于"按名字找"
    re.compile(r"\bdir\b[^|&;]*\*", re.I),
    # PowerShell 的递归遍历
    re.compile(r"Get-ChildItem[^|&;]*-Recurse", re.I),
    # find . -name / -iname
    re.compile(r"\bfind\b[^|&;]*-(?:i?name)\b", re.I),
    # where /r：Windows 上的递归查找
    re.compile(r"\bwhere\b\s+/r\b", re.I),
)

# 命中时追加到结果末尾的建议。措辞要点：
#   · 给出可直接照抄的调用示例，模型最缺的是"path 该填什么"；
#   · 说清"已经执行过了"，否则它会以为命令没跑；
#   · 提到并发，打消"多调用几次会更慢"的顾虑（这是它选 shell 的真实理由之一）。
_FILE_SEARCH_HINT = """
---
[thinker 提示] 上面这条命令做的是"按文件名查找"，这类事情有专用工具，不必拼 shell：
  file_glob(pattern="**/*关键词*", path="C:/Users/用户名/Desktop")
它支持任意路径和 ** 递归，只读工具还会并发执行（几个目录可以同时搜），
比递归扫盘快，也不会因为超时被杀掉。下次遇到"找某个文件"请直接用它。"""


class CmdRunner(BaseTool):
    def __init__(self):
        super().__init__()  # 调用父类的初始化（这里其实什么都没做）

    @staticmethod
    def get_tool_name():
        return "cmd_runner"  # 这是它在模型眼里的名字

    async def act(self, command="", timeout=DEFAULT_TIMEOUT):
        """执行命令；如果这条命令其实是在找文件，就在结果末尾补一句引导（第 15.3 轮新增）

        为什么要把原来的函数体挪到 _execute、这里只留一层薄壳：
          原来的 act 里有 5 个 return 分支（成功 / 成功但无输出 / 失败有 stderr /
          失败无 stderr / 抛异常）。要在每个分支后面都补提示，等于把同一段逻辑
          抄五遍，将来改一句措辞必然漏一处。包一层壳就变成"算完结果、再决定要不要加尾巴"。

        为什么是"追加提示"而不是"拒绝执行"：
          拒绝会让模型以为工具坏了，然后换更奇怪的方式绕——第 15.1 轮的
          cmd_runner 绕过工作区边界就是这么来的。追加提示不改变它拿到的结果，
          只是让它在下一轮看见"这件事有专用工具"。
        """
        result = await self._execute(command, timeout)
        if command and self._looks_like_file_search(command):
            return result + _FILE_SEARCH_HINT
        return result

    @staticmethod
    def _looks_like_file_search(command: str) -> bool:
        """这条命令看起来是不是在按文件名查找（第 15.3 轮新增）

        判据故意写得窄：只认"递归"或"带通配符"的列目录，以及 find / where 的查找写法。
        为什么要窄：普通的 `dir`（不带 /s、不带 *）是正当需求——用户会说
        "看看这个目录里有什么"，那种情况加提示只是噪音。提示一多，
        模型和用户都会开始忽略它，等于白加。
        """
        return any(p.search(command) for p in _FILE_SEARCH_PATTERNS)

    async def _execute(self, command="", timeout=DEFAULT_TIMEOUT):
        # 真正的执行入口。command 是要跑的命令，timeout 是最多等几秒
        if not command:  # 如果命令是空的
            return "没有提供任何命令。请把要执行的命令放进 command 参数。"  # 直接告诉他"你没给命令"

        # 【2026-11-06 优化】对 timeout 做"清洗"：模型有时候会传字符串、负数或者巨大的数
        try:
            timeout = float(timeout)  # 先尝试转成小数
        except (TypeError, ValueError):  # 转不了（比如传了 "abc"）
            timeout = DEFAULT_TIMEOUT  # 就用默认值
        timeout = max(1.0, min(timeout, MAX_TIMEOUT))  # 夹在 1 秒到 10 分钟之间

        try:  # 试：跑命令
            # 【2026-11-06 优化】原来用的是同步的 subprocess.run，
            # 命令一跑起来整个程序（包括正在刷新的界面）都会卡住。
            # 现在改成 asyncio 的异步子进程：等待命令的同时，程序还能干别的。
            process = await asyncio.create_subprocess_shell(
                command,  # 要执行的命令字符串（和原来的 shell=True 效果一致）
                stdout=asyncio.subprocess.PIPE,  # 把正常输出接到管道里，方便我们读取
                stderr=asyncio.subprocess.PIPE,  # 把错误输出也接出来
            )

            # 【2026-11-10 改】输出改为"边读边收"（增量捕获），不再用 communicate() 一次读完。
            #   为什么两个流要各自起一个 task 并同时读：
            #     如果先读 stdout 再读 stderr，一旦 stderr 管道被写满（64KB 缓冲区），
            #     子进程会卡在"往 stderr 写"上再也不往下走，stdout 也就再也没有新数据——
            #     经典的管道死锁。两个 task 并发读，谁都不会把对方憋死。
            stdout_task = asyncio.create_task(self._read_stream(process.stdout))
            stderr_task = asyncio.create_task(self._read_stream(process.stderr))

            try:  # 再试一层：等命令跑完，但最多等 timeout 秒
                # 只等进程退出即可；输出的收集由上面两个读取 task 自己做，
                # 它们读到 EOF（管道关闭）就会自然结束，和退出互不阻塞。
                await asyncio.wait_for(
                    process.wait(),
                    timeout=timeout  # 【2026-11-06 优化】这里才真正用上了 timeout 参数
                    # 原来的写法是 timeout=30，写死在代码里，参数传什么都等于没传
                )
            except asyncio.TimeoutError:  # 等超时了
                await self._kill_process_tree(process)  # 【2026-11-06 优化】连子进程一起杀掉
                # 杀完之后管道会关闭，读取 task 会自己结束；给 3 秒收尾时间，
                # 收不回来就取消（极端情况：有个杀不掉的孙进程还攥着管道句柄）。
                await self._drain_readers((stdout_task, stderr_task))
                return f"命令执行超时：等了 {timeout:g} 秒仍未结束，进程已被强制终止。"  # 告诉模型"超时了"

            # 进程已退出，管道随后关闭；等两个读取 task 跑完（通常此刻已接近 EOF）。
            # return_exceptions=True：某个流读挂了（比如传输层提前关闭）不炸整个工具，
            # 把它当"这个流什么都没读到"处理——另一条流的内容仍然有效。
            results = await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            stdout = self._decode_captured(results[0])  # 标准输出 → 文字（超出预算的中间部分有省略标记）
            stderr = self._decode_captured(results[1])  # 错误输出 → 文字

            if process.returncode == 0:  # returncode 是"退出码"，0 表示成功（这是命令行世界的约定）
                if stdout.strip():  # 如果标准输出（正常结果）去掉空白后还有内容
                    return self._truncate(stdout)  # 就返回结果（超出上下文预算时按字符再截一道，兜底）
                else:  # 命令成功了但什么都没输出
                    return "命令执行成功，但没有产生任何输出。"  # 告诉他"跑成功了，但没输出"
            else:  # 退出码不是 0，说明命令出错了
                if stderr.strip():  # 有错误输出
                    return self._truncate(f"命令执行失败。错误输出：\n{stderr}")  # 把错误输出返回给模型
                # 【2026-11-06 优化】有时候命令失败但错误输出是空的，
                # 原来会返回 "Error: "（什么信息都没有），模型完全不知道发生了什么。
                return f"命令执行失败，退出码 {process.returncode}，没有任何错误输出。"  # 至少告诉他退出码
        except Exception as e:  # 其他任何意外
            return f"cmd_runner 工具执行时发生异常：{str(e)}"  # 返回错误信息

    @staticmethod
    async def _read_stream(stream) -> tuple:
        """增量读取一条输出管道：保留头尾、只对中间计数（2026-11-10 新增）

        为什么是这个形状（返回四元组而不是直接返回文字）：
          解码要等读完全部字节才能做（多字节字符可能跨块），所以这里只攒字节；
          把"攒了什么"交给 _decode_captured 统一处理，读和解码各管一段，测试也好写。

        策略（详见文件头常量处的注释）：
          · 总量不超过 头预算+尾预算 时：全部保留（绝大多数命令都落在这档）；
          · 超过之后：开头 head 预算固定不动，结尾用滚动窗口只留最后 tail 预算——
            `tail = (tail + chunk)[-MAX:]` 每次裁一刀，成本和窗口大小成正比，恒定有界；
          · 中间被丢掉的字节只累计总数（dropped），最后以一行省略标记呈现。

        参数 stream：asyncio 的 StreamReader（process.stdout 或 process.stderr）。
        返回 (总字节数, 开头字节, 结尾字节, 被丢弃字节数)。
        """
        head = bytearray()  # 开头部分（命令"在干什么"）
        tail = bytearray()  # 结尾部分（命令"结果如何"——通常才是最有用的）
        total = 0           # 一共读到了多少字节（用于算 dropped）
        while True:
            chunk = await stream.read(STREAM_CHUNK_SIZE)
            if not chunk:  # 读到 EOF：进程端关闭了管道
                break
            total += len(chunk)

            # 还没填满头部预算：先往 head 里塞
            if len(head) < MAX_STREAM_HEAD_BYTES:
                take = min(len(chunk), MAX_STREAM_HEAD_BYTES - len(head))
                head += chunk[:take]
                chunk = chunk[take:]  # 剩下的部分继续走 tail 逻辑（可能为空）

            # 剩余部分进入尾部滚动窗口：拼上之后只留最后 MAX_STREAM_TAIL_BYTES 个字节
            if chunk:
                tail = (tail + chunk)[-MAX_STREAM_TAIL_BYTES:]

        dropped = total - len(head) - len(tail)
        # dropped 不可能是负数：head 和 tail 里的每个字节都来自读到的 chunk。
        # 防御性 max 一下，万一未来改窗口逻辑时不小心重复计了数，报个 0 也好过报负数。
        return (total, bytes(head), bytes(tail), max(0, dropped))

    def _decode_captured(self, captured) -> str:
        """把 _read_stream 的捕获结果转成文字，超出预算的中间部分替换为省略标记

        为什么要单独一层而不在 _read_stream 里直接解码：
          解码失败要换编码再试（_decode 的逻辑），那个"试"针对的是一段完整字节；
          head 和 tail 被窗口切过之后各自都可能把多字节字符切半，要分开解码、分开兜底，
          最后再用省略标记拼起来。混在一起做会让"省略了多少字节"和"哪个编码成功"搅在一起。

        参数 captured：_read_stream 的返回值；也可能是 gather 塞进来的 Exception
          （某个流读挂了）——这时按"这个流什么都没读到"处理，不让另一条流白读。
        """
        if isinstance(captured, Exception):  # 读取 task 失败：当空输出处理
            return ""
        _total, head, tail, dropped = captured
        if dropped <= 0:
            # 没丢过任何字节：head+tail 拼起来就是完整输出，按原来的路子解码
            return self._decode(head + tail)
        # 丢过字节：头尾分别解码（切缝处最多损失一个多字节字符，_decode 的
        # errors="replace" 兜底会把它换成一个 �，不影响大局），
        # 中间用一行标记说清省略了多少——让模型知道"下面不是完整输出"。
        return (
            self._decode(head)
            + f"\n... [输出过长，中间省略了 {dropped} 字节] ...\n"
            + self._decode(tail)
        )

    @staticmethod
    async def _drain_readers(tasks) -> None:
        """给读取 task 一个收尾窗口，超时没结束就取消（2026-11-10 新增）

        什么时候会被调：超时杀进程树之后。正常情况下 taskkill /T 杀掉整棵进程树，
        管道随之关闭、读取 task 立刻 EOF；但万一有个杀不掉的进程还攥着管道句柄，
        读取 task 会一直挂着——不能让它拦着"返回超时结果"这件事。
        """
        done, pending = await asyncio.wait(set(tasks), timeout=3)
        for task in pending:  # 3 秒还没结束的：取消，宁可少收一段输出也要按时返回
            task.cancel()

    @staticmethod
    async def _kill_process_tree(process):
        """超时后把命令进程连同它的子孙进程一起杀掉（2026-11-06 新增）

        为什么不能只调 process.kill()？
          因为我们是"通过 shell 执行"命令的：
             我们 → cmd.exe → python.exe（真正干活的）
          process.pid 指向的是 cmd.exe，kill() 只能杀掉它，
          底下那个 python.exe 还活着，而且它还占着输出管道，
          于是后面的 communicate() 会一直等到它自己跑完——
          实测设 timeout=1 却等了 7 秒，就是这个原因。
          Windows 上正确做法是 taskkill /T（T 就是"连同子进程树"）。
        """
        if sys.platform == "win32":  # 只在 Windows 上这么做
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(process.pid),  # /F 强制，/T 杀整棵进程树
                    stdout=asyncio.subprocess.DEVNULL,  # 不需要它的输出
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=3)  # 最多等 3 秒让它干完
            except Exception:  # taskkill 不存在或失败，退回下面的普通方式
                pass

        try:  # 兜底：无论上面成没成功，都再杀一次直接子进程
            process.kill()  # 杀掉 shell 本身
        except ProcessLookupError:  # 已经死了
            pass
        except Exception:  # 其他异常也不用管，反正要返回了
            pass

        # 【2026-11-10 改】原来这里还有一段 `await process.communicate()` 收残留输出，
        #   现在必须删掉：输出已经改成并发读取 task 在收（见 _execute），communicate 内部
        #   会再次去读同一对管道，两处并发读同一个流会直接抛 RuntimeError。
        #   收尾这件事移交给了调用方的 _drain_readers（杀完进程管道关闭，读取 task 自然 EOF）。

    @staticmethod
    def _decode(raw: bytes) -> str:
        """把命令输出的原始字节转成文字（2026-11-06 新增，第 4 轮修正编码顺序）

        为什么要猜编码？
          Linux/Mac 上命令输出基本都是 UTF-8，但中文 Windows 的 cmd 输出是 GBK。
          如果一律按 UTF-8 解码，遇到中文就会抛 UnicodeDecodeError。

        第 4 轮踩到的坑：
          原来只试 [utf-8, 系统默认编码]。但在这个环境里系统默认也是 UTF-8，
          于是两个都失败，最后走到 errors="replace"，中文就全变成了 �����。
          现在把 GBK 显式加进候选列表（GBK 是中文 Windows 命令行的实际编码）。
        """
        if not raw:  # 没有任何输出
            return ""  # 直接返回空字符串

        candidates = ["utf-8", "gbk", locale.getpreferredencoding(False)]
        # 注意顺序：UTF-8 优先（现代工具的输出），其次 GBK（中文 Windows 命令行），最后系统默认
        tried = set()  # 记录试过的编码，避免重复
        for encoding in candidates:
            if not encoding or encoding.lower() in tried:  # 重复或为空
                continue
            tried.add(encoding.lower())
            try:
                return raw.decode(encoding)  # 严格解码，失败会抛异常
            except (UnicodeDecodeError, LookupError):  # 这个编码解不了
                continue  # 换下一个

        return raw.decode("utf-8", errors="replace")  # 都失败：强行解码，把不能认的字符替换成 �

    @staticmethod
    def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
        """输出太长就掐头去尾留中间（2026-11-06 新增）

        为什么必须截断？
          像 npm install、pytest 这类命令的输出可能有几万行，
          全部塞给模型会瞬间占满上下文，甚至直接超过模型上限报错。
          这里保留开头 60% 和结尾 40%：开头通常是"在干什么"，结尾通常是"结果如何"。
        """
        if len(text) <= limit:  # 没超过上限
            return text  # 原样返回
        head_length = int(limit * 0.6)  # 开头留多少
        tail_length = limit - head_length  # 结尾留多少
        omitted = len(text) - limit  # 中间省略了多少字符
        return (
            text[:head_length]  # 开头部分
            + f"\n... [输出过长，中间省略了 {omitted} 个字符] ...\n"  # 一句说明，让模型知道中间被省略了
            + text[-tail_length:]  # 结尾部分
        )

    def json_schema(self):
        # 这份"自我介绍"会原封不动发给大模型，模型就是靠它决定怎么调用这个工具的
        # 【2026-11-06 第 3 轮】所有给模型看的文字统一改成中文
        return {
            "type": "function",  # 固定写法，表示这是一个"函数"
            "function": {
                "name": self.get_tool_name(),  # 工具名：cmd_runner
                "description": self._tool_description(),  # 详细说明（在文件末尾，很长）
                "parameters": {
                    "type": "object",  # 参数是一个"对象"（也就是字典）
                    "properties": {
                        # properties 里列出每个参数的名字、类型、说明
                        "need_user_approve": {
                            "type": "boolean",  # 布尔值：真或假
                            "description": "执行这条命令前是否需要用户先明确同意。危险命令必须设为 true。",
                            "default": True  # 默认值是"需要同意"
                        },
                        "command": {
                            "type": "string",  # 字符串
                            "description": "要执行的 shell 命令"  # 要执行的命令
                        },
                        "timeout": {
                            "type": "integer",  # 整数
                            "description": "最多等待多少秒（默认 30，最大 600）。超时后进程会被强制终止。",
                            "default": 30  # 默认 30 秒
                        }
                    },
                    "required": ["need_user_approve", "command"]
                    # required 表示"这两个参数必须给"，timeout 可以不给
                }
            }
        }

    def get_status(self):
        return ""  # 这个工具没有状态要汇报，返回空字符串

    # TODO: Read and Edit file will separate as serval tools
    # ↑ 这是原作者留下的待办，【2026-11-06 第 8 轮】已完成：
    #   见 tools/file_tools.py 的 file_read / file_write / file_edit / file_glob / file_grep。
    #   保留这行是因为它记录了这次拆分的来由。

    def _tool_description(self):
        # 下面是给模型看的使用说明书。
        # 【2026-11-06 第 3 轮】整段从英文翻译成中文，内容和结构保持一一对应：
        #   1. 安全提示（危险命令必须请求用户同意）
        #   2. 目录检查（建文件前先确认父目录存在）
        #   3. 读文件的写法（cat -n 带行号）
        #   4. 改文件的写法（sed 替换/插入，grep 搜索，不确定就先预览）
        #   5. 使用注意（参数、超时、尽量用绝对路径）
        #   6. git 提交与 PR 的规范
        return """
在当前会话中执行一条 shell 命令，支持超时控制、用户授权和安全措施。

本工具只负责"执行命令"：跑测试、构建、git 操作等。文件操作请用专用工具：
  - 读文件：file_read（带行号、可分段）
  - 新建或整体重写文件：file_write
  - 改文件里的一小段：file_edit（会返回改动 diff）
  - 按文件名找文件：file_glob
  - 按内容搜索：file_grep

不要再用 cat -n / sed -i / grep 去拼文件操作：这些是 Linux 命令，
在 Windows 的 cmd.exe 里默认不存在，照着执行会直接失败。

执行命令前，请按以下步骤检查：

1. 安全检查：
   - 始终判断这条命令是否有危险（例如 rm、sudo、chmod 777、format、del 等）
   - 如果命令危险，或者可能造成系统损坏、数据丢失，必须把 need_user_approve 设为 true

2. 目录检查：
   - 如果命令会新建目录，先确认父目录存在且位置正确（**用 file_glob 查看**）
   - 例如：执行 "mkdir foo/bar" 之前，先确认 "foo" 存在且就是想要的父目录
   - 【第 15.3 轮】这里原来写的是"可用 file_glob，或用 `dir` 查看"。
     那半句被删掉了，因为它是一份**授权**：模型读到"dir 可以查看目录"，
     遇到"帮我找一下文件"就直接 `dir /s /b`，而不是用 file_glob。
     实测确实如此——用户问简历在哪，模型连发 6 条 dir / Get-ChildItem，
     还有一条全盘递归超时 30 秒。工具说明书不该同时给出两条去做同一件事的路，
     模型只会记住更熟的那条（shell）。

使用说明：
  - command 和 need_user_approve 两个参数是必填的。
  - 可以传一个可选的 timeout（单位：秒，默认 30，最大 600）。不传则 30 秒超时。
  - 命令要匹配当前操作系统（系统提示词里有"操作系统"一项）。Windows 上请用 `dir`、`type`、PowerShell 命令，不要假设存在 ls / cat / sed / grep。
  - 整个会话中尽量保持当前工作目录不变：用绝对路径，避免使用 `cd`。只有用户明确要求时才用 `cd`。

用 git 提交改动

当用户要求你创建一个 git 提交时，请仔细按以下步骤执行：

1. 你可以在一次回复里调用多个工具。当需要多项互不依赖的信息时，把这些调用放在一起并行执行以提高效率。始终并行运行下面这些命令：
  - 运行 git status，查看所有未跟踪的文件。
  - 运行 git diff，查看已暂存和未暂存的改动。
  - 运行 git log，查看最近的提交信息，以便沿用这个仓库的提交信息风格。
2. 分析所有已暂存的改动（包括之前暂存的和新加入的），然后起草提交信息：
  - 概括改动的性质（例如：新功能、功能增强、缺陷修复、重构、测试、文档等）。确保信息准确反映改动的目的（"新增"指全新的功能，"更新"指对已有功能的增强，"修复"指修 bug，等等）。
  - 检查是否包含不应提交的敏感信息
  - 起草一句简洁（1-2 句）的提交信息，重点写"为什么改"而不是"改了什么"
  - 确保它准确反映了改动内容和目的
3. 你可以在一次回复里调用多个工具。当需要多项互不依赖的信息时，把这些调用放在一起并行执行以提高效率。始终并行运行下面这些命令：
   - 把相关的未跟踪文件加入暂存区。
   - 运行 git status，确认提交成功。
4. 如果提交因为 pre-commit 钩子修改了文件而失败，重试一次提交以包含这些自动改动。如果再次失败，通常是某个钩子阻止了提交。如果提交成功但你发现文件被钩子改过，必须补一次提交把这些改动包含进去。

重要说明：
- 绝对不要修改 git config
- 除了 git 相关命令，绝对不要额外运行别的命令去读代码或探索代码
- 绝对不要使用 todo_write 或 task 工具
- 除非用户明确要求，不要推送到远程仓库
- 重要：绝对不要使用带 -i 参数的 git 命令（如 git rebase -i、git add -i），因为它们需要交互式输入，本环境不支持
- 如果没有改动可提交（既没有未跟踪文件也没有修改），不要创建空提交
- 为了保证格式正确，提交信息一律用 HEREDOC 传入，示例如下：
<example>
git commit -m "$(cat <<'EOF'
   这里写提交信息。
   EOF
   )"
</example>

### 创建 Pull Request
所有 GitHub 相关的事（issue、PR、检查、发布）都用 gh 命令完成。如果用户给了 GitHub 链接，用 gh 命令去取需要的信息。

重要：用户要求你创建 Pull Request 时，请仔细按以下步骤执行：

1. 你可以在一次回复里调用多个工具。当需要多项互不依赖的信息时，把这些调用放在一起并行执行。始终并行运行下面这些命令，以了解当前分支从主干分叉以来的状态：
   - 运行 git status，查看所有未跟踪的文件
   - 运行 git diff，查看已暂存和未暂存的改动
   - 检查当前分支是否跟踪了远程分支、是否与远程同步，以判断是否需要推送
   - 运行 git log 和 `git diff [基础分支]...HEAD`，了解当前分支的完整提交历史
2. 分析将要包含在 PR 里的所有改动，确保查看了所有相关提交（不只是最新一条，而是**所有**会被包含的提交），然后起草 PR 说明
3. 你可以在一次回复里调用多个工具。当需要多项互不依赖的信息时，把这些调用放在一起并行执行。始终并行运行下面这些命令：
   - 如有需要，创建新分支
   - 如有需要，用 -u 参数推送到远程
   - 用下面的格式通过 gh pr create 创建 PR。正文用 HEREDOC 传入以保证格式正确。
<example>
gh pr create --title "PR 标题" --body "$(cat <<'EOF'
#### 概述
<1-3 条要点>

重要：
- 绝对不要修改 git config
""".strip()
        # .strip() 的作用：去掉这段字符串首尾的空行和空格，避免占没用的空间
