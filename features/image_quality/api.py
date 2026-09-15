import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .engine import GenerationInput, generate

WEB_DIR = Path(__file__).parent / "web"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


async def wait_for_disconnect(request, finished):
    while not finished.is_set():
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.1)


@app.post("/api/generate")
async def create_image(body: GenerationInput, request: Request):
    if not body.confirm_live:
        raise HTTPException(400, "请确认本次发送 1 次真实生图请求")
    finished = asyncio.Event()
    operation = asyncio.create_task(generate(body, getattr(app.state, "upstream_transport", None)))
    disconnected = asyncio.create_task(wait_for_disconnect(request, finished))
    try:
        done, _pending = await asyncio.wait({operation, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        raise HTTPException(499, "客户端已停止等待，上游处理状态未确认")
    finally:
        finished.set()
        for task in (operation, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, disconnected, return_exceptions=True)


app.mount("/assets", StaticFiles(directory=WEB_DIR))
