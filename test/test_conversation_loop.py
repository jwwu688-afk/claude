# =============================================================================
# 文件：test/test_conversation_loop.py
# 作用：测试第 8 轮对 Conversation 主循环的改造——
#       1. 递归改成 while 后，跑几百轮调用栈都不再变厚
#       2. "只读工具免确认"的授权默认值判定是否正确
#
# 和 test_crop_message.py 一样，是"函数 + assert"的手写测试：
#   assert 要求某件事必须为真，不成立就抛 AssertionError。
# 直接运行方式：在项目根目录执行 python test/test_conversation_loop.py
#
# 注意：这里用 __new__ 造了一个"空壳"Conversation，把 _run_one_iteration
# 换成假函数，所以不会连 MCP、不会建 API 客户端、更不会调用模型 API。
# 测的是"循环骨架"，不是模型链路。
#
# 【2026-11-06 第 8 轮】新增本文件。
# =============================================================================

import asyncio  # 主循环是 async，要用 asyncio.run 启动
import inspect  # 读方法源码，静态确认里面没有"自己调自己"
import os  # 拼路径时用到
import sys  # 改模块搜索路径；也用来量调用栈深度
import traceback  # 失败时打印完整错误现场

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# 从 test/ 跑脚本时 Python 找不到 src/ 下的模块，手动把 "../src" 塞进搜索路径

from core.conversation import Conversation, READ_ONLY_TOOLS  # 导入要测的主循环和只读工具清单


def make_conversation():
    """造一个不连任何外部资源的"空壳" Conversation（2026-11-06 第 8 轮新增）

    为什么用 __new__ 而不是 Conversation()？
      正常的 __init__ 会建 ToolManager（连 MCP）、APIClient（读密钥）、UIManager，
      这些都是测试不需要的。__new__ 只造对象、不初始化，
      我们再把唯一要用的 _run_one_iteration 换成假函数即可。
    """
    Conversation._instance = None  # 清掉单例，避免拿到上一次测试留下的对象
    Conversation._initialized = False
    return Conversation.__new__(Conversation)


def stack_depth() -> int:
    """量一下当前调用栈有多少层（用来证明栈深度不随轮数增长）"""
    depth = 0  # 从当前帧开始数
    frame = sys._getframe()
    while frame is not None:  # f_back 指向"调用我的那一帧"，一直往上走
        depth += 1
        frame = frame.f_back
    return depth


def test_loop_no_stack_growth():
    """测试：连跑 600 轮，栈深度保持恒定（本轮最核心的一条）"""
    print("测试: 主循环跑 600 轮不累积调用栈")

    async def scenario():
        conv = make_conversation()
        depths = []  # 记录每一轮量到的栈深度
        counter = {"round": 0}  # 用字典存计数，闭包里改起来方便

        async def fake_iteration():
            """假的"单层迭代"：不调模型，只记深度并决定要不要继续"""
            counter["round"] += 1
            depths.append(stack_depth())
            return counter["round"] < 600  # 前 599 轮继续，第 600 轮收尾

        conv._run_one_iteration = fake_iteration  # 实例属性会覆盖类里的真方法
        await conv._recursive_message_handling()  # 跑驱动器
        return counter["round"], depths

    rounds, depths = asyncio.run(scenario())

    assert rounds == 600, f"应该跑满 600 轮，实际 {rounds}"
    spread = max(depths) - min(depths)  # 最深与最浅之差
    assert spread <= 2, f"栈深度不应随轮数增长，实际波动 {spread} 层"
    assert depths[-1] == depths[0], "最后一轮和第一轮应该一样深"
    print(f"  · 实测：600 轮，栈深度 {depths[0]} 层 → {depths[-1]} 层，波动 {spread} 层")
    print("✓ 通过: while 驱动下调用栈深度恒定")


def test_loop_stops_on_false():
    """测试：单层迭代返回 False 时，驱动器要立刻停下来"""
    print("\n测试: 返回 False 时循环停止")
    conv = make_conversation()
    counter = {"round": 0}

    async def stop_immediately():
        counter["round"] += 1
        return False  # 模拟"子 agent 交差"或"请求彻底失败"

    conv._run_one_iteration = stop_immediately
    asyncio.run(conv._recursive_message_handling())

    assert counter["round"] == 1, f"应该只跑 1 轮，实际 {counter['round']}"
    print("✓ 通过: 收到 False 后立刻收尾")


