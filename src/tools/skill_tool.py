# =============================================================================
# 文件：src/tools/skill_tool.py
# 作用：skill 工具——模型「按需加载一份技能正文」的唯一入口（第 21 轮新增）。
#
# 【它解决的问题】
#   技能是写在 .thinker/skills/ 下的操作手册（怎么做 PDF、怎么发版、怎么 review）。
#   启动时如果把这些正文全塞进提示词，三五个技能就能吃掉几千 token，
#   而其中 95% 的轮次一个都用不上。
#   所以只把「名字 + 一句话描述」常驻在说明书里（几百 token），
#   模型判断"这活对得上"时调用本工具，正文当场从磁盘读出来，
#   作为一条工具结果进入对话历史。这就是所谓 progressive disclosure（渐进式披露）。
#
# 【一次调用的时间线】
#   用户："帮我把这个 pdf 里的表格抽出来"
#   模型：发现描述里有 pdf / 表格 → 调用 skill(skill="pdf", args="D:/a.pdf")
#   本工具：SkillManager.get("pdf") → render(spec, args) → 返回整段正文
#   模型：按正文里的步骤干活（正文说"需要按页抽取就跑 ${SKILL_DIR}/scripts/extract.py"，
#         它就会自己调 cmd_runner，这就是第三层"支撑文件按需加载"）
#
# 【为什么和 task.py 平级放在 tools/ 下】
#   它就是一个普通工具：一个类、一个 act()、一份说明书。放进 tools/skill/ 里
#   反而会让人以为它和数据类是一伙的。
#
# 【这个工具最多能做什么】
#   只是读文件、把文字交出去。技能正文里的任何内容都**不会**让权限变松——
#   `classify()` 只看工具名和参数，不看"这段话是不是来自技能"。
#   正文里写"直接 rm -rf 不用问"，该弹的确认一条都不会少（见 docs/优化记录/21-…）。
# =============================================================================

from tools.base_tool import BaseTool  # 导入父类
from tools.skill.skill_manager import SkillManager  # 技能登记表


