"""Server-owned admission execution; subscriptions never own request lifetime."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from shared.network import guarded_transport
from shared.redaction import EventRedactor, scrub
from shared.registry import Conflict, RegistryError, normalize
from shared.scheduler_lock import scheduler_lock
from . import main as engine, reporting, storage


TERMINAL = {"succeeded", "failed", "cancelled"}


class AdmissionManager:
    def __init__(self, path: Path, registry, *, transport=None, token_check=None, token_reference=None, on_submit=None, budget_seconds=None):
        self.store = storage.JobStore(path)
        self.registry = registry
        self.transport = transport
        self.token_check = token_check
        self.token_reference = token_reference
        self._token_service = None
        self.on_submit = on_submit
        self.budget_seconds = budget_seconds
        self._credentials: dict[int, str] = {}
        self._wake = asyncio.Event()
        self._consumer = None
        self._active = None
        self._active_id = None
        self._lock = None
        self._closing = False

    @property
    def running(self):
        return self._consumer is not None and not self._consumer.done()

    async def start(self):
        if self._consumer is not None:
            return
        lock = scheduler_lock(self.store.path.parent)
        lock.__enter__()
        self._lock = lock
        try:
            self.store.recover()
            self._closing = False
            self._consumer = asyncio.create_task(self._consume())
        except BaseException:
            lock.__exit__(None, None, None)
            self._lock = None
            raise

    async def stop(self):
        self._closing = True
        self._wake.set()
        if self._active is not None:
            self._active.cancel()
        if self._consumer is not None:
            await asyncio.gather(self._consumer, return_exceptions=True)
            self._consumer = None
        self._credentials.clear()
        if self.store.path.exists():
            self.store.recover()
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None

    def _reference(self, configuration):
        selection = configuration["reference"]
        channel = self.registry.resolve(selection["channel_id"])
        if self.registry.connection_fingerprint(channel) != configuration["reference_identity"]:
            raise Conflict("参照渠道连接已改变，请重新创建任务")
        protocol = engine.required_protocol(selection["model"]) or selection["protocol"]
        if protocol != selection["protocol"]:
            raise Conflict("参照模型协议已改变，请重新创建任务")
        return {"base_url": channel["base_url"], "api_key": channel["api_key"],
                "model": selection["model"], "protocol": protocol}

    def _trusted_reference(self, model, protocol):
        if self.token_reference is not None:
            return self.token_reference(model, protocol)
        if self._token_service is None:
            from features.token_usage.service import TokenUsageService
            self._token_service = TokenUsageService(self.registry)
        return self._token_service.admission_binding_for(model, protocol)

    @staticmethod
    def _binding_configuration(binding):
        return {"snapshot": binding["resolved"].snapshot, "revision": binding["revision"],
                "thresholds": binding["thresholds"], "condition_hash": binding["condition_hash"]}

    async def enqueue(self, body, *, idempotency_key=None):
        if body.candidate.model != body.reference.model:
            raise RegistryError("成对测试要求候选端与参照端使用相同的请求模型名")
        candidate = normalize({"base_url": body.candidate.base_url, "api_key": body.candidate.api_key.get_secret_value()})
        channel = self.registry.resolve(body.reference.channel_id)
        secrets = [candidate["api_key"], channel["api_key"]]
        if any(secret in body.candidate.model for secret in secrets) or body.candidate.model.lower().startswith(("sk-", "bearer ")):
            raise RegistryError("模型名不能包含凭据")
        identity = hashlib.sha256((idempotency_key or uuid4().hex).encode()).hexdigest()
        run_id = identity[:32]
        configuration = {
            "run_id": run_id, "rounds": body.rounds,
            "candidate": {"base_url": candidate["base_url"], "model": body.candidate.model, "protocol": body.candidate.protocol},
            "reference": body.reference.model_dump(),
            "candidate_identity": self.registry.connection_fingerprint(candidate),
            "reference_identity": self.registry.connection_fingerprint(channel),
        }
        if self.token_check is not None:
            try:
                binding = self._trusted_reference(body.reference.model, body.reference.protocol)
                configuration["token_reference"] = self._binding_configuration(binding)
            except (KeyError, RegistryError, ValueError):
                configuration["token_reference"] = None
        digest = hashlib.sha256(json.dumps(configuration, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        budget = self.budget_seconds or min(86400, 300 * (len(engine.QUESTIONS) * body.rounds + 2))
        task = self.store.queue.enqueue(storage.JOB_TYPE, {"run_id": run_id, "configuration_digest": digest},
                                        idempotency_key="admission:" + identity, budget_seconds=budget)
        try:
            existing = self.store.get(task["id"])
        except KeyError:
            report = {
                "version": 2, "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "running", "rounds": body.rounds, "warmup_rounds": 1 if body.rounds >= 2 else 0,
                "candidate": configuration["candidate"],
                "reference": {"base_url": channel["base_url"], "name": channel["name"], "channel_id": channel["id"],
                              "multiplier": channel["multiplier"], "model": body.reference.model, "protocol": body.reference.protocol},
                "questions": [{key: q[key] for key in ("id", "title", "difficulty", "prompt")} for q in engine.QUESTIONS],
                "responses": [], "measurements": [],
            }
            steps = [f"{number}:{question['id']}:{side}" for number in range(1, body.rounds + 1)
                     for question in engine.QUESTIONS for side in ("candidate", "reference")]
            if self.token_check is not None:
                steps += [f"token:{side}:{pair_index}" for pair_index in (0, 1) for side in ("candidate", "reference")]
            self.store.create(task["id"], scrub(configuration, secrets), scrub(report, secrets), steps)
            if self.on_submit is not None:
                self.on_submit(candidate["base_url"], body.candidate.model)
        else:
            if existing["state"] in TERMINAL:
                return self.public(task["id"])
        if task["state"] == "queued":
            self._credentials[task["id"]] = candidate["api_key"]
        self._wake.set()
        return self.public(task["id"])

    def public(self, task_id, *, include_report=True):
        job = self.store.get(task_id)
        counts = {state: sum(value == state for value in job["steps"].values()) for state in ("not_sent", "sending", "confirmed", "unknown", "not_needed")}
        result = {key: job[key] for key in ("id", "state", "created_at", "updated_at", "started_at", "finished_at", "cancel_requested", "queue_position", "report_id", "error")}
        result.update(status=job["state"], needs_credentials=job["state"] == "waiting_credentials",
                      model=job["configuration"]["candidate"]["model"], candidate_model=job["configuration"]["candidate"]["model"],
                      progress={"total_steps": len(job["steps"]), **counts, "completed_steps": counts["confirmed"] + counts["unknown"] + counts["not_needed"]})
        if include_report:
            result["report"] = reporting.finalize_report(job["report"]) if job["report"] else None
        return result

    def list(self):
        return [self.public(job["id"], include_report=False) for job in self.store.list()]

    def supply(self, task_id, api_key):
        job = self.store.get(task_id)
        if job["state"] != "waiting_credentials":
            raise Conflict("任务当前不能补充凭据")
        clean = normalize({"base_url": job["configuration"]["candidate"]["base_url"], "api_key": api_key})
        if self.registry.connection_fingerprint(clean) != job["configuration"]["candidate_identity"]:
            raise Conflict("候选凭据与原任务不一致，请提供同一 Key 或重新创建任务")
        self._reference(job["configuration"])
        self.store.supply(task_id)
        self._credentials[task_id] = clean["api_key"]
        self._wake.set()
        return self.public(task_id)

    async def cancel(self, task_id):
        job = self.store.get(task_id)
        if job["state"] in TERMINAL:
            return self.public(task_id)
        task = self.store.queue.cancel(task_id)
        if task["state"] == "cancelled":
            report = job["report"]
            report["status"] = "canceled"
            self.store.finish(task_id, reporting.finalize_report(report), "cancelled")
            self._credentials.pop(task_id, None)
        elif self._active_id == task_id and self._active is not None:
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
        self._wake.set()
        return self.public(task_id)

    async def subscribe(self, task_id, request):
        after = 0
        while True:
            replay = self.store.events(task_id, after)
            for event in replay["events"]:
                after = event["id"]
                yield engine.encode_event({key: value for key, value in event.items() if key != "id"})
            if self.store.get(task_id)["state"] in TERMINAL:
                if not self.store.events(task_id, after)["events"]:
                    return
                continue
            if await request.is_disconnected():
                return
            await asyncio.sleep(.05)

    async def _consume(self):
        while not self._closing:
            self._wake.clear()
            task = self.store.queue.claim_next(task_type=storage.JOB_TYPE, serial=True)
            if task is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=.2)
                except asyncio.TimeoutError:
                    pass
                continue
            self._active_id = task["id"]
            self._active = asyncio.create_task(self._execute(task["id"]))
            await asyncio.gather(self._active, return_exceptions=True)
            self._credentials.pop(task["id"], None)
            self._active = self._active_id = None

    async def _execute(self, task_id):
        job = self.store.get(task_id)
        report = job["report"]
        configuration = job["configuration"]
        state, error = "succeeded", ""
        try:
            key = self._credentials.get(task_id)
            if key is None:
                self.store.recover()
                return
            candidate = {**configuration["candidate"], "api_key": key}
            reference = self._reference(configuration)
            run = engine.CompareRequest(candidate=engine.EndpointConfig(**candidate), reference=engine.EndpointConfig(**reference), rounds=configuration["rounds"])
            redactor = EventRedactor([key, reference["api_key"]])
            total = len(engine.QUESTIONS) * run.rounds
            event = {"type": "run_started", "run_id": configuration["run_id"], "job_id": task_id,
                     "question_count": len(engine.QUESTIONS), "rounds": run.rounds, "warmup_rounds": report["warmup_rounds"],
                     "protocols": {"candidate": candidate["protocol"], "reference": reference["protocol"]}, "total_question_runs": total}
            self.store.checkpoint(task_id, report, event)
            seconds = self.store.start_budget(task_id)
            await asyncio.wait_for(self._run_steps(task_id, job, run, report, redactor), timeout=seconds)
            report["status"] = "completed"
        except asyncio.CancelledError:
            if self._closing:
                return
            state, report["status"] = "cancelled", "canceled"
        except asyncio.TimeoutError:
            state, report["status"], error = "failed", "failed", "准入任务超过运行预算"
        except (RegistryError, KeyError):
            state, report["status"], error = "failed", "failed", "任务连接已失效或改变，请重新创建任务"
        except Exception:
            state, report["status"], error = "failed", "failed", "准入执行或报告保存失败"
        try:
            self.store.finish(task_id, reporting.finalize_report(report), state, error)
        except ValueError:
            saved = self.store.get(task_id)["report"]
            saved["status"] = "failed"
            self.store.finish(task_id, saved, "failed", "准入报告超过保存上限，已保留最后确认的检查点")

    async def _run_steps(self, task_id, job, run, report, redactor):
        timeout = httpx.Timeout(connect=20, read=240, write=20, pool=20)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                    transport=self.transport or guarded_transport()) as client:
            if self.token_check is not None and "token:complete" not in self.store.get(task_id)["step_results"]:
                await self._run_token_check(task_id, run, client, report, redactor)
            for number in range(1, run.rounds + 1):
                for index, question in enumerate(engine.QUESTIONS, 1):
                    if self.store.queue.request_cancelled(task_id):
                        raise asyncio.CancelledError
                    selected = engine.select_question_variant(question, f"{report['run_id']}:{number}")
                    pair_id = f"{report['run_id']}:{number}:{selected['id']}"
                    metadata = {"round": number, "pair_id": pair_id, "round_role": "warmup" if number <= report["warmup_rounds"] else "evaluated",
                                "variant_id": selected.get("variant_id", "original")}
                    steps = self.store.get(task_id)["steps"]
                    pending = [side for side in ("candidate", "reference") if steps[f"{number}:{question['id']}:{side}"] == "not_sent"]
                    for side in ("candidate", "reference"):
                        if steps[f"{number}:{question['id']}:{side}"] == "unknown":
                            self._record(task_id, report, {"type": "side_finished", "question_id": question["id"], "side": side,
                                         "ok": False, "status": "unknown", "error_category": "unknown", "error": "上次请求已发送但结果未确认，未重放", **metadata})
                    if not pending:
                        continue
                    started = {"type": "question_started", "question_id": question["id"], "prompt": selected["prompt"], "index": index, **metadata}
                    self._record(task_id, report, started)
                    async def before_send(side):
                        self._reference(job["configuration"])
                        if not self.store.begin_step(task_id, f"{number}:{question['id']}:{side}"):
                            raise asyncio.CancelledError
                    if len(pending) == 2:
                        stream = engine.run_question(run, selected, client, before_send=before_send)
                    else:
                        stream = self._single_side(pending[0], run, selected, client, before_send)
                    async for event in stream:
                        for safe in redactor.filter(event):
                            safe = scrub({**safe, **metadata}, redactor.secrets)
                            self._record(task_id, report, safe, step_id=f"{number}:{question['id']}:{event['side']}" if safe["type"] == "side_finished" else None)
                    completed = (number - 1) * len(engine.QUESTIONS) + index
                    self._record(task_id, report, {"type": "question_finished", "question_id": question["id"], "completed_question_runs": completed,
                                                  "total_question_runs": len(engine.QUESTIONS) * run.rounds, **metadata})
                    if completed < len(engine.QUESTIONS) * run.rounds:
                        await engine.wait_between_questions()

    async def _single_side(self, side, run, question, client, before_send):
        queue = asyncio.Queue()
        task = asyncio.create_task(engine.measure_side(side, getattr(run, side), question, queue, client, before_send=before_send))
        event_task = None
        try:
            while True:
                event_task = asyncio.create_task(queue.get())
                await asyncio.wait([event_task, task], return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    task.result()
                event = await event_task
                event_task = None
                yield event
                if event["type"] == "side_finished":
                    break
            await task
        finally:
            if event_task is not None:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _record(self, task_id, report, event, *, step_id=None):
        if event["type"] == "question_started":
            for side in ("candidate", "reference"):
                identity = (event["round"], event["question_id"], side)
                if not any((row["round"], row["question_id"], row["side"]) == identity for row in report["responses"]):
                    report["responses"].append({**{key: event[key] for key in ("round", "question_id", "prompt", "pair_id", "round_role", "variant_id")}, "side": side, "content": "", "reasoning": ""})
        elif event["type"] == "chunk":
            for row in report["responses"]:
                if (row["round"], row["question_id"], row["side"]) == (event["round"], event["question_id"], event["side"]):
                    for field in ("content", "reasoning"):
                        row[field] = (row[field] + event.get(field, ""))[:200_000]
                    break
        elif event["type"] == "side_finished":
            report["measurements"] = [row for row in report["measurements"] if (row["round"], row["question_id"], row["side"]) != (event["round"], event["question_id"], event["side"])]
            report["measurements"].append(event)
        self.store.checkpoint(task_id, report, event, step_id=step_id, state="confirmed" if step_id else None)

    async def _run_token_check(self, task_id, run, client, report, redactor):
        from features.token_usage.domain import empty_result
        from features.token_usage.transport import SendControl
        initial = self.store.get(task_id)
        configuration = initial["configuration"]
        def sent_count(job):
            return sum(state in {"sending", "unknown"} or
                       (state == "confirmed" and job["step_results"].get(step, {}).get("status") != "not_sent")
                       for step, state in job["steps"].items() if step.startswith("token:"))
        historical_sends = sent_count(initial)
        def unavailable(reason):
            result = empty_result(reason, source="admission")
            count = sent_count(self.store.get(task_id))
            return {**result, "requests_sent": count, "new_requests_sent": max(0, count - historical_sends)}
        async def on_result(result):
            safe = scrub({**result, "source": "admission"}, redactor.secrets)
            previous = report.get("token_usage")
            if previous and previous.get("severity") == "high_risk" and safe.get("severity") != "high_risk":
                reason = safe.get("reason", "interrupted")
                safe = {**previous, "verification_status": safe.get("verification_status", reason),
                        "verification_reason": reason,
                        "requests_sent": max(previous.get("requests_sent", 0), safe.get("requests_sent", 0)),
                        "new_requests_sent": safe.get("new_requests_sent", 0)}
            report["token_usage"] = safe
            if configuration.get("token_reference"):
                report["token_usage"]["binding_revision"] = configuration["token_reference"]["revision"]
            self.store.checkpoint(task_id, report, {"type": "token_usage", "result": report["token_usage"]})
        def resolve_trusted():
            binding = self._trusted_reference(run.reference.model, run.reference.protocol)
            trusted = binding["resolved"]
            if self._binding_configuration(binding) != configuration.get("token_reference") or trusted.snapshot["registry_channel_id"] != configuration["reference"]["channel_id"] or self.registry.connection_fingerprint(trusted.channel) != configuration["reference_identity"] or trusted.channel["model"] != run.reference.model or trusted.channel["protocol"] != run.reference.protocol:
                raise Conflict("trusted_reference_missing_or_mismatch")
            return binding
        try:
            binding = resolve_trusted()
        except (KeyError, RegistryError, ValueError):
            await on_result(unavailable("trusted_reference_missing_or_mismatch"))
            self.store.skip_unused_token_steps(task_id)
            self.store.checkpoint(task_id, report, {"type": "token_usage_finished"}, step_id="token:complete", state="confirmed", result={"complete": True})
            return
        async def before_send(side, pair_index):
            self._reference(self.store.get(task_id)["configuration"])
            resolve_trusted()
            if not self.store.begin_step(task_id, f"token:{side}:{pair_index}"):
                raise asyncio.CancelledError
        async def on_attempt(side, pair_index, attempt):
            safe = scrub(attempt, redactor.secrets)
            self.store.checkpoint(task_id, report, {"type": "token_usage_attempt", "side": side, "pair_index": pair_index},
                                  step_id=f"token:{side}:{pair_index}", state="unknown" if safe.get("status") == "unknown" else "confirmed", result=safe)
        job = self.store.get(task_id)
        resume = {step: (job["step_results"].get(step) if state == "confirmed" else {"status": "unknown"})
                  for step, state in job["steps"].items() if step.startswith("token:") and state in {"confirmed", "unknown"}}
        reference = {**run.reference.model_dump(), "_token_thresholds": binding["thresholds"]}
        try:
            result = await self.token_check(run.candidate.model_dump(), reference, client=client,
                                           before_send=before_send, on_attempt=on_attempt, on_result=on_result, resume_attempts=resume)
        except SendControl as exc:
            if not isinstance(exc.__cause__, (KeyError, RegistryError, ValueError)):
                raise
            result = unavailable("trusted_reference_missing_or_mismatch")
        except asyncio.CancelledError:
            if not self._closing and report.get("token_usage", {}).get("severity") == "high_risk":
                reason = "cancelled_unknown" if self.store.queue.request_cancelled(task_id) else "execution_interrupted_unknown"
                await on_result(unavailable(reason))
            raise
        if result is not None:
            await on_result(result)
        self.store.skip_unused_token_steps(task_id)
        self.store.checkpoint(task_id, report, {"type": "token_usage_finished"}, step_id="token:complete", state="confirmed", result={"complete": True})
