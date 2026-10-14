"""Owned loopback browser app with real upstream HTTP and synthetic Feishu transport."""
from datetime import datetime, timedelta
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import Response

from features.daily_checks import service
from features.stability.app import storage, scheduler
from shared.registry import get_registry
from tests.fixtures.daily_checks import Upstream
from workbench import create_app

port = int(sys.argv[1])
upstream = Upstream()
notifications = []
today = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
now = today.replace(hour=9, minute=29, second=0, microsecond=0).timestamp()
time.time = lambda: now
app = create_app("all")
registry = get_registry()
for name, family, key, status in (("Synthetic GPT", "codex", "synthetic-candidate", "online"),
                                  ("Synthetic Claude", "claude", "synthetic-claude-candidate", "online"),
                                  ("Synthetic reference", "gpt/claude", "synthetic-reference", "recorded")):
    registry.save({"name": name, "scope": family, "api_key": key, "status": status, "multiplier": .15,
                   "base_url": f"http://127.0.0.1:{port}/mock"})
daily = service.get_service()
daily.clock = lambda: now
with registry.connect() as db:
    db.execute("UPDATE model_catalog SET family='codex' WHERE model='gpt-6-astra'")


async def notify(request):
    notifications.append(json.loads(request.content)["content"]["text"])
    return httpx.Response(200, json={"code": 0})


daily.notification_transport = httpx.MockTransport(notify)


@app.post("/mock/v1/{endpoint:path}")
async def model(endpoint: str, request: Request):
    body = await request.json()
    upstream.calls.append({"model": body["model"], "stream": body.get("stream"), "path": endpoint})
    response = upstream.response("/v1/" + endpoint, body, request.headers)
    return Response(response.content, media_type=response.headers["content-type"])


@app.get("/fixture/state")
async def state():
    return {"mockRequests": len(upstream.calls), "notifications": len(notifications),
            "batches": [{"id": b["id"], "status": b["status"], "rows": daily.report(b["id"])["coverage"]} for b in daily.store.batches()]}


@app.post("/fixture/{phase}")
async def advance(phase: str):
    global now
    if phase not in {"start", "report", "second-start", "second-report"}:
        raise HTTPException(400, "Unknown synthetic phase")
    hour, minute = {"start": (9, 30), "report": (10, 30), "second-start": (13, 0), "second-report": (14, 0)}[phase]
    now = today.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    await daily.tick()
    if daily.worker:
        await daily.worker
    if daily.notification:
        await daily.notification
    return await state()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
