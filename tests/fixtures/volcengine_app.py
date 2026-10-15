"""Owned browser fixture: HTTP UI/API, real parser/store, synthetic Ark transport."""
import asyncio
import base64
from collections import Counter
import io
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
from fastapi import HTTPException
from PIL import Image
import uvicorn

from features.volcengine_admission import api, storage as media_storage
from features.volcengine_admission.transport import MOCK_VIDEO
from workbench import create_app

directory = Path(os.environ["PLATFORM_DATA_DIR"]).resolve(strict=True)
if directory == ROOT or ROOT in directory.parents or directory in ROOT.parents:
    raise RuntimeError("Browser fixture requires an external data directory")

calls = []
tasks = {}
holding = False
unknown_next = False
unexpected = 0
submit_started = asyncio.Event()
submit_release = asyncio.Event()
submit_release.set()
configured_media_files = media_storage.MAX_MEDIA_FILES


async def reply(request):
    global unknown_next, unexpected
    host, path, method = request.url.host, request.url.path, request.method
    if host == "media.example.org" and method == "GET" and path == "/synthetic.mp4":
        calls.append({"kind": "media", "method": method})
        if "authorization" in request.headers or "referer" in request.headers:
            raise AssertionError("Ark credentials must not reach the media origin")
        return httpx.Response(200, content=MOCK_VIDEO, headers={"content-type": "video/mp4"})
    if host != "ark.cn-beijing.volces.com":
        unexpected += 1
        raise AssertionError("Unknown fixture destination")
    if method == "GET" and path == "/api/v3/contents/generations/tasks":
        calls.append({"kind": "auth", "method": method})
        return httpx.Response(200, json={"items": [{"content": {"video_url": "https://media.example.org/private?signature=do-not-retain"}}]})
    if method == "GET" and path.startswith("/api/v3/contents/generations/tasks/"):
        task_id = path.rsplit("/", 1)[-1]
        if task_id not in tasks:
            unexpected += 1
            raise AssertionError("Queries must reference an already submitted task")
        calls.append({"kind": "query", "method": method, "task_id": task_id})
        if holding:
            return httpx.Response(200, json={"id": task_id, "status": "running"})
        return httpx.Response(200, json={"id": task_id, "status": "succeeded", "usage": {"completion_tokens": 38400},
                                       "content": {"video_url": "https://media.example.org/synthetic.mp4?signature=do-not-retain"}})
    if method == "POST" and path in {"/api/v3/contents/generations/tasks", "/api/v3/images/generations"}:
        body = json.loads(request.content)
        model_id = body["model"]
        calls.append({"kind": "generate", "method": method, "model_id": model_id})
        if unknown_next:
            unknown_next = False
            raise httpx.RemoteProtocolError("Synthetic connection closed after submission")
        if path.endswith("images/generations"):
            output = io.BytesIO()
            Image.new("RGB", (128, 128), "navy").save(output, format="PNG")
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}],
                                           "usage": {"generated_images": 1}})
        task_id = "fixture-task-" + str(len(tasks) + 1)
        tasks[task_id] = model_id
        if not submit_release.is_set():
            submit_started.set()
            await submit_release.wait()
        return httpx.Response(200, json={"id": task_id})
    unexpected += 1
    raise AssertionError("Unsupported fixture operation")


api.app = api.create_app(data_dir=directory / "volcengine-admission", transport=httpx.MockTransport(reply),
                         poll_interval=.05, poll_window=30)
app = create_app("all")


@app.get("/fixture/state")
async def state():
    kinds = Counter(call["kind"] for call in calls)
    return {"mockRequests": len(calls), "generations": kinds["generate"], "queries": kinds["query"],
            "authReads": kinds["auth"], "mediaReads": kinds["media"], "unexpected": unexpected,
            "generationModels": [call["model_id"] for call in calls if call["kind"] == "generate"],
            "queryTaskIds": [call["task_id"] for call in calls if call["kind"] == "query"],
            "taskIds": list(tasks), "holding": holding,
            "heldSubmission": submit_started.is_set() and not submit_release.is_set()}


@app.post("/fixture/{phase}")
async def phase(phase: str):
    global holding, unknown_next
    if phase == "hold":
        holding = True
    elif phase == "release":
        holding = False
    elif phase == "unknown":
        unknown_next = True
    elif phase == "hold-submit":
        submit_started.clear()
        submit_release.clear()
    elif phase == "release-submit":
        submit_release.set()
    elif phase == "limit-capacity":
        media_storage.MAX_MEDIA_FILES = api.app.state.get_service().capacity()["used_files"]
    elif phase == "release-capacity":
        media_storage.MAX_MEDIA_FILES = configured_media_files
    else:
        raise HTTPException(400, "Unknown fixture phase")
    return await state()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, log_level="error")
