# UI 模块

这个模块把"界面上的事"和"业务逻辑"彻底分开：输入归 `InputHandler` 管，显示归 `DisplayManager` 管，
上层只跟 `UIManager` 打交道。这样代码更好维护，也更容易单独测试。

> 最后核对：2026-11-07（第 10 轮）。下面的类名和方法名都与当前代码一致。

## 快速上手

```python
from ui.ui_manager import UIManager

# 创建界面管理器（单例，全程序共用一个）
ui = UIManager()

# 显示各类消息
ui.print_assistant_message("你好 **世界**！")
ui.print_error("出错了")
ui.print_success("操作完成")

# 获取用户输入
user_input = await ui.get_user_input()
# 确认：回车即同意；输入别的内容 = 拒绝 + 那句内容当作说明（返回 (是否执行, 理由)）
should_run, reason = await ui.wait_for_user_approval("是否执行？")

# 流式显示
ui.start_stream_display()
ui.print_streaming_content("正在逐字输出…")
ui.stop_stream_display()
```

## 模块结构

### `UIManager`
总协调者（外观模式 / Facade）：给所有界面操作提供统一入口，采用单例模式，全程序只有一份状态。

### `InputHandler`
负责所有"从键盘拿输入"的操作：
- `get_user_input()` —— 带提示符的基础输入
- `wait_for_user_approval()` —— 读一行并解释成 (是否执行, 拒绝理由)：回车走默认值，命中同意/拒绝词表照词表，其他内容一律当"拒绝 + 那句反馈"
- `interpret_approval()` —— 上面那条规则的纯函数版本，不读键盘，方便单测
- `get_choice_input()` —— 从预设选项里选一个

### `DisplayManager`
负责所有"往屏幕上打印"的操作：
- `print_assistant_message()` —— 按 Markdown 渲染模型回复
- `print_error/success/info()` —— 带样式的错误 / 成功 / 提示消息
- `print_status()` —— 状态行（上下文水位、花费、缓存命中率，前两项会按水位变色）
- `start/stop_stream_display()` —— 流式显示的开始与结束
- `print_streaming_content()` —— 逐段显示模型吐出来的文字
- `tool_header()/tool_result()` —— 工具调用的两行式输出（● / ⎿）
- `approval_question()/tool_denied()` —— 授权提问（? 那一行）与被拒绝后的提示

### 基类（`base.py`）
为以后扩展预留的抽象接口：
- `BaseInputHandler` —— 输入组件的合同
- `BaseDisplayManager` —— 显示组件的合同
- `UIConfig` —— 一组可配置项（各类图标、刷新频率）

## 功能特性

- **Markdown 渲染**：用 Rich 库显示富文本
- **流式显示**：内容实时刷新；等待模型时还有 ✻ 旋转动画和秒数（第 7 轮新增）
- **图标与配色**：第 7 轮起改为 Claude 风格的圆点 / 星形 + 品牌橙，emoji 已大幅收敛
- **异步输入**：等键盘输入时不阻塞事件循环
- **单例模式**：界面状态全程序一致
- **类型标注**：关键方法都带类型提示
- **方便扩展**：抽象基类留好了换界面后端的位置

## 这样拆分的好处

1. **职责分离**：界面逻辑不再混在业务逻辑里
2. **好维护**：接口清晰，各管一摊
3. **好测试**：界面可以整个换成假对象（mock）
4. **可复用**：别的模块也能直接用这些组件
5. **可配置**：`UIConfig` 定义了配置项，配色集中在 `theme.py`（见文末说明）
6. **好扩展**：以后加新界面功能不用动上层

## 从旧写法迁移过来

### 改前
```python
# 在 Conversation 类里自己管输入和显示
user_input = await self.get_user_input()
self.print_assistant_messages("你好")
```

### 改后
```python
# 改用 ui 模块
ui = UIManager()
user_input = await ui.get_user_input()
ui.print_assistant_message("你好")
```

## 配置

```python
from ui.base import UIConfig
from ui.ui_manager import UIManager

config = UIConfig(
    default_emoji_user="👨‍💻",
    default_emoji_assistant="🤖",
    stream_refresh_rate=15
)
```

注意：`UIConfig` 目前只是把配置项定义好了，`UIManager` 还没有接收它——
上面这段只能说明"配置长什么样"，不会真的改变界面。
实际的配色、提示符、spinner 文案统一放在 `ui/theme.py` 里（第 7 轮起），改那里才会生效。

## 第 10 轮的两条硬规矩

1. **圆点只能用 `theme.BULLET`（`●` U+25CF），不要写 `⏺`（U+23FA）。**
   后者会被 Windows 终端交给 emoji 字体渲染，颜色不受 rich 控制，还会和橙色 `✻`
   交替闪成"橙绿闪烁"。同理，任何"要靠颜色区分"的符号都要挑纯文本字符。
2. **等待态与正文态之间要有一次性开关（`_stream_has_content`）。**
   流式接口会发空 delta，如果按"缓冲区当前是否为空"判断，界面会在 spinner
   和正文之间反复横跳。
