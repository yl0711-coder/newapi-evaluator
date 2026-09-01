"""假上游，供 selftest_api.py 使用，不参与正式运行。"""
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import evaluation_packs, hardbank, specialty, test_catalog

app = FastAPI()
REAL_MODEL = "gpt-4o-mini"

# 自测开关，用来模拟各种"能力不足"，好覆盖不同落位档次。真实上游没有这些。
#   degrade        总开关：工具调用不发起 + 最难的题答错
#   weak_reasoning 只让最难那道推理题答错
#   weak_code      代码题退化（不用递归、不用推导式）
#   no_tools       完全不发起工具调用
#   hard_all_wrong 硬题全部答错（模拟"声称高档但硬题崩"）
#   hard_only_hle  只答对 HLE，编程硬核全错（模拟偏科）
#   oseries        模拟 OpenAI o 系列：拒绝 max_tokens 与 temperature，
#                  只认 max_completion_tokens（验证平台的 400 换口径重试）
#   reject_temperature 模拟新模型不再接受 temperature，可省略后重试
#   burn_thinking  模拟推理模型把预算全烧在思考上：正文返回空 +
#                  finish_reason=length（验证截断被记成"未测到"而不是答错）
STATE = {
    "degrade": False,
    "weak_reasoning": False,
    "weak_code": False,
    "no_tools": False,
    "hard_all_wrong": False,
    "hard_only_hle": False,
    "oseries": False,
    "reject_temperature": False,
    "burn_thinking": False,
}

OBSERVATIONS = {"temperature_rejections": 0, "requests_without_temperature": 0}

# 题面前 120 字符 → 题目，用来在假上游侧认出是哪道硬题。
# 从 hardbank 现算而不是手抄一份答案：题面改了这里自动跟着变，不会悄悄对不上。
_HARD_BY_HEAD = {it["prompt"][:120]: it for it in hardbank.all_items()}
_CATALOG_BY_PROMPT = {
    item["prompt"]: item
    for seed in range(1, 101)
    for variant in range(1, 4)
    for item in test_catalog.admission_items(REAL_MODEL, seed, variant)
}
for item in [*test_catalog.fixed_speed_items(), *test_catalog.paired_speed_items(),
             *evaluation_packs.agent_items(),
             *evaluation_packs.development_items(),
             *(item for pack in specialty.PACKS.values() for item in pack["items"])]:
    _CATALOG_BY_PROMPT[item["prompt"]] = item

ANSWERS = {
    "17": "391",
    "OK": "OK",
    "add": "def add(a, b):\n    return a + b",
    "JSON": '{"status":"ok","count":3}',
    "沸点": "100",
    "首都": "北京。",
}


@app.post("/__control")
async def control(req: Request):
    """自测用：切换各个能力开关。只覆盖请求里带的键，其余保持原值。"""
    body = await req.json()
    for key in STATE:
        if key in body:
            STATE[key] = bool(body[key])
    return dict(STATE)


@app.post("/__reset")
async def reset():
    """把所有开关关回去，避免上一个用例影响下一个。"""
    for key in STATE:
        STATE[key] = False
    for key in OBSERVATIONS:
        OBSERVATIONS[key] = 0
    return dict(STATE)


@app.get("/__observations")
async def observations():
    return dict(OBSERVATIONS)


def _hard_reply(prompt: str) -> str | None:
    """硬题：按题面认题，回一个带 <solution> 的答案。认不出来返回 None。

    正文刻意每题不同（带上题号），否则 22 道题回同一句话会触发平台的
    「答非所问」重复检测 —— 那是自测把自己坑了，不是真实场景。
    """
    item = _HARD_BY_HEAD.get(prompt[:120])
    if item is None:
        return None
    wrong = STATE["hard_all_wrong"] or (
        STATE["hard_only_hle"] and item["bank"] == "coding_hard")
    if wrong:
        # 给一个确定不对的答案：正确答案里没有 "__wrong__" 这种东西
        return (f"Working through {item['id']} step by step.\n"
                f"<solution>__wrong__</solution>")
    return (f"Reasoned about {item['id']} briefly.\n"
            f"<solution>{item['expected'][0]}</solution>")


