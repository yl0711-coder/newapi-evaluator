"""Owned browser fixture: both upstreams use actual guarded loopback HTTP."""
from pathlib import Path
import copy
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import Response

from features.channel_monitor.service import get_service
from features.stability.app import scheduler
from shared.registry import get_registry
from tests.fixtures.channel_monitor import Management, Models, OnlineInventory
from workbench import create_app

port = int(sys.argv[1])
now = (int(time.time()) // 1800 + 48) * 1800
time.time = lambda: now
app = create_app("all")
registry = get_registry()
inventory = OnlineInventory(registry, base=f"http://127.0.0.1:{port}/direct")
inventory.rows.append({"record_id": "record-2", "fields": {
    **inventory.rows[0]["fields"], "apikey": "synthetic-second-key", "渠道名称": "Synthetic second"}})
inventory.import_rows()
monitor = get_service()
monitor.inventory_reader = inventory
async def immediate_sleep(seconds):
    return None
monitor.sleep = immediate_sleep
monitor.clock = lambda: now
management = Management(base=f"http://127.0.0.1:{port}/direct")
models = Models()


@app.api_route("/api/status", methods=["GET"])
@app.api_route("/api/channel/", methods=["GET"])
@app.api_route("/api/channel/{path:path}", methods=["GET", "POST"])
async def manage(request: Request):
    incoming = httpx.Request(request.method, str(request.url), headers=dict(request.headers), content=await request.body())
    result = await management.handler(incoming)
    return Response(result.content, status_code=result.status_code, media_type="application/json")


@app.post("/direct/v1/responses")
async def model(request: Request):
    incoming = httpx.Request("POST", str(request.url), headers=dict(request.headers), content=await request.body())
    result = await models.handler(incoming)
    return Response(result.content, status_code=result.status_code, media_type="application/json")


@app.get("/fixture/state")
async def state():
    return {"mockRequests": len(models.calls), "managementReads": sum(m == "GET" for m, _ in management.calls),
            "writes": management.writes, "remoteStatuses": {str(k): v["status"] for k, v in management.rows.items()},
            "bindings": len(monitor.store.bindings()), "plan": monitor.store.plan(), "circuit": monitor.store.circuit(),
            "history": monitor.store.history()}


@app.post("/fixture/phase/{phase}")
async def phase(phase: str):
    if phase not in {"healthy", "mismatch", "candy-pass-mismatch", "fault", "three-backups", "four-backups"}:
        raise HTTPException(400, "Unknown synthetic phase")
    if phase == "three-backups":
        management.rows.pop(5, None)
    elif phase == "four-backups":
        management.rows[5] = {**copy.deepcopy(management.rows[1]), "id": 5, "status": 1, "name": "synthetic-5"}
    else:
        models.phase = phase
    return await state()


@app.post("/fixture/advance")
async def advance():
    global now
    now += 1800
    await scheduler.tick(now)
    if monitor.worker:
        await monitor.worker
    return await state()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
