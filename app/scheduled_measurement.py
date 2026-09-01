"""第一阶段定时监测执行器：精确配置、十轮串行测量和可追溯结果。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
import time
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException

from . import egress, paired_admission, paired_engine, paired_protocol, protocol, store
from .security import audit_hmac, decrypt, encrypt, redact_url, scrub

TASK_KIND = "scheduled_measurement"
LOCK_LEASE_SECONDS = 30
PRIMARY_OUTCOMES = {"success", "upstream_error", "platform_error", "attribution_pending"}


def templates() -> list[dict[str, Any]]:
    short = [
        "用 100 到 120 个英文单词概述一个高可靠 API 的重试原则。只输出正文。",
        "用 100 到 120 个英文单词解释如何避免缓存键冲突。只输出正文。",
        "用 100 到 120 个英文单词说明流式响应的正常结束条件。只输出正文。",
        "用 100 到 120 个英文单词总结最小可审计日志应包含什么。只输出正文。",
        "用 100 到 120 个英文单词描述一个健康检查的边界。只输出正文。",
    ]
    medium = [
        "用 360 到 440 个英文单词写一份简短运行手册，说明如何排查单个上游模型的间歇性超时。只输出正文。",
        "用 360 到 440 个英文单词写一份简短设计说明，比较延迟、吞吐和稳定性指标的用途。只输出正文。",
        "用 360 到 440 个英文单词写一份简短复盘模板，覆盖错误归因、证据和后续动作。只输出正文。",
        "用 360 到 440 个英文单词写一份简短变更说明，解释为什么精确连接配置需要不可变指纹。只输出正文。",
        "用 360 到 440 个英文单词写一份简短值班指南，说明遇到 429 时应该记录哪些事实。只输出正文。",
    ]
    return [
        {"id": f"short-{index + 1}", "kind": "short", "prompt": prompt, "max_tokens": 220,
         "minimum": 80, "maximum": 160}
        for index, prompt in enumerate(short)
    ] + [
        {"id": f"medium-{index + 1}", "kind": "medium", "prompt": prompt, "max_tokens": 800,
         "minimum": 320, "maximum": 640}
        for index, prompt in enumerate(medium)
    ]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _append_evidence(task_id: int, request_id: int | None, record_type: str, payload: dict[str, Any], raw: str | None = None) -> str:
    now = time.time()
    payload_json = _canonical(payload)
    with store.cursor() as cur:
        previous = cur.execute(
            "SELECT record_seq,record_hash FROM scheduled_measurement_evidence WHERE task_id=? ORDER BY record_seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        sequence = int(previous["record_seq"] + 1) if previous else 1
        previous_hash = str(previous["record_hash"]) if previous else "0" * 64
        raw_block_id = None
        raw_hash = ""
        if raw is not None:
            raw_hash = _hash(raw)
            data_key = Fernet.generate_key()
            cur.execute(
                "INSERT INTO scheduled_measurement_raw_blocks "
                "(task_id,block_type,key_ciphertext,ciphertext,content_hash,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, record_type, encrypt(data_key.decode("ascii")),
                 Fernet(data_key).encrypt(raw.encode("utf-8")).decode("ascii"), raw_hash, now),
            )
            raw_block_id = int(cur.lastrowid)
        material = _canonical({
            "task_id": task_id, "record_seq": sequence, "record_type": record_type,
            "payload": json.loads(payload_json), "raw_hash": raw_hash,
            "previous_hash": previous_hash, "created_at": now,
        })
        record_hash = _hash(material)
        cur.execute(
            "INSERT INTO scheduled_measurement_evidence "
            "(task_id,request_id,record_seq,record_type,payload_json,raw_block_id,previous_hash,record_hash,record_hmac,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, request_id, sequence, record_type, payload_json, raw_block_id,
             previous_hash, record_hash, audit_hmac("scheduled-measurement-evidence", record_hash), now),
        )
    return record_hash


def verify_evidence(task_id: int) -> dict[str, Any]:
    previous = "0" * 64
    expected_sequence = 1
    for row in store.query(
        "SELECT * FROM scheduled_measurement_evidence WHERE task_id=? ORDER BY record_seq", (task_id,)
    ):
        payload = store.loads(row["payload_json"], {})
        material = _canonical({
            "task_id": task_id, "record_seq": row["record_seq"], "record_type": row["record_type"],
            "payload": payload, "raw_hash": "", "previous_hash": row["previous_hash"], "created_at": row["created_at"],
        })
        # Raw blocks alter the record material. Recompute the hash through the immutable stored block hash.
        if row.get("raw_block_id"):
            block = store.get("scheduled_measurement_raw_blocks", row["raw_block_id"])
            raw_hash = (block or {}).get("content_hash", "")
            material = _canonical({
                "task_id": task_id, "record_seq": row["record_seq"], "record_type": row["record_type"],
                "payload": payload, "raw_hash": raw_hash, "previous_hash": row["previous_hash"], "created_at": row["created_at"],
            })
        if row["record_seq"] != expected_sequence or row["previous_hash"] != previous \
                or _hash(material) != row["record_hash"] \
                or audit_hmac("scheduled-measurement-evidence", row["record_hash"]) != row["record_hmac"]:
            return {"ok": False, "reason": "evidence_chain_invalid", "record_seq": row["record_seq"]}
        previous = row["record_hash"]
        expected_sequence += 1
    return {"ok": expected_sequence > 1, "root": previous, "records": expected_sequence - 1}


def create_task(run_id: int, plan_version: dict[str, Any], configuration: dict[str, Any], model_order: list[str]) -> int:
    mappings = store.query(
        "SELECT * FROM configuration_model_mappings WHERE configuration_id=? ORDER BY sort_order,id",
        (configuration["id"],),
    )
    by_model = {row["canonical_model"]: row for row in mappings}
    selected = [{
        "canonical_model": model, "request_model": by_model[model]["request_model"],
        "target_id": by_model[model]["legacy_target_id"],
    } for model in model_order]
    now = time.time()
    snapshot = {
        "scheduled_run_id": run_id, "plan_version_id": plan_version["id"],
        "configuration_id": configuration["id"], "configuration_fingerprint": configuration["configuration_fingerprint"],
        "protocol": configuration["protocol"], "base_url": redact_url(configuration["base_url"]),
        "model_mappings": selected, "templates": templates(), "model_order": model_order,
        "measurement_rules": store.loads(plan_version["measurement_rules_json"], {}),
        "threshold_version": plan_version["threshold_version"], "tokenizer": "paired-tokenizer-v1",
    }
    task_id = store.insert("tasks", {
        "kind": TASK_KIND, "target_name": f"定时监测 · {configuration['channel_name']} · {configuration['display_name']}",
        "pack_name": "精确连接配置定时监测", "pack_version": "scheduled-measurement-v1",
        "status": "queued", "snapshot": store.dumps(snapshot),
        "progress": store.dumps({"done": 0, "total": len(selected) * 10, "current": "等待定时监测"}),
        "include_hard": 0, "created_at": now,
    })
    _append_evidence(task_id, None, "task_snapshot", snapshot)
    return task_id


def _claim_lock(task_id: int, fingerprint: str) -> bool:
    now = time.time()
    with store.cursor() as cur:
        cur.execute("DELETE FROM configuration_execution_locks WHERE lease_until<=?", (now,))
        current = cur.execute(
            "SELECT task_id FROM configuration_execution_locks WHERE configuration_fingerprint=?", (fingerprint,)
        ).fetchone()
        if current and current["task_id"] != task_id:
            return False
        cur.execute(
            "INSERT OR REPLACE INTO configuration_execution_locks "
            "(configuration_fingerprint,task_id,task_kind,acquired_at,lease_until) VALUES (?,?,?,?,?)",
            (fingerprint, task_id, TASK_KIND, now, now + LOCK_LEASE_SECONDS),
        )
    return True


async def _renew_lock(task_id: int, fingerprint: str) -> None:
    while True:
        await asyncio.sleep(10)
        store.execute(
            "UPDATE configuration_execution_locks SET lease_until=? WHERE configuration_fingerprint=? AND task_id=?",
            (time.time() + LOCK_LEASE_SECONDS, fingerprint, task_id),
        )


def _release_lock(task_id: int) -> None:
    store.execute("DELETE FROM configuration_execution_locks WHERE task_id=?", (task_id,))


def _outcome_from_status(status: int, body: str) -> tuple[str, str]:
    lower = body.lower()
    if status in {401, 403, 404, 429} or 500 <= status < 600:
        return "upstream_error", f"http_{status}"
    if status in {400, 422} and ("model" in lower and ("not found" in lower or "does not exist" in lower)):
        return "upstream_error", "model_unavailable"
    return "attribution_pending", f"http_{status}"


async def _send_once(
    client: httpx.AsyncClient, configuration: dict[str, Any], mapping: dict[str, Any],
    template: dict[str, Any], probe: str,
) -> dict[str, Any]:
    started_wall = time.time()
    started_mono = time.monotonic()
    raw_events: list[dict[str, Any]] = []
    try:
        canonical = {
            "messages": [{"role": "user", "content": f"{template['prompt']}\n\n[measurement-probe:{probe}]"}],
            "max_tokens": template["max_tokens"], "stream": True, "temperature": 0.0,
        }
        payload = paired_protocol.adapt_request(configuration["protocol"], mapping["request_model"], canonical)
    except paired_protocol.AdapterError as exc:
        return {"outcome": "platform_error", "error_code": "adapter_error", "error": str(exc),
                "started_wall": started_wall, "finished_wall": time.time(),
                "started_mono": started_mono, "finished_mono": time.monotonic(), "raw": ""}
    key = decrypt(configuration["key_enc"])
    state = paired_protocol.new_stream_state(configuration["protocol"])
    ttft: float | None = None
    try:
        timeout = 180 if template["kind"] == "short" else 300
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST", protocol.chat_url(configuration["protocol"], configuration["base_url"]),
                headers=protocol.headers(configuration["protocol"], key), json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    body = (await response.aread()).decode("utf-8", "replace")
                    outcome, code = _outcome_from_status(response.status_code, body)
                    return {"outcome": outcome, "error_code": code, "error": scrub(body, key)[:300],
                            "http_status": response.status_code, "retry_after": response.headers.get("retry-after", ""),
                            "started_wall": started_wall, "finished_wall": time.time(),
                            "started_mono": started_mono, "finished_mono": time.monotonic(), "raw": scrub(body, key)}
                lines = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=90 if ttft is None else 60)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        return {
                            "outcome": "upstream_error",
                            "error_code": "first_token_timeout" if ttft is None else "stream_idle_timeout",
                            "error": "未在测量门槛内收到新的可见正文分片",
                            "started_wall": started_wall, "finished_wall": time.time(),
                            "started_mono": started_mono, "finished_mono": time.monotonic(),
                            "raw": _canonical(raw_events),
                        }
                    now_mono = time.monotonic()
                    raw_events.append({"monotonic_at": now_mono, "wall_at": time.time(), "line": scrub(line, key)})
                    delta = paired_protocol.consume_sse_line(state, line)
                    if delta and ttft is None and paired_engine.normalize_answer(state["text"]).strip():
                        ttft = now_mono - started_mono
        final = paired_protocol.finalize_stream(state)
        finished_mono = time.monotonic()
        text = paired_engine.normalize_answer(str(final["text"] or ""))
        if not final["normal_terminal"] or not text.strip():
            return {"outcome": "upstream_error", "error_code": "stream_incomplete", "error": "流式响应未正常结束或正文为空",
                    "started_wall": started_wall, "finished_wall": time.time(), "started_mono": started_mono,
                    "finished_mono": finished_mono, "raw": _canonical(raw_events)}
        if final["identity_conflict"] or (final["actual_model"] and not paired_admission.models_equivalent(mapping["request_model"], final["actual_model"])):
            return {"outcome": "upstream_error", "error_code": "model_identity", "error": "上游返回模型与冻结映射不一致",
                    "started_wall": started_wall, "finished_wall": time.time(), "started_mono": started_mono,
                    "finished_mono": finished_mono, "raw": _canonical(raw_events)}
        visible_tokens = len(paired_engine.tokenize(text))
        speed_eligible = template["minimum"] <= visible_tokens <= template["maximum"]
        return {"outcome": "success", "error_code": "", "error": "", "started_wall": started_wall,
                "finished_wall": time.time(), "started_mono": started_mono, "finished_mono": finished_mono,
                "ttft": ttft, "duration": finished_mono - started_mono, "visible_tokens": visible_tokens,
                "speed_eligible": speed_eligible, "actual_model": final["actual_model"], "raw": _canonical(raw_events)}
    except TimeoutError:
        return {"outcome": "upstream_error", "error_code": "total_timeout", "error": "达到本模板总耗时门槛",
                "started_wall": started_wall, "finished_wall": time.time(), "started_mono": started_mono,
                "finished_mono": time.monotonic(), "raw": _canonical(raw_events)}
    except httpx.HTTPError as exc:
        return {"outcome": "attribution_pending", "error_code": type(exc).__name__, "error": type(exc).__name__,
                "started_wall": started_wall, "finished_wall": time.time(), "started_mono": started_mono,
                "finished_mono": time.monotonic(), "raw": _canonical(raw_events)}


def _save_request(
    task_id: int, configuration_id: int, mapping: dict[str, Any], template: dict[str, Any],
    request_index: int, attempt_kind: str, original_request_id: int | None, result: dict[str, Any],
) -> dict[str, Any]:
    metrics = {key: result[key] for key in ("ttft", "duration", "visible_tokens", "speed_eligible", "actual_model") if key in result}
    request_id = store.insert("scheduled_measurement_requests", {
        "task_id": task_id, "original_request_id": original_request_id,
        "configuration_id": configuration_id, "canonical_model": mapping["canonical_model"],
        "template_id": template["id"], "template_kind": template["kind"], "request_index": request_index,
        "attempt_kind": attempt_kind, "sent_at": result.get("started_wall"), "finished_at": result.get("finished_wall"),
        "monotonic_started": result.get("started_mono"), "monotonic_finished": result.get("finished_mono"),
        "status": result["outcome"], "attribution": result["outcome"] if result["outcome"] != "success" else "",
        "error_code": result.get("error_code", ""), "error_detail": scrub(result.get("error", ""))[:500],
        "response_summary": scrub(str(result.get("actual_model", "")))[:200], "metrics_json": store.dumps(metrics),
        "created_at": time.time(),
    })
    evidence_hash = _append_evidence(task_id, request_id, "measurement_attempt", {
        "canonical_model": mapping["canonical_model"], "template_id": template["id"],
        "attempt_kind": attempt_kind, "outcome": result["outcome"], "error_code": result.get("error_code", ""),
        "started_at": result.get("started_wall"), "finished_at": result.get("finished_wall"), "metrics": metrics,
    }, raw=result.get("raw", ""))
    store.update("scheduled_measurement_requests", request_id, {"evidence_hash": evidence_hash})
    return store.get("scheduled_measurement_requests", request_id) or {}


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * quantile
    lower, upper = math.floor(pos), math.ceil(pos)
    return ordered[lower] if lower == upper else ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def _effective_status(row: dict[str, Any]) -> str:
    """Return the latest reviewed attribution without mutating the original observation."""
    if row["status"] != "attribution_pending":
        return row["status"]
    decisions = store.query(
        "SELECT attribution FROM scheduled_attribution_decisions WHERE request_id=? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    )
    return decisions[0]["attribution"] if decisions else "attribution_pending"


def _formal_measurements(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep originals immutable and select only the measurement facts eligible for metrics.

    A replacement replaces an original platform error, but is diagnostic-only while
    the original remains attribution-pending.  Upstream failures are never retried.
    """
    originals = [dict(row) for row in rows if row["attempt_kind"] == "original"]
    replacements: dict[int, dict[str, Any]] = {
        int(row["original_request_id"]): dict(row)
        for row in rows
        if row["attempt_kind"] == "replacement" and row.get("original_request_id")
    }
    formal: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for original in originals:
        original["effective_status"] = _effective_status(original)
        replacement = replacements.get(int(original["id"]))
        if replacement:
            replacement["effective_status"] = _effective_status(replacement)
            diagnostics.append(replacement)
        if original["effective_status"] == "platform_error":
            if replacement:
                formal.append(replacement)
            continue
        formal.append(original)
    return formal, diagnostics


