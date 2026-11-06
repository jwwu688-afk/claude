# =============================================================================
# 文件：src/core/prompt/enviroment.py
# 作用：收集"当前环境信息"，塞进系统提示词，让模型知道自己在哪儿干活。
#
# 为什么要告诉模型这些？
#   模型本身不知道你现在在哪个文件夹、用的是 Windows 还是 Mac、今天几号。
#   不告诉它，它就会瞎猜：比如在 Windows 上给你写 rm -rf，
#   或者以为自己在 Linux 上，写一堆 Mac 才有的命令。
#
# 这里收集 5 样东西：当前目录、是不是 git 仓库、操作系统、系统版本、今天日期。
# 【2026-11-06 第 3 轮】输出的标签和措辞已全部改成中文。
# =============================================================================

# adapt from Claude Code
# 上面说明：这段也是照着 Claude Code 改的

import os  # os 库：获取当前工作目录
import platform  # platform 库：获取操作系统名称、版本
from pathlib import Path  # Path 是"路径对象"，处理文件夹路径比字符串方便得多
from datetime import datetime  # datetime 用来取当前时间


def get_working_directory():
    """Get the current working directory（拿到当前工作目录，也就是你从哪个文件夹启动的程序）"""
    return f"当前工作目录：{os.getcwd()}"  # os.getcwd() = "current working directory"


def check_git_repository():
    """Check if current directory is a git repository（看看当前目录是不是在某个 git 仓库里）"""
    current_dir = Path(os.getcwd())  # 把当前目录变成一个 Path 对象

    # Check current directory and parent directories for .git folder
    for path in [current_dir] + list(current_dir.parents):
        # current_dir.parents 是"所有上级目录"的列表，比如 C:/a/b 会给出 C:/a 和 C:/
        # 拼起来就是：先看自己，再一层层往上看
        git_dir = path / '.git'  # "/" 在 Path 对象里表示"拼路径"，得到 C:/a/b/.git
        if git_dir.exists():  # 如果这个 .git 文件夹真的存在
            return f"是否在 git 仓库中：是，仓库根目录为 {path}"  # 说明在仓库里，并告诉它在哪

    return "是否在 git 仓库中：否"  # 一直找到最顶层都没找到，说明不在 git 仓库里


def get_platform():
    """Get the current platform（拿到操作系统名字）"""
    return f"操作系统：{platform.system().lower()}"  # system() 会返回 "Windows" / "Linux" / "Darwin"
    # .lower() 把它变成小写（windows / linux / darwin）


def get_os_version():
    """Get the OS version information（拿到操作系统的详细版本）"""
    return f"系统版本：{platform.platform()}"  # platform() 会返回更完整的信息，比如 Windows-11-10.0.22631


def get_current_date():
    """Get today's date（拿到今天的日期）"""
    today = datetime.now().strftime("%Y-%m-%d")  # strftime 是"把时间格式化成字符串"
    # %Y 是四位年份，%m 是两位月份，%d 是两位日期，所以结果是 2026-11-06 这种样子
    return f"今天日期：{today}"


def get_enviroment_info():
    """Get all environment information as a formatted string（把上面 5 样拼成一段文字）"""
    # 注意函数名里的 enviroment 是原作者拼错的（应该是 environment），
    # 但因为别的文件都是按这个错名字调用它的，所以不能改
    info_parts = [
        get_working_directory(),  # 当前目录
        check_git_repository(),  # 是否 git 仓库
        get_platform(),  # 操作系统
        get_os_version(),  # 系统版本
        get_current_date()  # 今天日期
    ]
    return "\n".join(info_parts)  # 用换行符把它们连成一整段文字
