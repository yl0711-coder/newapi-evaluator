import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from features.model_coverage.catalog import Catalog
from features.model_coverage.discovery import fetch_models, request_models
from .catalog import fingerprint, probes
from .engine import endpoint, execute
from .mock import response as mock_response
from .report import evaluate


class Manager:
    def __init__(self, store, registry, transport_factory=None):
        self.store, self.registry = store, registry
        self.transport_factory = transport_factory
        self.task = None
        self.active_id = None

    @asynccontextmanager
    async def lifespan(self):
        for report in self.store.list():
            if report["state"] == "running":
                report["state"] = "interrupted"
                for row in report["probes"]:
                    if row["status"] in {"running", "not_run"}:
                        row.update(status="not_run", error_class="interrupted")
                self.store.save(evaluate(report))
        try:
            yield
        finally:
            if self.task and not self.task.done():
                await self.stop(self.active_id)

    def preview(self, plan):
        config = plan.model_dump(mode="json", exclude={"api_key", "mode", "confirm_live", "preview_fingerprint"})
        base = plan.base_url
        channel_version = None
        selected_channels = []
        if getattr(plan, "all_channels", False):
            selected_channels = [c for c in self.registry.list() if c["enabled"]]
            if not selected_channels:
                raise ValueError("没有启用的公共渠道")
            checks = [asdict(p) for c in selected_channels for p in probes(plan, c["id"])]
            config["channel_ids"] = [c["id"] for c in selected_channels]
            config["channel_versions"] = {str(c["id"]): c["version"] for c in selected_channels}
            return {"fingerprint": fingerprint(config), "request_count": len(checks), "probes": checks,
                    "maximum_seconds": min(1800, len(checks) * plan.total_timeout),
                    "attempts_per_probe": 1, "gateway_retries": "不可观察", "phase": "direct_probe",
                    "channel_version": None, "channels": selected_channels}
        if plan.channel_id:
            channel = self.registry.get(plan.channel_id)
            if not channel["enabled"]:
                raise ValueError("所选公共渠道已停用")
            base = channel["base_url"]
            channel_version = channel["version"]
        if not base:
            # Mock demonstrations may omit a destination; live start still validates it.
            base = "https://mock.invalid/v1"
        endpoint(base, "/responses")
        checks = [asdict(p) for p in probes(plan)]
        config["base_url"] = base
        config["channel_version"] = channel_version
        return {"fingerprint": fingerprint(config), "request_count": len(checks), "probes": checks,
                "maximum_seconds": min(1800, len(checks) * plan.total_timeout),
                "attempts_per_probe": 1, "gateway_retries": "不可观察", "phase": "direct_probe",
                "channel_version": channel_version}

    def connection(self, plan):
        base, key = plan.base_url, plan.api_key.get_secret_value()
        if getattr(plan, "all_channels", False):
            if plan.mode == "live" and not plan.confirm_live:
                raise ValueError("真实请求需要本次明确确认")
            if not [c for c in self.registry.list() if c["enabled"]]:
                raise ValueError("没有启用的公共渠道")
            return "", ""
        if plan.channel_id:
            channel = self.registry.resolve(plan.channel_id)
            base, key = channel["base_url"], channel["api_key"]
        if plan.mode == "live":
            if not plan.confirm_live:
                raise ValueError("真实请求需要本次明确确认")
            if not base or not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("请填写有效接口根地址和 API Key")
            endpoint(base, "/responses")
        return base, key

    def cached_models(self, channel_id):
        channel = self.registry.resolve(channel_id)
        value = Catalog(self.registry).discoveries().get(channel_id)
        valid = value and value.get("succeeded_at") and value["connection_fingerprint"] == self.registry.connection_fingerprint(channel)
        return {"models": value["models"] if valid else [], "source": "saved",
                "succeeded_at": value["succeeded_at"] if valid else None,
                "error": value["error"] if valid else "", "ok": bool(valid)}

    async def discover(self, plan):
        base, key = self.connection(plan)
        if plan.mode == "mock":
            return {"models": ["demo-model-a", "demo-model-b"], "ok": True, "error": "", "source": "mock"}
        transport = self.transport_factory() if self.transport_factory else None
        if plan.channel_id:
            result = await fetch_models(self.registry, plan.channel_id, "auto", transport)
        else:
            models, error = await request_models(base, key, transport=transport)
            result = {"models": models or [], "ok": not error, "error": error}
        return {**result, "source": "upstream"}

    async def start(self, plan):
        if self.task and not self.task.done():
            raise ValueError("已有协议探测运行中，请先停止或等待完成")
        preview = self.preview(plan)
        if plan.preview_fingerprint != preview["fingerprint"]:
            raise ValueError("配置或渠道已变化，请重新预览请求清单")
        base, key = self.connection(plan)
        if plan.channel_id:
            channel = self.registry.resolve(plan.channel_id)
            if channel["version"] != preview["channel_version"]:
                raise ValueError("渠道已变化，请重新预览")
            base, key = channel["base_url"], channel["api_key"]
        config = plan.model_dump(mode="json", exclude={"api_key", "base_url", "confirm_live", "preview_fingerprint"})
        if key and key in json.dumps(config, ensure_ascii=False):
            raise ValueError("模型名称不能包含渠道密钥")
        config.update(channel_version=preview["channel_version"], masked_host="host-" + fingerprint(urlsplit(base).hostname or "mock")[:12],
                      preview_fingerprint=preview["fingerprint"])
        if getattr(plan, "all_channels", False):
            config["channel_ids"] = [c["id"] for c in preview["channels"]]
        identifier = uuid4().hex
        report = {"version": 2, "id": identifier, "created_at": time.time(), "state": "running", "config": config,
                  "request_count": preview["request_count"], "search_session_id": "eval-" + uuid4().hex,
                  "probes": [{**row, "status": "not_run", "attempts": 0, "error_class": "not_run"} for row in preview["probes"]]}
        self.store.save(evaluate(report))
        self.active_id = identifier
        self.task = asyncio.create_task(self.run(report, plan, base, key))
        return {"id": identifier, "state": "running"}

    async def run(self, report, plan, base, key):
        current = None
        try:
            async def sequence():
                nonlocal current
                run_probes = probes(plan) if not plan.all_channels else [
                    probe for channel in self.registry.list() if channel["enabled"]
                    for probe in probes(plan, channel["id"])
                ]
                for index, probe in enumerate(run_probes):
                    current = index
                    report["probes"][index].update(status="running", error_class="", attempts=1)
                    self.store.save(evaluate(report))
                    transport = httpx.MockTransport(mock_response) if plan.mode == "mock" else self.transport_factory() if self.transport_factory else None
                    probe_base, probe_key = base, key
                    if probe.channel_id:
                        channel = self.registry.resolve(probe.channel_id)
                        probe_base, probe_key = channel["base_url"], channel["api_key"]
                    value = await execute(probe, probe_base or "https://mock.invalid/v1", probe_key or "synthetic-mock", plan, report["search_session_id"], transport)
                    report["probes"][index] = value
                    self.store.save(evaluate(report))
                    current = None
            await asyncio.wait_for(sequence(), 1800)
            report["state"] = "completed"
        except asyncio.CancelledError:
            report["state"] = "cancelled"
            if current is not None:
                report["probes"][current].update(status="unconfirmed", error_class="cancelled")
        except asyncio.TimeoutError:
            report["state"] = "interrupted"
            if current is not None:
                report["probes"][current].update(status="unconfirmed", error_class="total_timeout")
        except Exception:
            report["state"] = "failed"
            if current is not None:
                report["probes"][current].update(status="failed", error_class="internal_error")
        finally:
            self.store.save(evaluate(report))
            self.active_id = None

    def report(self, identifier):
        report = evaluate(self.store.get(identifier))
        config = report["config"]
        current = "snapshot_only"
        if config["channel_id"]:
            try:
                channel = self.registry.get(config["channel_id"])
                current = "current" if channel["version"] == config.get("channel_version") and channel["enabled"] else "changed"
            except KeyError:
                current = "unavailable"
            if current != "current":
                report["warnings"].append("公共渠道已变化或不可用，请重新检测；以下为历史结果")
        report["freshness"] = current
        return report

    async def stop(self, identifier):
        if identifier != self.active_id:
            self.store.get(identifier)
            return {"stopped": False}
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # A task cancelled before its first instruction never enters run()'s finally.
        report = self.store.get(identifier)
        if report["state"] == "running":
            report["state"] = "cancelled"
            self.store.save(evaluate(report))
        if self.active_id == identifier:
            self.active_id = None
        return {"stopped": True}
