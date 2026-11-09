# =============================================================================
# 文件：src/tools/subagent/agent_spec.py
# 作用：定义「一种子 agent 长什么样」，以及它的 system prompt 怎么拼。
#
# 【2026-11-09 第 18 轮·简化版】这里原来还有一套 frontmatter 解析器
#   （_split_frontmatter / parse_agent_file / _parse_tools），用来读
#   `.thinker/agents/*.md` 和 `~/.thinker/agents/*.md`。
#   现在这两层目录整个去掉了：子 agent 只有内置这一种来源，定义直接写在
#   builtin_agents.py 里。解析器没有调用方就是死代码，一并删掉——
#   留着它只会让人以为"还能放 md 文件"，然后去找一个不存在的入口。
#
#   于是这个文件只剩两件事：
#     1. AgentSpec：一种 agent 的字段（名字、描述、人设、工具、模型）；
#     2. build_system_prompt：把「人设」拼成子 agent 真正收到的 system prompt。
#
# 为什么还要有 AgentSpec 这个类，而不是直接传 dict：
#   它被四个地方同时依赖（task 拼说明书、conversation 过滤工具、task 传模型、
#   /agents 展示）。有类型约束的话，加字段时改一处就够；dict 拼错键名不会报错，
#   只会表现为"某个功能悄悄不生效"。
# =============================================================================

from dataclasses import dataclass  # 只装字段的小类
from typing import Optional, Tuple  # 3.9 兼容（不用 `str | None`）

# 描述最多这么长（进 task 说明书时按这个长度截断）。
#   为什么需要上限：每个类型的描述都会进 `task` 的 tools 字段，
#   而 tools 字段**每次请求都要发**。四个类型各写 500 字，就是每轮白烧 2000 字。
#   160 字大约两句话，足够主 agent 判断"该不该派"。
MAX_DESCRIPTION_CHARS = 160


@dataclass(frozen=True)
class AgentSpec:
    """一种子 agent 的完整定义

    frozen=True：加载完就当成只读数据。谁都能改的话，
    某处顺手把 tools 改成 None，就会让一个只读 agent 突然拿到写权限，
    而这种 bug 在运行中几乎看不出来。
    """

    name: str
    description: str
    body: str  # 人设正文（system prompt 的主体）
    tools: Optional[Tuple[str, ...]]  # None = 全部工具（除 task）
    model: Optional[str]  # None = 继承全局模型

    def tools_text(self) -> str:
        """给人看/给模型看的工具说明

        为什么 None 要写「全部（除 task）」而不是「全部」：
          子 agent 永远拿不到 task，写「全部」会让主 agent 以为它还能再派活，
          进而写出「让 explore 再找个子 agent 去查」这种不可能执行的说明。
        """
        if self.tools is None:
            return "全部（除 task）"
        return "、".join(self.tools) if self.tools else "（无）"

    def model_text(self) -> str:
        """给人看的模型说明（None 统一显示成「继承」）"""
        return self.model or "继承"


def build_system_prompt(spec: AgentSpec) -> str:
    """拼出子 agent 真正收到的 system prompt = 人设正文 + 精简环境 + 边界

    为什么不用 PromptManager 生成完整系统提示词：
      主 agent 的提示词里有项目记忆、自动记忆索引、系统规则、todo 提醒……
      整套塞给子 agent 有两个问题：
        1. 白烧 token（子 agent 的任务通常很窄，一份长提示词比任务本身还贵）；
        2. 越权：它看到「可以 /memory」「可以写 THINKER.md」这些规则，
           会以为自己也有这些权责，然后去做不该做的事。
      所以这里只给三样它真的需要知道的：在哪个目录、什么系统、今天几号。

    为什么环境只取三行、不取「是否 git 仓库」：
      子 agent 的活是「找东西 / 看代码 / 出方案」，git 状态对判断没帮助，
      而每次拼提示词都要往上翻目录找 `.git`，属于白花的开销。

    Claude Code 的做法是同一套：子 agent 只拿到自己的提示词 + 基本环境信息，
    不拿完整的 Claude Code 系统提示词。这不是省事，是刻意的隔离。
    """
    # 复用现成的三个小函数，而不是自己写 os.getcwd()：
    # 主 agent 和子 agent 看到的「当前目录/系统/日期」必须口径一致，
    # 否则会出现「主 agent 说在 D 盘，子 agent 说在 C 盘」这种对不上的情况。
    from core.prompt.enviroment import get_working_directory, get_platform, get_current_date

    env_lines = [get_working_directory(), get_platform(), get_current_date()]

    # 边界统一在这里追加，而不是让每个内置定义自己写一遍。
    # 理由：这几句话对所有子 agent 都成立，写进十几份人设迟早有一份漏掉；
    # 后两道闸（清单里没有 task、执行层拦截）虽然也在，但提示词这一层
    # 说清楚能省掉一次无效尝试——模型看到「你不能派活」就不会去试。
    boundary = (
        "你是被主 agent 派来处理一个具体任务的子 agent，做完把结论交回去即可。\n"
        "- 你不能把任务再派给别的 agent（你也没有 task 工具）。\n"
        "- 只做任务说明里要求的事，不要顺手改别的文件。\n"
        "- 汇报里出现的文件路径一律用绝对路径。"
    )

    return f"{spec.body}\n\n【当前环境】\n" + "\n".join(env_lines) + f"\n\n【边界】\n{boundary}\n"
