# =============================================================================
# 文件：src/core/api_client.py
# 作用：整个程序里唯一真正"跟大模型说话"的地方。
#       它把一堆消息打包，通过 HTTP 发给 OpenAI 兼容的接口，再把回复一点点收回来。
#
# 你需要知道的 3 个概念：
#   1. 流式（stream）：模型不是一次性把话说完，而是一两个字一两个字地吐出来，
#      所以这里用 yield 一点点交出去，界面上就能看到"逐字打印"的效果。
#   2. tool_calls（工具调用）：模型不会自己去执行命令，它只会"提出申请"——
#      说"我想调用叫 cmd_runner 的工具，参数是 ls"。真正的执行在别的文件里。
#   3. 单例（Singleton）：整个程序只允许存在一个 APIClient 实例，
#      这样累计花费（total_cost）才不会各算各的。
# =============================================================================

from openai import OpenAI  # 导入 OpenAI 官方 SDK，用它来发请求（兼容任何 OpenAI 格式的接口）
from typing import Dict, Any, Optional, Generator, Tuple  # 导入类型标注工具，只为让代码更易读
import json  # 导入 json 库，用来处理 JSON 格式的数据（大模型返回的东西就是 JSON）
import os  # 导入 os 库，用来读取环境变量（比如 API 密钥）
from dotenv import load_dotenv  # 导入工具函数：把 .env 文件里的配置读进环境变量
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall
# 上面这行导入两个"类型"：前者代表"模型的一条完整回复"，后者代表"一次工具调用申请"
from openai.types.chat.chat_completion_message_function_tool_call import Function
# 导入 Function 类型：用来装"工具名 + 参数字符串"


# Load environment variables
load_dotenv()  # 立刻执行：把项目根目录 .env 文件里的 OPENAI_API_KEY 等配置加载到内存


