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
  core/          对话总管、API 客户端、历史/压缩、权限、提示词（含 memory.py）
  tools/         内置工具 + MCP 适配 + 子 agent
  ui/            显示与输入（theme 管配色符号）
  mcp_servers/   示例 MCP 服务器（weather / calculator）
test/            脚本式测试，不是 pytest
docs/优化记录/    每轮改动的详细记录
```

## 常用命令

```bash
# 启动（必须在项目根目录，.env 是相对 cwd 读的）
thinker

# 生成 / 更新项目记忆骨架
thinker --init          # 已存在则跳过
thinker --init --force  # 覆盖重写

# 跑测试：都是独立脚本，没有 pytest
python test/test_memory.py
python test/test_conversation_loop.py
python test/test_p0_fixes.py
```

两个 conda 环境都装了 editable 版：`D:\Anaconda\envs\claude` 和 `D:\Anaconda\envs\thinker`。

## 代码约定

- **注释用中文，解释"为什么"而不是"是什么"**。这是项目最重要的风格，每个非平凡的决定都要写清背景和踩过的坑。
- 每个文件顶部有一段 `# =====` 包围的说明，讲这个文件在整体里负责什么。
- 改动涉及行为时，注释里标注轮次，例如 `【2026-11-07 第 11 轮】`，方便对照 `docs/优化记录/`。
- 新增功能要补测试，测试写成**独立脚本**：直接 `python test/xxx.py`，最后打印 `🎉 所有测试通过（N/N）`。
- 界面上的符号和颜色只能从 `ui/theme.py` 取，不要在各处硬编码。

## 不要做的事

- **不要用 `⏺`（U+23FA）**——Windows 终端会把它交给 emoji 字体渲染成彩色圆点，颜色不受 rich 控制。圆点只能用 `●`（U+25CF）。
- **等待动画不要用 `rich.Live`**——它靠"光标上移 + 覆盖"重绘，从不发清行指令，宽度一变就留残影。等待态用单行 `\r` 覆盖（见 `DisplayManager._spin_loop`）。
- **不要引入 pytest**，保持测试零依赖。
- **不要改 `enviroment.py` 的函数名**（`get_enviroment_info` 是原作者拼错的 environment），多处调用，改了会连锁报错。
- 不要动 `core/permissions.py` 的判定顺序，那是有意排的。

## 已知的坑

- `mcp` 库 1.x 用 `inputSchema`、2.x 用 `input_schema`，`McpTool` 里做了双向回退，改这块要同时考虑两个版本。
- `ToolManager` / `Conversation` / `HistoryManager` 全是单例 + 类属性，一次只能跑一个子 agent。做并行之前必须先去掉单例。
- 上下文压缩会整体替换消息列表，导致前缀缓存失效；压缩动作应尽量落在缓存断点之后。