def _model_report(rows: list[dict[str, Any]], canonical_model: str) -> dict[str, Any]:
    all_rows = [row for row in rows if row["canonical_model"] == canonical_model]
    formal, diagnostics = _formal_measurements(all_rows)
    denominator = [row for row in formal if row["effective_status"] in {"success", "upstream_error", "attribution_pending"}]
    successes = [row for row in denominator if row["effective_status"] == "success"]
    upstream = [row for row in denominator if row["effective_status"] == "upstream_error"]
    pending = [row for row in denominator if row["effective_status"] == "attribution_pending"]
    speed_rows = []
    for row in successes:
        metrics = store.loads(row["metrics_json"], {})
        if metrics.get("speed_eligible"):
            speed_rows.append((row, metrics))
    ttft = [float(metrics["ttft"]) for _, metrics in speed_rows if metrics.get("ttft") is not None]
    short = [float(metrics["duration"]) for row, metrics in speed_rows if row["template_kind"] == "short" and metrics.get("duration") is not None]
    medium = [float(metrics["duration"]) for row, metrics in speed_rows if row["template_kind"] == "medium" and metrics.get("duration") is not None]
    return {
        "canonical_model": canonical_model,
        "planned_opportunities": len([row for row in all_rows if row["attempt_kind"] == "original"]),
        "requests_sent": len([row for row in all_rows if row["attempt_kind"] != "not_sent"]),
        "stability_denominator": len(denominator),
        "success_count": len(successes), "upstream_error_count": len(upstream),
        "attribution_pending_count": len(pending),
        "platform_error_count": sum(_effective_status(row) == "platform_error" for row in all_rows),
        "success_rate": len(successes) / len(denominator) if denominator else None,
        "upstream_error_rate": len(upstream) / len(denominator) if denominator else None,
        "speed": {
            "ttft_median": statistics.median(ttft) if len(ttft) >= 8 else None,
            "ttft_samples": sorted(ttft), "short_duration_p95": _percentile(short, .95) if len(short) >= 4 else None,
            "short_duration_samples": sorted(short), "medium_duration_p95": _percentile(medium, .95) if len(medium) >= 4 else None,
            "medium_duration_samples": sorted(medium),
        },
        "connection_errors": sorted({
            row.get("error_code", "") for row in formal
            if row["effective_status"] == "upstream_error"
            and row.get("error_code", "") in {"http_401", "http_403", "model_unavailable", "protocol_error"}
        }),
        "replacement_diagnostic_count": len(diagnostics),
    }