def test_exception_propagates():
    """测试：异常照样往外冒（和递归版本一样由 start_conversation 兜底）"""
    print("\n测试: 异常不被循环吞掉")

    async def boom():
        raise RuntimeError("模拟工具炸了")

    conv = make_conversation()
    conv._run_one_iteration = boom

    caught = None  # 记下抓到的异常
    try:
        asyncio.run(conv._recursive_message_handling())
    except RuntimeError as e:
        caught = e
    assert caught is not None, "异常应该继续向外抛，不能被 while 悄悄吃掉"
    assert "模拟工具炸了" in str(caught), str(caught)
    print("✓ 通过: 异常传播语义与改造前一致")


def test_no_self_recursion_in_source():
    """测试：驱动器里确实没有"自己调自己"，且单层迭代有四个明确出口"""
    print("\n测试: 源码里不再有自递归")
    driver = inspect.getsource(Conversation._recursive_message_handling)
    iteration = inspect.getsource(Conversation._run_one_iteration)

    assert "while True" in driver, "驱动器应该是 while 循环"
    assert "await self._recursive_message_handling(" not in driver, "驱动器里不能还剩自调用"
    assert "_run_one_iteration()" in driver, "驱动器应该把活交给单层迭代"
    assert "return True" in iteration and "return False" in iteration, "单层迭代要有明确的返回值"
    print("✓ 通过: while 驱动 + 单层迭代，自调用已清除")


def test_approval_rules():
    """测试：只读工具免确认、其他默认要问、模型显式写的优先"""
    print("\n测试: 授权默认值集中判定")

    for name in READ_ONLY_TOOLS:  # 只读清单里的工具，没写参数时不该弹确认
        assert Conversation._needs_user_approval(name, {}) is False, f"{name} 不该弹确认"

    for name in ("cmd_runner", "file_write", "file_edit", "task", "smart_context_cropper"):
        assert Conversation._needs_user_approval(name, {}) is True, f"{name} 默认应该要确认"

    # 模型显式写了就按它写的来（兼容 cmd_runner 一直以来的用法）
    assert Conversation._needs_user_approval("cmd_runner", {"need_user_approve": False}) is False
    assert Conversation._needs_user_approval("file_read", {"need_user_approve": True}) is True
    print(f"  · 只读清单 {sorted(READ_ONLY_TOOLS)} 免确认，其余默认要问")
    print("✓ 通过: 三条授权规则都正确")


