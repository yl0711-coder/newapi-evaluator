"""Independent synthetic demonstration responses, never upstream capability evidence."""
import json
import httpx


def response(request):
    body = json.loads(request.content)
    model = body["model"]
    path = request.url.path
    usage = {"input_tokens": 12, "output_tokens": 4}
    tool = bool(body.get("tools"))
    if path.endswith("/alpha/search"):
        return httpx.Response(200, json={"output": "RFC 9110 HTTP Semantics https://www.rfc-editor.org/rfc/rfc9110.html",
            "results": [{"type": "text_result", "url": "https://www.rfc-editor.org/rfc/rfc9110.html"}]})
    if path.endswith("/responses"):
        content = [{"type": "function_call", "call_id": "synthetic-call", "name": "protocol_probe", "arguments": '{"marker":"ready"}'}] if tool else [{"type": "message", "content": [{"type": "output_text", "text": "READY"}]}]
        value = {"id": "synthetic-response", "object": "response", "model": model, "status": "completed", "output": content, "usage": usage}
        events = [{"type": "response.created", "response": {"id": "synthetic-response", "status": "in_progress"}},
                  {"type": "response.output_text.delta", "delta": "READY"}, {"type": "response.completed", "response": value}]
    elif path.endswith("/messages"):
        content = [{"type": "tool_use", "id": "synthetic-tool", "name": "protocol_probe", "input": {"marker": "ready"}}] if tool else [{"type": "text", "text": "READY"}]
        value = {"id": "synthetic-message", "type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": "tool_use" if tool else "end_turn", "usage": usage}
        events = [{"type": "message_start", "message": {**value, "content": [], "stop_reason": None}},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "READY"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
    else:
        usage = {"prompt_tokens": 12, "completion_tokens": 4}
        value = {"model": model, "choices": [{"message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}], "usage": usage}
        events = [{"model": model, "choices": [{"index": 0, "delta": {"content": "READY"}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}, "[DONE]"]
    if body.get("stream"):
        wire = "".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events)
        return httpx.Response(200, content=wire.encode(), headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json=value)