def _save_report(task_id: int, snapshot: dict[str, Any], *, reason: str, created_by: int | None = None) -> dict[str, Any]:
    integrity = verify_evidence(task_id)
    rows = store.query("SELECT * FROM scheduled_measurement_requests WHERE task_id=? ORDER BY id", (task_id,))
    reports = [_model_report(rows, mapping["canonical_model"]) for mapping in snapshot["model_mappings"]]
    version_rows = store.query(
        "SELECT version FROM scheduled_measurement_report_revisions WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)
    )
    version = int(version_rows[0]["version"]) + 1 if version_rows else 1
    report = {
        "kind": "scheduled_measurement", "report_version": version,
        "configuration_id": snapshot["configuration_id"], "configuration_fingerprint": snapshot["configuration_fingerprint"],
        "plan_version_id": snapshot["plan_version_id"], "measurement_rules": snapshot["measurement_rules"],
        "threshold_version": snapshot["threshold_version"], "models": reports,
        "integrity": integrity, "measurement_problems": {
            "platform_system_errors": sum(model["platform_error_count"] for model in reports),
            "attribution_pending": sum(model["attribution_pending_count"] for model in reports),
        },
        "generated_at": time.time(),
    }
    root = integrity.get("root", "")
    store.insert("scheduled_measurement_report_revisions", {
        "task_id": task_id, "version": version, "reason": reason, "report_json": store.dumps(report),
        "input_evidence_root": root, "created_by": created_by, "created_at": time.time(),
    })
    store.update("tasks", task_id, {"report": store.dumps(report)})
    return report