def test_tool_call_chain_approval():
    """测试：从对话层发起真实工具调用，验证授权分流与结果回写整条链

    前面几个用的是假的 _run_one_iteration，这里换成真的 _handle_tool_calls：
    真 ToolManager + 真 file_read，只有界面和历史换成假的。
    """
    print("\n测试: 工具调用链（只读免确认 / 写入要确认）")
    import json  # 把参数字典转成模型给的那种 JSON 字符串
    import tempfile  # 造临时工作区
    from pathlib import Path
    from tools import file_tools  # 要把工作区指到临时目录
    from tools.tool_manager import ToolManager

    tmp = tempfile.TemporaryDirectory(prefix="quickstar_chain_")
    root = Path(tmp.name).resolve()
    file_tools.WORKSPACE_ROOT = root
    (root / "a.py").write_text("print('hi')\n", encoding="utf-8")

    class FakeUI:
        """假界面：只记"问了几次、真正执行了哪些工具"，不真的往屏幕上打印"""
        def __init__(self):
            self.approvals = 0  # 弹了几次确认
            self.prepared = []  # 画过"⏺ 工具名"头部的工具
            self.executed = []  # 真正进到执行阶段的工具
            self.denied = []  # 被拒绝的工具提示
        def show_preparing_tool(self, name, args):
            # 【第 8.2 轮】头部现在在审批之前就画了，所以它只能说明"打算执行"，
            # 不能再用它判断"是否真的执行"——那个看下面的 executed。
            self.prepared.append(name)
        def show_tool_execution(self, name, args, success=True, result=""):
            self.executed.append(name)
        def show_tool_denied(self, reason=""):
            self.denied.append(reason)
        def print_error(self, msg):
            print("  [界面报错]", msg)
        async def wait_for_user_approval(self, question="是否执行？", default_yes=True):
            self.approvals += 1
            assert default_yes is True, "目前调用方应默认回车=同意"
            return False, "我不同意"  # 一律拒绝，验证拒绝分支

    class FakeHistory:
        """假历史：把消息存进列表，方便断言"""
        def __init__(self):
            self.messages = []
        def add_message(self, message):
            self.messages.append(message)

    class FakeFunction:
        """模仿 SDK 里 tool_call.function 的形状"""
        def __init__(self, name, args):
            self.name = name
            self.arguments = json.dumps(args, ensure_ascii=False)  # 模型给的是 JSON 字符串
    class FakeToolCall:
        """模仿 SDK 里 tool_call 的形状"""
        def __init__(self, name, args):
            self.id = "call_test_1"
            self.function = FakeFunction(name, args)

    async def scenario():
        conv = make_conversation()
        ui, history = FakeUI(), FakeHistory()
        conv._ui_manager = ui  # 替换成假界面
        conv._history_manager = history  # 替换成假历史
        ToolManager._instance = None  # 清单例，保证工具表是干净的
        ToolManager._initialized = False
        conv._tool_manager = ToolManager()  # 真工具表（同步环境，不会连 MCP）
        # 第一次：只读的 file_read，不该弹确认
        await conv._handle_tool_calls([FakeToolCall("file_read", {"path": "a.py"})])
        # 第二次：要改文件的 file_edit，默认应该弹确认；假界面会拒绝
        await conv._handle_tool_calls([FakeToolCall(
            "file_edit", {"path": "a.py", "old_string": "hi", "new_string": "bye"})])
        return ui, history

    ui, history = asyncio.run(scenario())

    assert ui.approvals == 1, f"应该只弹 1 次确认（给 file_edit），实际 {ui.approvals} 次"
    assert "file_read" in ui.executed, "只读工具应直接执行"
    assert "file_edit" not in ui.executed, "被拒绝的写入工具不该执行"
    assert ui.prepared == ["file_read", "file_edit"], f"两次调用都该先画出头部，实际 {ui.prepared}"
    assert ui.denied == ["我不同意"], "拒绝后要有一行提示，并把理由带出来"
    assert len(history.messages) == 2, "两次调用都要有工具结果回写"
    first = history.messages[0]
    assert first["role"] == "tool", first
    assert "print('hi')" in first["content"][0]["text"], "工具结果应包含文件内容"
    assert "<reminder>" in first["content"][1]["text"], "最后一个工具结果后应附待办提醒"
    assert "用户拒绝执行这个工具" in history.messages[1]["content"][0]["text"], "拒绝原因要回写给模型"
    tmp.cleanup()
    print("  · 实测：file_read 直接执行；file_edit 弹确认且被拒后未执行")
    print("✓ 通过: 授权分流 + 工具执行 + 结果回写整条链通畅")


def test_old_style_recursion_would_fail():
    """对照用例：如果还用原来的递归写法，同样轮数会直接 RecursionError

    这一条不是在测新代码，而是在证明"这个改动确实解决了一个真实问题"：
    Python 默认递归上限 1000（本机实测），1200 层的异步递归必然炸。
    """
    print("\n测试: 对照——旧式递归会崩")

    async def old_style(n):
        if n <= 0:  # 递归出口
            return 0
        return await old_style(n - 1)  # 自己调自己：每轮栈 +1

    raised = False
    try:
        asyncio.run(old_style(1200))
    except RecursionError:
        raised = True
    assert raised, "对照用例：旧式递归跑 1200 层应当触发 RecursionError"
    print(f"  · 实测递归上限 {sys.getrecursionlimit()}，旧写法 1200 层即 RecursionError")
    print("✓ 通过: 对照成立，新 while 写法不会触发这个问题")


def run_all_tests():
    """运行所有测试（把上面 7 个用例挨个跑一遍）"""
    print("开始运行主循环改造测试...\n")

    tests = [
        test_loop_no_stack_growth,
        test_loop_stops_on_false,
        test_exception_propagates,
        test_no_self_recursion_in_source,
        test_approval_rules,
        test_tool_call_chain_approval,
        test_old_style_recursion_would_fail,
    ]

    passed = 0
    try:  # 整体包一层 try，任何一个用例失败都能被抓住
        for test in tests:
            test()
            passed += 1
        print(f"\n🎉 所有测试通过（{passed}/{len(tests)}）")
    except Exception as e:  # 任何一个失败都会跳到这里
        print(f"\n❌ 测试失败（已通过 {passed}/{len(tests)}）：{e}")
        traceback.print_exc()  # 打印完整的出错现场


if __name__ == "__main__":
    # 直接运行这个文件时才执行（被 import 时不执行）
    run_all_tests()  # 开跑
