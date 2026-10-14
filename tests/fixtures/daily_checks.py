"""Independent synthetic upstream data used only by owned regression services."""
import json

import httpx


class Upstream:
    def __init__(self):
        self.calls = []
        self.candidate_tokens = 2000
        self.missing_usage = False
        self.block = None

    async def handler(self, request):
        body = json.loads(request.content)
        self.calls.append({"path": request.url.path, "model": body["model"], "stream": body.get("stream"),
                           "max_tokens": body.get("max_tokens", body.get("max_output_tokens")),
                           "reasoning": body.get("reasoning", body.get("reasoning_effort")),
                           "system": "system" in body})
        if self.block is not None:
            await self.block(request, body)
        return self.response(request.url.path, body, request.headers)

    def response(self, path, body, headers):
        key = headers.get("authorization", headers.get("x-api-key", ""))
        count = self.candidate_tokens if "candidate" in key else 2000
        text = "OK" if body.get("stream") else " ".join(str(1 + i % 355) for i in range(331))
        if path.endswith("/responses"):
            usage = {"input_tokens": count, "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0}}
            payload = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "usage": usage}
            events = [{"type": "response.created", "response": {"status": "in_progress"}},
                      {"type": "response.output_text.delta", "delta": text}, {"type": "response.completed", "response": payload}]
        elif path.endswith("/messages"):
            usage = {"input_tokens": count - 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 50, "output_tokens": 1}
            payload = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}
            events = [{"type": "message_start", "message": {"usage": usage}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                      {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
        else:
            usage = {"prompt_tokens": count, "completion_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 0}}
            payload = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": usage}
            events = [{"choices": [{"delta": {"content": text}, "finish_reason": None}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}]
        if self.missing_usage:
            payload.pop("usage", None)
            for event in events:
                event.pop("usage", None)
                for key in ("response", "message"):
                    if isinstance(event.get(key), dict):
                        event[key].pop("usage", None)
        if not body.get("stream"):
            return httpx.Response(200, json=payload)
        raw = "\n\n".join("data: " + json.dumps(event) for event in events)
        if path.endswith("/chat/completions"):
            raw += "\n\ndata: [DONE]"
        return httpx.Response(200, content=(raw + "\n\n").encode(), headers={"content-type": "text/event-stream"})
