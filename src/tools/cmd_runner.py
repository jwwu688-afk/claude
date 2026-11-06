# =============================================================================
# 文件：src/tools/cmd_runner.py
# 作用：命令执行工具。这是目前这个程序里"唯一能干活的手"——
#       读文件、改文件、跑测试，全靠它去执行 shell 命令。
#
# 它的工作流程：
#   1. 模型说："我想执行 ls -la，不需要用户同意"
#   2. 这个工具收到命令，用 subprocess 真的去跑
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
            return "No command provided"  # 直接告诉他"你没给命令"

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
                return f"Command timed out after {timeout:g} seconds"  # 告诉模型"超时了，等了几秒"

            # 把字节转成文字（Windows 上命令输出常常是 GBK，不是 UTF-8，所以要猜）
            stdout = self._decode(stdout_bytes)
            stderr = self._decode(stderr_bytes)

            if process.returncode == 0:  # returncode 是"退出码"，0 表示成功（这是命令行世界的约定）
                if stdout.strip():  # 如果标准输出（正常结果）去掉空白后还有内容
                    return self._truncate(stdout)  # 就返回结果（太长会被截断）
                else:  # 命令成功了但什么都没输出
                    return "Command executed successfully and no return"  # 告诉他"跑成功了，但没输出"
            else:  # 退出码不是 0，说明命令出错了
                if stderr.strip():  # 有错误输出
                    return self._truncate(f"Error: {stderr}")  # 把错误输出返回给模型
                # 【2026-11-06 优化】有时候命令失败但错误输出是空的，
                # 原来会返回 "Error: "（什么信息都没有），模型完全不知道发生了什么。
                return f"Error: command exited with code {process.returncode}"  # 至少告诉他退出码
        except Exception as e:  # 其他任何意外
            return f"cmd_runner Exception: {str(e)}"  # 返回错误信息

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
            + f"\n... [output truncated, {omitted} characters omitted] ...\n"  # 一句说明，让模型知道中间被省略了
            + text[-tail_length:]  # 结尾部分
        )

    def json_schema(self):
        # 这份"自我介绍"会原封不动发给大模型，模型就是靠它决定怎么调用这个工具的
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
                            "description": "Whether the command requires explicit user approval before execution",
                            # 说明：这个命令是否需要用户先点头同意
                            "default": True  # 默认值是"需要同意"
                        },
                        "command": {
                            "type": "string",  # 字符串
                            "description": "The shell command to execute"  # 要执行的命令
                        },
                        "timeout": {
                            "type": "integer",  # 整数
                            "description": "Maximum number of seconds to wait for the command to finish",
                            # 最多等多少秒
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
        # 下面是给模型看的使用说明书（英文原文，因为模型对英文指令理解更稳）
        #
        # 【这段说明书的中文大意，按顺序分 4 块】
        #
        # 第 1 块：安全提示
        #   遇到 rm、sudo、chmod 777、format、del 这类危险命令，
        #   必须把 need_user_approve 设成 true，让用户先确认。
        #
        # 第 2 块：目录检查
        #   如果要创建新目录或新文件，先用 ls 确认父目录存在、位置对不对。
        #
        # 第 3 块：怎么读文件、怎么改文件（这是最要命的一块）
        #   - 读文件用 cat -n（带行号，方便后面按行号改）
        #   - 改文件用 sed：sed -i "起始行,结束行 c 新内容" 文件
        #   - 插入内容用 sed -i "行号 a 要插入的内容"
        #   - 搜索用 grep
        #   - 不确定行号时，先用 sed -n "起,止 p" 预览那几行
        #   ⚠️ 问题就在这儿：sed 是 Linux/macOS 的命令，Windows 上默认没有。
        #      虽然下面一句提了"Windows 请用 dir/type"，但模型多数时候还是会照着
        #      sed 的例子去写，在你机器上就会反复失败。
        #
        # 第 4 块：git 提交和 PR 的规范
        #   - 提交前并行跑 git status / git diff / git log，了解现状和提交风格
        #   - 提交信息要写清"为什么改"，1-2 句
        #   - 绝对不要改 git config，不要主动 push
        #   - 用 gh 命令处理 GitHub 相关的事
        return """
Executes a given bash command in a persistent shell session with optional timeout, user approval, and security measures.

Before executing the command, please follow these steps:
1. Security Check:
   - Always check if the command is potentially dangerous (e.g., rm, sudo, chmod 777, format, del, etc.)
   - If the command is dangerous or could cause system damage/data loss, you MUST set need_user_approve=true

2. Directory Verification:
   - If the command will create new directories or files, first use the ls command to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use ls to check that "foo" exists and is the intended parent directory

3. Read File Command:
   - When you need to read a file for the first time, always use `cat -n <file>` to display the contents with line numbers (default max 2000 lines)

4. Edit File Command:
   - Before updating any file, you must read the file content in the current conversation first
   - When updating content in an existing file, always use sed for replacement except when the updated content is very large:
     Example: `sed -i "start_line,end_line c new_content" <file>`
   - When inserting content into an existing file, use sed for insertion:
     Example: `sed -i "insert_line_number a insert_content" <file>`
   - When searching within files, always use grep:
     Example: `grep "search_pattern" <file>`
   - Before editing a file, use sed to preview the specific lines with their line numbers if you are unsure of the exact range. Make sure it’s the code you actually want to update.
     Example: sed -n "start_line,end_line p" <file>

Note: The above commands are for Linux/macOS systems. For Windows, use appropriate command-line equivalents (e.g., `dir`, `type`, PowerShell commands).

Usage notes:
  - The command and need_user_approve arguments are required.
  - You can specify an optional timeout in seconds (default 30, maximum 600). If not specified, commands will timeout after 30 seconds.
  - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.

Committing changes with git

When the user asks you to create a new git commit, follow these steps carefully:

1. You have the capability to call multiple tools in a single response. When multiple independent pieces of information are requested, batch your tool calls together for optimal performance. ALWAYS run the following bash commands in parallel, each using the Bash tool:
  - Run a git status command to see all untracked files.
  - Run a git diff command to see both staged and unstaged changes that will be committed.
  - Run a git log command to see recent commit messages, so that you can follow this repository's commit message style.
2. Analyze all staged changes (both previously staged and newly added) and draft a commit message:
  - Summarize the nature of the changes (eg. new feature, enhancement to an existing feature, bug fix, refactoring, test, docs, etc.). Ensure the message accurately reflects the changes and their purpose (i.e. "add" means a wholly new feature, "update" means an enhancement to an existing feature, "fix" means a bug fix, etc.).
  - Check for any sensitive information that shouldn't be committed
  - Draft a concise (1-2 sentences) commit message that focuses on the "why" rather than the "what"
  - Ensure it accurately reflects the changes and their purpose
3. You have the capability to call multiple tools in a single response. When multiple independent pieces of information are requested, batch your tool calls together for optimal performance. ALWAYS run the following commands in parallel:
   - Add relevant untracked files to the staging area.
   - Run git status to make sure the commit succeeded.
4. If the commit fails due to pre-commit hook changes, retry the commit ONCE to include these automated changes. If it fails again, it usually means a pre-commit hook is preventing the commit. If the commit succeeds but you notice that files were modified by the pre-commit hook, you MUST amend your commit to include them.

Important notes:
- NEVER update the git config
- NEVER run additional commands to read or explore code, besides git bash commands
- NEVER use the TodoWrite or Task tools
- DO NOT push to the remote repository unless the user explicitly asks you to do so
- IMPORTANT: Never use git commands with the -i flag (like git rebase -i or git add -i) since they require interactive input which is not supported.
- If there are no changes to commit (i.e., no untracked files and no modifications), do not create an empty commit
- In order to ensure good formatting, ALWAYS pass the commit message via a HEREDOC, a la this example:
<example>
git commit -m "$(cat <<'EOF'
   Commit message here.
   EOF
   )"
</example>

### Creating pull requests
Use the gh command via the Bash tool for ALL GitHub-related tasks including working with issues, pull requests, checks, and releases. If given a Github URL use the gh command to get the information needed.

IMPORTANT: When the user asks you to create a pull request, follow these steps carefully:

1. You have the capability to call multiple tools in a single response. When multiple independent pieces of information are requested, batch your tool calls together for optimal performance. ALWAYS run the following bash commands in parallel using the Bash tool, in order to understand the current state of the branch since it diverged from the main branch:
   - Run a git status command to see all untracked files
   - Run a git diff command to see both staged and unstaged changes that will be committed
   - Check if the current branch tracks a remote branch and is up to date with the remote, so you know if you need to push to the remote
   - Run a git log command and `git diff [base-branch]...HEAD` to understand the full commit history for the current branch (from the time it diverged from the base branch)
2. Analyze all changes that will be included in the pull request, making sure to look at all relevant commits (NOT just the latest commit, but ALL commits that will be included in the pull request!!!), and draft a pull request summary
3. You have the capability to call multiple tools in a single response. When multiple independent pieces of information are requested, batch your tool calls together for optimal performance. ALWAYS run the following commands in parallel:
   - Create new branch if needed
   - Push to remote with -u flag if needed
   - Create PR using gh pr create with the format below. Use a HEREDOC to pass the body to ensure correct formatting.
<example>
gh pr create --title "the pr title" --body "$(cat <<'EOF'
#### Summary
<1-3 bullet points>

Important:
- NEVER update the git config
""".strip()
        # .strip() 的作用：去掉这段字符串首尾的空行和空格，避免占没用的空间
