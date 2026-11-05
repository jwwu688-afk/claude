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

import subprocess  # subprocess 库：让 Python 能去调用系统的命令（相当于帮你敲命令行）
from tools.base_tool import BaseTool  # 导入父类


class CmdRunner(BaseTool):
    def __init__(self):
        super().__init__()  # 调用父类的初始化（这里其实什么都没做）

    @staticmethod
    def get_tool_name():
        return "cmd_runner"  # 这是它在模型眼里的名字

    async def act(self, command="", timeout=30):
        # 真正的执行入口。command 是要跑的命令，timeout 是最多等几秒
        if not command:  # 如果命令是空的
            return "No command provided"  # 直接告诉他"你没给命令"

        try:  # 试：跑命令
            result = subprocess.run(
                # subprocess.run 会开一个子进程去执行命令，然后等它结束
                command,  # 要执行的命令字符串
                shell=True,  # 让系统用 shell 来解析这串命令（这样才能用管道 | 和重定向 >）
                capture_output=True,  # 把命令的输出抓下来，而不是直接打到屏幕上
                text=True,  # 把输出当成文本处理（而不是一堆字节），这样才好拼字符串
                timeout=30  # 最多等 30 秒，超时就强制结束
                # ⚠️ 注意：这里写死了 30，上面那个 timeout 参数根本没被用上。
                #    所以模型传 timeout=120 也照样 30 秒就断。
            )

            if result.returncode == 0:  # returncode 是"退出码"，0 表示成功（这是命令行世界的约定）
                if result.stdout.strip():  # 如果标准输出（正常结果）去掉空白后还有内容
                    return result.stdout  # 就把结果返回给模型
                else:  # 命令成功了但什么都没输出
                    return "Command executed successfully and no return"  # 告诉他"跑成功了，但没输出"
            else:  # 退出码不是 0，说明命令出错了
                return f"Error: {result.stderr}"  # 把错误输出（stderr）返回给模型
        except subprocess.TimeoutExpired:  # 如果等超时了
            return "Command timed out"  # 告诉他"超时了"
        except Exception as e:  # 其他任何意外
            self.status = "error"  # 记一下状态（不过父类里其实没定义 status 这个属性）
            return f"cmd_runner Exception: {str(e)}"  # 返回错误信息

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
  - You can specify an optional timeout in milliseconds (up to 120 seconds). If not specified, commands will timeout after 30 seconds.
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
