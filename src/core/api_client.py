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
    _cached_tokens = 0  # 【第 6 轮】累计"命中缓存"的 token 数（只有支持的接口才不为 0）
    _prompt_tokens = 0  # 【第 8.2 轮】累计"输入 token"总数，专门当命中率的分母

    @property
    def total_cost(self):
        # @property 装饰器：让这个方法可以像"属性"一样访问，写 client.total_cost 就行，不用加括号
        return round(self._total_cost, 2)  # 返回累计花费，保留 2 位小数

    @property
    def cached_tokens(self):
        """累计命中缓存的 token 数（第 6 轮新增）

        为什么值得看这个数字？
          命中缓存的那部分输入，价格通常只有正常输入的一成左右。
          如果它一直是 0，说明缓存压根没生效，你正在按原价重复付费。
        """
        return self._cached_tokens

    @property
    def prompt_tokens(self):
        """累计输入的 token 总数（2026-11-06 第 8.2 轮新增）

        它是"缓存命中率"的分母。为什么不能直接用最后一条消息的 prompt_tokens？
          因为状态行显示的是"这一整段会话的成本表现"，
          单次数字会随着你输入的长短剧烈跳动，看不出趋势。
        """
        return self._prompt_tokens

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率，百分比（2026-11-06 第 8.2 轮新增）

        为什么把"命中 8640 个 token"换成百分比？
          8640 这个数字本身没有参照系：它可能是 20 万输入里的 4%（很差），
          也可能是 1 万输入里的 86%（很好）。用户每次都要自己心算，
          等于没显示。百分比一眼就知道"缓存到底有没有在替我省钱"。

        口径说明见 _record_usage 里的注释：不同厂商对"输入 token"的定义不一样，
        这里统一折算成"命中缓存的部分 ÷ 全部输入"。
        """
        if not self._prompt_tokens:  # 还没发过请求，或者接口不返回用量
            return 0.0
        return 100.0 * self._cached_tokens / self._prompt_tokens

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
            # 【第 6 轮】生成摘要单独用一个模型：摘要不需要太聪明，用小模型能省不少钱。
            #   没配 OPENAI_SUMMARY_MODEL 就退回主模型，行为和以前一样。
            self.summary_model = os.getenv("OPENAI_SUMMARY_MODEL") or self.model
            # 【第 6 轮】是否给消息打"提示词缓存"标记。见 _detect_prompt_cache()。
            self.prompt_cache_enabled = self._detect_prompt_cache()

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

    def _detect_prompt_cache(self) -> bool:
        """判断当前接口要不要打提示词缓存标记（2026-11-06 第 6 轮新增）

        为什么要判断？
          cache_control 是 Anthropic 的私有字段。发给 OpenAI 官方接口会被拒绝
          （400 Unrecognized request argument），发给别的中转站也可能报错。
          所以默认只在"看起来像 Anthropic"时才开。

        判断顺序：
          1. 环境变量 ENABLE_PROMPT_CACHE 明确写了 1/0 → 听它的
          2. 地址里含 anthropic → 开
          3. 模型名里含 claude → 开
          4. 其他情况 → 关（宁可不缓存，也不要请求报错）
        """
        flag = os.getenv("ENABLE_PROMPT_CACHE")
        if flag is not None and flag.strip() != "":
            return flag.strip().lower() in ("1", "true", "yes", "on")
        base = (self.base_url or "").lower()
        model = (self.model or "").lower()
        return "anthropic" in base or "claude" in model

    def _record_usage(self, token_usage):
        """把一次请求的用量记到总账上（2026-11-06 第 6 轮新增；第 8.2 轮补了分母）

        记三样东西：
          - 花费：有些中转站会返回 cost 字段，没有就算 0
          - 缓存命中量：Anthropic 返回 cache_read_input_tokens，
            OpenAI 返回 prompt_tokens_details.cached_tokens，两种都试一下
          - 输入总量：命中率的分母

        为什么分母要分情况算（第 8.2 轮）？
          两家对 "prompt_tokens" 的定义不一样，
          混着算会得出超过 100% 的命中率，那个数字一眼就是错的：

            OpenAI 系：prompt_tokens 已经把缓存命中部分算进去了，
                      所以分母就是 prompt_tokens。
            Anthropic 系：input_tokens 不含缓存读写，
                      真正喂进去的是 input + cache_read + cache_creation，
                      三个加起来才是分母。

        怎么区分？OpenAI 一定带 prompt_tokens_details 这个对象，
        Anthropic 系则会出现 cache_read_input_tokens 字段。
        """
        if token_usage is None:  # 没用量就没什么可记的
            return
        extra = getattr(token_usage, "model_extra", {}) or {}
        if isinstance(extra, dict):
            self._total_cost += extra.get("cost", 0) or 0

        prompt_tokens = getattr(token_usage, "prompt_tokens", 0) or 0  # 输入 token（口径随厂商变）
        details = getattr(token_usage, "prompt_tokens_details", None)

        cached = 0
        if details is not None:  # OpenAI 口径
            cached = getattr(details, "cached_tokens", 0) or 0

        cache_read = getattr(token_usage, "cache_read_input_tokens", 0) or 0  # Anthropic 口径
        cache_creation = getattr(token_usage, "cache_creation_input_tokens", 0) or 0
        if not cached:  # OpenAI 没给，就试 Anthropic 的字段名
            cached = cache_read

        if details is not None:  # OpenAI：prompt_tokens 已包含缓存部分
            total_input = prompt_tokens
        elif cache_read or cache_creation:  # Anthropic：input 不含缓存，要把两块加回来
            total_input = prompt_tokens + cache_read + cache_creation
        else:  # 什么缓存字段都没有：分母就是普通输入
            total_input = prompt_tokens

        # 兜底：万一只拿到 cached、没拿到 prompt_tokens（部分中转站会这样），
        # 至少别让分母小于分子，否则命中率会算出 300% 这种笑话。
        total_input = max(total_input, cached)

        self._cached_tokens += cached or 0
        self._prompt_tokens += total_input or 0

    def get_completion(self, request_params: Dict[str, Any], model: Optional[str] = None) -> Tuple[Any, Any]:
        """
        发一次"普通"请求（非流式）：等模型把话全部说完，一次性拿到结果。

        参数 request_params：一个字典，里面装着 messages（消息列表）、tools（工具清单）等
        返回：一个元组 (模型回复对象, token 用量)
        """
        # 【Bug 修复 2026-11-10】先复制一份再改，不要动调用方传进来的那个字典。
        #   为什么：conversation 的流式兜底用的是**同一个** request 对象——
        #   流式先跑（get_completion_stream 往它里面写了 stream=True / stream_options），
        #   失败后紧跟着把同一个对象交给本函数做"非流式重试"。
        #   不复制的话，这次"非流式"请求实际还是流式的，SDK 返回的是 Stream 对象，
        #   下面 .choices 直接抛 'Stream' object has no attribute 'choices'，
        #   兜底就变成了"必然二次失败"，比没有兜底更让人困惑。
        request_params = dict(request_params)

        # 本函数的契约是"一次性拿到完整结果"，所以即使上游残留了流式参数也必须清掉。
        request_params["stream"] = False
        # stream_options 只在 stream=True 时有意义；孤零零留着，OpenAI 官方接口会直接 400。
        request_params.pop("stream_options", None)

        # 【第 18 轮】允许调用方在 request 里指定模型（子 agent 的 model 字段走这条路）。
        #   优先级：显式传参 > request 里已有的 model > 全局 self.model。
        #   为什么要读 request_params 里已有的值：改前这里是无条件覆盖，
        #   调用方就算在 request 里塞了 model 也会被抹掉，子 agent 的模型覆盖就永远不生效。
        request_params["model"] = model or request_params.get("model") or self.model
        try:  # 开始"试一试"，如果里面出错会跳到 except
            response = self.client.chat.completions.create(**request_params)
            # ** 的意思是"把字典拆开当参数传进去"，等价于 create(messages=..., tools=..., model=...)
            # 这一行就是真正在发网络请求，也是最慢的一行
            message = response.choices[0].message  # 从返回结果里取出"模型的那条回复"（取第一个候选）
            token_usage = response.usage  # 取出这次消耗了多少 token（字数统计）
            self._record_usage(token_usage)  # 【第 6 轮】统一记账（花费 + 缓存命中）

            return message, token_usage  # 把回复和用量一起返回给调用方
        except Exception as e:  # 捕获任何异常，e 是错误信息本体
            raise Exception(f"API request failed: {str(e)}")  # 包一层更好懂的错误提示，再抛出去

    def get_completion_stream(self, request_params: Dict[str, Any]) -> Generator[str, None, None]:
        """
        发一次"流式"请求：模型说一点，我们就收一点。

        这是个"生成器函数"（函数体里有 yield），调用它不会立刻执行，
        而是返回一个可以一段段取值的对象，用 for 循环去取。
        """
        # 【第 18 轮】流式请求也允许用 request 里指定的模型（子 agent 的 model 字段）。
        #   改前这一行是无条件 `= self.model`，等于把"每个子 agent 可以用不同模型"
        #   这条路彻底堵死——不管上层怎么传，发出去的都是全局模型。
        #   这里用 get() 兜底：没有指定就退回全局，行为和改前完全一致。
        # 【Bug 修复 2026-11-10】只改副本，不污染调用方的字典。
        #   原来这里是直接往 request_params 里塞 stream=True / stream_options，
        #   而调用方（conversation）之后还要拿同一个对象去发"非流式重试"，
        #   于是那条重试请求也变成流式，拿到 Stream 对象后 .choices 崩掉。
        #   放在函数体第一行（而不是 yield 之后）也有讲究：生成器要等到第一次 next()
        #   才执行，但那时调用方还没机会改这个字典，复制出来就是调用时的真实参数。
        request_params = dict(request_params)
        request_params["model"] = request_params.get("model") or self.model  # 填模型名（子 agent 可覆盖）
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
                    self._record_usage(token_usage)  # 【第 6 轮】统一记账
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