class APIClient:
    # 定义"接口客户端"这个类。类可以理解成一张"图纸"，实例才是按图纸造出来的东西
    _instance = None  # 类属性：用来存放"唯一的那一个实例"，一开始是空的
    _initialized = False  # 类属性：记录"是否已经初始化过"，防止重复初始化
    _total_cost = 0  # 类属性：累计花了多少钱，所有请求共用这一个数字

    @property
    def total_cost(self):
        # @property 装饰器：让这个方法可以像"属性"一样访问，写 client.total_cost 就行，不用加括号
        return round(self._total_cost, 2)  # 返回累计花费，保留 2 位小数

    def __new__(cls):
        # __new__ 是"造对象"的第一步（比 __init__ 更早），单例模式就是在这里做手脚
        if cls._instance is None:  # 如果还没有造过实例
            cls._instance = super().__new__(cls)  # 那就正常造一个，并存到 _instance 里
        return cls._instance  # 如果已经造过了，直接返回老的——保证永远只有一个

    def __init__(self):
        # __init__ 是"给对象填内容"的第二步，每次 APIClient() 都会被调用
        if not self._initialized:  # 只有第一次才真正做初始化，后面都跳过
            # Read configuration from environment variables
            self.api_key = os.getenv("OPENAI_API_KEY")  # 从环境变量里取密钥
            self.base_url = os.getenv("OPENAI_BASE_URL")  # 从环境变量里取接口地址（比如 openrouter）
            self.model = os.getenv("OPENAI_MODEL")  # 从环境变量里取要用的模型名字

            # Check if required environment variables exist
            if not self.api_key:  # 如果密钥是空的
                raise ValueError("OPENAI_API_KEY environment variable not set")  # 直接报错，不许继续
            if not self.base_url:  # 如果接口地址是空的
                raise ValueError("OPENAI_BASE_URL environment variable not set")  # 报错
            if not self.model:  # 如果模型名是空的
                raise ValueError("OPENAI_MODEL environment variable not set")  # 报错

            self.client = OpenAI(
                # 创建真正的 HTTP 客户端对象，后面的请求都靠它发
                api_key=self.api_key,  # 把密钥传进去
                base_url=self.base_url  # 把接口地址传进去（换成 openrouter 之类的中转站）
            )
            self._initialized = True  # 标记"已经初始化完成"，下次再创建就跳过这一段

    def get_completion(self, request_params: Dict[str, Any]) -> Tuple[Any, Any]:
        """
        发一次"普通"请求（非流式）：等模型把话全部说完，一次性拿到结果。

        参数 request_params：一个字典，里面装着 messages（消息列表）、tools（工具清单）等
        返回：一个元组 (模型回复对象, token 用量)
        """
        request_params["model"] = self.model  # 把模型名字填进请求参数（不让调用方自己指定）
        try:  # 开始"试一试"，如果里面出错会跳到 except
            response = self.client.chat.completions.create(**request_params)
            # ** 的意思是"把字典拆开当参数传进去"，等价于 create(messages=..., tools=..., model=...)
            # 这一行就是真正在发网络请求，也是最慢的一行
            message = response.choices[0].message  # 从返回结果里取出"模型的那条回复"（取第一个候选）
            token_usage = response.usage  # 取出这次消耗了多少 token（字数统计）
            cost = getattr(token_usage, 'model_extra', {})
            # getattr 的意思是"尝试取某个属性，没有就返回默认值"
            # 有些中转站（如 OpenRouter）会额外返回一个 cost 字段表示这次花了多少钱
            if isinstance(cost, dict):  # 如果取到的是个字典
                self._total_cost += cost.get("cost", 0)  # 把这次的花费加到总账上（没有 cost 字段就算 0）

            return message, token_usage  # 把回复和用量一起返回给调用方
        except Exception as e:  # 捕获任何异常，e 是错误信息本体
            raise Exception(f"API request failed: {str(e)}")  # 包一层更好懂的错误提示，再抛出去

    def get_completion_stream(self, request_params: Dict[str, Any]) -> Generator[str, None, None]:
        """
        发一次"流式"请求：模型说一点，我们就收一点。

        这是个"生成器函数"（函数体里有 yield），调用它不会立刻执行，
        而是返回一个可以一段段取值的对象，用 for 循环去取。
        """
        request_params["model"] = self.model  # 填模型名
        request_params["stream"] = True  # 告诉接口：我要流式返回，别憋到最后才给我
        request_params["stream_options"] = {"include_usage": True}
        # 额外要求：最后也要告诉我用了多少 token（不加这句，流式模式下往往拿不到用量）

        try:  # 开始试
            stream = self.client.chat.completions.create(**request_params)
            # 现在返回的不再是一个完整结果，而是一个"数据流"，可以用 for 循环一段段读

            full_content = ""  # 攒文字用的字符串，最后拼成完整的回复
            tool_calls = []  # 攒工具调用申请用的列表
            current_tool_call = None  # 记录"当前正在处理哪一条工具申请"
            token_usage = None  # 记录 token 用量，一开始是空的

            for chunk in stream:  # 一段一段地读数据流，每个 chunk 就是一小片
                # Handle token usage information
                if hasattr(chunk, 'usage') and chunk.usage:  # 如果这一片里带着用量信息
                    token_usage = chunk.usage  # 记下来
                    cost = getattr(token_usage, 'model_extra', {})  # 再试试能不能拿到花费
                    if isinstance(cost, dict):  # 如果能
                        self._total_cost += cost.get("cost", 0)  # 加到总账
                    continue  # 这一片只有用量、没有文字，直接跳过，处理下一片

                if chunk.choices[0].delta.content:
                    # delta 是"增量"的意思：这一片里新增的那点文字
                    # 如果这篇有文字，才进这个分支
                    content_chunk = chunk.choices[0].delta.content  # 取出这一小段文字
                    full_content += content_chunk  # 拼到大字符串上
                    yield content_chunk  # 立刻把它交出去（界面收到就能马上显示出来）

                # Handle tool calls
                if hasattr(chunk.choices[0].delta, 'tool_calls') and chunk.choices[0].delta.tool_calls:
                    # 如果这一片里带着"工具调用申请"（申请也是一点一点传过来的）
                    for tool_call_delta in chunk.choices[0].delta.tool_calls:  # 一次可能有多个申请
                        if tool_call_delta.index is not None:  # index 表示"这是第几个申请"
                            # Ensure enough tool_calls slots
                            while len(tool_calls) <= tool_call_delta.index:
                                # 如果列表还不够长，就补一个"空壳子"占位
                                tool_calls.append({
                                    'id': None,  # 申请的编号，等会儿才传过来
                                    'type': 'function',  # 类型固定是"函数调用"
                                    'function': {'name': None, 'arguments': ''}  # 工具名和参数，先留空
                                })

                            current_tool_call = tool_calls[tool_call_delta.index]  # 定位到这一条

                            if tool_call_delta.id:  # 如果这一片带了编号
                                current_tool_call['id'] = tool_call_delta.id  # 记下来

                            if tool_call_delta.function:  # 如果这一片带了函数信息
                                if tool_call_delta.function.name:  # 带了工具名
                                    current_tool_call['function']['name'] = tool_call_delta.function.name  # 记下来
                                if tool_call_delta.function.arguments:  # 带了参数（参数是分片传的，所以要拼接）
                                    current_tool_call['function']['arguments'] += tool_call_delta.function.arguments
                                    # 注意这里是 += 而不是 =，因为参数会被切成好几片陆续传来

            # Convert tool_calls to OpenAI standard format
            formatted_tool_calls = None  # 准备一个变量，装"整理好格式"的工具申请
            if tool_calls and any(tc['id'] for tc in tool_calls):
                # 如果攒到了申请，并且至少有一个带了编号（没编号的是残缺数据，要丢掉）
                formatted_tool_calls = []  # 准备空列表
                for tc in tool_calls:  # 逐个检查
                    if tc['id'] and tc['function']['name']:  # 编号和工具名都齐了，才算有效
                        formatted_tool_calls.append(
                            # 把普通字典包装成 SDK 要求的对象类型，后面的代码才认得
                            ChatCompletionMessageFunctionToolCall(
                                id=tc['id'],  # 编号
                                function=Function(
                                    name=tc['function']['name'],  # 工具名
                                    arguments=tc['function']['arguments']  # 参数字符串（此时还是 JSON 文本，没解析）
                                ),
                                type='function'  # 类型
                            )
                        )

            # Return standard ChatCompletionMessage object
            message = ChatCompletionMessage(
                # 把攒好的内容，包装成一个"标准回复对象"，让上层代码不用区分流式和非流式
                content=full_content,  # 完整文字
                role="assistant",  # 角色是"助手"（模型）
                tool_calls=formatted_tool_calls,  # 工具申请
                refusal=None,  # 拒答信息，这里没有
                annotations=None,  # 注释信息，这里没有
                audio=None,  # 语音信息，这里没有
                function_call=None,  # 旧的函数调用字段，这里不用
                reasoning=None  # 推理过程字段，这里不用
            )

            # Add usage information to the message for tracking
            if token_usage:  # 如果拿到了用量
                message.usage = token_usage  # 挂到消息对象上，让上层能读到

            yield message  # 最后把这一整条消息交出去（这是流里的最后一片）
        except Exception as e:  # 出错就捕获
            raise Exception(f"Streaming API request failed: {str(e)}")  # 包一层提示再抛出