def _retry_after_wait(result: dict[str, Any]) -> float:
    if result.get("error_code") != "http_429":
        return 0.0
    raw = str(result.get("retry_after") or "").strip()
    try:
        return min(60.0, max(0.0, float(raw)))
    except ValueError:
        return 30.0


async def run(task_id: int) -> None:
    task = store.get("tasks", task_id)
    if not task:
        return
    snapshot = store.loads(task["snapshot"], {})
    configuration = store.get("channel_configurations", snapshot.get("configuration_id"))
    if not configuration or configuration["configuration_fingerprint"] != snapshot.get("configuration_fingerprint"):
        store.update("tasks", task_id, {"status": "interrupted", "finished_at": time.time()})
        store.add_event(task_id, "精确连接配置已变化，定时任务在发送前停止", stage="配置", level="warn")
        return
    fingerprint = f"configuration:{configuration['configuration_fingerprint']}"
    while not _claim_lock(task_id, fingerprint):
        if (store.get("tasks", task_id) or {}).get("cancel_flag"):
            store.update("tasks", task_id, {"status": "cancelled", "finished_at": time.time()})
            return
        store.add_event(task_id, "等待同一精确连接配置的双端任务释放执行权", stage="调度")
        await asyncio.sleep(1)
    renewer = asyncio.create_task(_renew_lock(task_id, fingerprint))
    try:
        store.update("tasks", task_id, {"status": "running", "started_at": time.time()})
        _append_evidence(task_id, None, "execution_started", {"configuration_fingerprint": configuration["configuration_fingerprint"]})
        mappings = {row["canonical_model"]: row for row in snapshot["model_mappings"]}
        ordered_models = list(snapshot["model_order"])
        request_templates = list(snapshot["templates"])
        sent = 0
        blocked_configuration_reason = ""
        blocked_models: set[str] = set()
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, max_redirects=egress.EGRESS_MAX_REDIRECTS,
                                     event_hooks=egress.event_hooks()) as client:
            for round_index, template in enumerate(request_templates):
                rotation = ordered_models[round_index % len(ordered_models):] + ordered_models[:round_index % len(ordered_models)]
                for model in rotation:
                    current = store.get("tasks", task_id)
                    if not current or current.get("cancel_flag"):
                        store.update("tasks", task_id, {"status": "cancelled", "finished_at": time.time()})
                        _append_evidence(task_id, None, "execution_cancelled", {"sent": sent})
                        _save_report(task_id, snapshot, reason="任务取消")
                        return
                    mapping = mappings[model]
                    if blocked_configuration_reason or model in blocked_models:
                        skipped = {
                            "outcome": "not_sent", "error_code": "deterministic_connection_error",
                            "error": blocked_configuration_reason or "该模型已确认不可用",
                            "started_wall": None, "finished_wall": time.time(),
                            "started_mono": None, "finished_mono": time.monotonic(), "raw": "",
                        }
                        _save_request(task_id, configuration["id"], mapping, template, round_index + 1, "not_sent", None, skipped)
                        continue
                    probe = f"r{round_index + 1}-{model}-{task_id}"
                    result = await _send_once(client, configuration, mapping, template, probe)
                    row = _save_request(task_id, configuration["id"], mapping, template, round_index + 1, "original", None, result)
                    sent += 1
                    if result.get("error_code") in {"http_401", "http_403"}:
                        blocked_configuration_reason = "鉴权失败，停止该配置本批尚未发送的请求"
                    elif result.get("error_code") == "model_unavailable":
                        blocked_models.add(model)
                    if result["outcome"] in {"platform_error", "attribution_pending"}:
                        completed_mono = float(result.get("finished_mono") or time.monotonic())
                        cooldown = max(0.0, 5.0 - (time.monotonic() - completed_mono))
                        if cooldown:
                            _append_evidence(task_id, row["id"], "measurement_replacement_cooldown", {"seconds": cooldown})
                            await asyncio.sleep(cooldown)
                        replacement = await _send_once(client, configuration, mapping, template, f"{probe}-replacement")
                        _save_request(task_id, configuration["id"], mapping, template, round_index + 1, "replacement", row["id"], replacement)
                    wait = _retry_after_wait(result)
                    if wait:
                        _append_evidence(task_id, row["id"], "retry_after_wait", {"seconds": wait, "source": "upstream"})
                        await asyncio.sleep(wait)
                    store.update("tasks", task_id, {"progress": store.dumps({
                        "done": sent, "total": len(ordered_models) * len(request_templates),
                        "current": f"第 {round_index + 1} 轮 · {model}",
                    })})
        report = _save_report(task_id, snapshot, reason="首版封存")
        status = "success" if report["integrity"]["ok"] else "failed"
        store.update("tasks", task_id, {"status": status, "finished_at": time.time()})
        store.add_event(task_id, "定时监测报告已封存" if status == "success" else "证据完整性失败，报告仅保留历史", stage="完成", level="info" if status == "success" else "error")
    except Exception as exc:
        store.update("tasks", task_id, {"status": "failed", "finished_at": time.time()})
        store.add_event(task_id, f"定时监测执行异常：{type(exc).__name__}", stage="执行", level="error")
    finally:
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        _release_lock(task_id)


