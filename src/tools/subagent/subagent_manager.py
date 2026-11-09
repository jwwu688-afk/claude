# =============================================================================
# 文件：src/tools/subagent/subagent_manager.py
# 作用：子 agent 的登记表。四种内置类型，全程序共用一份。
#
# 【2026-11-09 第 18 轮·简化版】删掉了用户级（~/.thinker/agents）和项目级
#   （<cwd>/.thinker/agents）两层目录，也删掉了 frontmatter 解析。
#   现在的模型只有一层：内置。
#
#   为什么砍掉目录：
#     1. 需求是"所有人都能用的一套内置 agent"，不需要每人再维护一份；
#     2. 三层加载带来三个概念（优先级、同名覆盖、来源标注）和一堆边界
#        （坏文件隔离、编码、改完要重扫），而维护这些定义的只有一个人；
#     3. 目录不存在时它本来就不生效，等于"代码里留着两条永不执行的路"。
#   想要第五种 agent 就直接在 builtin_agents.py 加一个 AgentSpec 常量——
#   代价是改代码，好处是没有"为什么我改了没生效"这类问题。
#
# 为什么还是单例：Task / Conversation / ToolManager / HistoryManager 全是单例，
#   这里保持一致。真要并行子 agent 得先把这一整套拆掉（见 ROADMAP #10）。
# =============================================================================

import re  # 兼容接口里解析 "a, b, c" 这种写法
from typing import Dict, List, Optional

from tools.subagent.agent_spec import AgentSpec, build_system_prompt
from tools.subagent.builtin_agents import BUILTIN_AGENTS


def _normalize_tools(raw) -> Optional[tuple]:
    """把 "全部" / "a, b, c" 归一化成工具元组（None = 继承全部）

    为什么还留着这个：`register_subagent_prompt()` 是第 8.1 轮的老接口，
    测试和临时脚本会用它造一个假类型。它接受字符串，所以需要这一步解析。
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if value in ("", "*", "all", "ALL", "全部"):
        return None
    names = []
    for piece in re.split(r"[,，、;；]", value):  # 中英文分隔符都认
        name = piece.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


class SubagentManager:
    _instance = None  # 唯一实例
    _initialized = False  # 是否已初始化

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SubagentManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        # 构造即加载：内置定义是代码的一部分，不需要读盘、也不用等工具表注册完。
        #   为什么用 dict 而不是 list：主 agent 传进来的 subagent_type 是字符串，
        #   按名字查是最高频的操作（每次派活都走一遍），O(1) 比遍历合适。
        self._specs: Dict[str, AgentSpec] = {spec.name: spec for spec in BUILTIN_AGENTS}
        self._initialized = True

    # -------------------------------------------------------------------------
    # 查询
    # -------------------------------------------------------------------------
    def list_specs(self) -> List[AgentSpec]:
        """按登记顺序返回所有定义（顺序由 builtin_agents.BUILTIN_AGENTS 决定）"""
        return list(self._specs.values())

    def get_spec(self, name: str) -> AgentSpec:
        """按名字取定义；取不到抛 ValueError

        为什么这里可以抛：调用方（Task.act）已经先做过 list_types() 校验，
        走到这里说明名字一定合法。真抛了就是代码 bug，不该被当成"用户写错了"。
        """
        if name not in self._specs:
            raise ValueError(f"子 agent 类型 '{name}' 不存在")
        return self._specs[name]

    def list_types(self) -> List[str]:
        """所有类型名（task 的候选值、错误提示、/agents、测试都从这里取）"""
        return list(self._specs.keys())

    def describe(self, prompt_type: str):
        """取某个类型的（用途, 可用工具），给 task 说明书用

        取不到返回 ("", "未说明") 而不是抛异常：它只用于拼说明书，
        拼不出来顶多难看，不该把整轮对话打断。
        """
        spec = self._specs.get(prompt_type)
        if spec is None:
            return "", "未说明"
        return spec.description, spec.tools_text()

    def get_subagent_prompt(self, prompt_type: str) -> str:
        """取某个类型的完整 system prompt（人设正文 + 环境 + 边界）

        为什么在这里拼、而不是返回裸正文：调用方（conversation.start_task）
        需要的是"能直接塞进 system 消息的整段文字"。环境信息每次都要重算
        （日期会变），放在调用点会让 start_task 多背一份拼装逻辑。
        """
        return build_system_prompt(self.get_spec(prompt_type))

    # -------------------------------------------------------------------------
    # 兼容接口
    # -------------------------------------------------------------------------
    def register_subagent_prompt(self, prompt_type, prompt, description="", tools="全部"):
        """手工登记/覆盖一种类型（第 8.1 轮的接口，保留）

        什么时候用它：测试和临时脚本想造一个假类型，不想新增内置定义。
        注意它**会覆盖**同名内置定义，而且只影响当前进程（不落盘）。
        """
        self._specs[prompt_type] = AgentSpec(
            name=prompt_type,
            description=description,
            body=prompt,
            tools=_normalize_tools(tools),
            model=None,
        )
