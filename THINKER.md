# THINKER.md

> 这是这个项目的"长期记忆"。Thinker 每次启动都会读它，写在这里的约定对每一轮对话都生效。

## 这个项目是什么

Thinker 是一个用 Python 复刻 Claude Code 的终端编程助手：单层主循环 + 工具调用 + MCP 外部工具 + 子 agent + 上下文压缩。
**这是一个教学向项目**——代码里的注释比代码本身还多，每个"为什么这么写"都要讲清楚，读者是正在学习 agent 实现的人。

## 技术栈

- Python 3.12（`pyproject.toml` 里写的是 `>=3.8`，但实际开发和测试都在 3.12）
- `openai` SDK 走 OpenAI 兼容接口（`.env` 里配 base_url / model，当前用 `spark-x2.6`）
- `rich` 负责终端渲染，`mcp` 负责外部工具协议
- 依赖刻意保持轻量，**不要引入新依赖**

## 目录结构

```
src/
  core/          对话总管、API 客户端、历史/压缩、权限、提示词（含 memory.py）、斜杠命令（commands.py）
  tools/         内置工具 + MCP 适配 + 子 agent + 技能（skill/，见下）
  ui/            显示与输入（theme 管配色符号）
  mcp_servers/   示例 MCP 服务器（weather / calculator）
test/            脚本式测试，不是 pytest
docs/优化记录/    每轮改动的详细记录
```

## 常用命令

```bash
# 启动（必须在项目根目录）
#   项目记忆、自动记忆目录、会话归属、文件工具的默认搜索起点，全都按"启动时的 cwd"算。
#   从 C:\Users\<你> 启动会把"家"当成项目：记忆写到家目录、改不到 D 盘的项目文件。
#   （.env 不受影响：python-dotenv 跟着包所在目录向上找，换目录照样读得到。）
thinker

# 跑测试：都是独立脚本，没有 pytest
python test/test_memory.py
python test/test_conversation_loop.py
python test/test_p0_fixes.py
```

项目记忆在会话内用 `/init` 生成（见下）。

会话内可用的斜杠命令（第 14 轮，`core/commands.py`）：

```
/help                              列出全部命令
/memory [文件名]                    看模型记了什么（不带参数看索引，带参数看正文）
/resume                            列出本项目历史会话，输编号切过去
/init [--global] [--force] [--here] 生成 / 更新 THINKER.md（主目录里默认拒绝）
/cost                              本次会话的花费、token、缓存命中率、上下文占用
/compact                           立刻压缩一次上下文（不等 80%，会调一次摘要模型）
/clear                             清空上下文并开一个新会话（旧会话文件保留，可 /resume 回去）
/agents                            列出子 agent 类型、工具白名单、模型与来源（每次执行都会重扫 .thinker/agents）
/skills [reload|install <地址>]    列出技能；reload 重扫；install 从 GitHub 地址/本地目录装新技能
```

技能文件放在 `.thinker/skills/` 下的技能会**自动登记成斜杠命令**（`/pdf`、`/release` …），
不需要改代码。格式与坑见下面「技能」一节。

## 技能（第 21 轮）

技能是**写在文件里的操作手册**，只把 `name + description` 常驻在 `skill` 工具的说明书里，
正文在模型（或用户 `/名字`）真正用到时才从磁盘读。三层加载：

```text
.thinker/skills/<名字>/SKILL.md     项目级（进仓库）
~/.thinker/skills/<名字>/SKILL.md   全局（跟人走）
.thinker/skills/<名字>.md           单文件形态也行（没有支撑文件时）
```

同名时**项目覆盖全局**（和 THINKER.md 的层叠方向一致），被覆盖的那份在 `/skills` 里会标出来。

放在哪一档：
- **`~/.thinker/skills/`（全局）**：给"开发任何项目都用得上"的活，比如发版流程、排查套路、某技术栈的加接口步骤。
  Thinker 本身是工具，绝大多数技能应该放这里——绑死某个仓库的目录结构，换个项目就废了。
- **`<项目>/.thinker/skills/`（项目级）**：只放这个仓库特有的约定（比如它自己的文档结构、它自己的发版步骤）。

