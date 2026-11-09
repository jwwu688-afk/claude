# Thinker

**用 Python 从零复刻 Claude Code 的终端编程助手 —— 一个把 Agent 内部机制摊开来讲的教学项目。**

Thinker 跑在终端里，能读代码、改文件、执行命令、自己决定下一步做什么。它和 Claude Code、Cursor Agent 是同一类东西（terminal coding agent），区别在于：**这里没有黑盒**。主循环怎么转、上下文怎么压、权限怎么判、记忆怎么存，每一处都在代码注释里写清了「为什么这么写、踩过什么坑」。

适合三类人：

- 想搞明白「Agent 到底是怎么跑起来的」，光看框架文档不过瘾的人
- 想自己动手改一个 Agent、但不想从零搭脚手架的人
- 想给自己的项目配一个能读代码的终端助手的人

项目要求注释占有效行的 60%~75%（标杆 `core/session.py` 是 76%），`docs/优化记录/` 里有 27 份逐轮改动记录，包含每次踩坑的根因分析。

---

## 目录

- [它是什么 / 不是什么](#它是什么--不是什么)
- [能做什么](#能做什么)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [内置工具](#内置工具)
- [权限与安全](#权限与安全)
- [斜杠命令](#斜杠命令)
- [记忆系统](#记忆系统)
- [上下文五层压缩](#上下文五层压缩)
- [接入 MCP 外部工具](#接入-mcp-外部工具)
- [目录结构](#目录结构)
- [跑测试](#跑测试)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [文档索引](#文档索引)
- [License](#license)

---

## 它是什么 / 不是什么

**是：**

- 一个真的能干活的 Agent。不是 demo，能连续几十分钟改一个项目：读文件 → 定位问题 → 改代码 → 跑测试 → 看报错 → 再改。
- 一个可读的实现。16 轮迭代、61 个 Python 文件，单层主循环，没有框架封装，`asyncio` + `openai` SDK + `rich` 就到顶了。
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
| **9 个内置工具** | 读写改文件、按名/按内容搜索、执行命令、待办清单、子 Agent、上下文裁剪 |
| **MCP 外部工具** | 标准 MCP 协议，`~/.thinker/mcp.json` 里配一行就能接任意 MCP Server（兼容 `mcp` 库 1.x / 2.x） |
| **权限分级** | 只读免问、写操作必问、22 条危险命令正则命中则"回车=拒绝"；**模型无法给自己放行** |
| **五层上下文压缩** | 从"零成本剪枝"到"调模型摘要"分五档，压力越大手段越重，绝大多数情况不花额外 token |
| **项目记忆** | `THINKER.md`（人写，全文注入）+ `MEMORY.md`（模型自己写，注入索引），支持全局/项目/父目录分层 |
| **会话持久化** | 每条消息即时落盘 JSONL，Ctrl+C 也不丢；`--continue` 接上次，`/resume` 挑历史 |
| **随时打断** | Esc 打断当前生成，不用杀进程 |
| **斜杠命令** | 7 条本地命令（`/cost` `/memory` `/resume` …），不发给模型、不占上下文、不计费 |
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
git clone git@github.com:jwwu688-afk/claude.git
cd claude

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
# thinker v0.7.2
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
> 所以你在 A 项目里跑 thinker，读的仍然是 `claude/.env` —— 换项目不用重新配。
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
> 跑 test/test_file_tools.py，挂了的话定位并修掉
```

第一次让它改文件时会弹确认，回车即同意；想拒绝就直接输入理由，那句话会带回给模型。

### 常用命令行参数

| 参数 | 作用 |
|---|---|
| `thinker` | 开新会话 |
| `thinker --continue` / `-c` | 接着**本项目**最近一次会话聊 |
| `thinker --version` / `-v` | 打印版本 |
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
| `THINKER_STREAM` | `plain` | 设成 `live` 走 rich Live 渲染（超长回复更顺，但宽度变化会留残影） |
| `THINKER_PLAIN_INPUT` | 空 | 设 `1` 关闭自管行编辑，退回原生 `input()`（补全/方向键失效，用于排查输入问题） |
| `THINKER_NO_SPINNER` | 空 | 设 `1` 关掉等待动画（日志重定向时用） |

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

> 说清楚风险：这是一道防手滑的闸，不是沙箱。它拦不住一个**存心**（或被提示注入）绕路的模型。真正隔离请用容器 / 虚拟机 / 受限账号跑。

---

## 斜杠命令

斜杠命令在**本地**处理——不发给模型、不进上下文、不计费。它们必须在消息进历史**之前**被截住，否则命令本身会变成一条付费消息，而且模型给不出正确答案（它不知道你花了多少钱）。

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令 |
| `/cost` | 本次会话花费、token、缓存命中率、上下文占用 |
| `/memory [文件名]` | 看模型记了什么：不带参数看索引，带参数看某条正文 |
| `/resume` | 列出本项目历史会话，输编号切过去 |
| `/init [--global] [--force] [--here]` | 生成 / 更新 `THINKER.md` 骨架 |
| `/compact` | 立刻压缩一次上下文（不等 80%，会调一次摘要模型，**花钱**） |
| `/clear` | 清空上下文并开一个新会话（旧会话文件保留，可 `/resume` 回去） |

`/init` 的几个开关：`--global` 写全局记忆（`~/.thinker/THINKER.md`）；`--force` 覆盖已存在的文件；`--here` 允许在用户主目录生成（默认拒绝）。

输入 `/` 后按 **Tab** 补全：唯一匹配直接补上，多个匹配先补到公共前缀、再按一次列候选。

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
| L5 | Auto-compact 全量摘要 | `≥ 0.95` | **高** | 调模型把旧对话总结成摘要（唯一花钱的一层） |

设计要点：

- **前四层都不调模型**，所以绝大多数会话一辈子不会触发 L5。
- **有副作用的工具（写文件、执行命令）不会被折叠**——那些结果丢了就没法重建。
- 压缩会整体替换消息列表，**前缀缓存因此失效**，下一轮输入按原价计费。所以压缩动作要尽量落在缓存断点之后，界面上也会明确告诉你。
- 想手动压，用 `/compact`；它会在摘要比原文还长时老实告诉你"这次没省下 token"。

---

## 接入 MCP 外部工具

Thinker 支持标准 [MCP](https://modelcontextprotocol.io)（stdio 传输）。配置文件是 **`~/.thinker/mcp.json`**：

```json
{
  "mcpServers": {
    "weather": {
      "type": "stdio",
      "command": "C:/Python312/python.exe",
      "args": ["D:/code/claude/src/mcp_servers/weather.py"]
    }
  }
}
```

几个坑先说：

- **`command` 和 `args` 都写绝对路径**。MCP 子进程的 cwd 继承自你启动 `thinker` 的目录，相对路径基本找不到文件。
- **没有这个文件不影响使用**，程序会打印一行提示然后跳过外部工具，内置工具照常。配置文件写坏了（少个逗号）也只跳过，不会崩。
- 兼容 `mcp` 库 **1.x（`inputSchema`）和 2.x（`input_schema`）**，不用管版本。
- 仓库里的 [`src/mcp_servers/mcp.json`](src/mcp_servers/mcp.json) 只是**示例模板**，程序不读它。`src/mcp_servers/weather.py` 和 `calculator.py` 可以直接拿来试。

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
│   │   ├── session.py          # JSONL 会话存档（--continue / /resume）
│   │   ├── auto_memory.py      # 自动记忆读写
│   │   ├── limits.py           # 输出上限常量（只在这里定义一次）
│   │   └── prompt/             # 系统提示词拼装：行为规矩 / 记忆 / 环境信息
│   ├── tools/                  # 9 个内置工具 + MCP 适配 + 子 Agent
│   ├── ui/                     # 显示与输入（theme 管配色符号，见 ui/README.md）
│   └── mcp_servers/            # 示例 MCP 服务器（weather / calculator）
├── test/                       # 21 个独立测试脚本，不是 pytest
├── docs/
│   ├── ROADMAP.md              # 还有什么没做、为什么这么排
│   └── 优化记录/                # 27 份逐轮改动记录（含根因分析）
├── THINKER.md                  # 项目记忆（模型每轮都会读到）
├── .env.example                # 配置模板，复制成 .env 后填
├── LICENSE
└── pyproject.toml
```

**推荐阅读顺序**：`main.py` → `core/conversation.py` → `core/api_client.py` → `tools/tool_manager.py` → `tools/base_tool.py` → 具体工具 → `ui/`。

---

## 跑测试

**不用 pytest**（刻意保持零测试依赖），每个文件都是独立脚本，跑完打印 `🎉 所有测试通过（N/N）`：

```bash
python test/test_file_tools.py
python test/test_commands.py
python test/test_conversation_loop.py
python test/test_history_compress.py
```

一次跑全部：

```bash
# Windows (Git Bash)
for f in test/test_*.py; do echo "== $f"; python "$f" || break; done

# PowerShell
Get-ChildItem test/test_*.py | ForEach-Object { python $_.FullName }
```

> 测试全部是本地逻辑验证，**不联网、不消耗模型额度**——需要模型的地方都被替身或桩函数换掉了。放心跑。

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
| 没有 Plan mode | 复杂任务不会先出方案再动手，直接干 |
| 没有插话队列 | Esc 打断后只能重新打字，不能排队下一条；等回复期间提前敲的键会被吞 |
| 没有 Hooks | 改完文件不能自动格式化 / 自动跑测试 |
| 没有 WebSearch / WebFetch | 只能搜本地，外部信息得你自己贴进来 |
| 子 Agent 不能并行 | `ToolManager` / `Conversation` / `HistoryManager` 都是单例 + 类属性，硬并行会互相踩 |
| 会话文件不会自动清理 | 只增不减，攒久了要手动删 `~/.thinker/sessions/` |
| 跨平台不完整 | Tab 补全、方向键、Esc 打断依赖 Windows 的 `msvcrt`，其他平台降级为原生输入 |
| 状态栏不固定 | 长对话滚动后看不到底部状态 |

完整清单和排期理由见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

---

## 文档索引

`docs/优化记录/` 是这个项目最值得看的部分——每一次改动都记了「为什么要改、改前是什么样、踩了什么坑、怎么验证的」（共 27 份）。

- **[ROADMAP.md](docs/ROADMAP.md)** — 当前进度、优先级、依赖关系、遗留问题索引（先看这个）
- **[THINKER.md](THINKER.md)** — 项目记忆：代码约定、不要做的事、已知的坑
- **[src/ui/README.md](src/ui/README.md)** — UI 模块的分层与用法

按主题挑着看：

| 主题 | 文档 |
|---|---|
| 主循环与工具集 | [08.0 文件工具集与主循环去递归](docs/优化记录/08.0-第8.0轮-文件工具集与主循环去递归.md) |
| 上下文与成本 | [06 上下文成本控制](docs/优化记录/06-第6轮-上下文成本控制.md)、[08.2 缓存命中率](docs/优化记录/08.2-第8.2轮-缓存命中率与一键确认.md) |
| 权限 | [09 权限规则化与五个 P0 修复](docs/优化记录/09-第9轮-权限规则化与五个P0修复.md) |
| 记忆 | [11 项目记忆](docs/优化记录/11-第11轮-项目记忆.md)、[12 自动记忆](docs/优化记录/12-第12轮-自动记忆.md) |
| 中断与持久化 | [13 中断与会话持久化](docs/优化记录/13-第13轮-中断与会话持久化.md) |
| 斜杠命令 | [14](docs/优化记录/14-第14轮-斜杠命令.md)、[14.5 压缩与清空](docs/优化记录/14.5-第14.5轮-压缩与清空.md) |
| 并发 | [15 只读工具并发](docs/优化记录/15-第15轮-只读工具并发.md) |
| 输入层 | [16 Tab 补全](docs/优化记录/16-第16轮-Tab补全.md)、[16.2 输入层重做](docs/优化记录/16.2-第16.2轮-输入层重做.md) |
| 界面 | [07 对齐 Claude Code 视觉](docs/优化记录/07-第7轮-对齐ClaudeCode视觉.md)、[10 等待动画与界面还原](docs/优化记录/10-第10轮-等待动画与界面还原.md) |

---

## 数据存在哪

| 路径 | 内容 |
|---|---|
| `~/.thinker/sessions/*.jsonl` | 会话存档，一行一条消息，可直接 `tail` 看 |
| `~/.thinker/projects/<项目slug>/memory/` | 模型自己写的记忆（`MEMORY.md` + 条目） |
| `~/.thinker/THINKER.md` | 全局记忆（不自动创建，要就自己建） |
| `~/.thinker/mcp.json` | MCP 服务器配置 |
| `<项目>/THINKER.md` | 项目记忆（`/init` 生成） |
| `<仓库>/.env` | 密钥与配置（已被 `.gitignore` 排除） |

全部在**本机**，不上传、不同步。删掉 `~/.thinker/` 就等于重置所有会话与记忆。

---

## License

[MIT](LICENSE)。拿去用、改、商用都行，保留版权声明即可。

如果这个项目帮你搞懂了 Agent 是怎么跑的，给个 Star 是最好的鼓励。