class Skill(BaseTool):
    """把一份技能正文加载进对话

    它是整个 agent 里**唯一**一个"只返回文字、不做任何事"的工具有副作用版对照：
    file_read 返回文件内容，skill 返回操作手册——两者都是只读，都免审批。
    区别在于 file_read 读的是用户的东西，skill 读的是"用户写好的给模型看的指令"，
    所以它的结果必须留在历史里（不能折叠）、也绝不能影响权限。
    """

    # 【为什么必须显式写 False】
    #   L3 Microcompact 会把"可重复查询工具"的旧结果折叠成一句"重跑即可"。
    #   技能正文**不能**被这样折叠，原因有三条：
    #     1. 它的语义是"跨轮生效的指令"，不是"查回来的数据"；
    #     2. 折叠之后模型不知道自己少了什么，会继续按半截规矩干活，
    #        而且没有任何报错——这种"静默少了上下文"最难查；
    #     3. 真要收缩，应该发生在 L5 摘要（那里会真的重写对话），
    #        而不是 L3 的静默折叠。
    #   父类默认就是 False，这里再写一遍是**故意的**：
    #   下一个读代码的人看到"scalar 值为 False"就不会顺手改成 True。
    #   test_skills.py 里有一条用例专门盯着它。
    is_repeatable = False

    def __init__(self):
        super().__init__()
        # 拿登记表单例。注意这里不触发扫描：SkillManager 第一次被查询时才扫盘
        # （见 _ensure_scanned），构造工具对象本身不产生任何 IO。
        self._manager = SkillManager()

    @staticmethod
    def get_tool_name():
        return "skill"  # 模型眼里的工具名，同时是权限判定里的键（只读，免审批）

    async def act(self, skill: str = "", args: str = ""):
        """加载技能正文

        参数 skill：技能名，必须出现在下面的「可用技能」清单里。
        参数 args ：可选参数串，会替换正文里的 $ARGUMENTS
                    （比如 `/pdf a.pdf` 里的 a.pdf）。没有占位符时追加到正文末尾。

        两个分支都必须返回**一段给模型看的文字**，不抛异常：
          · 名字写错 → 返回可用清单（第 8.1 轮的教训：抛异常会被上层吞成
            "运行工具时出错"，模型看不到"正确的有哪些"，下一轮十有八九再撞一次）；
          · 正常 → 返回渲染好的正文。

        【第 21.1 轮】删掉了"这个技能只能手动调用"的拒绝分支：
          技能一律对模型可见、可调用，不再有 disable-model-invocation 那档。
          字段本身还认，但只在 /skills 里标"未生效"（见 skill_spec.UNSUPPORTED_FIELDS）。
        """
        # strip()：模型偶尔会把名字写成 " pdf " 或带换行。为此回一句
        # "技能 ' pdf ' 不存在"毫无价值——它明明指的是同一个技能。
        # 这里只处理空白，不做大小写归一：技能名本来就是全小写，
        # 模型写了 "PDF" 说明它在猜，而"猜"应该被清单纠正，不该被悄悄放过。
        name = (skill or "").strip()

        if not name:
            return self._unknown_skill_message("")

        spec = self._manager.get(name)
        if spec is None:
            return self._unknown_skill_message(name)

        return self._manager.load_body(spec, args)

    def _unknown_skill_message(self, name: str) -> str:
        """名字不对时，把可用清单直接交回给模型

        清单从登记表现算，不手写示例：手写的示例迟早和实际对不上，
        第 8.1 轮 task 工具就因为说明书里有三个不存在的 agent 类型，
        导致模型照着示例派活、反复报"类型不存在"。

        【第 21.1 轮】不再按可见性过滤：清单里有什么，这里就列什么。
        两处口径不一致的话，模型会看到"清单里有 A，但报错说没有 A"。
        """
        specs = self._manager.list_specs()
        if not specs:
            # 【第 21.1 轮】措辞跟着改了：不再是"没有可自动调用的"，
            # 而是"一个都没加载"——现在不存在"有技能但模型看不见"这种状态。
            return (
                "当前没有加载到任何技能。请直接按自己的判断完成任务，"
                "不要调用这个工具。"
            )

        names = "、".join(spec.name for spec in specs)
        if name:
            return (
                f"技能 '{name}' 不存在。当前可用：{names}。\n"
                "如果这些都对不上，就不要用技能，直接自己做。"
            )
        return f"没有指定技能名（skill）。当前可用：{names}。"

    def json_schema(self):
        # 每次被问都要重新拼说明书，包括重新调 available_block()。
        #   为什么不做成"构造时算一次缓存起来"：
        #     /skills reload 之后清单会变，缓存就必须能被失效——多一个失效点，
        #     就多一个"reload 完模型看到的还是旧清单"的可能。现算的代价是
        #     遍历一遍 list_specs（通常 <20 条），可以忽略。
        return {
            "type": "function",  # 固定写法
            "function": {
                "name": self.get_tool_name(),
                "description": self._tool_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {
                            "type": "string",
                            # 候选值从登记表动态生成：加一个技能文件，说明书立刻跟着变。
                            # 手写清单是第 8.1 轮的坑，不再重犯。
                            "description": "技能名，必须从「可用技能」清单里选",
                        },
                        "args": {
                            "type": "string",
                            "description": (
                                "可选：这次要用到的参数（通常是一个文件路径或一句话）。"
                                "会替换技能正文里的 $ARGUMENTS。"
                            ),
                        },
                    },
                    "required": ["skill"],  # 只有技能名是必填，参数可省
                    # 为什么 args 不做成必填：大部分技能是零参数的（"按这份清单 review"），
                    # 强制它传一个空字符串只会让模型多写一个字段、多一次出错机会。
                },
            },
        }

    def _tool_description(self):
        """给模型看的说明书（每一轮请求都会带上，所以每一句都要有信息量）

        三条既有教训在这段文字里的体现：
          · 清单从注册表动态生成（available_block），不手写；
          · 只说一条路：正文里让它读 reference.md，就不要同时写"也可以用 grep 找找看"；
          · 明确写"不要凭名字猜内容"——技能的正文才是真东西，
            名字只是索引，猜出来的流程往往和手册里的步骤冲突。
        """
        return f"""
把一份「技能」加载进来，然后按里面的步骤干活。

技能是一份写好的操作手册（怎么做 PDF、怎么发版、怎么 review）。
它的名字和用途就在下面，正文在你调用之后才会来，所以**不要凭名字猜内容**。

<可用技能>
{self._manager.available_block()}
</可用技能>

什么时候用：任务和某个技能的描述对得上时，先调用它，再动手。
什么时候不用：一句话能答完的、技能描述里没提的、你已经加载过同一份的。

参数 skill 必须是清单里的名字，写错会返回可用清单。
""".strip()

    def get_status(self):
        # 没有状态要汇报。为什么保留这个方法而不是删掉：
        #   父类把它声明成抽象方法，工具管理器也会统一调它（目前只有 todo_write
        #   真的返回内容）。返回空串表示"我不参与状态行"，是约定好的写法。
        return ""