最小可用文件：

```markdown
---
name: pdf
description: 读取、解析、抽取 PDF 的正文与表格。用户提到 pdf/论文/扫描件/发票时使用。
argument-hint: [文件路径]
---

# PDF 处理

要处理的文件：$ARGUMENTS

1. 先用 `file_read` 看一眼文件头。
2. 需要按页抽取时跑 `${SKILL_DIR}/scripts/extract.py <路径>`。
```

- 可用变量只有三个：`$ARGUMENTS`、`${SKILL_DIR}`、`${PROJECT_DIR}`（都不支持 `$0/$1`）。
  为了能直接导入 Claude Code 的技能，也认 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}`；
  `description: >-` / `|` 这两种多行写法也能解析。
- 认得的字段只有四个：`name` / `description` / `user-invocable` / `argument-hint`。
  其余字段（`allowed-tools` / `model` / `hooks` / `disable-model-invocation` …）**一律忽略**，
  不报错也不提示——从别处拷技能过来多写几行是正常事。
- **所有技能一律对模型可见**（第 21.1 轮定的口径）：没写 `description` 也进清单，只是显示占位文字。
- 名字只能是小写字母 + 数字 + 单个连字符（它同时是斜杠命令名）。
- 只改正文不用 reload（每次调用都现读）；改了名字/描述/开关要 `/skills reload`。

输入 `/` 开头后按 **Tab** 可以补全命令名（第 16 轮）：唯一匹配直接补上并跟一个空格，
多个匹配先补到公共前缀、再按一次列出候选。候选来自 `REGISTRY`，新增命令会自动出现在补全里。

两个 conda 环境都装了 editable 版：`D:\Anaconda\envs\claude` 和 `D:\Anaconda\envs\thinker`。

## 代码约定

- **注释用中文，解释"为什么"而不是"是什么"**。这是项目最重要的风格，每个非平凡的决定都要写清背景和踩过的坑。
- 每个文件顶部有一段 `# =====` 包围的说明，讲这个文件在整体里负责什么。
- 改动涉及行为时，注释里标注轮次，例如 `【2026-11-07 第 11 轮】`，方便对照 `docs/优化记录/`。
- 新增功能要补测试，测试写成**独立脚本**：直接 `python test/xxx.py`，最后打印 `🎉 所有测试通过（N/N）`。
- 界面上的符号和颜色只能从 `ui/theme.py` 取，不要在各处硬编码。

### 注释标准（每轮优化都必须做到）

这不是"有空就多写点"，是硬要求。本项目是**教学向**的，读者是来学 agent 怎么实现的人，注释比代码多是正常状态。

- **文件头**：写清"解决什么问题、在整体里处于什么位置"。尽量带一个具体例子——一次典型的时间线、一条调用链、或者"改前 vs 改后"的对比。抽象描述谁都写得出来，具体例子才让人真懂。
- **每个常量**：解释这个数字/字符串的来历。"为什么是 6 不是 5""为什么上限 12000 而不是 120000"。写不出来说明这个值本身就是拍的，那就该重新想。
- **每个函数**：作用 + 为什么这么设计 + 参数含义 + 边界情况（失败了会怎样、空值怎么处理）。
- **关键代码行**：加行尾注释，尤其是"看着可以更简单、但故意没这么写"的地方——那正是最需要解释的。
- **参考密度**：注释行占总有效行的 **60%~75%**。标杆是 `core/session.py`（76%）和 `core/prompt/memory.py`（62%）。低于 50% 基本要返工。
- **写完自检**：跑一段统计确认密度达标，别凭感觉。

```python
# 自检命令（在项目根执行）
python -c "
lines = open('src/core/xxx.py', encoding='utf-8').read().splitlines()
total = len([l for l in lines if l.strip()])
c = len([l for l in lines if l.strip().startswith('#')])
doc, in_doc = 0, False
for l in lines:
    s = l.strip()
    if s.startswith('\"\"\"'):
        doc += 1
        if s.count('\"\"\"') == 1: in_doc = not in_doc
    elif in_doc: doc += 1
print(f'有效行 {total}，注释 {c+doc}，占比 {(c+doc)/total:.0%}')
"
```

