# =============================================================================
# 文件：src/ui/esc_watcher.py
# 作用：等模型回复的时候，监听 Esc 键，让用户能随时打断。
#
# 【一、解决什么问题】
#   现在模型一旦开始生成，用户就只能干等：
#     · 它理解错了，正在写一段你不想要的长解释
#     · 它卡在一个死循环里反复调工具
#     · 你已经想到更好的说法，想换一个问法
#   唯一的办法是 Ctrl+C，但那是"杀掉整个程序"——上下文全丢。
#   所以需要一个"只打断当前这一轮，不退出"的键，就是 Esc。
#
# 【二、为什么用后台线程而不是在主循环里轮询】
#   主循环在等网络数据（`for chunk in iterator`），两段数据之间可能隔几百毫秒。
#   如果在那个循环里检查键盘，用户按下 Esc 之后要等到下一块数据到了才响应，
#   体感上就是"按了没反应"。
#   独立线程每 50ms 查一次，按下就能立刻置标志，主循环下一块数据到达时马上看到。
#
# 【三、为什么要处理"读不到键盘"的情况】
#   · 非 Windows 系统没有 msvcrt 模块
#   · 输出被重定向到文件时（管道、CI）根本没有终端可以读
#   这两种情况直接降级成"不可用"，程序照常跑，只是不能按 Esc 打断。
#   绝不能让"打断功能"本身成为启动失败的原因。
#
# 【四、只管 Esc，不管别的键】
#   msvcrt.getch() 会**消费**读到的字符。如果这个线程把所有按键都读走，
#   用户在别处输入的字符就丢了。所以只认 Esc（0x1b），其他字符原样丢弃但不处理。
#
#   代价是：等回复期间用户提前敲的字会被吞掉。这个取舍是有意的——
#   目前输入框（input()）在等模型时本来就是关闭的，用户没有地方可以打字，
#   真正会丢字符的场景几乎不存在。
#   等以后做了"流式输入队列"，这里需要改成"把非 Esc 字符塞进队列"。
# =============================================================================

import sys  # 判断是不是真终端
import threading  # 后台监听线程
import time  # 轮询间隔

ESC = b"\x1b"  # Esc 键的字节值（ASCII 27）


class EscWatcher:
    """监听 Esc 的后台线程（2026-11-07 第 13 轮）

    用法：
        watcher = EscWatcher()
        watcher.start()          # 开始监听
        for chunk in stream:
            if watcher.triggered:   # 用户按了 Esc
                break
        watcher.stop()           # 收工（一定要调，否则线程会一直转）

    生命周期要包在 try/finally 里，或者用 with。见 conversation.py 的用法。
    """

    def __init__(self):
        self._pressed = threading.Event()  # 按下的标志，主线程查这个
        self._stop = threading.Event()  # 通知监听线程退出
        self._thread = None  # 监听线程本体
        self._msvcrt = None  # Windows 的键盘模块，拿到才能用
        try:
            import msvcrt  # 只有 Windows 有
            self._msvcrt = msvcrt
        except ImportError:
            self._msvcrt = None  # 其他系统：保持不可用，不报错

    @property
    def available(self) -> bool:
        """这个环境能不能用 Esc 打断

        三个条件都得满足：有 msvcrt（Windows）、标准输入是真终端、
        当前没在读别的东西。
        """
        if self._msvcrt is None:
            return False
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except (ValueError, AttributeError):
            # 某些环境下 stdin 被关掉或不是文件对象，isatty 会抛异常
            return False

    @property
    def triggered(self) -> bool:
        """用户有没有按过 Esc（查到就可以一直查，标志不会自动清）"""
        return self._pressed.is_set()

    def start(self) -> None:
        """开始监听（2026-11-07 第 13 轮）

        重复调用是安全的：已经在监听就直接返回，不会起第二个线程。
        （两个线程同时读键盘会互相抢字符，行为会变得不可预测）
        """
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.available:
            return  # 环境不支持，静默降级

        self._pressed.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="esc-watcher")
        self._thread.start()

    def _loop(self) -> None:
        """轮询键盘（2026-11-07 第 13 轮）

        50ms 一次。为什么不是 10ms：
          再快人也感知不到差别，却会让这个线程占掉不必要的 CPU。
        为什么不是 200ms：
          200ms 的延迟已经能感觉到"按下去愣了一下"。
        """
        while not self._stop.is_set():
            try:
                if self._msvcrt.kbhit():  # 缓冲区里有按键
                    char = self._msvcrt.getch()
                    if char == ESC:
                        self._pressed.set()
                        return  # 已经达成目的，线程可以直接结束
                    # 其他键忽略：见文件头"只管 Esc"的说明
            except Exception:
                # 终端被关掉、句柄失效等，安静退出，不要让异常冒到主线程
                return
            self._stop.wait(0.05)

    def stop(self) -> None:
        """停止监听并等一下线程结束（2026-11-07 第 13 轮）

        必须 join：否则这次对话结束、下一次又要 start 一个新的，
        旧线程还活着就会和新线程抢键盘输入。
        timeout 给 1 秒，正常情况几毫秒就退出了；
        万一卡住也不能无限等——宁可留个僵尸线程，也不能让界面卡死。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def reset(self) -> None:
        """清掉"按下过"的标志，准备下一轮（2026-11-07 第 13 轮）

        每轮请求前调一次。不清的话，这一轮按过 Esc，后面每一轮都会被判成"已中断"。
        """
        self._pressed.clear()

    # 支持 with 语句，避免调用方忘记 stop
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
