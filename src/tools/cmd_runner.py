# =============================================================================
# 文件：src/tools/cmd_runner.py
# 作用：命令执行工具。这是目前这个程序里"唯一能干活的手"——
#       读文件、改文件、跑测试，全靠它去执行 shell 命令。
#
# 它的工作流程：
#   1. 模型说："我想执行 ls -la，不需要用户同意"
#   2. 这个工具收到命令，真的去跑
#   3. 把命令的输出（或错误信息）当作结果返回给模型
#
# 注意：现在所有文件操作都靠 cat / sed / grep 这些命令凑，
# 没有专门的"读文件/改文件"工具，所以模型很容易写错命令。
# =============================================================================

import asyncio  # asyncio 库：用来"异步"执行命令（不阻塞界面）
import locale  # locale 库：用来猜当前系统的默认文字编码（中文 Windows 是 GBK）
import sys  # sys 库：这里只用来判断"当前是不是 Windows"（决定用哪种方式杀进程）
from tools.base_tool import BaseTool  # 导入父类


# 【2026-11-06 优化】下面三个常量是这轮新增的，把"魔法数字"集中到一处，改的时候只改这里
DEFAULT_TIMEOUT = 30  # 默认最多等 30 秒
MAX_TIMEOUT = 600  # 上限 10 分钟（防止模型传个几小时把程序挂住）
MAX_OUTPUT_CHARS = 30000  # 输出超过约 3 万字符就截断（不截断会把上下文撑爆）


class CmdRunner(BaseTool):
    def __init__(self):
        super().__init__()  # 调用父类的初始化（这里其实什么都没做）

    @staticmethod
    def get_tool_name():
        return "cmd_runner"  # 这是它在模型眼里的名字

    async def act(self, command="", timeout=DEFAULT_TIMEOUT):
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

            try:  # 再试一层：等命令跑完，但最多等 timeout 秒
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),  # 等命令结束并收齐所有输出
                    timeout=timeout  # 【2026-11-06 优化】这里才真正用上了 timeout 参数
                    # 原来的写法是 timeout=30，写死在代码里，参数传什么都等于没传
                )
            except asyncio.TimeoutError:  # 等超时了
                await self._kill_process_tree(process)  # 【2026-11-06 优化】连子进程一起杀掉
                return f"命令执行超时：等了 {timeout:g} 秒仍未结束，进程已被强制终止。"  # 告诉模型"超时了"

            # 把字节转成文字（Windows 上命令输出常常是 GBK，不是 UTF-8，所以要猜）
            stdout = self._decode(stdout_bytes)
            stderr = self._decode(stderr_bytes)

            if process.returncode == 0:  # returncode 是"退出码"，0 表示成功（这是命令行世界的约定）
                if stdout.strip():  # 如果标准输出（正常结果）去掉空白后还有内容
                    return self._truncate(stdout)  # 就返回结果（太长会被截断）
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

        try:  # 收一下残留输出，别让管道一直挂着
            await asyncio.wait_for(process.communicate(), timeout=2)
        except Exception:  # 收不回来就算了
            pass

    @staticmethod
    def _decode(raw: bytes) -> str:
        """把命令输出的原始字节转成文字（2026-11-06 新增）

        为什么要猜编码？
          Linux/Mac 上命令输出基本都是 UTF-8，但中文 Windows 的 cmd 输出是 GBK。
          如果一律按 UTF-8 解码，遇到中文就会抛 UnicodeDecodeError。
          所以先试 UTF-8，不行再试系统默认编码，最后才用"忽略错误"的兜底方式。
        """
        if not raw:  # 没有任何输出
            return ""  # 直接返回空字符串
        for encoding in ("utf-8", locale.getpreferredencoding(False)):  # 依次尝试两种编码
            try:
                return raw.decode(encoding)  # 解码成功就直接返回
            except UnicodeDecodeError:  # 这个编码解不了
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
    # ↑ 原作者的待办：以后要把"读文件"和"改文件"拆成独立的工具（确实该拆）

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

执行命令前，请按以下步骤检查：

1. 安全检查：
   - 始终判断这条命令是否有危险（例如 rm、sudo、chmod 777、format、del 等）
   - 如果命令危险，或者可能造成系统损坏、数据丢失，必须把 need_user_approve 设为 true

2. 目录检查：
   - 如果命令会新建目录或文件，先用 ls 确认父目录存在，并且位置正确
   - 例如：执行 "mkdir foo/bar" 之前，先用 ls 确认 "foo" 存在且就是想要的父目录

3. 读取文件：
   - 第一次读取某个文件时，始终使用 `cat -n <文件>`，这样能看到带行号的内容（默认最多 2000 行）

4. 修改文件：
   - 修改任何文件之前，必须先在当前对话里读过它的内容
   - 修改已有文件的内容时，除内容特别多的情况外，一律用 sed 做替换：
     示例：`sed -i "起始行,结束行 c 新内容" <文件>`
   - 向已有文件插入内容时，用 sed 插入：
     示例：`sed -i "插入位置上一行的行号 a 要插入的内容" <文件>`
   - 在文件内搜索时，一律用 grep：
     示例：`grep "要搜索的内容" <文件>`
   - 不确定要改哪几行时，先用 sed 预览带行号的片段，确认那确实是你想改的代码：
     示例：`sed -n "起始行,结束行 p" <文件>`

注意：以上命令适用于 Linux/macOS 系统。在 Windows 上请使用对应的等价命令（例如 `dir`、`type`、PowerShell 命令）。

使用说明：
  - command 和 need_user_approve 两个参数是必填的。
  - 可以传一个可选的 timeout（单位：秒，默认 30，最大 600）。不传则 30 秒超时。
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
