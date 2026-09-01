"""请求传输自测：瞬时断连重试与安全的参数错误边界。"""
import asyncio
import json

import httpx

from app import probes


fails: list[str] = []


def check(name: str, condition: bool, extra: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {extra}"))
    if not condition:
        fails.append(name)


def cfg() -> dict:
    return {
        "protocol": "openai", "base_url": "https://upstream.test",
        "model": "gpt-4o-mini", "key": "sk-test",
    }


def chat_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, request=request, json={
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })


async def main() -> int:
    print("=" * 50)
    print("请求传输自适应")

    chat_calls = 0

    def flaky_chat(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request)
        return chat_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky_chat)) as cli:
        recovered = await probes.probe_chat(cli, cfg(), prompt="hi")
    check("非流式请求在首次断连后恢复", recovered["ok"] and chat_calls == 2,
          recovered)
    check("非流式报告保留网络重试证据",
          recovered["extra"].get("attempts") == 2
          and recovered["extra"].get("retry_reasons") == ["network_retry"],
          recovered["extra"])

    stream_calls = 0

    def flaky_stream(request: httpx.Request) -> httpx.Response:
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request)
        events = [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, request=request, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky_stream)) as cli:
        streamed = await probes.probe_stream(cli, cfg(), prompt="hi")
    check("流式请求在首个分片前断连后恢复", streamed["ok"] and stream_calls == 2,
          streamed)
    check("流式报告保留网络重试证据",
          streamed["extra"].get("attempts") == 2
          and streamed["extra"].get("retry_reasons") == ["network_retry"],
          streamed["extra"])

    stream_parameter_calls: list[dict] = []

    def adaptive_stream_parameter(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        stream_parameter_calls.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, request=request, json={
                "error": {"type": "invalid_request_error", "param": "max_tokens",
                          "message": "Unsupported parameter: max_tokens; use "
                                     "max_completion_tokens instead."},
            })
        events = [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        body_text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, request=request, text=body_text)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(adaptive_stream_parameter),
    ) as cli:
        adapted_stream = await probes.probe_stream(cli, cfg(), prompt="hi")
    check("流式请求会切换到上游要求的 token 参数",
          adapted_stream["ok"] and len(stream_parameter_calls) == 2
          and "max_tokens" in stream_parameter_calls[0]
          and "max_completion_tokens" in stream_parameter_calls[1],
          stream_parameter_calls)

    unknown_calls = 0

    def unknown_parameter(request: httpx.Request) -> httpx.Response:
        nonlocal unknown_calls
        unknown_calls += 1
        return httpx.Response(400, request=request, json={
            "error": {"message": "tools are not supported"},
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unknown_parameter),
    ) as cli:
        rejected = await probes.probe_chat(cli, cfg(), prompt="hi")
    check("未知或语义参数错误不触发降参重试",
          not rejected["ok"] and unknown_calls == 1, rejected)

    temperature_calls: list[dict] = []

    def adaptive_temperature(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        temperature_calls.append(body)
        if "temperature" in body:
            return httpx.Response(422, request=request, json={
                "error": {"type": "invalid_request_error",
                          "message": "`temperature` may only be set to 1 when thinking "
                                     "is enabled or in adaptive mode."},
            })
        return chat_response(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(adaptive_temperature),
    ) as cli:
        adapted = await probes.probe_chat(cli, cfg(), prompt="hi")
    check("thinking/adaptive temperature 错误会删除参数后恢复",
          adapted["ok"] and len(temperature_calls) == 2
          and "temperature" in temperature_calls[0]
          and "temperature" not in temperature_calls[1], temperature_calls)

    ambiguous_calls = 0

    def ambiguous_temperature(request: httpx.Request) -> httpx.Response:
        nonlocal ambiguous_calls
        ambiguous_calls += 1
        return httpx.Response(400, request=request, json={
            "error": {"message": "`temperature` must be supplied"},
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(ambiguous_temperature),
    ) as cli:
        ambiguous = await probes.probe_chat(cli, cfg(), prompt="hi")
    check("模糊 temperature 错误不触发删除或重试",
          not ambiguous["ok"] and ambiguous_calls == 1, ambiguous)

    print("-" * 50)
    print("失败项：无" if not fails else "失败项：" + repr(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