## 不要做的事

- **不要用 `⏺`（U+23FA）**——Windows 终端会把它交给 emoji 字体渲染成彩色圆点，颜色不受 rich 控制。圆点只能用 `●`（U+25CF）。
- **等待期间的光标隐藏必须成对恢复**。`_spin_loop` 写 `\x1b[?25l` 藏光标，`_stop_spinner`、线程的 `finally`、`atexit` 三处各恢复一次。漏掉任何一处，用户终端会变成"没有光标"，只能 reset 或重开窗口——而正常跑一千次都发现不了，只在异常退出时出现（第 18.5 轮）。
- **等待动画不要用 `rich.Live`**——它靠"光标上移 + 覆盖"重绘，从不发清行指令，宽度一变就留残影。等待态用单行 `\r` 覆盖（见 `DisplayManager._spin_loop`）。
- **不要引入 pytest**，保持测试零依赖。
- **不要改 `enviroment.py` 的函数名**（`get_enviroment_info` 是原作者拼错的 environment），多处调用，改了会连锁报错。
- 不要动 `core/permissions.py` 的判定顺序，那是有意排的。
- **不要让「技能正文」影响权限判定**。技能只是文字：正文写"直接 rm -rf 不用问"也不生效，
  `classify()` 只看工具名和参数。所以也不要加 `allowed-tools`——那等于开一个
  "仓库里的文本能让权限变松"的口子；要免审批就去做 `classify()` 里的用户允许清单。

## 已知的坑