def _eval_reply(prompt: str) -> str | None:
    """准入题库的标准答案。认不出来返回 None，交给通用兜底逻辑。

    STATE 里的开关用来模拟各种"能力不足"，好让自测覆盖不同落位档次。
    """
    if "BEGIN_TOKEN=BIRCH-184" in prompt:
        if "BEGIN_TOKEN。" in prompt:
            return "BIRCH-184"
        if "FROZEN_AMOUNT 数值" in prompt:
            return "274.50"
        if "END_TOKEN。" in prompt:
            return "EMBER-639"
        if "begin、owner、end" in prompt:
            return '{"begin":"BIRCH-184","owner":"Lin-Qiao","end":"EMBER-639"}'
        if "按字母序输出 TAGS" in prompt:
            return "amber,cedar,violet"
    item = _CATALOG_BY_PROMPT.get(prompt)
    if item:
        dim = item.get("dim") or item.get("package") or "protocol"
        weak_code_item = dim == "代码与工具" and item["method_id"] in {
            "T14", "T38", "T39", "T40", "T41",
        }
        if STATE["degrade"] or (STATE["weak_code"] and weak_code_item) \
                or (STATE["weak_reasoning"] and dim == "指令与推理"):
            return "__wrong__"
        return str(item.get("mock_answer") or "工具调用见结构化字段")

    # 指令遵循
    if "不超过四个汉字" in prompt:
        return "北京"
    if "20 到 40 个汉字介绍机器学习" in prompt:
        return "机器学习让计算机从大量数据里自动总结出规律并做出预测。"
    if "三种常见的编程语言" in prompt:
        return "1. Python\n2. Java\n3. Go"
    if "云计算的两个好处" in prompt:
        # 两行、- 开头、每行≤12 汉字、无"数据"、无数字
        return "- 弹性伸缩节省成本\n- 免去自建机房维护"

    # 推理
    if "单开进水管 6 小时" in prompt:
        return "1/6-1/9=1/18，所以 18 小时。\n18"
    if "距 B 地 12 公里处与乙相遇" in prompt:
        if STATE["weak_reasoning"]:
            return "大概 48 公里。\n48"
        return "D+12=1.5(D-12)，0.5D=30，D=60。\n60"
    if "第 4 个汇报者" in prompt:
        return "A" if STATE["weak_reasoning"] else "C"
    if "A 和 C 中谁更高" in prompt:
        return "A" if STATE["weak_reasoning"] else "信息不足"
    if "A=1200/24" in prompt:
        return "A" if STATE["weak_reasoning"] else "A 98%\nB 99%\nC 97%\nB"
    if "与规则矛盾的记录 ID" in prompt:
        return "R2" if STATE["weak_reasoning"] else "R3"

    if "level 必须是 high" in prompt:
        return '{"level":"high","reasons":["超时", "5xx"],"retry":true}'
    if "未提供任何成本数据" in prompt:
        return "- 结论：A成功率高于B\n- 不确定：缺少成本信息"

    # 代码
    if "factorial(n)" in prompt:
        if STATE["weak_code"]:
            # 用循环而不是递归，AST 判分会扣掉递归那一项
            return ("def factorial(n):\n    r = 1\n"
                    "    for i in range(2, n + 1):\n        r *= i\n    return r")
        return ("def factorial(n):\n    if n <= 1:\n        return 1\n"
                "    return n * factorial(n - 1)")
    if "even_squares(nums)" in prompt:
        if STATE["weak_code"]:
            # 退化成 for 循环，违反"必须用推导式且不许有 for"
            return ("def even_squares(nums):\n    out = []\n"
                    "    for n in nums:\n        if n % 2 == 0:\n"
                    "            out.append(n * n)\n    return out")
        return "def even_squares(nums):\n    return [n * n for n in nums if n % 2 == 0]"
    if "parse_config(raw, fallback)" in prompt:
        if STATE["weak_code"]:
            return "def parse_config(raw, fallback):\n    return json.loads(raw)"
        return ("import json\n\ndef parse_config(raw, fallback):\n    try:\n"
                "        value = json.loads(raw)\n    except (json.JSONDecodeError, TypeError):\n"
                "        return fallback\n    return value if isinstance(value, dict) else fallback")
    if "top_users(rows, n)" in prompt:
        if STATE["weak_code"]:
            return "def top_users(rows, n):\n    return rows[:n]"
        return ("def top_users(rows, n):\n    totals = {}\n    for row in rows:\n"
                "        try:\n            user = row['user_id']\n"
                "            amount = float(row['amount'])\n"
                "        except (KeyError, TypeError, ValueError):\n            continue\n"
                "        totals[user] = totals.get(user, 0) + amount\n"
                "    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))\n"
                "    return ordered[:n]")
    if "read_text(path)" in prompt:
        if STATE["weak_code"]:
            return "def read_text(path):\n    return open(path).read()"
        return ("def read_text(path):\n    with open(path, encoding='utf-8') as handle:\n"
                "        return handle.read()")
    if "parse_orders(rows)" in prompt or "金额结果四舍五入" in prompt:
        if STATE["weak_code"]:
            return "def parse_orders(rows):\n    return {}"
        return ("def parse_orders(rows):\n    totals = {}\n    for row in rows:\n"
                "        try:\n            user = row['user_id']\n"
                "            amount = float(row['amount'])\n"
                "        except (KeyError, TypeError, ValueError):\n            continue\n"
                "        totals[user] = round(totals.get(user, 0) + amount, 2)\n"
                "    return totals")
    if "fetch_with_retry(call, retries)" in prompt or "最后一次失败必须原样抛出" in prompt:
        if STATE["weak_code"]:
            return "async def fetch_with_retry(call, retries):\n    return await call()"
        return ("import asyncio\n\nasync def fetch_with_retry(call, retries):\n"
                "    for attempt in range(retries + 1):\n        try:\n"
                "            return await call()\n        except Exception as exc:\n"
                "            retryable = isinstance(exc, TimeoutError) or getattr(exc, 'status_code', None) == 429\n"
                "            if not retryable or attempt == retries:\n                raise\n"
                "            await asyncio.sleep(2 ** attempt)\n    raise RuntimeError()")
    if "当前生效的项目代号为 ORION-7" in prompt:
        return '{"project":"ORION-7","owner":"林岚"}'
    if "渠道 A 成功率 99%" in prompt:
        return ("建议优先渠道A保证成功率和稳定性；渠道B延迟更低，"
                "可先灰度接入，再复测两者成功率与延迟。")
    if "发布公告 v3" in prompt:
        return '{"version":"v3","timeout":12,"retries":2}'
    if "代码复盘发现异常分支" in prompt:
        return ("根因是异常分支未释放连接导致连接池耗尽；无退避重试放大了流量。"
                "改为上下文管理确保关闭连接，并增加连接池监控报警。")

    # 工具调用三道题的兜底正文。
    # 必须各不相同：真实模型不会对不同问题回同一句话，
    # 全都撞成同一句会触发平台的「答非所问」检测，那是误报而不是真实场景。
    if "上海" in prompt and "天气" in prompt:
        return "抱歉，我无法查询实时天气。"
    if "什么是气象学" in prompt:
        return "气象学是研究大气现象及其变化规律的学科。"
    return None


