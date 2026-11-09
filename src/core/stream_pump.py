# =============================================================================
# 文件：src/core/stream_pump.py
# 作用：把「读流式响应」和「检查 Esc」解耦，让用户在模型思考时也能打断。
#
# 【2026-11-09 第 18.7 轮新增】为什么需要这个文件：
#   改前 Esc 的检查点是这么写的：
#
#       for chunk in iterator:          # ← 阻塞在 next() 上
#           if esc.triggered: break     # ← 只有"收到一块数据"才会执行到
#
#   模型思考的时候（首字节还没到），`for` 循环体一次都不执行，检查点形同虚设。
#   实测：问一个要思考 11 秒的问题，这 11 秒里按 Esc 完全没反应——标志位设了，
#   但主线程卡在网络读上，没人看它。
#
#   解法只有一条：不能在主线程里同步等网络。把"读流"挪到后台线程，
#   主线程每隔 50ms 醒一次，先看 Esc 再看队列。这样无论模型在思考还是在吐字，
#   按键都能在 50ms 内生效。
#
# 为什么不用 asyncio 的 wait_for / 超时：
#   这里的流是 openai SDK 返回的**同步生成器**，没有 await 点，wait_for 管不到它。
#   除非把整个客户端换成 AsyncOpenAI，那是另一个量级的改动，
#   而"按 Esc 要等 11 秒"不值得为它换客户端。
#
# 为什么用线程 + 队列而不是直接读多块：
#   队列天然保序（单生产者单消费者），chunk 的顺序和网络到达顺序一致；
#   而且异常的传递也有地方放（放进队列，主线程再抛出来），
#   不会出现"后台线程抛了异常、主线程永远卡在 q.get"。
# =============================================================================

import queue  # 线程间传数据：读流的线程 put，主线程 get（带超时）
import threading  # 后台泵线程

# 主线程多久醒一次看 Esc。
#   50ms 是"按键手感"和"空转开销"的折中：再短（比如 5ms）纯属空转，
#   再长（比如 200ms）按键会明显有延迟。EscWatcher 自己也是 50ms 轮询一次。
DEFAULT_POLL_INTERVAL = 0.05


class StreamInterrupted(Exception):
    """用户按 Esc 打断了流式输出

    为什么用异常而不是返回一个特殊值：
      "被打断"和"正常读完"是两条完全不同的收尾路径（前者要保留已输出的半截内容
      并标记 [用户中断]，后者要写完整回复）。用异常能让调用方的两条路径
      在代码上分开，而不是在每个 chunk 处理分支里都判断一次"我是不是该退出了"。
      它是一条**正常控制流**，不是错误——所以单独定义类型，不要混进 Exception 的兜底里。
    """


def iter_stream_with_esc(iterator, esc, poll_interval=DEFAULT_POLL_INTERVAL):
    """边产出流数据、边检查 Esc；按键立刻抛 StreamInterrupted

    参数 iterator：同步迭代器（openai SDK 的流式生成器）。
    参数 esc：EscWatcher（有 `.triggered` 属性）；传 None 表示这个环境不支持打断，
              此时退化成"直通"，行为和旧代码完全一致——非 Windows / 非终端
              （管道、CI）都走这条路，不能因为"没有键盘"就让对话跑不起来。
    参数 poll_interval：多久检查一次，默认 50ms。

    产出：原样产出 iterator 里的每一项，顺序不变。
    异常：
      · StreamInterrupted —— 用户按了 Esc；
      · 源流的异常 —— 原样抛出（调用方本来就有 fallback 逻辑依赖它）。

    实现要点：
      · 泵线程是 daemon：主流程异常退出时它不会拖住进程；
      · 收到"完整消息"后调用方会 break，生成器被关闭 → finally 里设 stop_pump，
        泵线程下一次循环就会自己退出，不会泄漏；
      · 打断时泵线程可能仍阻塞在 `next(iterator)` 上（网络读没法中断），
        它会在这块数据到达后看到 stop 标志并退出。daemon 线程，不影响主流程。
    """
    if esc is None:
        # 没有监听器 → 直接透传。这不是"降级实现"，而是明确的产品行为：
        # 没有键盘环境时，Esc 这个功能不存在，其它一切都该照常。
        yield from iterator
        return

    chunk_queue = queue.Queue()
    stop_pump = threading.Event()

    def _pump():
        """后台线程：把 iterator 里的东西搬进队列

        为什么要包一层 try/except：读流的过程中可能抛网络异常（超时、断连）。
        不接住的话异常会死在子线程里，主线程永远等不到结束信号，
        表现为"程序卡死"——比报错难查得多。
        """
        try:
            for item in iterator:
                if stop_pump.is_set():  # 主线程已经不要了（Esc 打断 / 已 break）
                    return
                chunk_queue.put(("chunk", item))
            chunk_queue.put(("end", None))  # 正常读完
        except Exception as exc:  # noqa: BLE001 —— 原样转交给主线程抛
            chunk_queue.put(("error", exc))

    threading.Thread(target=_pump, name="thinker-stream-pump", daemon=True).start()

    try:
        while True:
            if esc.triggered:
                raise StreamInterrupted()
            try:
                kind, payload = chunk_queue.get(timeout=poll_interval)
            except queue.Empty:
                continue  # 这 50ms 没数据也没按键，继续等
            if kind == "end":
                return
            if kind == "error":
                raise payload  # 把子线程的异常在主线程重新抛出，调用方的 fallback 才接得住
            yield payload
    finally:
        stop_pump.set()  # 无论怎么退出，都通知泵线程收工
