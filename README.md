# Thinker

**用 Python 从零复刻 Claude Code 的终端编程助手 —— 一个把 Agent 内部机制摊开来讲的教学项目。**

Thinker 跑在终端里，能读代码、改文件、执行命令、自己决定下一步做什么。它和 Claude Code、Cursor Agent 是同一类东西（terminal coding agent），区别在于：**这里没有黑盒**。主循环怎么转、上下文怎么压、权限怎么判、记忆怎么存，每一处都在代码注释里写清了「为什么这么写、踩过什么坑」。

适合三类人：

- 想搞明白「Agent 到底是怎么跑起来的」，光看框架文档不过瘾的人
- 想自己动手改一个 Agent、但不想从零搭脚手架的人
- 想给自己的项目配一个能读代码的终端助手的人

项目要求注释占有效行的 60%~75%（标杆 `core/session.py` 是 76%），`docs/优化记录/` 里有 49 份逐轮改动记录，包含每次踩坑的根因分析。

---

## Thinker 写的 Demo：逐云 · 飞行作战

<img src="demo/plane/preview-play.gif" width="760" alt="逐云 · 飞行作战（实际游玩画面）">

零依赖的浏览器纵向飞机大战。**代码是 Thinker 自己在这个仓库里写出来的**——建文件、写战斗逻辑、改测试、跑回归，人只负责点头。双击 [`demo/plane/index.html`](demo/plane/index.html) 就能开打，不用装、不用构建。

`WASD` / 方向键移动 · 自动开火 · `Space` 放炸弹 · `P` 暂停。战斗逻辑与渲染分离，`node demo/plane/test/game.test.js` 跑规则回归；3 条命、护盾、四种补给、20 秒一个波次，35 秒后出空中堡垒。

<details>
<summary>展开看开始界面</summary>

<img src="demo/plane/preview-home.png" width="760" alt="逐云 · 飞行作战（开始界面）">

</details>

---

## 目录