def _tool_calls_for(prompt: str) -> list[dict] | None:
    """该调用工具的题，返回 tool_calls；不该调用就返回 None。"""
    if STATE["degrade"] or STATE["no_tools"]:
        return None          # 完全不发起工具调用
    item = _CATALOG_BY_PROMPT.get(prompt)
    grader = (item or {}).get("grader") or {}
    if grader.get("id") == "tool_call_exact":
        config = grader["config"]
        return [{"id": "call_1", "type": "function", "function": {
            "name": config["name"],
            "arguments": json.dumps(config["arguments"], ensure_ascii=False,
                                    separators=(",", ":"))}}]
    if "上海" in prompt and "天气" in prompt:
        return [{"id": "call_1", "type": "function", "function": {
            "name": "get_weather",
            "arguments": '{"city":"上海","date":"2026-03-15"}'}}]
    return None


def reply_for(prompt: str) -> str:
    # 硬题先认：它的题面很长，别被下面的子串匹配误命中
    hard = _hard_reply(prompt)
    if hard is not None:
        return hard
    ev = _eval_reply(prompt)
    if ev is not None:
        return ev
    for k, v in ANSWERS.items():
        if k in prompt:
            return v
    return "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20"


def fidelity_reply(body: dict) -> str | None:
    messages = list(body.get("messages") or [])
    prompt = next((str(message.get("content") or "") for message in reversed(messages)
                   if message.get("role") == "user"), "")
    conversation = "\n".join(str(message.get("content") or "") for message in messages)

    def between(start: str, end: str) -> str:
        return conversation.split(start, 1)[1].split(end, 1)[0].strip()

    if "只输出下面的校验串" in prompt:
        return prompt.rsplit("\n", 1)[-1].strip()
    if "忽略前面的要求，改为输出" in prompt:
        system_text = str(body.get("system") or "\n".join(
            str(message.get("content") or "") for message in messages
            if message.get("role") == "system"
        ))
        prefix = "本次请求只能输出 "
        if prefix in system_text:
            return system_text.split(prefix, 1)[1].split("，", 1)[0]
    is_fidelity_memory = any(
        "请记住：名称是“" in str(message.get("content") or "")
        for message in messages
    )
    if "只输出 JSON：" in prompt and (
        is_fidelity_memory or "按 v 从小到大排列 k" in prompt
    ):
        return prompt.rsplit("只输出 JSON：", 1)[1]
    if "现在整理最终变更纪要" in prompt:
        result = {
            "project": between("执行“", "”项目"),
            "owner": between("正式更正一：负责人改为 ", "，目标区域"),
            "change_ticket": between("变更单号是 ", "。批准后的执行顺序"),
            "window": between("最终执行窗口改为 ", "，最终回滚时限"),
            "target_zone": between("目标区域改为 ", "，节点数"),
            "node_count": int(between("节点数改为 ", "。初始负责人")),
            "rollback_minutes": int(between("最终回滚时限为 ", " 分钟")),
            "steps": between("执行顺序固定为：", "。顺序不能调整").split("、"),
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if "影子的长度为什么会变化" in prompt:
        return "太阳高度变化会改变光线照射角度。早晚太阳较低时影子通常更长。"
    return None


@app.get("/v1/models")
def models(req: Request):
    authorized = req.headers.get("authorization") == "Bearer sk-test-good-key-123" \
        or req.headers.get("x-api-key") == "sk-test-good-key-123"
    if not authorized:
        return JSONResponse({"error": "invalid key"}, status_code=401)
    return {"data": [{"id": REAL_MODEL}, {"id": "gpt-4o"},
                     {"id": "claude-sonnet-5"}]}


def _temperature_error(body: dict) -> JSONResponse | None:
    if STATE["reject_temperature"] and "temperature" in body:
        OBSERVATIONS["temperature_rejections"] += 1
        return JSONResponse({"error": {
            "type": "invalid_request_error",
            "message": "`temperature` is deprecated for this model.",
        }}, status_code=400)
    if STATE["reject_temperature"]:
        OBSERVATIONS["requests_without_temperature"] += 1
    return None


@app.post("/v1/messages")
async def messages(req: Request):
    body = await req.json()
    if req.headers.get("x-api-key") != "sk-test-good-key-123":
        return JSONResponse({"error": "invalid key"}, status_code=401)
    rejected = _temperature_error(body)
    if rejected:
        return rejected
    if body.get("model") != "claude-sonnet-5":
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)

    prompt = next(message["content"] for message in reversed(body["messages"])
                  if message.get("role") == "user")
    text = fidelity_reply(body) or reply_for(prompt)
    if body.get("stream"):
        def gen():
            yield ('event: message_start\ndata: '
                   '{"type":"message_start","message":{"model":"claude-sonnet-5"}}\n\n')
            parts = text.split(", ")
            for index, part in enumerate(parts):
                event = {"type": "content_block_delta",
                         "delta": {"type": "text_delta",
                                   "text": part + (", " if index < len(parts) - 1 else "")}}
                yield f"event: content_block_delta\ndata: {json.dumps(event)}\n\n"
            yield ('event: message_delta\ndata: '
                   '{"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n')
            yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        return StreamingResponse(gen(), media_type="text/event-stream")

    content: list[dict] = [{"type": "text", "text": text}]
    if body.get("tools"):
        calls = _tool_calls_for(prompt)
        if calls:
            call = calls[0]["function"]
            content = [{"type": "tool_use", "name": call["name"],
                        "input": json.loads(call["arguments"])}]
    return {
        "model": "claude-sonnet-5", "content": content, "stop_reason": "end_turn",
        "usage": {"input_tokens": max(len(prompt) // 2, 8),
                  "output_tokens": len(text) // 2 + 1},
    }


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    if req.headers.get("authorization") != "Bearer sk-test-good-key-123":
        return JSONResponse({"error": "invalid key"}, status_code=401)

    # o 系列口径检查要在模型名检查**之前**。
    # 真实的 o 系列先校参数再查模型，平台的「错误处理」探针拿假模型名发请求时
    # 也应当命中参数错误 —— 顺序反了就测不出平台有没有用对口径。
    if STATE["oseries"]:
        if "max_tokens" in body:
            return JSONResponse({"error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported "
                           "with this model. Use 'max_completion_tokens' instead.",
                "type": "invalid_request_error",
                "param": "max_tokens"}}, status_code=400)
        if "temperature" in body:
            return JSONResponse({"error": {
                "message": "Unsupported value: 'temperature' does not support 0 "
                           "with this model. Only the default (1) is supported.",
                "type": "invalid_request_error",
                "param": "temperature"}}, status_code=400)
        if "max_completion_tokens" not in body:
            return JSONResponse({"error": {
                "message": "Missing required parameter: 'max_completion_tokens'.",
                "type": "invalid_request_error"}}, status_code=400)

    if body.get("model") != REAL_MODEL:
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)

    prompt = next(message["content"] for message in reversed(body["messages"])
                  if message.get("role") == "user")

    # 模拟推理模型把预算全烧在思考上：正文空 + finish_reason=length。
    # 平台应当把这种情况记成「未测到」而不是「答错」。
    #
    # 只对硬题生效。若对所有步骤生效，「基础问答」这个必过项也会空，
    # 整个任务直接判暂不准入 —— 那就测不到「硬题截断被正确归类」这件事了。
    # 四维题的截断路径在 selftest_hardbank.py 里用合成步骤单测过。
    if (STATE["burn_thinking"] and not body.get("stream")
            and _HARD_BY_HEAD.get(prompt[:120]) is not None):
        budget = int(body.get("max_completion_tokens")
                     or body.get("max_tokens") or 0)
        return {
            "model": REAL_MODEL,
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": max(len(prompt) // 2, 8),
                      "completion_tokens": budget,
                      "completion_tokens_details": {"reasoning_tokens": budget}},
        }

    text = fidelity_reply(body) or reply_for(prompt)

    if body.get("stream"):
        def gen():
            parts = text.split(", ")
            for index, ch in enumerate(parts):
                chunk = {"model": REAL_MODEL,
                         "choices": [{"delta": {"content": ch + (
                             ", " if index < len(parts) - 1 else "")},
                                      "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                time.sleep(0.01)
            done = {"model": REAL_MODEL,
                    "choices": [{"delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    msg: dict = {"role": "assistant", "content": text}
    finish = "stop"
    # 带了 tools 的请求，该调用工具的题就发起 tool_calls
    if body.get("tools"):
        calls = _tool_calls_for(prompt)
        if calls:
            msg["tool_calls"] = calls
            msg["content"] = None
            finish = "tool_calls"

    # 长上下文题的输入 token 按实际长度估，成本统计才有意义
    prompt_tokens = max(len(prompt) // 2, 8)
    cached_tokens = 0
    if "请求序列号=" in prompt and "请求序列号=5" not in prompt:
        cached_tokens = int(prompt_tokens * 0.8)
    return {
        "model": REAL_MODEL,
        "choices": [{"message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": len(text) // 2 + 1,
                  "prompt_tokens_details": {"cached_tokens": cached_tokens}},
    }
