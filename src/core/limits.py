# =============================================================================
# 文件：src/core/limits.py
# 作用："喂给模型的输出上限"只在这里定义一次，其他地方一律 import。
#
# 【为什么要有这个文件】（2026-11-06 第 9 轮）
#   原来同一个数字被定义了两遍：
#     cmd_runner.py       MAX_OUTPUT_CHARS = 30000     ← 工具自己先砍一刀
#     conversation.py     MAX_TOOL_OUTPUT_CHARS = 12000 ← 写进历史前又砍一刀
#   结果 30000 那一刀等于白写：输出刚到 30000，进对话历史时又被砍到 12000。
#   更难受的是 file_read：一次读 2000 行（约 15~20k 字符）本来读全了，
#   也会在写进历史时被腰斩，模型只能反复调整 offset 重读，反而多花钱。
#
# 【现在的规则】
#   1. 工具自己可以设更严格的上限（比如 file_read 的 2000 行），那是"业务上限"；
#   2. MAX_TOOL_OUTPUT_CHARS 是"兜底上限"，只负责拦住异常巨大的输出；
#   3. 兜底上限必须 >= 任何一个业务上限，否则业务上限就是假的。
#
# 【用环境变量改】
#   MAX_TOOL_OUTPUT_CHARS=30000  （默认 30000，约相当于 1 万 token 的中文）
# =============================================================================

import os  # 读环境变量


def _int_env(name: str, default: int, minimum: int = 1000) -> int:
    """从环境变量读一个整数，读不出来或明显不合理就用默认值

    为什么要有 minimum？
      如果谁手滑写成 MAX_TOOL_OUTPUT_CHARS=10，工具结果会被砍成两三个字，
      模型完全没法干活，而且这种错很难看出来。这里兜住下限。
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():  # 没配
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):  # 配了个 "abc"
        return default
    return max(minimum, value)


# 单条工具输出写进对话历史时的兜底上限（字符数）
MAX_TOOL_OUTPUT_CHARS = _int_env("MAX_TOOL_OUTPUT_CHARS", 30000)
