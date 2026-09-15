import asyncio
from contextlib import asynccontextmanager
import time
import uuid
from urllib.parse import urlsplit

import httpx

from shared.network import guarded_transport
from shared.scheduler_lock import scheduler_lock
from .mock import LocalMock
from .models import fingerprint, make_plan
from .transport import measure


def target_snapshot(target, registry):
    if target.mode == "mock":
        return {"mode": "mock", "alias": "本地 Mock", "protocol": target.protocol, "model": target.model,
                "scenario": target.mock_scenario}
    channel = registry.get(target.channel_id)
    if not channel["enabled"]:
        raise ValueError("所选渠道已停用")
    host = urlsplit(channel["base_url"]).hostname or ""
    return {"mode": "live", "alias": f"渠道 #{channel['id']}", "channel_id": channel["id"],
            "version": channel["version"], "protocol": target.protocol, "model": target.model,
            "host_masked": "***", "host_fingerprint": fingerprint(host),
            "endpoint_fingerprint": fingerprint(channel["base_url"])}


class Manager:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.previews = {}
        self.task = None
        self.current_id = None
        self.current_request = None
        self.stop_reason = ""
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(self):
        with scheduler_lock(self.store.directory):
            self.store.interrupt_stale()
            try:
                yield
            finally:
                await self.stop(reason="service_shutdown")

    def preview(self, body):
        case = self.store.case(body.case_id)
        snapshot = make_plan(body, case, target_snapshot(body.target, self.registry))
        now = time.monotonic()
        self.previews = {k: v for k, v in self.previews.items() if v[0] > now}
        if len(self.previews) >= 100:
            raise ValueError("待确认计划过多，请稍后再试")
        identifier = uuid.uuid4().hex
        self.previews[identifier] = (now + 300, snapshot, body.target)
        return {"preview_id": identifier, "expires_in_seconds": 300, "plan": snapshot}

    async def start(self, body):
        async with self.lock:
            try:
                return self.store.run(preview_id=body.preview_id)
            except KeyError:
                pass
            entry = self.previews.get(body.preview_id)
            if entry is None or entry[0] <= time.monotonic():
                raise ValueError("预览已过期，请重新预览")
            _, plan, target = entry
            if target.mode == "live" and not body.confirm_live:
                raise ValueError("本次真实请求需要勾选预览确认")
            if target_snapshot(target, self.registry) != plan["target"]:
                raise ValueError("渠道配置已变化，请重新预览")
            if self.task and not self.task.done():
                raise ValueError("已有诊断运行，请结束后再开始")
            run = self.store.create_run(body.preview_id, plan)
            self.previews.pop(body.preview_id)
            self.current_id, self.stop_reason = run["id"], ""
            self.task = asyncio.create_task(self.execute(run, target))
            return run

    async def stop(self, identifier=None, *, reason="operator_stop"):
        if identifier is not None and identifier != self.current_id:
            return self.store.run(identifier)
        if self.task and not self.task.done():
            self.stop_reason = reason
            if self.current_request:
                self.current_request.cancel()
            await self.task
        return self.store.run(identifier) if identifier else None

    @asynccontextmanager
    async def client(self, target):
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        if target.mode == "mock":
            async with LocalMock(target.mock_scenario) as mock:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0, limits=limits),
                                            timeout=None, follow_redirects=False, trust_env=False) as client:
                    yield client, mock.url
        else:
            async with httpx.AsyncClient(transport=guarded_transport(limits=limits), timeout=None,
                                        follow_redirects=False, trust_env=False) as client:
                yield client, None

    async def execute(self, run, target):
        deadline = time.monotonic() + run["plan"]["config"]["max_duration_seconds"]
        try:
            async with self.client(target) as (client, mock_url):
                for index, row in enumerate(run["results"]):
                    if self.stop_reason or time.monotonic() >= deadline:
                        self.stop_reason = self.stop_reason or "duration_budget"
                        break
                    key, url = "", mock_url
                    if target.mode == "live":
                        try:
                            current_target = target_snapshot(target, self.registry)
                            channel = self.registry.resolve(target.channel_id)
                        except (ValueError, KeyError):
                            self.stop_reason = "target_changed"
                            break
                        if current_target != run["plan"]["target"]:
                            self.stop_reason = "target_changed"
                            break
                        if channel["version"] != run["plan"]["target"]["version"]:
                            self.stop_reason = "target_changed"
                            break
                        url, key = channel["base_url"], channel["api_key"]
                    row["outcome"] = "running"
                    self.store.update(run)

                    async def progress(metrics):
                        run["results"][index] = {**row, **metrics}
                        self.store.update(run)

                    config = {**run["plan"]["config"]}
                    config["timeout_seconds"] = min(config["timeout_seconds"], max(0.001, deadline - time.monotonic()))
                    self.current_request = asyncio.create_task(measure(client, url, key, target.model_dump(), row, config, progress))
                    try:
                        result = await self.current_request
                    except asyncio.CancelledError:
                        result = {"outcome": "cancelled", "request_ok": False}
                    self.current_request = None
                    run["results"][index] = {**row, **result}
                    self.store.update(run)
                    if time.monotonic() >= deadline:
                        self.stop_reason = "duration_budget"
                        break
                    if result["outcome"] in ("authentication_error", "permission_error"):
                        self.stop_reason = "target_rejected"
                        break
            run["state"] = "stopped" if self.stop_reason else "completed"
        except Exception:
            run["state"] = "interrupted"
            self.stop_reason = "internal_error"
        finally:
            self.current_request = None
            run["stop_reason"] = self.stop_reason
            for row in run["results"]:
                if row["outcome"] == "pending":
                    row["outcome"] = "not_sent"
                elif row["outcome"] == "running":
                    row["outcome"] = "unknown"
            self.store.update(run)
