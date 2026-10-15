"""Serial one-time permits, ephemeral secrets and query-only video recovery."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import time
import uuid

import httpx

from . import catalog
from .storage import ACTIVE, Conflict, Store
from .transport import ArkTransport, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, mock_transport

PREVIEW_TTL = 600
MAX_PREVIEWS = 64
MAX_QUEUED = 10
RESUMABLE = {"submitted", "polling", "inconclusive"}


class AdmissionService:
    def __init__(self, directory, *, transport=None, poll_interval=5, poll_window=3600, clock=time.time):
        self.clock = clock
        self.store = Store(directory, clock=clock)
        self.transport = transport
        self.poll_interval, self.poll_window = poll_interval, poll_window
        self.previews = {}
        self.credentials = {}
        self.worker = None
        self.current_run_id = None
        self.lock = asyncio.Lock()
        self.wake = {}
        self.closed = False

    def preview(self, body):
        now = self.clock()
        self.previews = {key: value for key, value in self.previews.items() if value["expires_at"] > now}
        if len(self.previews) >= MAX_PREVIEWS:
            raise Conflict("有效预览数量达到上限，请等待旧预览过期")
        ids = [item.model_id for item in body.items]
        if len(set(ids)) != len(ids):
            raise ValueError("同一次验收每个精确型号只能选择一次")
        items = [catalog.normalize_item(item.model_id, item.spec.model_dump()) for item in body.items]
        prompt_hash = catalog.fingerprint(body.prompt)
        signature = catalog.fingerprint({"contract_version": catalog.CONTRACT_VERSION, "mode": body.mode,
                                         "items": items, "prompt_hash": prompt_hash})
        result = {"preview_id": uuid.uuid4().hex, "fingerprint": signature, "expires_at": now + PREVIEW_TTL,
                  "mode": body.mode, "items": items, "estimated_cny": float(sum(Decimal(str(item["estimated_cny"])) for item in items)),
                  "billing_status": "unverified", "notes": list(catalog.NOTES), "capacity": self.capacity()}
        result["capacity_warnings"] = ["本地容量不足以保守预留部分所选样片；发送前会跳过这些项，请先清理历史样片。"] if any(
            result["capacity"]["required_bytes_per_item"][item["kind"]] > result["capacity"]["available_bytes"]
            or result["capacity"]["available_files"] < 1 for item in items) else []
        self.previews[result["preview_id"]] = {**deepcopy(result), "prompt": body.prompt}
        return result

    def capacity(self):
        state = self.store.capacity()
        return {**state, "available_bytes": max(0, state["max_bytes"] - state["used_bytes"] - state["reserved_bytes"]),
                "available_files": max(0, state["max_files"] - state["used_files"] - state["reserved_files"]),
                "required_bytes_per_item": {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES}}

    def _upstream(self, mode):
        # Mock is a real parser/transport path backed exclusively by synthetic local replies.
        local = self.transport if isinstance(self.transport, httpx.MockTransport) else mock_transport()
        return ArkTransport(transport=local if mode == "mock" else self.transport)

    async def auth(self, body):
        key = body.api_key.get_secret_value() if body.mode == "live" else ""
        if body.mode == "live" and (body.confirm_read is not True or not key):
            raise ValueError("请填写临时 Key 并确认本次只读任务列表请求")
        result = await self._upstream(body.mode).auth(key)
        return {"status": result.status, "http_status": result.http_status, "error_category": result.error_category,
                "elapsed_ms": result.elapsed_ms, "scope": "task_list_only", "mode": body.mode,
                "note": "仅验证官方任务列表端点鉴权；不创建生成任务，不证明模型生成权限或 Key 来源身份。"}

    async def create(self, body):
        async with self.lock:
            if self.closed:
                raise Conflict("执行器已关闭")
            old = self.store.by_idempotency(body.idempotency_key)
            if old:
                key = body.api_key.get_secret_value() if old["mode"] == "live" else ""
                if old["preview_id"] != body.preview_id or old["fingerprint"] != body.fingerprint or not self.store.matches_key(old["id"], key):
                    raise Conflict("幂等标识已用于不同验收请求")
                return self.detail(old["id"])
            preview = self.previews.get(body.preview_id)
            if not preview or preview["expires_at"] <= self.clock() or preview["fingerprint"] != body.fingerprint:
                raise Conflict("预览无效、已使用或已过期，请重新预览并确认")
            key = body.api_key.get_secret_value() if preview["mode"] == "live" else ""
            if preview["mode"] == "live" and (body.confirm_live is not True or not key):
                raise ValueError("请填写临时 Key 并确认按此次预算逐型号发送一次真实生成请求")
            if sum(run["status"] in ACTIVE for run in self.store.runs()) >= MAX_QUEUED:
                raise Conflict("队列已满，请等待现有验收完成")
            now, run_id = self.clock(), uuid.uuid4().hex
            prompt_digest = catalog.fingerprint(preview["prompt"])
            items = [{**deepcopy(item), "item_id": uuid.uuid4().hex, "status": "pending", "task_id": None,
                      "upstream_status": None, "http_status": None, "error_category": None, "elapsed_ms": None,
                      "submission_elapsed_ms": None, "last_query_elapsed_ms": None, "query_total_ms": 0,
                      "query_count": 0, "usage": {}, "media_status": "not_available",
                      "media_id": None, "media_bytes": None, "usage_estimated_cny": None,
                      "request_contract_digest": catalog.contract_digest(item, prompt_digest)}
                     for item in preview["items"]]
            run = {"id": run_id, "mode": preview["mode"], "status": "queued", "created_at": now, "updated_at": now,
                   "contract_version": catalog.CONTRACT_VERSION, "preview_id": preview["preview_id"], "fingerprint": preview["fingerprint"],
                   "_prompt_digest": prompt_digest, "_prompt_length": len(preview["prompt"]),
                   "estimated_cny": preview["estimated_cny"], "billing_status": "unverified", "actual_charged_cny": None,
                   "resume_only": False, "items": items, "notes": list(catalog.NOTES)}
            self.store.create(run, body.idempotency_key, key)
            self.credentials[run_id] = (key, preview["prompt"])
            del self.previews[body.preview_id]
            self._ensure_worker()
            return self.detail(run_id)

    def _ensure_worker(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._consume(), name="volcengine-admission-worker")

    async def _consume(self):
        while not self.closed:
            run = next((row for row in reversed(self.store.runs()) if row["status"] == "queued"), None)
            if run is None:
                return
            self.current_run_id = run["id"]
            try:
                await self._execute(run["id"])
            except asyncio.CancelledError:
                self._interrupt(run["id"])
                raise
            except Exception:
                # Do not persist exception text: transport bodies/credentials can appear there.
                self._interrupt(run["id"], error="local_execution_failed")
            finally:
                self.credentials.pop(run["id"], None)
                self.wake.pop(run["id"], None)
                self.store.release(run["id"])
                self.current_run_id = None
                self.store.prune()

    def _interrupt(self, run_id, error="process_interrupted"):
        run = self.store.get(run_id)
        for item in run["items"]:
            if item["status"] == "sending":
                item.update(status="unknown", error_category=error)
            elif item["status"] == "pending":
                item.update(status="not_sent", error_category=error)
            elif item["status"] == "polling":
                item["status"] = "submitted"
        self.store.update(run_id, {"status": "interrupted", "items": run["items"]})

    def _active(self, run_id):
        return not self.closed and self.store.get(run_id)["status"] == "running"

    async def _execute(self, run_id):
        run = self.store.get(run_id)
        credentials = self.credentials.get(run_id)
        if not credentials:
            self._interrupt(run_id, error="credential_unavailable")
            return
        key, prompt = credentials
        self.store.update(run_id, {"status": "running"})
        upstream = self._upstream(run["mode"])
        self.wake[run_id] = asyncio.Event()
        for original in run["items"]:
            if not self._active(run_id):
                break
            item_id = original["item_id"]
            item = next(item for item in self.store.get(run_id)["items"] if item["item_id"] == item_id)
            if item["status"] == "pending" and not run["resume_only"]:
                if item["request_contract_digest"] != catalog.contract_digest(item, catalog.fingerprint(prompt)):
                    self.store.update(run_id, item_id=item_id, item_changes={"status": "not_sent", "error_category": "request_contract_changed"})
                    continue
                if not self.store.acquire(run_id, item_id, MAX_IMAGE_BYTES if item["kind"] == "image" else MAX_VIDEO_BYTES):
                    continue
                started = time.monotonic()
                result = await upstream.submit(item, prompt, key)
                self._apply(run_id, item_id, result, extra={"submission_elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
                item = next(item for item in self.store.get(run_id)["items"] if item["item_id"] == item_id)
            if item["task_id"] and item["status"] in RESUMABLE and self._active(run_id):
                await self._poll(run_id, item_id, upstream, key)
        if self._active(run_id):
            self.store.update(run_id, {"status": "completed"})

    def _apply(self, run_id, item_id, result, *, extra=None):
        current = next(item for item in self.store.get(run_id)["items"] if item["item_id"] == item_id)
        changes = {"status": result.status, "http_status": result.http_status, "error_category": result.error_category,
                   "media_status": result.media_status, "media_http_status": result.media_http_status}
        if result.task_id:
            changes["task_id"] = result.task_id
        if result.upstream_status:
            changes["upstream_status"] = result.upstream_status
        if result.usage:
            changes["usage"] = result.usage
            if current["kind"] == "video" and "completion_tokens" in result.usage:
                rate = current["unit_price_cny_per_million_tokens"]
                changes["usage_estimated_cny"] = float(Decimal(result.usage["completion_tokens"]) * Decimal(str(rate)) / 1_000_000)
            elif current["kind"] == "image" and "generated_images" in result.usage:
                changes["usage_estimated_cny"] = float(Decimal(str(current["estimated_cny"])) * result.usage["generated_images"])
        if result.media is not None:
            try:
                changes["media_id"] = self.store.save_media(run_id, current["kind"], result.media, item_id=item_id)
                changes["media_bytes"] = result.media_bytes
            except (ValueError, OSError):
                changes.update(status="inconclusive", error_category="media_capacity_or_storage_failed", media_status="failed")
        if current.get("started_at") is not None:
            changes["elapsed_ms"] = round(max(0, self.clock() - current["started_at"]) * 1000, 2)
        if extra:
            changes.update(extra)
        self.store.update(run_id, item_id=item_id, item_changes=changes)
        if result.status not in {"submitted", "polling"}:
            self.store.release(run_id, item_id)

    async def _poll(self, run_id, item_id, upstream, key):
        run = self.store.get(run_id)
        if not self.store.reserve(run_id, item_id, MAX_VIDEO_BYTES):
            self.store.update(run_id, item_id=item_id, item_changes={"status": "inconclusive", "error_category": "media_capacity_unavailable"})
            return
        deadline = time.monotonic() + self.poll_window
        queries = 0
        while self._active(run_id) and queries < 720 and time.monotonic() < deadline:
            item = next(item for item in self.store.get(run_id)["items"] if item["item_id"] == item_id)
            queries += 1
            result = await upstream.poll(item["task_id"], key, prompt_digest=run.get("_prompt_digest"), prompt_length=run.get("_prompt_length"))
            self._apply(run_id, item_id, result, extra={"query_count": item["query_count"] + 1,
                                                       "last_query_elapsed_ms": result.elapsed_ms,
                                                       "query_total_ms": round(item.get("query_total_ms", 0) + (result.elapsed_ms or 0), 2)})
            if result.status != "polling":
                return
            if not self._active(run_id):
                return
            try:
                await asyncio.wait_for(self.wake[run_id].wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
        if self._active(run_id):
            self.store.update(run_id, item_id=item_id, item_changes={"status": "inconclusive", "error_category": "query_window_exhausted"})

    @staticmethod
    def _resumable(run):
        return [item for item in run["items"] if item["kind"] == "video" and item.get("task_id")
                and item["status"] in RESUMABLE and item.get("upstream_status") not in {"failed", "cancelled", "expired"}]

    def detail(self, run_id):
        run = self.store.get(run_id)
        run.pop("_prompt_digest", None)
        run.pop("_prompt_length", None)
        run["execution_active"] = self.current_run_id == run_id
        run["can_resume"] = bool(self._resumable(run)) and run["status"] not in ACTIVE and not run["execution_active"]
        if run["status"] == "stopped" and run["execution_active"]:
            run["notes"].append("已停止后续发送，当前请求仍在收尾；收尾完成后可继续只读查询。")
        counts = {status: sum(item["status"] == status for item in run["items"]) for status in {item["status"] for item in run["items"]}}
        run["counts"] = counts
        for item in run["items"]:
            if item.get("media_id"):
                item["media_url"] = f"api/runs/{run_id}/media/{item['media_id']}"
        return run

    def runs(self):
        return [self.detail(row["id"]) for row in self.store.runs()]

    async def stop(self, run_id):
        self.store.stop(run_id)
        if run_id in self.wake:
            self.wake[run_id].set()
        if self.store.get(run_id)["status"] != "running":
            self.credentials.pop(run_id, None)
        return self.detail(run_id)

    async def cleanup(self, run_id, body):
        async with self.lock:
            if body.confirm_local_delete is not True:
                raise ValueError("请确认仅删除当前报告的本地样片，保留报告与上游任务")
            if self.current_run_id == run_id:
                raise Conflict("当前请求仍在收尾，不能清理本地样片")
            self.store.cleanup_media(run_id)
            return self.detail(run_id)

    async def resume(self, run_id, body):
        async with self.lock:
            if self.closed:
                raise Conflict("执行器已关闭")
            run = self.store.get(run_id)
            if self.current_run_id == run_id:
                raise Conflict("当前请求仍在收尾，请等待后再继续只读查询")
            if run["status"] in ACTIVE:
                raise Conflict("此验收仍在执行")
            if run["contract_version"] != catalog.CONTRACT_VERSION:
                raise Conflict("请求合同版本已变更，拒绝恢复旧验收")
            if not self._resumable(run):
                raise Conflict("没有可继续查询的已提交视频任务；不会补发生成请求")
            try:
                compatible = all(item["request_contract_digest"] == catalog.contract_digest(item, run["_prompt_digest"])
                                 for item in self._resumable(run))
            except (KeyError, ValueError):
                compatible = False
            if not compatible:
                raise Conflict("请求构造已变更，拒绝恢复旧验收")
            key = body.api_key.get_secret_value() if run["mode"] == "live" else ""
            if run["mode"] == "live" and (body.confirm_read is not True or not key):
                raise ValueError("请补充原 Key 并确认只读查询已提交视频任务")
            if not self.store.matches_key(run_id, key):
                raise Conflict("所填 Key 与原验收不匹配")
            for item in run["items"]:
                if item["status"] == "pending":
                    item.update(status="not_sent", error_category="resume_is_read_only")
                elif item in self._resumable(run):
                    item["status"] = "submitted"
            self.store.update(run_id, {"items": run["items"], "status": "queued", "resume_only": True})
            self.credentials[run_id] = (key, "")
            self._ensure_worker()
            return self.detail(run_id)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.worker and not self.worker.done():
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            # Queued credentials cannot survive shutdown; durable permits remain consumed.
            for run in self.store.runs():
                if run["status"] in ACTIVE:
                    self._interrupt(run["id"])
                self.store.release(run["id"])
        finally:
            self.credentials.clear()
            self.previews.clear()
            self.store.close()