- `mcp` 库 1.x 用 `inputSchema`、2.x 用 `input_schema`，`McpTool` 里做了双向回退，改这块要同时考虑两个版本。
- `ToolManager` / `Conversation` / `HistoryManager` 全是单例 + 类属性，一次只能跑一个子 agent。做并行之前必须先去掉单例。
- 上下文压缩会整体替换消息列表，导致前缀缓存失效；压缩动作应尽量落在缓存断点之后。
- 斜杠命令只在 `Conversation._read_user_input()` 里拦一次，别在三个调用点各写一遍；拦截必须发生在 `add_message` 之前，否则命令自己会进历史、还会计费。
- **进历史的每条消息必须能被 `json.dumps` 序列化**。SDK 返回的 `tool_calls` 是 pydantic 对象，直接放进去会让落盘抛 `TypeError`（第 14.2 轮踩过：第一次用工具就中断）。统一在 `Conversation.add_message` 里过一遍 `normalize_message()`，不要在调用处各自转换。
- **`async def` 不等于真的异步**。`file_tools` 里 `act` 原来是"async 外壳 + 全同步内核"，一个 `await` 都没有——这种函数 `gather` 起来仍然串行。要并发就得把阻塞段丢进 `asyncio.to_thread`（第 15 轮已改）。改完记得检查：光看代码"改成并发了"不算数，得有耗时对比。
- **测试里别构造真的 `ToolManager()`**：它会给 `MCPClient` 起后台任务去连服务器，然后在 `asyncio.run` 收尾时卡住（第 15 轮踩过）。要工具表就把 `ToolManager._ensure_mcp_registration` 和 `MCPClient._ensure_started` 都换成空函数。
- **重装前必须先退出 thinker**。`pip install -e .` 要替换 `Scripts\thinker.exe`，Windows 下正在运行的这个文件是被锁住的 → pip 会「先卸旧的、再装新的」，卡在替换那一步，结果是包被卸掉、`thinker` 变成 `ModuleNotFoundError: No module named 'main'`。真的踩了就先写一个 `site-packages\__editable__.thinker.pth`（内容就是 `src` 目录的绝对路径）把导入接回去，关掉会话后再重装。
- PyCharm 里 `from core...` 全红，通常不是代码问题：项目是 src 布局，`src` 必须被标记为 Sources Root（`.idea/thinker.iml` 里的 `sourceFolder`）。之前 editable 安装会顺手把这个洞盖住，包一旦卸掉就全红了。
- **别在用户主目录启动**（`C:\Users\<你>`）。项目记忆、自动记忆目录、会话归属、文件工具的默认搜索起点，全都按"启动时的 cwd"算，从家里启动等于把家当项目：`/init` 会生成家目录的 THINKER.md，模型说 "src/main.py" 也会被补成家目录下的路径。第 14.1 轮加了启动提醒和 `/init` 守卫，但根治办法只有一条——先 `cd` 到项目目录再启动。
- **路径不设限是刻意的，别"顺手"把边界加回 `_resolve_path()`**。第 15.1 轮查清了根因：当时那道边界只拦 file_*，`cmd_runner` 一行检查都没有，模型遇到"超出工作区"就改拼 shell 绕过去——对愿意绕的模型这道门是软的，对守规矩的模型它只是把"搜个桌面文件"变成"先改 .env 再重启"。所以第 15.2 轮整条删掉了，五个工具现在都能操作任意路径。真要重新引入限制，去做 `classify()`（权限层），让权限层和工具层说同一句话，别让工具层单独拒绝、逼模型去找后门。
- **`_resolve_path()` 现在只做路径规范化**，不判断权限：反斜杠转正斜杠、相对路径补 `WORKSPACE_ROOT`、`resolve()` 展开 `..` 和软链接。其中 `resolve()` 不能省——`_read_files` 是拿路径字符串当键来支撑"先读后写"的，同一个文件用不同写法指过来必须落成同一个字符串，否则 SHA1 校验会漏判。
- **工作区外的路径在回执里显示为绝对路径**（`_relative()` 的 `except ValueError` 分支）。这不是 bug：模型拿到完整路径，正好可以直接喂给下一次 `file_read`。
- **工具说明书里的每一句话都是"授权"**。`cmd_runner` 的说明书原来写着「可用 file_glob，或用 `dir` 查看」，模型记住的是更熟的 `dir`——于是连发 6 条 `dir /s` 去找文件，其中一条全盘递归超时（第 15.3 轮）。同一件事永远只在一份说明书里给一条路，别写"二选一"；模型会挑它最熟的，不是最好的。同理，说明书里残留的"工作区"字样会让它以为还有范围限制。
- **事前约束无效时，把引导放进工具结果里**。提示词和工具说明书是"事前"的，模型可能根本没读进去（第 15.3 轮实测：加载着新提示词照样用 shell）；而工具结果是它每轮必读的。`CmdRunner.act` 命中查找类命令时在结果末尾追加一行建议，就是这个思路。注意判据要收窄，否则提示变噪音、然后被忽略。
- **主输入走自管，其余走 `input()`**。`InputHandler._read_line` 只在"需要补全 + 环境支持 + 没开 `THINKER_PLAIN_INPUT`"时才用 `LineEditor`（逐字符读）；授权提问、选会话编号一律用原生 `input()`。所以按键级的新功能（上下键历史、光标左右移动、Ctrl+U）只能加在自管这条路上，`input()` 那条永远做不到——加之前先想清楚哪些调用点需要它。（第 16.2 轮之前这里是"先探一个字符再分流"，那条路已废，见下面那条。）
- **Esc 的检查点不能只放在「收数据」的循环里**。第 18.7 轮之前，`for chunk in iterator: if esc.triggered: break` 看着没问题，但模型思考时（首字节未到）循环体一次都不执行，那十几秒里按 Esc 完全无效。现在读流在后台线程（`core/stream_pump.py`），主线程每 50ms 醒一次先看 Esc。以后任何「等外部 IO」的地方（网络、子进程、文件锁）想支持打断，都得用同样的形状：别在主线程同步等，否则检查点再密也没机会跑。
- **不许 `except Exception: return False` 这种静默降级**。第 16.1 轮的 `unget` 就是这么写的：`msvcrt.ungetch` 要的是 bytes（`b"a"`），传 str 必 TypeError；宽字符版 `ungetwch` 对中文又抛 OSError。异常被 `except` 吞掉后，代码静默退回有 bug 的老写法——用户看到的是"补丁完全没生效"。降级分支至少要往 stderr 打一行，否则它不是兜底，是把 bug 藏起来。
- **不确定的 API，先跑一行验证再动结构**。`ungetch(str)` 会不会报错，一行 `python -c` 就知道；第 16.1 轮却先写了整套逻辑、测试和文档，最后发现这一步从根上不成立。同理：`getwch` 返回 str 但 `ungetch` 收 bytes，这种"读和写不对称"的地方特别容易想当然。
- **输入层是按「一行」写的；第 18.8 轮起，多行输入不再靠反推光标行列，而是整段受控重绘**。18.6 轮那套"`\b` 擦 + 上移一行 + CHA `\x1b[nG` 跳列"在 conhost 真机上没修好，三个根因叠着：① `theme.prompt_text()` 带颜色转义码，旧 `display_width` 把 `\x1b[38;5;209m` 当成可见字符算，折行点凭空早了十几列；② `\b` 在可视行行首不跨行，被删字符留在下一行没人擦——就是用户看到的"第一行留下最后一个字符"；③ conhost 与 xterm 写满一行时的 autowrap 时机不同，反推行号会差 1，CHA 在部分终端还不生效。现在 `_repaint_frame()` 自己按 `cols-1` 切行、用 `\x1b[1B` 显式下移、逐行空格覆盖，光标位置完全由代码状态（`_frame_rows` / `_frame_cursor_row`）决定。`_cursor_rowcol()` 降级为诊断算例，**不要再用它驱动光标指令**。改这块时一定要跑 `test_line_wrap.py` 的 `FakeScreen` 用例：它按 VT 语义执行真实输出序列并检查屏幕上有没有残字，"数 `\x1b[1A` 出现几次"证明不了屏幕干净。
- **`THINKER_INPUT_PADDING` 默认 4（18.6 轮是 2），会改变输入行下方的空白**。它靠 `\n` × N + `\x1b[NA` 实现。关键是 **N 同时也是"能往上滚几行"**：conhost 只要检测到程序有输出就把视口拉回光标所在行，往上滚超过 N 行光标就出视口，敲第一个字屏幕立刻弹回底部——2 行的体感就是"只能往下滚、字永远贴在最后一行"。长输入把留白吃掉时，`_repaint_frame()` 会按需再撑开缓冲区。改输入层测试时若看到提示符前多了 `\n` 和上移序列，那是它，在测试文件里设 `0` 关掉即可——不要为此改断言去迎合它。
- **Windows 控制台的键值和 Unix 不一样**：退格是 `\x08` 不是 `\x7f`，方向键是 `\x00` / `\xe0` 开头的**两字节**（漏掉第二个字节，它就会被当成普通字符插进输入里变成乱码）。这些值散在代码里极难排查，统一放在 `ui/line_editor.py` 的常量区。
- **`input()` 的 prompt 不是输入内容**。拼进 prompt 的字符显示得出来，但退格删不掉、也不会出现在返回值里（第 16.1 轮踩过：用户输入的第一个字删不动，而且被静默丢掉一个字）。要让一个已读走的字符"回到输入流"，用 `msvcrt.ungetch()` 塞回控制台缓冲区，然后正常调 `input(prompt)`——别拼 prompt。
- **测试断言别写"我的实现长什么样"**。第 16 轮有一条断言是 `seen_prompts == ["> 帮"]`，它把 bug 当成了正确答案：测试全绿，而用户根本删不掉那个字。断言要写用户视角能观察到的结果（返回值完不完整、提示符干不干净、能不能删），不要写"当时我是怎么实现的"——后者只会把错误的选择一起固化下来。
- **压缩档位是「落点直做」，层与层之间不会互相兜底**。消息不足 50 条、占用落在 `[0.80, 0.85)` 时，L2（Snip）根本剪不动，这一轮就等于没压，要等占用涨到 0.85 由 L3 接手。这是有意的：改成"本层无效就往下试"会把"83% 就调摘要模型"的老问题请回来，而分层的全部意义是成本可预测。第 17.1 轮确认过，别当成 bug 去"修"。
- **`get_projection()` 返回的必须是全量深拷贝**。它的文档写着"与底层存储完全解耦"，但最早那版 `system_messages` 只做了列表推导，元素还是原字典——发请求前给投影打 `cache_control` 会直接改到 `messages_history` 里的 system 消息，当下看不出问题，等换到不认这个字段的接口就是 400（第 17.1 轮抓到的）。以后往投影里加东西，先确认没有共享引用。
- **测试脚本失败必须 `sys.exit(1)`**。有几份老脚本只在 `except` 里打印 `❌` 就结束，退出码仍是 0，批量跑会被算成通过——第 17.1 轮就是这么发现两个压缩用例早已失效的（"21 个脚本全绿"其实藏了两个红）。新写的测试一律带非 0 退出码；批量跑时同时看退出码和输出里的 `❌|Traceback`。
- **子 agent 的工具白名单是「工具粒度」，不是「命令粒度」**。给了 `cmd_runner` 就等于给了全部命令，做不到"只允许 git diff"。所以内置 `explore` 的白名单只有 `file_glob/file_grep/file_read` 三个只读工具——这也是它全程零审批的前提：审批判定（`permissions.classify`）不看"当前是不是子 agent"，只要工具要问就一定会问（第 18 轮）。
- **`todo_write` 的状态存在工具实例上，而实例在 ToolManager 单例里只有一份**。子 agent 一调它就会覆盖主 agent 的待办清单，连状态行提醒都跟着错，且没有任何报错。所以任何子 agent 的白名单都别给 `todo_write`（第 18 轮发现）。
- **子 agent 只有内置这一种来源**。第 18 轮先做过 `.thinker/agents` + `~/.thinker/agents` 两层目录，后来按需求整套删掉了。加类型要在 `src/tools/subagent/builtin_agents.py` 里加一个 `AgentSpec` 常量；**不要再把目录扫描加回来**——那会带回优先级、同名覆盖、来源标注三个概念，而维护定义的只有一个人。`test_subagent_types.py` 里有一条 `test_project_dir_is_ignored` 专门盯着这件事。
- **技能的元数据是注册期快照，正文才是实时的**。改了 `name` / `description` / 两个布尔要 `/skills reload`（它们进了工具说明书和 `REGISTRY`）；只改正文不用——`skill_spec.render()` 每次都从磁盘现读。别把这两件事混成一句"技能支持热更新"，不然用户会以为改了描述立刻生效。
- **子 agent 不给 `skill`**，和 `task` 一样是两道闸（`_get_available_tools` 的 exclude + `_is_tool_allowed`）。理由不只是省 token：技能正文是写给主 agent 的，里面常有"用 `todo_write` 记进度"这类话，子 agent 照做会踩掉主 agent 的待办清单（单例实例）。
- **技能命令要记在 `_skill_command_names` 里**。`REGISTRY` 里同时有内置命令和技能命令，同名冲突检查（`builtin_command_names()`）必须把技能那部分减掉，否则 reload 之后技能会"跟自己的旧命令重名"而被拒载——表现为"能用一次，reload 就消失"。
- **假 YAML 会把 `argument-hint: [文件路径]` 当成内联列表**。已在 `skill_spec._hint_text()` 里还原成方括号写法；以后再加"看起来像列表"的字段，记得同样处理，别去改解析器——那会变成给每个字段开特例。
- **会话文件是懒创建的（第 21.7 轮），别"顺手"改回启动即落盘**。启动、/clear、本地斜杠命令都不建 JSONL；第一条真正发给模型的消息（含技能命令）才建，system 行由 `SessionStore.create(messages=...)` 一次补齐。空壳会话文件会把 `/resume` 列表填满，这才是这条规则的动机。
- **不要再给技能加"模型看不见"的开关，也不要加"未生效字段"的标注**（21.1 / 21.2 各删过一次）。可见性多一维就多一个"我明明写了技能，模型却说不知道"的排查方向；字段标注则要"识别 → 存字段 → 拼展示 → 写测试"四处跟着维护，而它不改变任何行为。现在规则只有一条：**认得就读，不认得就丢**。清单长度靠 `MAX_SKILLS_*` 卡，不靠藏技能。