- [它是什么 / 不是什么](#它是什么--不是什么)
- [能做什么](#能做什么)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [内置工具](#内置工具)
- [权限与安全](#权限与安全)
- [斜杠命令](#斜杠命令)
- [技能（Skills）](#技能skills)
- [记忆系统](#记忆系统)
- [上下文五层压缩](#上下文五层压缩)
- [接入 MCP 外部工具](#接入-mcp-外部工具)
- [生命周期 hook](#生命周期-hook)
- [目录结构](#目录结构)
- [验证](#验证)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [文档索引](#文档索引)
- [License](#license)

---

## 它是什么 / 不是什么

**是：**

- 一个真的能干活的 Agent。不是 demo，能连续几十分钟改一个项目：读文件 → 定位问题 → 改代码 → 跑测试 → 看报错 → 再改。
- 一个可读的实现。21 轮迭代、49 个 Python 文件，单层主循环，没有框架封装，`asyncio` + `openai` SDK + `rich` 就到顶了。
- 一个能当模板用的底座。想加工具就加一个类，想加命令就 `register()` 一行。

**不是：**

- 不是 Claude Code 的替代品。功能覆盖度大概六七成，缺 Plan mode、Hooks、WebSearch、插话队列。
- 不是开箱即用的商业产品。它需要你自己配 API Key，界面是终端，出错信息是给开发者看的。
- 不是跨平台完美。**Windows 是主要开发和测试平台**；macOS / Linux 能跑，但 Tab 补全、方向键、Esc 打断这些键盘增强会降级成普通 `input()`（依赖 `msvcrt`）。

---

## 能做什么

| 能力 | 说明 |
|---|---|
| **多轮工具调用** | 单层主循环（已去递归）。模型提申请 → 执行 → 结果回灌 → 继续，直到它不再要工具 |
| **流式输出** | 逐字打印，不用等整段生成完 |
| **内置工具** | 读写改文件、按名/按内容搜索、执行命令、待办清单、子 Agent、上下文裁剪、技能加载（有技能时才登记） |
| **MCP 外部工具** | 标准 MCP 协议，`~/.thinker/mcp.json` 里配一行就能接任意 MCP Server（兼容 `mcp` 库 1.x / 2.x） |
| **权限分级** | 四档工作模式（默认 / 计划 / 接受编辑 / 自动，Shift+Tab 循环）；只读免问、写操作必问、22 条危险命令正则命中则"回车=拒绝"；**模型无法给自己放行** |
| **五层上下文压缩** | 从"零成本剪枝"到"调模型摘要"分五档，压力越大手段越重，绝大多数情况不花额外 token |
| **项目记忆** | `THINKER.md`（人写，全文注入）+ `MEMORY.md`（模型自己写，注入索引），支持全局/项目/父目录分层 |
| **技能** | `.thinker/skills/` 下的操作手册：描述常驻、正文按需加载；由模型自己判断何时调用，**不占用斜杠命令** |
| **会话持久化** | 每条消息即时落盘 JSONL，Ctrl+C 也不丢；`--continue` 接上次，`/resume` 挑历史 |
| **随时打断** | Esc 打断当前生成，不用杀进程 |
| **斜杠命令** | 12 条本地命令（`/cost` `/memory` `/resume` `/mode` …），不发给模型、不占上下文、不计费；技能不占名额 |
| **Tab 补全** | 输入 `/` 后按 Tab 补全命令名 |
| **只读并发** | 连续多个只读工具调用自动 `gather` 并发（文件 IO 已真正丢进线程池） |
| **成本可见** | `/cost` 看累计花费、token、缓存命中率、上下文占用 |

---

## 快速开始

### 0. 环境要求

- **Python 3.9+**（开发和测试都在 3.12 / 3.13 上做的；3.8 会在导入时 `TypeError`，别试）
- 一个 **OpenAI 兼容的接口**：OpenAI 官方、OpenRouter、各家中转站、本地 vLLM / Ollama 都行
- 模型需要支持 **function calling（工具调用）**，否则它只会聊天，不会动手

### 1. 安装

```bash
git clone git@github.com:jwwu688-afk/thinker.git
cd thinker   

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

> **推荐 `-e`（editable）**：这是个教学项目，你会一边读一边改，editable 装完改代码立刻生效。
> 直接 `pip install .` 也能装（`pyproject.toml` 已声明 `py-modules`，普通安装不会再缺 `main` 模块），但改源码要重装。

装完确认一下：

```bash
thinker --version
# thinker v0.10.1
```

### 2. 配置

在**仓库根目录**建 `.env`（`python-dotenv` 从模块所在目录向上找，所以这个位置固定）：

```bash
cp .env.example .env
```

最少填三项：

```dotenv
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=anthropic/claude-sonnet-4
```

`.env.example` 里每个变量都有注释，说明它影响什么、为什么默认值是这个。

> **只认仓库根目录的 `.env`**：`python-dotenv` 是从「代码文件所在目录」向上找的，不是从你当前所在的目录找。
> 所以你在 A 项目里跑 thinker，读的仍然是 `thinker/.env` —— 换项目不用重新配。
> 反过来说，如果你用 `pip install .`（非 editable）装到了 site-packages，这个 `.env` 就找不到了，得改用系统环境变量。

### 3. 启动

```bash
cd /path/to/你的项目      # ← 关键，别在用户主目录启动
thinker
```

> **为什么必须 `cd` 到项目目录**
> 项目记忆、自动记忆目录、会话归属、文件工具的默认搜索起点，全都按「启动时的 cwd」算。
> 从 `C:\Users\<你>` 启动等于把整个家目录当成一个项目：`/init` 会生成一份描述不了任何项目的 `THINKER.md`，
> 模型说的相对路径也会被补到错的地方。程序会提醒你，但根治办法只有先 `cd`。

启动后直接说人话：

```
> 看一下 src/core/conversation.py 的主循环，用三句话讲清楚它每一轮做了什么
> 给 FileGrep 补一段中文注释，说明 include 参数怎么用
> 跑 node demo/plane/test/game.test.js，挂了的话定位并修掉
```

第一次让它改文件时会弹确认，回车即同意；想拒绝就直接输入理由，那句话会带回给模型。

### 常用命令行参数

| 参数 | 作用 |
|---|---|
| `thinker` | 开新会话 |
| `thinker --continue` / `-c` | 接着**本项目**最近一次会话聊 |
| `thinker --version` / `-V` | 打印版本 |
| `thinker mcp add\|remove\|list\|get` | 增删查 MCP 服务器，改完退出（见[接入 MCP 外部工具](#接入-mcp-外部工具)） |
| `thinker --init` | **已移除**，会提示你改用会话内的 `/init` |

---

## 配置项

全部通过 `.env` 或环境变量设置，都有合理默认值，**只有前三项必填**。

### 必填

| 变量 | 说明 |
|---|---|
| `OPENAI_API_KEY` | 接口密钥 |
| `OPENAI_BASE_URL` | 接口地址，必须 OpenAI 兼容（`/v1/chat/completions`） |
| `OPENAI_MODEL` | 模型名，必须支持 function calling |

### 可选

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPENAI_SUMMARY_MODEL` | 空 | 压缩上下文时用的摘要模型。留空沿用主模型。**配个便宜小模型能明显省钱** |
| `ENABLE_PROMPT_CACHE` | 自动 | `1` 强制开 / `0` 强制关 / 空=自动判断（地址含 `anthropic` 或模型名含 `claude` 才开）。OpenAI 官方接口不认这个字段，所以有自动判断 |
| `MODEL_MAX_TOKENS` | `200` | **单位是 k**，即 20 万 token 的上下文窗口。压缩比例都按它算，配错会导致压缩时机全错 |
| `MAX_TOOL_OUTPUT_CHARS` | `30000` | 单条工具结果进历史时的兜底上限（字符）。必须 ≥ 各工具自己的业务上限，否则业务上限是假的 |
| `MICROCOMPACT_TOOL_OUTPUT_CHARS` | `8000` | L3 折叠阈值：旧工具结果超过这个长度才值得折叠 |
| `COMPRESS_THRESHOLD` | `0.8` | 五档压缩的起点 |
| `SNIP_THRESHOLD` | `0.85` | L2 → L3 分界 |
| `MICRO_THRESHOLD` | `0.90` | L3 → L4 分界 |
| `COLLAPSE_THRESHOLD` | `0.95` | L4 → L5 分界 |
| `COMPACT_KEEP_RECENT` | `10` | L5 摘要时最近多少条消息原样保留。调小压得更狠、但模型可能立刻重读刚压掉的文件；调大保留更多细节，但 95% 可能压不到安全水位 |
| `THINKER_STREAM` | `plain` | 设成 `live` 走 rich Live 渲染（超长回复更顺，但宽度变化会留残影） |
| `THINKER_PLAIN_INPUT` | 空 | 设 `1` 关闭自管行编辑，退回原生 `input()`（补全/方向键失效，用于排查输入问题） |
| `THINKER_NO_SPINNER` | 空 | 设 `1` 关掉等待动画（日志重定向时用） |
| `THINKER_INPUT_PADDING` | `4` | 输入行下方留几行空白，同时也是"滚轮能往上滚几行"。留白后滚轮能把输入行滚到屏幕中间，滚多了 conhost 会把视口拉回光标处；嫌占屏幕就调小，设 `0` 关掉 |

> 四个压缩阈值必须满足 `0.80 ≤ 0.85 ≤ 0.90 ≤ 0.95`，否则区间会重叠，落到哪一档变成看运气。

---

## 内置工具

模型看到的工具名就是下表这些，全部在 `src/tools/` 下，每个都是 `BaseTool` 的子类。

| 工具 | 作用 | 要点 |
|---|---|---|
| `file_read` | 读文件 | 带行号，默认最多 2000 行，用 `offset`/`limit` 翻页 |
| `file_write` | 新建 / 整体覆盖 | 写之前要求先读过（防盲写） |
| `file_edit` | 精确替换一段 | 按 `old_string` 匹配，返回 diff；`replace_all` 可全替换 |
| `file_glob` | 按**文件名**找 | `**/*.py` 这类模式 |
| `file_grep` | 按**内容**搜索 | 支持 `include` 限定文件类型 |
| `cmd_runner` | 执行 shell 命令 | 输出超长会截断；命中查找类命令时会提示改用 `file_glob` |
| `todo_write` | 待办清单 | 让模型把多步任务列出来，减少中途跑偏 |
| `task` | 派子 Agent | 独立上下文跑一个子任务，只回结论 |
| `smart_context_cropper` | 裁剪上下文 | 主动丢弃无关历史 |

**路径不设边界是刻意设计**：五个文件工具和 `cmd_runner` 都能操作任意路径。原因是曾经加过"工作区边界"，只拦 `file_*` 拦不住 shell，模型遇到越界就改用 `cmd_runner` 绕过去——对一个愿意绕的模型，那道门是软的；对守规矩的模型，它只是把"读个桌面文件"变成"先改配置再重启"。风险改由[权限层](#权限与安全)兜。

---

## 权限与安全

**这是本项目和很多玩具 Agent 最重要的区别：判断权不在模型手里。**

早期版本让模型在参数里写 `need_user_approve`，它为了"少打扰用户"经常给 `del /s /q` 也标 `false`。现在改成三层判定，顺序固定，集中在 `src/core/permissions.py`：

```
① 命中危险规则（22 条正则）  → 必须问，且回车默认 = 拒绝（模型写 false 也不管用）
② 只读工具 / 白名单命令       → 不问，直接执行
③ 其余一切                   → 必须问
```

- **只读工具**：`file_read` `file_glob` `file_grep` `todo_write`
- **只读命令白名单**：`dir` `ls` `cat` `git status` `git diff` `git log` `python --version` 等。整条命令必须都是只读，出现 `|` `&&` `;` `>` `<` `` ` `` 一律不算
- **危险规则举例**：`rm -rf`、`del /s /q`、`format`、`diskpart`、fork 炸弹、`git push --force`、`git reset --hard`、`curl ... | sh`、`shutdown`、`pip uninstall`、`reg delete`
- **记忆目录免审批**：写入 `~/.thinker/projects/<项目>/memory/` 不弹确认（那是它自己的工作区）

模型只能**收紧**（对只读工具也要求确认），不能**放松**。`cmd_runner` 的情况下模型的 `need_user_approve` **完全不生效**——代码是否危险由代码判断，不由想执行它的那个东西判断。

### 工作模式（四档）

判定之上还有一层"模式"，决定这一整轮按什么口径问。Shift+Tab 循环（输入框为空或非 `/` 开头时按 Tab 等效），也可以 `/mode` 直接切：

| 档位 | 状态行颜色 | 行为 |
|---|---|---|
| `default` | 灰 | 每个写操作、每条命令都要确认（默认，行为与第 9 轮完全一致） |
| `plan` | 浅绿 | 只能读和查。工具照常下发，写工具与写命令在执行前被拦（第 33.5 轮起不再剪清单） |
| `accept` | 紫 | `file_write` / `file_edit` 直接执行，命令仍要确认 |
| `auto` | 金黄 | 全部直接执行，不再询问（高危命令只打一行提示，不阻断） |

`plan` 和 `auto` 真正改变的是**能不能执行**：plan 下模型看得到写工具、调用会被拦（和 Claude Code 一致，这样它写方案时能说清"用哪个工具改哪一处"），auto 下连危险规则都不参与判定。

MCP 工具在 plan 下的处理也照 Claude Code：服务器用 `annotations.readOnlyHint` 声明了只读就直接放行，没声明就问你一次（回车即执行）。

> conhost 传统模式下 Shift+Tab 和 Tab 送到程序里是同一个 `\t`（控制台不告诉字符流"按没按 Shift"），所以快捷键实际按 Tab 判：**输入以 `/` 开头时是补全，其余情况是切模式**。`/mode` 在任何终端都有效。

当前模式**不写进系统提示词**——那会让前缀缓存每切一次就整段失效；它每轮附在尾部的 `<mode>` 提醒里，而系统提示词里只有一段永远不变的"有哪几档"。

> 说清楚风险：这是一道防手滑的闸，不是沙箱。它拦不住一个**存心**（或被提示注入）绕路的模型。真正隔离请用容器 / 虚拟机 / 受限账号跑。`auto` 档等于把这道闸整个抬起来。

---

## 斜杠命令

斜杠命令在**本地**处理——不发给模型、不进上下文、不计费。它们必须在消息进历史**之前**被截住，否则命令本身会变成一条付费消息，而且模型给不出正确答案（它不知道你花了多少钱）。

> 技能**不在**这张表里：它由模型按需加载（`skill` 工具），加载时才把正文当成"你说的这句话"发给模型。所以上面那句话对表里每一条都成立——这张表里没有任何一条会花钱。

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令 |
| `/cost` | 本次会话花费、token、缓存命中率、上下文占用 |
| `/memory [文件名]` | 看模型记了什么：不带参数看索引，带参数看某条正文 |
| `/resume` | 列出本项目历史会话，输编号切过去 |
| `/init [--global] [--force] [--here]` | 生成 / 更新 `THINKER.md` 骨架 |
| `/compact` | 立刻压缩一次上下文（不等 80%，会调一次摘要模型，**花钱**） |
| `/clear` | 清空上下文并开一个新会话（旧会话文件保留，可 `/resume` 回去） |
| `/clean` | 清理旧会话存档：列出占用 → 选「压缩归档」（默认，数据不丢）或「删除」（二次确认）。见[数据存在哪](#数据存在哪) |
| `/agents` | 列出内置子 agent 类型、工具白名单与模型 |
| `/mode [default\|plan\|accept\|auto]` | 查看或切换工作模式（四档）。不带参数时列出全部并标出当前档 |
| `/mcp` | MCP 服务器运行时状态：哪些已连接、各自几个工具、谁失败/被跳过 |
| `/model` | 换模型：列出接口支持的模型让你选。只影响本次进程，不写回 `.env` |
| `/skills [reload\|install <地址>]` | 列出已加载的技能、来源与扫描问题；`install` 从 GitHub 地址或本地目录装新技能 |

`/init` 的几个开关：`--global` 写全局记忆（`~/.thinker/THINKER.md`）；`--force` 覆盖已存在的文件；`--here` 允许在用户主目录生成（默认拒绝）。

子 agent 只有内置这一种来源：四种类型写在 `src/tools/subagent/builtin_agents.py`，各自带工具白名单与模型。加第五种就改那个文件（不用动其它模块），细节见 [18 轮文档](docs/优化记录/18-第18轮-子Agent类型化.md)。

输入 `/` 后按 **Tab** 补全：唯一匹配直接补上，多个匹配先补到公共前缀、再按一次列候选。

---

## 技能（Skills）

技能是**写在文件里的操作手册**，用来装"这类活该怎么做"——怎么做 PDF、怎么发版、怎么 review。
它和 `THINKER.md` 的分工是：

| | 放什么 | 什么时候进上下文 |
|---|---|---|
| `THINKER.md` | 永远要遵守的规矩 | 每轮全文注入 |
| 技能 | 某类任务的操作步骤 | 只有名字 + 一句话描述常驻；正文**用到时才读** |

```text
.thinker/skills/pdf/SKILL.md        项目级（进仓库，跟项目走）
~/.thinker/skills/pdf/SKILL.md      全局（跟人走，跨项目）
.thinker/skills/pdf.md              单文件形态也行
```

同名时**项目覆盖全局**（和 `THINKER.md` 的层叠方向一致），`/skills` 里会标出被覆盖的那份。

> 放哪一档：**想在任何项目里都用**的技能放全局 `~/.thinker/skills/`（跟人走）；
> 只有这个仓库特有的约定才放 `<项目>/.thinker/skills/`。Thinker 是工具，技能大多该是全局的。

一个最小的技能：

```markdown
---
name: pdf
description: 读取、解析、抽取 PDF 的正文与表格。用户提到 pdf/论文/扫描件/发票时使用。
---

# PDF 处理

要处理的文件：$ARGUMENTS

1. 先用 `file_read` 看一眼文件头，确认不是加密或纯图片扫描件。
2. 需要按页抽取时，运行 `${SKILL_DIR}/scripts/extract.py <路径>`。
3. 汇报里写清页数、字符数、有没有表格。
```

- 变量只有三个：`$ARGUMENTS`、`${SKILL_DIR}`、`${PROJECT_DIR}`；也认 Claude Code 的 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}`。
- **怎么用**：直接说要做什么（"帮我把这个 PDF 拆成两半"），模型看到描述里写着 pdf 就会自己加载，不需要敲任何命令。想确认加载了哪些技能，用 `/skills`。
- **所有技能都会出现在模型可见的清单里**（没写描述也会列出，只是显示占位文字）。
- 正文里的 `reference.md`、脚本不会被自动加载，模型会自己 `file_read` / `cmd_runner`。
  带支撑文件的技能，加载时会在正文前补一段「【技能目录】…」，因为项目里的相对路径是以**项目根**为基准的，
  不说清楚的话模型会把 `examples/x.md` 找到项目根下。
- 只改正文**不用**重启（每次都现读）；改了名字/描述要 `/skills reload`。
- 技能**不能改权限**：正文写"直接 rm -rf 不用问"也照样会弹确认，`classify()` 只看工具名和参数。

细节、字段表、踩过的坑见 [21 轮文档](docs/优化记录/21-第21轮-技能机制.md)。

---

## 记忆系统

模型每次对话都是"第一次来这个项目"。记忆就是让它记住规矩的地方，分两套：

| | `THINKER.md`（人写） | `MEMORY.md`（模型写） |
|---|---|---|
| 谁维护 | 你 | 模型，在对话中自己判断值得记就写 |
| 存哪 | 项目根目录 / `~/.thinker/THINKER.md` | `~/.thinker/projects/<项目slug>/memory/` |
| 怎么进上下文 | **全文**注入系统提示词 | 只注入**索引**，正文按需用 `/memory` 看 |
| 适合写 | 代码约定、不要做的事、常用命令 | "这个项目用 poetry 不用 pip" 这类经验 |

`THINKER.md` 的查找顺序（越靠后优先级越高，和 CSS 层叠一个道理）：

```
~/.thinker/THINKER.md      全局，跟自己走，不跟项目走
D:\THINKER.md              父目录 …
D:\code\THINKER.md         …
D:\code\project\THINKER.md 当前目录，优先级最高
```

限制：单文件最多 12000 字符，合计 24000 字符，向上最多找 6 层父目录，`@import` 最多套 3 层。

> **记忆是启动时读进系统提示词的，改完要重启 `thinker` 才生效。**

仓库根目录的 [`THINKER.md`](THINKER.md) 就是本项目的项目记忆，可以直接当模板抄——里面「不要做的事」「已知的坑」两节质量最高。

---

## 上下文五层压缩

上下文满了不能直接砍，砍掉的那段可能正被引用。Thinker 按「占用比例」分五档，落到哪档只做哪档的动作，不爬层、不叠加：

| 层 | 手段 | 触发区间（默认） | 成本 | 做什么 |
|---|---|---|---|---|
| L1 | 工具输出设上限 | 每次写历史 | 0 | 单条结果超过 30000 字符掐头去尾 |
| L2 | Snip 剪中间 | `[0.80, 0.85)` | 0 | 保头 3 条 + 保尾 47 条，中间换成占位 |
| L3 | Microcompact 折叠工具结果 | `[0.85, 0.90)` | 0 | 较早的「可重复查询」工具结果折叠成一句中文占位，需要时模型自己重跑 |
| L4 | Context Collapse 只读投影 | `[0.90, 0.95)` | 0 | 不改存储，只换视图 |
| L5 | Auto-compact 全量摘要 | `≥ 0.95` | **高** | 保留 system + 最近 10 条，其余整段**一次性**压成摘要（唯一花钱的一层） |

设计要点：

- **前四层都不调模型**，所以绝大多数会话一辈子不会触发 L5。
- **有副作用的工具（写文件、执行命令）不会被折叠**——那些结果丢了就没法重建。
- 压缩会整体替换消息列表，**前缀缓存因此失效**，下一轮输入按原价计费。所以压缩动作要尽量落在缓存断点之后，界面上也会明确告诉你。
- **L5 是一次压到底，不会反复压**。压完之后保留区之外已经没有内容可压，再触发会直接跳过（**不调模型、不花钱**），并提示你 `/clear`。所以它是"95% 进、通常 10% 出"，不是"每轮压一点"。
- **压缩结果会落盘，`/resume` 不会重新压一次**。压缩成功后往会话文件追加一条 `compaction` 记录（`keep_from` + 摘要），恢复时按它重建"公告 + 保留区"。文件仍是 append-only，只是多了一种行类型——老版本读到会跳过，不影响兼容。`/resume` 列表里的条数也是压缩后的（会标「（压缩后）」），与恢复后实际条数一致。
- 想手动压，用 `/compact`；它会在摘要比原文还长时老实告诉你"这次没省下 token"。

---

## 接入 MCP 外部工具

Thinker 支持标准 [MCP](https://modelcontextprotocol.io)，**只走 stdio 传输**（服务器是本地子进程）。接一个服务器用一条命令：

```bash
thinker mcp add 12306 -- npx -y 12306-mcp   # 加一个（已存在就覆盖）
thinker mcp list                             # 看现在配了哪些
thinker mcp get 12306                        # 看它的配置原文
thinker mcp remove 12306                     # 删掉（别名 rm）

# 给单个服务器设环境变量（最常用的是代理，只影响这个子进程）
thinker mcp add duckduckgo -e HTTPS_PROXY=http://127.0.0.1:7890 -- C:/tools/duckduckgo-mcp.exe
```

`-e/--env KEY=VALUE` 可以写多次；它落进配置的 `env` 字段，子进程启动时会和当前环境**合并**（不是替换），所以 `PATH` 这些不会丢。

用法和 `claude mcp add` 一致：`--` 后面的部分原样交给子进程，所以 `-y`、`--port 8080` 这类参数不会被当成 Thinker 自己的选项吞掉。`--` 也可以省略（`thinker mcp add 12306 npx -y 12306-mcp`），效果一样。

两条安全保证：每次改动前把旧文件备份成 `mcp.json.bak`；文件本来就不是合法 JSON 时**直接中止、一个字都不写**（手写配置最怕的「改一处、丢全部」不会发生）。

也可以手写配置文件 **`~/.thinker/mcp.json`**，和命令完全等价（程序读的就是这个文件）：

```json
{
  "mcpServers": {
    "weather": {
      "type": "stdio",
      "command": "C:/Python312/python.exe",
      "args": ["D:/code/thinker/src/mcp_servers/weather.py"]
    }
  }
}
```

几个坑先说：

- **`command` 和 `args` 都写绝对路径**。MCP 子进程的 cwd 继承自你启动 `thinker` 的目录，相对路径基本找不到文件。
- **没有这个文件不影响使用**，程序会打印一行提示然后跳过外部工具，内置工具照常。配置文件写坏了（少个逗号）也只跳过，不会崩。
- 兼容 `mcp` 库 **1.x（`inputSchema`）和 2.x（`input_schema`）**，不用管版本。
- 仓库里的 [`src/mcp_servers/mcp.json`](src/mcp_servers/mcp.json) 只是**示例模板**，程序不读它。`src/mcp_servers/weather.py` 和 `calculator.py` 可以直接拿来试。
- **改完要重启 `thinker`**：命令和手改都只动文件，当前会话里已经建立的连接不会热加载。
- **启动后敲 `/mcp` 看运行时状态**：哪个连上了、各自几个工具、谁失败、谁被跳过。它看的是当前进程的真实连接，和 `thinker mcp list`（只看配置文件）不是一回事——文件里配了 3 个而 `/mcp` 只显示 2 个连接 + 1 个失败，那个失败的就是要修的问题。
- **Windows 上直接写 `npx` 就行**（不用写 `npx.cmd`）：启动前客户端会用 `shutil.which` 把裸命令解析成真实路径。不解析会踩一个很闷的坑——CreateProcess 只会自动补 `.exe`，而 npm 装的是 `npx.cmd`，配置一字不差也会报 `FileNotFoundError: [WinError 2]`。
- `npx` 形式的服务器第一次启动要下载包（落在 npm 缓存 `%LOCALAPPDATA%\npm-cache\_npx\<hash>\node_modules\`，不进全局 node_modules），会明显慢一拍（`thinker` 启动时在后台连，不挡输入；刚启动就敲 `/mcp` 可能显示"连接中…"，再敲一次即可）。

---

## 生命周期 hook

在会话的四个点上挂 shell 命令。配置在 **`~/.thinker/settings.json`**（只支持用户级，理由见下）：

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "git status --short" }] }
    ],
    "PreToolUse": [
      { "matcher": "cmd_runner", "hooks": [{ "type": "command", "command": "python C:/hooks/guard.py" }] }
    ],
    "PostToolUse": [
      { "matcher": "file_write|file_edit", "hooks": [{ "type": "command", "command": "echo 改完了" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "echo bye >> C:/hooks/run.log" }] }
    ]
  }
}
```

| 事件 | 什么时候跑 | 能拦住吗 | 输出到哪 |
|---|---|---|---|
| `SessionStart` | 会话建立后、系统提示词生成前 | 不能 | stdout 拼进系统提示词 |
| `PreToolUse` | 工具调用前、**权限确认之前** | **能（exit 2）** | 拒绝原因回给模型 |
| `PostToolUse` | 工具成功执行后 | 半能（工具已跑完） | stdout 追加到工具结果 |
| `SessionEnd` | 进程退出前（Ctrl+C 也算） | 不能 | 打给用户 |

几个必须知道的：

- **退出码**：`0` 继续；`2` 阻断（stderr 作为原因）；其它当非阻断错误，打一行黄字继续跑。
- **stdin 是 JSON**，字段和 Claude Code 一致：`session_id` / `cwd` / `hook_event_name` / `tool_name` / `tool_input`。所以现成的 Claude Code hook 脚本基本能直接搬。
- **`matcher`** 只对 `PreToolUse`/`PostToolUse` 生效：空或 `*` 匹配全部，`file_write|file_edit` 匹配多个，`mcp__.*` 按正则。大小写敏感。
- **只支持用户级**。项目里的 `.thinker/settings.json` **不会被执行**——克隆一个仓库就能在你机器上跑 shell，这个口子不值得为团队共享开。项目里有配置时，启动会提示一句"已忽略"。
- **超时默认 30 秒**，上限 300 秒；超时按非阻断处理（杀子进程 + 黄字提示）。
- **输出上限 4000 字符**：超出部分被截断并注明「已截断 N 字符」。hook 的 stdout 会进上下文，一条 `git log` 就能顶掉预算。
- **同一个 hook 失败最多提示 3 次**：第 4 次改说「之后不再提示」，再往后彻底安静——否则一条写坏的 hook 会每调一次工具刷一行黄字。
- **Windows 上是 cmd 语法**，不是 bash：`$(...)`、`&&` 以外的 shell 特性都不认。复杂逻辑写成 `.py` 文件再用 `python xxx.py` 调，跨平台最稳。
- **改完要重启**：配置文件只在启动时读一次。
- **多个 hook 串行执行，遇到 exit 2 立即停**（Claude Code 是并行跑完再合并，见[第 32 轮文档](docs/优化记录/32-第32轮-hook机制.md)里的取舍说明）。

---

## 目录结构

```
.
├── src/
│   ├── main.py                 # 入口：画欢迎框、解析参数、启动主循环
│   ├── core/
│   │   ├── conversation.py     # 对话总管：主循环、斜杠命令拦截点
│   │   ├── api_client.py       # 唯一跟模型说话的地方（流式、usage 统计）
│   │   ├── history/            # 消息历史 + 五层压缩
│   │   ├── permissions.py      # 权限判定（危险规则 / 只读白名单 / 其余必问）
│   │   ├── commands.py         # 斜杠命令注册表与实现
│   │   ├── hooks.py            # 生命周期 hook：读 settings.json、跑 shell、判阻断
│   │   ├── session.py          # JSONL 会话存档（--continue / /resume）
│   │   ├── auto_memory.py      # 自动记忆读写
│   │   ├── limits.py           # 输出上限常量（只在这里定义一次）
│   │   └── prompt/             # 系统提示词拼装：行为规矩 / 记忆 / 环境信息
│   ├── tools/                  # 内置工具 + MCP 适配 + 子 Agent + 技能（skill/）
│   │   ├── mcp_config.py       # MCP 配置文件定位/读写（命令与运行时共用）
│   │   └── mcp_cli.py          # `thinker mcp add/remove/list/get`
│   ├── ui/                     # 显示与输入（theme 管配色符号，见 ui/README.md）
│   └── mcp_servers/            # 示例 MCP 服务器（weather / calculator）
├── demo/plane/                 # 用 Thinker 写的浏览器小游戏（零依赖，含 Node 逻辑回归）
├── test/                       # 34 个脚本式回归（不是 pytest）：压缩 / mcp / hooks / 模式 / 技能 / 会话 / 清理
├── docs/
│   ├── ROADMAP.md              # 还有什么没做、为什么这么排
│   └── 优化记录/                # 49 份逐轮改动记录（含根因分析）
├── THINKER.md                  # 项目记忆（模型每轮都会读到）
├── .env.example                # 配置模板，复制成 .env 后填
├── LICENSE
└── pyproject.toml
```

**推荐阅读顺序**：`main.py` → `core/conversation.py` → `core/api_client.py` → `tools/tool_manager.py` → `tools/base_tool.py` → 具体工具 → `ui/`。

---

## 验证

`test/` 下现在有 **34 个脚本**（都不是 pytest，直接 `python test/xxx.py`，靠打印的 ✓ 和退出码判定结果）。**全部可跑、全部离线**——不联网、不消耗模型额度，可以整个目录批量跑：

```bash
for f in test/*.py; do /path/to/python "$f" > /dev/null || echo "失败：$f"; done
```

> 两个流式脚本（`test_streaming.py` / `test_streaming_with_tools.py`）默认走**假客户端**，只验 chunk 分流与 tool_calls 拼装；想真打一次接口确认线上流式没坏，加 `--live`（会花钱、需要联网）。
>
> 2026-11-13 重写过一遍：其中 8 个是跟随旧接口写的（`history_token_usage` 结构、`get_reminder` 加 `mode`、`register_skill_commands` 删除、`ThinkingChunk` 没有 `.content`、L5 改成按边界压之后"没有可压内容"、输入层整行自管后的按键注入点），已全部对齐当前实现——**顺带把两个流式脚本从"真请求、只打印、永远绿"改成了有断言的离线脚本**。

下面 7 条是"出问题最先跑"的骨干：

**1. 全部模块能编译**

```bash
python -m compileall -q src
```

**2. 能正常打包安装**（这条抓的是 `pyproject.toml` 的配置错，不是业务逻辑）

```bash
pip wheel . --no-deps -w dist/
```

打出来的 wheel 里必须有顶层 `main.py` 和 `core/` 包，否则装完一敲 `thinker` 就 `ModuleNotFoundError`。

**3. 压缩回归**（37 条断言，不调模型）

```bash
python test/test_compress_boundary.py
```

覆盖四件手点验不出来的事：压完条数必须**真的**下降；连续压第二次不能再去调模型（否则就是"摘要的摘要"——条数不降、每次都花钱）；压完 `tool_call` 配对不能断；摘要失败时历史必须一条不动。

**4. demo 的逻辑回归**（15 条规则断言）

```bash
node demo/plane/test/game.test.js
```

**5. `thinker mcp` 命令回归**（49 条断言，不连服务器）

```bash
python test/test_mcp_cli.py
```

覆盖四件手点验不出来的事：命令写出来的文件，运行时 `MCPConfig` 必须读得懂；覆盖同名服务器前必须先备份，且备份里是**旧**内容；配置文件不是合法 JSON 时 add/remove 必须中止、原文件字节不变；`--` 前后的参数不能互相串味（`-y` 不能被当成 Thinker 的选项吞掉）。

**6. `/mcp` 状态判定**（20 条断言，不连服务器）

```bash
python test/test_mcp_status.py
```

五种状态各钉一条：已连接（带工具数 / 正在取清单 / 取清单失败）、连接失败、连接中、状态未知、配置阶段被跳过。判错的后果比没有这条命令更糟——把失败的显示成"已连接"，用户会照着去查为什么工具不出现。

**7. hook 机制**（42 条断言，不碰真实配置）

```bash
python test/test_hooks.py
```

盯的是这类机制最容易坏、又最不报错的地方：坏 JSON 只提示不崩、未知事件名有反馈、`matcher` 的四种写法、`exit 0/1/2` 三种结局、超时不阻断、串行短路（第一个拦下后后面的不执行）、超长输出被截断、失败提示的节流曲线、以及 Windows 上 `cmd echo` 的中文不被解成问号。

**8. 会话清理与归档**（41 条断言，只碰临时目录）

```bash
python test/test_session_clean.py
```

盯的是"可能丢数据"的那几条：压缩后解压回来必须与原文逐字节一致、压缩失败不能删原文、坏归档（截断的 `.gz`）只影响它自己而不能让 `/resume` 列表打不开、归档文件禁止追加（否则会把整份历史写坏）、小到压不下来的会话要撤销归档并保留原文。

> 上面编号的八条，以及 `test/` 里其余 26 个脚本，**都不联网、不消耗模型额度**，可以放心批量跑。唯一会发真请求的是给两个流式脚本加 `--live` 时。

业务逻辑的其余部分（主循环、权限判定）靠手动冒烟：改完启动一次，让它读一个文件、改一个文件、跑一条命令，看确认框和状态行对不对。动过 `core/history/` 或 `core/conversation.py` 时，务必聊到上下文过半触发一次压缩再退出。

---

## 常见问题

**`thinker` 报 `ModuleNotFoundError: No module named 'main'`**
几乎都是 Windows 上重装时踩的：`pip install -e .` 要替换 `Scripts\thinker.exe`，而这个文件在 thinker 正运行时被锁住 → pip 卸掉旧的、装不上新的，包就没了。
**先退出 thinker，再重装。** 已经踩了的话，手写一个 `site-packages\__editable__.thinker.pth`（内容就是 `src` 目录的绝对路径）把导入接回来，关掉会话后重装。

**PyCharm 里 `from core...` 全红**
不是代码问题，是 src 布局没标记：把 `src` 标成 **Sources Root**。editable 安装有时会把这个洞盖住，包一卸就全红了。

**模型老是 `dir /s`，不用 `file_glob`**
已知问题，根因是工具说明书里曾同时给了两条路（`dir` 和 `file_glob`），模型会挑它**最熟**的而不是最好的。现在说明书只留一条，并且在 `cmd_runner` 命中查找类命令时，结果末尾会追加一句建议。

**`/cost` 显示"未提供"**
接口没返回 `cost` 字段（中转站常见），不是 bug。不显示 `$0.00` 是刻意的——那会让人以为免费，然后放开了用。

**缓存命中率一直是 0%**
两个原因：接口不支持提示词缓存（要换供应商），或者消息前缀一直在变（比如系统提示词里带了时间戳）。

**中文输入法敲 `/` 没反应**
全角斜杠会被一致地挡在命令系统之外，当成普通消息。切半角。

**等待时按 Esc 没反应 / 输入的字删不掉**
Windows 控制台的老坑，已在第 16.2 轮整行自管重做。若仍异常，先 `THINKER_PLAIN_INPUT=1` 跑一次，确认是行编辑层的问题。

---

## 已知限制

坦白列出来，省得你踩了以为是 bug：

| 限制 | 说明 |
|---|---|
| 没有插话队列 | 生成期间打的字会被吞，Esc 打断后只能重新打字。`src/ui/esc_watcher.py` 已留好"把非 Esc 字符塞进队列"的口子，但队列本身还没做 |
| 没有 WebSearch / WebFetch | 只能搜本地，外部信息得你自己贴进来 |
| 子 Agent 白名单是工具粒度 | 只有 `file_read` / `cmd_runner` 这种粒度，没有"只允许 git diff"的写法；所以内置类型一律不给 `cmd_runner` |
| 子 Agent 不能并行 | `ToolManager` / `Conversation` / `HistoryManager` 都是单例 + 类属性，硬并行会互相踩 |
| 会话文件不会**自动**清理 | 只增不减，但 `/clean` 可以手动压缩归档或删除。刻意不做自动清理：启动时静默动用户的对话历史，出问题他只会觉得"记录怎么没了" |
| 429 / 5xx 一次失败就降级 | 走非流式兜底，能用，但没有指数退避重试 |
| 跨平台不完整 | Tab 补全、方向键、Esc 打断依赖 Windows 的 `msvcrt`，其他平台降级为原生输入 |
| 状态栏不固定 | 长对话滚动后看不到底部状态 |

完整清单和排期理由见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

---

## 文档索引

`docs/优化记录/` 是这个项目最值得看的部分——每一次改动都记了「为什么要改、改前是什么样、踩了什么坑、怎么验证的」（共 49 份）。

- **[ROADMAP.md](docs/ROADMAP.md)** — 当前进度、优先级、依赖关系、遗留问题索引（先看这个）
- **[THINKER.md](THINKER.md)** — 项目记忆：代码约定、不要做的事、已知的坑
- **[src/ui/README.md](src/ui/README.md)** — UI 模块的分层与用法

按主题挑着看：

| 主题 | 文档 |
|---|---|
| 主循环与工具集 | [08.0 文件工具集与主循环去递归](docs/优化记录/08.0-第8.0轮-文件工具集与主循环去递归.md) |
| 上下文与成本 | [06 上下文成本控制](docs/优化记录/06-第6轮-上下文成本控制.md)、[08.2 缓存命中率](docs/优化记录/08.2-第8.2轮-缓存命中率与一键确认.md)、[17 五层压缩](docs/优化记录/17-第17轮-五层压缩.md)、[17.1 压缩补测](docs/优化记录/17.1-第17.1轮-压缩补测.md)、[25 摘要改成边界压](docs/优化记录/25-第25轮-摘要压缩改成边界压.md) |
| 权限 | [09 权限规则化与五个 P0 修复](docs/优化记录/09-第9轮-权限规则化与五个P0修复.md) |
| 工作模式 | [33 四档工作模式](docs/优化记录/33-第33轮-四档工作模式.md) |
| 记忆 | [11 项目记忆](docs/优化记录/11-第11轮-项目记忆.md)、[12 自动记忆](docs/优化记录/12-第12轮-自动记忆.md) |
| 中断与持久化 | [13 中断与会话持久化](docs/优化记录/13-第13轮-中断与会话持久化.md)、[18.7 Esc 随时可打断](docs/优化记录/18.7-第18.7轮-Esc随时可打断.md) |
| 斜杠命令 | [14](docs/优化记录/14-第14轮-斜杠命令.md)、[14.5 压缩与清空](docs/优化记录/14.5-第14.5轮-压缩与清空.md) |
| 并发 | [15 只读工具并发](docs/优化记录/15-第15轮-只读工具并发.md) |
| 子 Agent | [18 类型化](docs/优化记录/18-第18轮-子Agent类型化.md)、[08.1 task 说明书修正](docs/优化记录/08.1-第8.1轮-task说明书修正.md) |
| 技能 | [21 技能机制](docs/优化记录/21-第21轮-技能机制.md) |
| 输入层 | [16 Tab 补全](docs/优化记录/16-第16轮-Tab补全.md)、[16.2 输入层重做](docs/优化记录/16.2-第16.2轮-输入层重做.md)、[18.6 跨行与留白](docs/优化记录/18.6-第18.6轮-输入层跨行与留白.md) |
| 界面 | [07 对齐 Claude Code 视觉](docs/优化记录/07-第7轮-对齐ClaudeCode视觉.md)、[10 等待动画与界面还原](docs/优化记录/10-第10轮-等待动画与界面还原.md)、[18.5 等待动画重做](docs/优化记录/18.5-第18.5轮-等待动画重做.md) |

---

## 数据存在哪

| 路径 | 内容 |
|---|---|
| `~/.thinker/sessions/*.jsonl` | 会话存档，一行一条消息，可直接 `tail` 看 |
| `~/.thinker/sessions/*.jsonl.gz` | 被 `/clean` 归档的旧会话（gzip 后通常只剩 8%~15%；`/resume` 选中它会自动解压回来） |
| `~/.thinker/projects/<项目slug>/memory/` | 模型自己写的记忆（`MEMORY.md` + 条目） |
| `~/.thinker/skills/`、`<项目>/.thinker/skills/` | 技能（操作手册）；同名时项目版本生效 |
| `~/.thinker/THINKER.md` | 全局记忆（不自动创建，要就自己建） |
| `~/.thinker/mcp.json` | MCP 服务器配置 |
| `<项目>/THINKER.md` | 项目记忆（`/init` 生成） |
| `<仓库>/.env` | 密钥与配置（已被 `.gitignore` 排除） |

全部在**本机**，不上传、不同步。删掉 `~/.thinker/` 就等于重置所有会话与记忆。

会话文件**只增不减**，攒久了会占地方（一个长会话能到 1.5 MB）。用 `/clean` 处理：它会先报"共几个、占多少、几个超过 30 天没动过"，再让你选压缩归档还是删除。压缩是默认项——JSONL 是纯文本，压完通常只剩一成，数据一条不丢。

---

## License

[MIT](LICENSE)。拿去用、改、商用都行，保留版权声明即可。

如果这个项目帮你搞懂了 Agent 是怎么跑的，给个 Star 是最好的鼓励。