def correct_attribution(request_id: int, attribution: str, reason: str, user: dict[str, Any]) -> dict[str, Any]:
    if attribution not in {"upstream_error", "platform_error"}:
        raise HTTPException(status_code=400, detail="归因只能改为上游错误或平台系统错误")
    row = store.get("scheduled_measurement_requests", request_id)
    if not row or row["status"] != "attribution_pending":
        raise HTTPException(status_code=409, detail="只有归因待确认的测量请求可以人工归因")
    store.insert("scheduled_attribution_decisions", {
        "request_id": request_id, "attribution": attribution, "reason": reason.strip(),
        "user_id": user["id"], "actor": user["username"], "created_at": time.time(),
    })
    task = store.get("tasks", row["task_id"])
    if not task:
        raise HTTPException(status_code=404, detail="所属定时任务不存在")
    snapshot = store.loads(task["snapshot"], {})
    _append_evidence(row["task_id"], request_id, "attribution_decision", {
        "attribution": attribution, "reason": reason.strip(), "actor": user["username"],
    })
    report = _save_report(row["task_id"], snapshot, reason=f"人工归因修正：{reason.strip()}", created_by=user["id"])
    from . import scheduled_configurations
    scheduled_configurations.recalculate_anomalies_for_task(row["task_id"])
    return {"request_id": request_id, "report_version": report["report_version"], "report": report}
