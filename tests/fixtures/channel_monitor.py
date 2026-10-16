"""Self-authored management and model HTTP fixtures; no business data."""
import asyncio
import copy
import json
import random

import httpx

from features.channel_monitor import domain
from features.integrity.strategies import get_strategy, load_bank


def matching_answers():
    manifest = get_strategy("modeltrace")
    counts = next(m["counts"] for m in load_bank(manifest)["models"] if m["id"] == "gpt-6-astra")
    rng = random.Random(0)
    return [" ".join(map(str, rng.choices(range(1, 356), weights=counts, k=p.expected_count))) for p in manifest.probes]


def envelope(text):
    return {"id": "synthetic-response", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 17, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 1}}}


class Management:
    def __init__(self, count=5, base="http://127.0.0.1:9/direct"):
        self.rows = {i: {"id": i, "status": 1, "type": 1, "models": "gpt-6-astra", "group": "default",
                         "base_url": base, "model_mapping": "{}", "name": f"synthetic-{i}",
                         "openai_organization": None, "other_info": "{}", "status_code_mapping": None,
                         "setting": '{"proxy":"","force_format":false,"thinking_to_content":false}',
                         "settings": '{"disable_store":false,"allow_service_tier":false}',
                         "channel_info": {"is_multi_key": False}}
                     for i in range(1, count + 1)}
        self.calls, self.writes = [], []
        self.version = "v1.0.0-rc.26"
        self.mode = "normal"
        self.on_write = None

    async def handler(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != "Bearer synthetic-management":
            return httpx.Response(403, text="synthetic rejected")
        if path == "/api/status":
            data = {"version": self.version}
        elif path == "/api/channel/":
            start = (int(request.url.params["p"]) - 1) * 100
            data = {"items": list(copy.deepcopy(self.rows).values())[start:start+100], "total": len(self.rows)}
        else:
            channel_id = int(path.split("/")[3])
            if request.method == "POST":
                status = json.loads(request.content)["status"]
                self.writes.append((channel_id, status))
                if self.mode != "no_change":
                    self.rows[channel_id]["status"] = status
                    info = json.loads(self.rows[channel_id]["other_info"])
                    info.update(status_reason="manual operation", status_time=len(self.writes))
                    self.rows[channel_id]["other_info"] = json.dumps(info)
                if self.on_write:
                    await self.on_write()
                if self.mode == "timeout":
                    raise httpx.ReadTimeout("synthetic management timeout", request=request)
                data = self.mode != "false"
            else:
                data = copy.deepcopy(self.rows[channel_id])
        return httpx.Response(200, json={"success": True, "data": data})


class Models:
    def __init__(self):
        self.queue, self.calls = [], []
        self.entered, self.gate = asyncio.Event(), asyncio.Event()
        self.phase = "healthy"
        self.matched = matching_answers()

    def reply(self, body):
        candy = body.get("input") == domain.candy_probe()["prompt"]
        self.calls.append({"candy": candy, "model": body["model"], "stream": body.get("stream"),
                           "store": body.get("store"), "max_output_tokens": body.get("max_output_tokens")})
        if self.phase == "fault":
            return 503
        if self.queue:
            return self.queue.pop(0)
        if candy:
            return "答案：20" if self.phase == "mismatch" else "答案：21"
        if self.phase == "mismatch":
            return "1 " * 331
        manifest = get_strategy("modeltrace")
        return next(self.matched[i] for i, p in enumerate(manifest.probes) if p.prompt == body["input"])

    async def handler(self, request):
        value = self.reply(json.loads(request.content))
        if value == "block":
            self.entered.set()
            await self.gate.wait()
            value = "答案：21"
        if value == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if value == "network":
            raise httpx.ConnectError("synthetic disconnect", request=request)
        if type(value) is int:
            return httpx.Response(value, text="synthetic HTTP error")
        return httpx.Response(200, json=envelope(value))
