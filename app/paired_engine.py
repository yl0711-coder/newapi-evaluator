"""双端配对准入执行器：连接隔离、对称调度、重试、聚合和报告。"""
from __future__ import annotations

import asyncio
import ctypes
import email.utils
import hashlib
import math
import os
import re
import shutil
import statistics
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from . import (egress, grading, paired_admission, paired_assets, paired_evidence,
               paired_protocol, protocol, store)
from .config import (PAIRED_COOLDOWN_SECONDS, PAIRED_FIDELITY_TIMEOUT_SECONDS,
                     PAIRED_FORMAL_TIMEOUT_SECONDS, PAIRED_TASK_ACTIVE_LIMIT_SECONDS)
from .security import redact_url, scrub

_execution_lock = asyncio.Lock()
_active_requests: dict[int, set[asyncio.Task[Any]]] = {}
_TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
_SYSTEM_ERRORS = {
    "connection_error", "request_timeout", "temporary_service_error",
    "protocol_error", "stream_interrupted",
}
_RETRYABLE = _SYSTEM_ERRORS | {"rate_limited"}


class StopExecution(RuntimeError):
    def __init__(self, reason: str, title: str):
        super().__init__(reason)
        self.reason = reason
        self.title = title


class TaskCanceled(RuntimeError):
    pass


class BudgetStopped(StopExecution):
    def __init__(self):
        super().__init__("budget_stopped", "预算停止")


def cancel_runtime(task_id: int) -> None:
    for task in tuple(_active_requests.get(task_id, set())):
        task.cancel()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def normalize_answer(text: str) -> str:
    value = text.lstrip()
    if value.startswith("<think>"):
        end = value.find("</think>")
        if end >= 0:
            return value[end + len("</think>"):].lstrip()
    return value


def _visible_offset(text: str) -> int | None:
    leading = len(text) - len(text.lstrip())
    value = text[leading:]
    if not value:
        return None
    if value.startswith("<think>"):
        end = value.find("</think>")
        if end < 0:
            return None
        offset = leading + end + len("</think>")
        while offset < len(text) and text[offset].isspace():
            offset += 1
        return offset if offset < len(text) else None
    return leading


def _first_visible_at(chunks: list[dict[str, Any]], text: str) -> float | None:
    offset = _visible_offset(text)
    if offset is None:
        return None
    cursor = 0
    for chunk in chunks:
        cursor += len(chunk["delta"])
        if cursor > offset:
            return float(chunk["received_monotonic"])
    return None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _difference(candidate: float | None, benchmark: float | None) -> dict[str, Any]:
    absolute = candidate - benchmark \
        if candidate is not None and benchmark is not None else None
    percent = absolute / benchmark * 100 \
        if absolute is not None and benchmark not in {None, 0} else None
    return {"candidate": candidate, "benchmark": benchmark,
            "absolute": absolute, "percent": percent}


def _apply_request_policy(
    canonical: dict[str, Any], policy: dict[str, Any],
) -> dict[str, Any]:
    omitted = set(policy.get("symmetric_omissions") or [])
    return {key: value for key, value in canonical.items() if key not in omitted}


def _check_cancel(task_id: int) -> None:
    task = store.get("tasks", task_id)
    paired = store.get("paired_tasks", task_id, key="task_id")
    if paired and paired.get("stop_reason") == "scheduling_isolation_lost":
        raise StopExecution("scheduling_isolation_lost", "调度隔离失效")
    if paired and paired.get("stop_reason") == "worker_lease_lost":
        raise StopExecution("worker_lease_lost", "执行节点失联")
    if paired and paired.get("stop_reason") == "evidence_storage_failed":
        raise paired_evidence.EvidenceError("evidence_storage_failed")
    if task and task.get("cancel_flag"):
        if paired and paired.get("stop_reason") == "fidelity_truth_invalidated":
            raise StopExecution("fidelity_truth_invalidated", "保真定义失效")
        raise TaskCanceled()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=None,
        follow_redirects=True,
        max_redirects=egress.EGRESS_MAX_REDIRECTS,
        event_hooks=egress.event_hooks(),
    )


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "content-type", "retry-after", "request-id", "x-request-id",
        "openai-request-id", "openai-processing-ms", "openai-version",
        "openai-system-fingerprint", "anthropic-request-id", "anthropic-version",
    }
    return {key: value for key, value in headers.items() if key.casefold() in allowed}


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _outcome(
    *, status_code: int | None, timed_out: bool, transport_error: bool,
    stream_error: bool, parsed: dict[str, Any], aliases: set[str],
) -> tuple[str, list[str]]:
    tags: list[str] = []
    if status_code is None:
        return ("request_timeout" if timed_out else "connection_error"), tags
    if status_code < 200 or status_code >= 300:
        if status_code == 429:
            return "rate_limited", tags
        if status_code in {408, 500, 502, 503, 504, 520, 521, 522, 523, 524}:
            return "temporary_service_error", tags
        return "http_error", tags
    if parsed.get("identity_conflict"):
        return "identity_mismatch", ["conflicting_model_fields"]
    actual_model = str(parsed.get("actual_model") or "")
    if actual_model and actual_model not in aliases:
        return "identity_mismatch", ["unexpected_model"]
    if timed_out:
        if not parsed.get("normal_terminal"):
            tags.append("stream_incomplete")
        return "request_timeout", tags
    if stream_error or transport_error:
        return "stream_interrupted", tags
    if parsed.get("malformed_events"):
        return "protocol_error", ["malformed_sse"]
    if parsed.get("identity_missing"):
        return "protocol_error", ["missing_model_field"]
    if not parsed.get("normal_terminal"):
        return "protocol_error", ["missing_protocol_terminal"]
    if parsed.get("truncated"):
        return "truncated", tags
    if not normalize_answer(str(parsed.get("text") or "")):
        return "protocol_error", ["empty_response"]
    return "success", tags


async def _request(
    task_id: int, side: str, client: httpx.AsyncClient,
    snapshot: dict[str, Any], canonical: dict[str, Any], *,
    stage: str, unit_id: str, pair_id: str | None, attempt_number: int,
    aliases: set[str], timeout_seconds: float,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    body = paired_protocol.adapt_request(snapshot["protocol"], snapshot["model"], canonical)
    url = protocol.chat_url(snapshot["protocol"], snapshot["base_url"])
    headers = protocol.headers(snapshot["protocol"], snapshot["key"])
    sent_monotonic = time.monotonic()
    sent_wall = time.time()
    paired_evidence.append(task_id, "side_request", {
        "stage": stage, "unit_id": unit_id, "pair_id": pair_id, "side": side,
        "request_id": request_id, "attempt_number": attempt_number,
        "sent_wall": sent_wall, "sent_monotonic": sent_monotonic,
        "adapter_version": paired_protocol.ADAPTER_VERSIONS[snapshot["protocol"]],
        "canonical_request_hash": hashlib_sha256(
            paired_evidence.canonical_json(canonical)
        ),
        "url": redact_url(url), "headers": {"content-type": "application/json"},
    }, raw=paired_evidence.canonical_json(body), block_type="request_body")
    parser_state = paired_protocol.new_stream_state(snapshot["protocol"])
    chunks: list[dict[str, Any]] = []
    raw_chunk_index = 0
    status_code: int | None = None
    response_headers: dict[str, str] = {}
    retry_after: float | None = None
    timed_out = False
    transport_error = False
    stream_error = False
    error_detail = ""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.stream("POST", url, headers=headers, json=body) as response:
                status_code = response.status_code
                response_headers = _safe_headers(response.headers)
                retry_after = _retry_after_seconds(response.headers)
                paired_evidence.append(task_id, "response_headers", {
                    "request_id": request_id, "side": side, "status_code": status_code,
                    "headers": response_headers, "received_wall": time.time(),
                    "received_monotonic": time.monotonic(),
                })
                if status_code < 200 or status_code >= 300:
                    raw_body = scrub(
                        (await response.aread()).decode("utf-8", errors="replace"),
                        snapshot["key"],
                    )
                    paired_evidence.append(task_id, "response_body", {
                        "request_id": request_id, "side": side,
                        "status_code": status_code,
                    }, raw=raw_body, block_type="http_error_body")
                else:
                    try:
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            raw_chunk_index += 1
                            received_wall = time.time()
                            received = time.monotonic()
                            delta = paired_protocol.consume_sse_line(parser_state, line)
                            safe_line = scrub(line, snapshot["key"])
                            paired_evidence.append(task_id, "response_chunk", {
                                "request_id": request_id, "side": side,
                                "chunk_index": raw_chunk_index,
                                "received_wall": received_wall,
                                "received_monotonic": received,
                                "content_hash": hashlib_sha256(safe_line),
                            }, raw=safe_line, block_type="sse_chunk")
                            if delta:
                                chunks.append({"delta": delta, "received_monotonic": received})
                    except httpx.RequestError as exc:
                        stream_error = True
                        error_detail = type(exc).__name__
    except TimeoutError:
        timed_out = True
        error_detail = "request_timeout"
    except httpx.RequestError as exc:
        transport_error = True
        error_detail = type(exc).__name__
    parsed = paired_protocol.finalize_stream(parser_state)
    primary, tags = _outcome(
        status_code=status_code, timed_out=timed_out, transport_error=transport_error,
        stream_error=stream_error, parsed=parsed, aliases=aliases,
    )
    ended_wall = time.time()
    ended_monotonic = time.monotonic()
    text = scrub(str(parsed.get("text") or ""), snapshot["key"])
    normalized = normalize_answer(text)
    first_visible = _first_visible_at(chunks, text)
    ttft = first_visible - sent_monotonic if first_visible is not None else None
    duration = ended_monotonic - sent_monotonic
    last_visible = chunks[-1]["received_monotonic"] if chunks else None
    generation_duration = last_visible - first_visible \
        if first_visible is not None and last_visible is not None and last_visible > first_visible else None
    token_count = len(tokenize(normalized))
    tps = token_count / generation_duration if generation_duration else None
    result = {
        "side": side, "request_id": request_id, "stage": stage,
        "unit_id": unit_id, "pair_id": pair_id, "attempt_number": attempt_number,
        "status_code": status_code, "primary_outcome": primary,
        "diagnostic_tags": tags, "retryable": primary in _RETRYABLE,
        "actual_model": parsed.get("actual_model") or "",
        "models": parsed.get("models") or [],
        "system_fingerprint": response_headers.get("openai-system-fingerprint", ""),
        "normal_terminal": parsed.get("normal_terminal", False),
        "truncated": parsed.get("truncated", False),
        "raw_text": text, "normalized_text": normalized,
        "sent_wall": sent_wall, "sent_monotonic": sent_monotonic,
        "ended_wall": ended_wall, "ended_monotonic": ended_monotonic,
        "duration": duration,
        "ttft": ttft, "tps": tps, "token_count": token_count,
        "text_chunk_count": len(chunks), "retry_after": retry_after,
        "error_detail": error_detail,
    }
    side_attempt_record = paired_evidence.append(task_id, "side_attempt", {
        key: value for key, value in result.items() if key not in {"raw_text"}
    }, raw=text, block_type="assembled_response")
    result["input_evidence_hashes"] = [side_attempt_record["record_hash"]]
    return result


def hashlib_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _grade(result: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if result["primary_outcome"] not in {"success"}:
        result["grade"] = {"status": "not_graded", "passed": None,
                           "reason": result["primary_outcome"]}
        return result
    observation = {
        "completion_status": "completed",
        "text": result["normalized_text"],
        "tool_calls": [],
        "finish_reason": "stop",
    }
    first = grading.grade(item, observation)
    grade_result = first
    recovered = False
    if first.get("status") == "grader_error":
        grade_result = grading.grade(item, observation)
        recovered = grade_result.get("status") != "grader_error"
    if grade_result.get("status") == "grader_error":
        grade = {"status": "invalid", "passed": None,
                 "reason": grade_result.get("reason") or "grader_error",
                 "grader": grade_result.get("grader"), "recovered": False}
    else:
        passed = grade_result.get("status") == "passed"
        grade = {"status": "passed" if passed else "failed", "passed": passed,
                 "reason": grade_result.get("reason") or "",
                 "grader": grade_result.get("grader"), "recovered": recovered,
                 "checks": grade_result.get("checks") or []}
        if not passed:
            result["primary_outcome"] = "content_failed"
            result["retryable"] = False
    result["grade"] = grade
    return result


def _fidelity_review(result: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    review_mode = str(item.get("review_mode") or "human")
    review: dict[str, Any] = {
        "stage": "fidelity",
        "item_id": item["id"],
        "instance_id": item.get("instance_id") or item["id"],
        "content_hash": item.get("content_hash"),
        "attempt_number": result["attempt_number"],
        "review_mode": review_mode,
        "actual_model": result.get("actual_model") or "",
        "protocol_outcome": result["primary_outcome"],
        "input_evidence_hashes": result.get("input_evidence_hashes") or [],
    }
    if review_mode == "human":
        review["check_status"] = "human_review_required"
        review["matched"] = None
        return review
    observation = {
        "completion_status": "completed",
        "text": result["normalized_text"],
        "tool_calls": [],
        "finish_reason": "stop",
    }
    grade_result = grading.grade(item, observation)
    matched = grade_result.get("status") == "passed"
    review["check_status"] = "matched" if matched else "manual_review_required"
    review["matched"] = matched
    review["grade"] = grade_result
    return review


def _budget_reservation(
    task_id: int, snapshots: list[dict[str, Any]], canonical: dict[str, Any],
) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    assert paired
    request_count = len(snapshots)
    input_tokens = len(tokenize("\n".join(
        str(message.get("content") or "") for message in canonical.get("messages") or []
    )))
    maximum_tokens = request_count * (input_tokens + int(canonical["max_tokens"]))
    maximum_money = 0.0
    for snapshot in snapshots:
        price_in, price_out = paired_admission.prices(snapshot)
        maximum_money += input_tokens / 1_000_000 * price_in \
            + int(canonical["max_tokens"]) / 1_000_000 * price_out
    if paired.get("request_limit") is not None \
            and paired["requests_used"] + request_count > paired["request_limit"]:
        raise BudgetStopped()
    if paired.get("token_limit") is not None \
            and paired["tokens_used"] + maximum_tokens > paired["token_limit"]:
        raise BudgetStopped()
    if paired.get("money_limit") is not None \
            and paired["money_used"] + maximum_money > paired["money_limit"]:
        raise BudgetStopped()


def _settle_budget(
    task_id: int, snapshots: list[dict[str, Any]], canonical: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    assert paired
    input_tokens = len(tokenize("\n".join(
        str(message.get("content") or "") for message in canonical.get("messages") or []
    )))
    tokens = 0
    money = 0.0
    for snapshot, result in zip(snapshots, results):
        output_tokens = int(result.get("token_count") or 0)
        tokens += input_tokens + output_tokens
        price_in, price_out = paired_admission.prices(snapshot)
        money += input_tokens / 1_000_000 * price_in \
            + output_tokens / 1_000_000 * price_out
    store.update("paired_tasks", task_id, {
        "requests_used": int(paired["requests_used"]) + len(results),
        "tokens_used": int(paired["tokens_used"]) + tokens,
        "money_used": float(paired["money_used"]) + money,
    }, key="task_id")


async def _dispatch_pair(
    task_id: int, clients: list[httpx.AsyncClient], snapshots: list[dict[str, Any]],
    canonical: dict[str, Any], *, stage: str, unit_id: str, pair_id: str,
    attempt_number: int, dispatch_seq: int, aliases: list[set[str]],
    timeout_seconds: float, retry_root_pair_id: str | None = None,
    retry_of_pair_id: str | None = None,
) -> list[dict[str, Any]]:
    _check_cancel(task_id)
    _verify_resource_locks(task_id, snapshots)
    _budget_reservation(task_id, snapshots, canonical)
    for snapshot in snapshots:
        paired_protocol.adapt_request(snapshot["protocol"], snapshot["model"], canonical)
    barrier_at = time.monotonic()
    order = [0, 1] if dispatch_seq % 2 else [1, 0]
    execution_unit_record = paired_evidence.append(
        task_id, "execution_unit", {
            "stage": stage, "unit_id": unit_id, "pair_id": pair_id,
            "attempt_number": attempt_number,
            "canonical_request_hash": hashlib_sha256(
                paired_evidence.canonical_json(canonical)
            ),
            "allowed_side_differences": [
                "base_url", "authentication", "model", "protocol_envelope",
                "required_headers",
            ],
            "retry_root_pair_id": retry_root_pair_id,
            "retry_of_pair_id": retry_of_pair_id,
            "dispatch_seq": dispatch_seq,
            "planned_first": "candidate" if order[0] == 0 else "benchmark",
        },
        raw=paired_evidence.canonical_json(canonical),
        block_type="canonical_request",
    )
    paired_evidence.append(task_id, "dispatch_barrier", {
        "stage": stage, "unit_id": unit_id, "pair_id": pair_id,
        "dispatch_seq": dispatch_seq,
        "planned_first": "candidate" if order[0] == 0 else "benchmark",
        "barrier_at": barrier_at,
    })
    coroutines = [
        _request(
            task_id, "candidate" if index == 0 else "benchmark",
            clients[index], snapshots[index], canonical,
            stage=stage, unit_id=unit_id, pair_id=pair_id,
            attempt_number=attempt_number, aliases=aliases[index],
            timeout_seconds=timeout_seconds,
        )
        for index in order
    ]
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    _active_requests.setdefault(task_id, set()).update(tasks)
    try:
        ordered_results = await asyncio.gather(*tasks)
    except paired_evidence.EvidenceError:
        for request_task in tasks:
            request_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    except asyncio.CancelledError:
        for request_task in tasks:
            request_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _check_cancel(task_id)
        raise
    finally:
        active = _active_requests.get(task_id, set())
        active.difference_update(tasks)
    results: list[dict[str, Any]] = [{}, {}]
    for index, result in zip(order, ordered_results):
        results[index] = result
        result.setdefault("input_evidence_hashes", []).insert(
            0, execution_unit_record["record_hash"]
        )
    _settle_budget(task_id, snapshots, canonical, results)
    sent_gap = abs(float(results[0]["sent_monotonic"]) - float(results[1]["sent_monotonic"]))
    paired_evidence.append(task_id, "dispatch_result", {
        "stage": stage, "pair_id": pair_id, "dispatch_seq": dispatch_seq,
        "actual_first": "candidate" if results[0]["sent_monotonic"] <= results[1]["sent_monotonic"]
        else "benchmark", "startup_skew": sent_gap,
    })
    return results


async def _dispatch_single(
    task_id: int, client: httpx.AsyncClient, snapshot: dict[str, Any],
    canonical: dict[str, Any], *, stage: str, unit_id: str,
    attempt_number: int, aliases: set[str], timeout_seconds: float,
) -> dict[str, Any]:
    _check_cancel(task_id)
    _verify_resource_locks(task_id, [snapshot])
    _budget_reservation(task_id, [snapshot], canonical)
    paired_evidence.append(
        task_id, "execution_unit", {
            "stage": stage, "unit_id": unit_id, "pair_id": None,
            "attempt_number": attempt_number,
            "canonical_request_hash": hashlib_sha256(
                paired_evidence.canonical_json(canonical)
            ),
            "allowed_side_differences": [],
            "retry_root_pair_id": None,
            "retry_of_pair_id": None,
        },
        raw=paired_evidence.canonical_json(canonical),
        block_type="canonical_request",
    )
    request_task = asyncio.create_task(_request(
        task_id, "candidate", client, snapshot, canonical, stage=stage,
        unit_id=unit_id, pair_id=None, attempt_number=attempt_number,
        aliases=aliases, timeout_seconds=timeout_seconds,
    ))
    _active_requests.setdefault(task_id, set()).add(request_task)
    try:
        result = await request_task
    except asyncio.CancelledError:
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        _check_cancel(task_id)
        raise
    finally:
        _active_requests.get(task_id, set()).discard(request_task)
    _settle_budget(task_id, [snapshot], canonical, [result])
    return result


def _resource_fingerprints(snapshots: list[dict[str, Any]]) -> list[str]:
    return sorted({paired_admission.resource_fingerprint(snapshot) for snapshot in snapshots})


def _verify_resource_locks(
    task_id: int, snapshots: list[dict[str, Any]],
) -> None:
    expected = _resource_fingerprints(snapshots)
    now = time.time()
    rows = store.query(
        "SELECT configuration_fingerprint,task_id,lease_until FROM configuration_execution_locks "
        "WHERE configuration_fingerprint IN ({})".format(
            ",".join("?" for _ in expected)
        ),
        tuple(expected),
    ) if expected else []
    observed = {
        row["configuration_fingerprint"] for row in rows
        if row["task_id"] == task_id and float(row["lease_until"]) > now
    }
    if observed != set(expected):
        raise StopExecution("scheduling_isolation_lost", "调度隔离失效")


def _acquire_resources(task_id: int, snapshots: list[dict[str, Any]]) -> list[str]:
    now = time.time()
    fingerprints = _resource_fingerprints(snapshots)
    with store.cursor() as cur:
        cur.execute("DELETE FROM configuration_execution_locks WHERE lease_until<=?", (now,))
        for fingerprint in fingerprints:
            locked = cur.execute(
                "SELECT task_id FROM configuration_execution_locks WHERE configuration_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if locked and locked["task_id"] != task_id:
                raise StopExecution("scheduling_isolation_lost", "调度隔离失效")
        for fingerprint in fingerprints:
            cur.execute(
                "INSERT OR REPLACE INTO configuration_execution_locks "
                "(configuration_fingerprint,task_id,task_kind,acquired_at,lease_until) VALUES (?,?,?,?,?)",
                (fingerprint, task_id, "paired", now, now + 30),
            )
    paired_evidence.append(task_id, "resource_locks_acquired", {
        "resource_fingerprints": fingerprints, "acquired_at": now,
    })
    return fingerprints


def _scheduled_measurement_waiting(fingerprints: list[str]) -> bool:
    expected = set(fingerprints)
    for task in store.query(
        "SELECT snapshot FROM tasks WHERE kind='scheduled_measurement' AND status='queued'"
    ):
        snapshot = store.loads(task["snapshot"], {})
        fingerprint = snapshot.get("configuration_fingerprint")
        if fingerprint and f"configuration:{fingerprint}" in expected:
            return True
    return False


async def _wait_acquire_resources(task_id: int, snapshots: list[dict[str, Any]]) -> list[str]:
    fingerprints = _resource_fingerprints(snapshots)
    announced = False
    while True:
        if _scheduled_measurement_waiting(fingerprints):
            if not announced:
                store.add_event(task_id, "同配置定时监测优先，等待其取得执行权", stage="调度")
                announced = True
            await asyncio.sleep(1)
            continue
        try:
            return _acquire_resources(task_id, snapshots)
        except StopExecution as exc:
            if exc.reason != "scheduling_isolation_lost":
                raise
            if not announced:
                store.add_event(task_id, "同一精确连接配置正在测试，等待执行权", stage="调度")
                announced = True
            await asyncio.sleep(1)


async def _renew_resources(task_id: int, fingerprints: list[str]) -> None:
    while True:
        await asyncio.sleep(10)
        now = time.time()
        rows = store.query(
            "SELECT configuration_fingerprint,lease_until FROM configuration_execution_locks "
            "WHERE task_id=?", (task_id,),
        )
        active = {row["configuration_fingerprint"] for row in rows
                  if float(row["lease_until"]) > now}
        if active != set(fingerprints):
            store.update(
                "paired_tasks", task_id,
                {"stop_reason": "scheduling_isolation_lost"}, key="task_id",
            )
            cancel_runtime(task_id)
            return
        leases = store.query(
            "SELECT lease_until FROM job_leases WHERE task_id=?", (task_id,)
        )
        if not leases or float(leases[0]["lease_until"]) <= now:
            store.update(
                "paired_tasks", task_id,
                {"stop_reason": "worker_lease_lost"}, key="task_id",
            )
            cancel_runtime(task_id)
            return
        store.execute(
            "UPDATE configuration_execution_locks SET lease_until=? WHERE task_id=?",
            (now + 30, task_id),
        )
        try:
            paired_evidence.append(task_id, "resource_lease_renewed", {
                "resource_fingerprints": fingerprints,
                "lease_until": now + 30,
            })
        except paired_evidence.EvidenceError:
            store.update(
                "paired_tasks", task_id,
                {"stop_reason": "evidence_storage_failed"}, key="task_id",
            )
            cancel_runtime(task_id)
            return


def _release_resources(task_id: int) -> None:
    store.execute("DELETE FROM configuration_execution_locks WHERE task_id=?", (task_id,))


def _available_memory_bytes() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.available_physical)
        return None
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None


def _environment_snapshot(task_id: int, fingerprints: list[str]) -> dict[str, Any]:
    data_path = store.DB_PATH if hasattr(store, "DB_PATH") else None
    disk = shutil.disk_usage(str(data_path.parent if data_path else "."))
    snapshot = {
        "worker_process_id": os.getpid(),
        "monotonic_clock": "time.monotonic",
        "same_process_dispatch": True,
        "exclusive_paired_worker": True,
        "resource_fingerprints": fingerprints,
        "evidence_store_available": True,
        "disk_free_bytes": disk.free,
        "available_memory_bytes": _available_memory_bytes(),
        "captured_at": time.time(),
        "load_thresholds_applied": False,
    }
    paired_evidence.append(task_id, "execution_environment", snapshot)
    return snapshot


async def _telemetry(task_id: int) -> None:
    previous_wall = time.monotonic()
    previous_cpu = time.process_time()
    expected_wake = previous_wall + 5
    persistence_latency: float | None = None
    while True:
        await asyncio.sleep(5)
        now_wall = time.monotonic()
        now_cpu = time.process_time()
        elapsed = max(now_wall - previous_wall, 1e-9)
        cpu_ratio = max(0.0, (now_cpu - previous_cpu) / elapsed)
        disk = shutil.disk_usage(str(store.DB_PATH.parent))
        try:
            paired_evidence.append(task_id, "resource_telemetry", {
                "process_cpu_ratio": cpu_ratio,
                "available_memory_bytes": _available_memory_bytes(),
                "disk_free_bytes": disk.free,
                "sample_window_seconds": elapsed,
                "evidence_persistence_seconds": persistence_latency,
                "scheduler_delay_seconds": max(0.0, now_wall - expected_wake),
                "load_thresholds_applied": False,
            })
        except paired_evidence.EvidenceError:
            paired_admission.mark_evidence_storage_failed(task_id)
            cancel_runtime(task_id)
            return
        persistence_latency = time.monotonic() - now_wall
        previous_wall, previous_cpu = now_wall, now_cpu
        expected_wake = now_wall + 5


async def _cooldown(task_id: int, seconds: float, reason: str) -> None:
    if seconds <= 0:
        return
    paired_evidence.append(task_id, "cooldown", {
        "reason": reason, "planned_seconds": 5 if seconds == PAIRED_COOLDOWN_SECONDS else seconds,
        "selftest_actual_seconds": seconds,
    })
    await asyncio.sleep(seconds)
    _check_cancel(task_id)


def _deadline_remaining(start_monotonic: float, active_before: float) -> float:
    used = active_before + (time.monotonic() - start_monotonic)
    remaining = PAIRED_TASK_ACTIVE_LIMIT_SECONDS - used
    if remaining <= 0:
        raise StopExecution("task_deadline", "任务时限停止")
    return remaining


async def _aux_pair(
    task_id: int, clients: list[httpx.AsyncClient], snapshots: list[dict[str, Any]],
    canonical: dict[str, Any], stage: str, aliases: list[set[str]],
    dispatch_state: dict[str, int], deadline: callable,
) -> tuple[list[httpx.AsyncClient], list[dict[str, Any]]]:
    last_results: list[dict[str, Any]] = []
    root_pair_id: str | None = None
    previous_pair_id: str | None = None
    for attempt in range(1, 4):
        timeout = min(PAIRED_FIDELITY_TIMEOUT_SECONDS, float(deadline()))
        pair_id = uuid.uuid4().hex
        root_pair_id = root_pair_id or pair_id
        dispatch_state[stage] = dispatch_state.get(stage, 0) + 1
        last_results = await _dispatch_pair(
            task_id, clients, snapshots, canonical, stage=stage,
            unit_id=canonical["id"], pair_id=pair_id, attempt_number=attempt,
            dispatch_seq=dispatch_state[stage], aliases=aliases,
            timeout_seconds=timeout, retry_root_pair_id=root_pair_id,
            retry_of_pair_id=previous_pair_id,
        )
        previous_pair_id = pair_id
        if any(result["primary_outcome"] == "identity_mismatch" for result in last_results):
            raise StopExecution("identity_mismatch", "身份冲突停止")
        if all(result["primary_outcome"] == "success" for result in last_results):
            return clients, last_results
        if any(result["primary_outcome"] not in _RETRYABLE for result in last_results):
            raise StopExecution(f"{stage}_deterministic_failure", f"{stage} 确定性失败")
        if attempt == 3:
            break
        await asyncio.gather(*(client.aclose() for client in clients))
        await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, f"{stage}_retry")
        clients = [_client(), _client()]
    raise StopExecution(f"{stage}_retry_exhausted", f"{stage} 连续三次未恢复")


async def _retry_warmup(
    task_id: int, snapshots: list[dict[str, Any]], aliases: list[set[str]],
    dispatch_state: dict[str, int], deadline: callable,
    warmup_request: dict[str, Any],
) -> list[httpx.AsyncClient]:
    clients = [_client(), _client()]
    try:
        clients, _ = await _aux_pair(
            task_id, clients, snapshots, warmup_request,
            "retry_warmup", aliases, dispatch_state, deadline,
        )
        return clients
    except Exception:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        raise


def _retry_wait(results: list[dict[str, Any]], attempt_number: int) -> float:
    limited = [result for result in results if result["primary_outcome"] == "rate_limited"]
    if not limited:
        return PAIRED_COOLDOWN_SECONDS
    provided = [float(result["retry_after"]) for result in limited
                if result.get("retry_after") is not None]
    if provided:
        wait = max(5.0, max(provided))
        if wait > 300:
            raise StopExecution("retry_after_exceeds_limit", "限流停止")
        return 0.01 if PAIRED_COOLDOWN_SECONDS < 5 else wait
    return (0.01 if PAIRED_COOLDOWN_SECONDS < 5 else (15.0 if attempt_number == 1 else 30.0))


async def _formal_logical_pair(
    task_id: int, clients: list[httpx.AsyncClient], snapshots: list[dict[str, Any]],
    item: dict[str, Any], *, phase: str, logical_id: str,
    aliases: list[set[str]], dispatch_state: dict[str, int], deadline: callable,
    retry_warmup_request: dict[str, Any], request_policy: dict[str, Any],
) -> tuple[list[httpx.AsyncClient], list[dict[str, Any]]]:
    canonical = _apply_request_policy({
        "id": item.get("instance_id") or item["id"],
        "messages": [{"role": "user", "content": item["prompt"]}],
        "max_tokens": int(item["max_tokens"]),
        "temperature": 0.0,
        "stream": True,
    }, request_policy)
    attempts: list[dict[str, Any]] = []
    root_pair_id: str | None = None
    previous_pair_id: str | None = None
    for attempt_number in range(1, 4):
        dispatch_state["formal"] = dispatch_state.get("formal", 0) + 1
        pair_id = uuid.uuid4().hex
        root_pair_id = root_pair_id or pair_id
        timeout = min(PAIRED_FORMAL_TIMEOUT_SECONDS, float(deadline()))
        results = await _dispatch_pair(
            task_id, clients, snapshots, canonical, stage=phase,
            unit_id=logical_id, pair_id=pair_id, attempt_number=attempt_number,
            dispatch_seq=dispatch_state["formal"], aliases=aliases,
            timeout_seconds=timeout, retry_root_pair_id=root_pair_id,
            retry_of_pair_id=previous_pair_id,
        )
        results = [_grade(result, item) for result in results]
        record = {
            "phase": phase, "logical_id": logical_id, "item_id": item["id"],
            "instance_id": item.get("instance_id") or item["id"],
            "round": item.get("round"), "attempt_number": attempt_number,
            "pair_id": pair_id, "retry_root_pair_id": root_pair_id,
            "retry_of_pair_id": previous_pair_id, "results": results,
        }
        previous_pair_id = pair_id
        attempts.append(record)
        paired_evidence.append(task_id, "derived_result", {
            **{key: value for key, value in record.items() if key != "results"},
            "classifier_version": "paired-outcome-v1.0.0",
            "normalizer_version": "leading-think-block-v1",
            "results": [{
                key: value for key, value in result.items()
                if key not in {"raw_text", "normalized_text"}
            } for result in results],
        })
        if any(result["primary_outcome"] == "identity_mismatch" for result in results):
            raise StopExecution("identity_mismatch", "正式阶段身份冲突")
        retryable = any(result["primary_outcome"] in _RETRYABLE for result in results)
        if not retryable or attempt_number == 3:
            return clients, attempts
        wait = _retry_wait(results, attempt_number)
        await asyncio.gather(*(client.aclose() for client in clients))
        await _cooldown(task_id, wait, "formal_retry_wait")
        clients = await _retry_warmup(
            task_id, snapshots, aliases, dispatch_state, deadline,
            retry_warmup_request,
        )
        await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "after_retry_warmup")
    return clients, attempts


def _final_attempts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_logical: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_logical.setdefault(record["logical_id"], []).append(record)
    output = []
    for attempts in by_logical.values():
        valid = next((record for record in attempts if all(
            result["primary_outcome"] in {"success", "content_failed"}
            for result in record["results"]
        )), None)
        output.append(valid or attempts[-1])
    return output


def _selected_speed_samples(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    speed_records = [record for record in records if record["phase"].startswith("speed")]
    by_logical: dict[str, list[dict[str, Any]]] = {}
    for record in speed_records:
        by_logical.setdefault(record["logical_id"], []).append(record)
    samples: list[dict[str, Any]] = []
    exclusions: dict[str, int] = {}
    recovered = 0
    for logical_id, attempts in by_logical.items():
        selected = next((record for record in attempts if all(
            result.get("grade", {}).get("passed") is True
            and result["primary_outcome"] == "success"
            and not result["truncated"] for result in record["results"]
        )), None)
        if selected:
            samples.append(selected)
            recovered += int(selected["attempt_number"] > 1)
        else:
            reason = "no_joint_correct_complete_pair"
            exclusions[reason] = exclusions.get(reason, 0) + 1
    return samples, exclusions, recovered


def _speed_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    samples, exclusions, recovered = _selected_speed_samples(records)
    enough = len(samples) >= 8
    metrics: dict[str, Any] = {
        "status": "available" if enough else "insufficient_data",
        "sample_count": len(samples), "required_samples": 8,
        "first_pair_samples": len(samples) - recovered,
        "retry_recovered_samples": recovered,
        "excluded": exclusions,
        "sample_pair_ids": [sample["pair_id"] for sample in samples],
    }
    paired_values: dict[str, list[tuple[float, float]]] = {}
    for field in ("ttft", "duration", "tps"):
        paired_values[field] = [
            (float(sample["results"][0][field]), float(sample["results"][1][field]))
            for sample in samples
            if sample["results"][0].get(field) is not None
            and sample["results"][1].get(field) is not None
        ]
    for side_index, side in enumerate(("candidate", "benchmark")):
        ttft = [pair[side_index] for pair in paired_values["ttft"]]
        total = [pair[side_index] for pair in paired_values["duration"]]
        tps = [pair[side_index] for pair in paired_values["tps"]]
        metrics[side] = {
            "ttft_median": _median(ttft) if enough else None,
            "total_median": _median(total) if enough else None,
            "total_p95": _percentile(total, 0.95) if enough else None,
            "tps_median": _median(tps) if enough else None,
            "ttft_samples": len(ttft), "total_samples": len(total),
            "tps_samples": len(tps),
        }
    metrics["differences"] = {
        key: {
            **_difference(metrics["candidate"][key], metrics["benchmark"][key]),
            "sample_count": len(paired_values[field]),
        }
        for key, field in (
            ("ttft_median", "ttft"), ("total_median", "duration"),
            ("total_p95", "duration"), ("tps_median", "tps"),
        )
    }
    return metrics


def _rate_limit_wait(record: dict[str, Any]) -> float:
    limited = [result for result in record["results"]
               if result["primary_outcome"] == "rate_limited"]
    if not limited or int(record["attempt_number"]) >= 3:
        return 0.0
    provided = [float(result["retry_after"]) for result in limited
                if result.get("retry_after") is not None]
    if provided:
        wait = max(5.0, max(provided))
        return wait if wait <= 300 else 0.0
    return 15.0 if int(record["attempt_number"]) == 1 else 30.0


def _latency_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    samples, _, _ = _selected_speed_samples(records)
    by_item: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_item.setdefault(sample["item_id"], []).append(sample)
    tail: dict[str, list[dict[str, Any]]] = {"candidate": [], "benchmark": []}
    common: list[dict[str, Any]] = []
    comparable = 0
    for item_id, item_samples in by_item.items():
        if len(item_samples) != 2:
            continue
        ordered = sorted(item_samples, key=lambda row: int(row.get("round") or 0))
        if any(result.get("duration") is None
               for row in ordered for result in row["results"]):
            continue
        comparable += 1
        durations = [
            [float(row["results"][side]["duration"]) for row in ordered]
            for side in (0, 1)
        ]
        slower = [0 if values[0] >= values[1] else 1 for values in durations]
        slow_ratios = [
            values[index] / values[1 - index] if values[1 - index] > 0 else math.inf
            for values, index in zip(durations, slower)
        ]
        if slower[0] == slower[1] and all(ratio >= 2.0 for ratio in slow_ratios):
            row = ordered[slower[0]]
            common.append({
                "item_id": item_id, "round": row.get("round"),
                "pair_id": row["pair_id"], "ratios": slow_ratios,
            })
            continue
        for side_index, side in enumerate(("candidate", "benchmark")):
            slow_index = slower[side_index]
            row = ordered[slow_index]
            own = durations[side_index][slow_index]
            peer = durations[1 - side_index][slow_index]
            if slow_ratios[side_index] >= 2.0 and peer > 0 and own / peer >= 1.5:
                tail[side].append({
                    "item_id": item_id, "round": row.get("round"),
                    "pair_id": row["pair_id"], "own_duration": own,
                    "paired_duration": peer,
                    "round_ratio": slow_ratios[side_index],
                    "paired_ratio": own / peer,
                })

    consecutive: dict[str, list[dict[str, Any]]] = {"candidate": [], "benchmark": []}
    for side in ("candidate", "benchmark"):
        for round_number in (1, 2):
            indexes = sorted(int(item["item_id"].rsplit("-", 1)[-1])
                             for item in tail[side] if item["round"] == round_number)
            run: list[int] = []
            for index in indexes:
                if run and index != run[-1] + 1:
                    if len(run) >= 2:
                        consecutive[side].append({"round": round_number, "length": len(run),
                                                  "item_indexes": run})
                    run = []
                run.append(index)
            if len(run) >= 2:
                consecutive[side].append({"round": round_number, "length": len(run),
                                          "item_indexes": run})
    return {
        "comparable_item_types": comparable,
        "tail_latency": tail,
        "tail_latency_counts": {side: len(values) for side, values in tail.items()},
        "consecutive_degradation": consecutive,
        "consecutive_degradation_counts": {
            side: len(values) for side, values in consecutive.items()
        },
        "common_latency_fluctuations": common,
        "common_latency_fluctuation_count": len(common),
    }


def _stability_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    speed_records = [record for record in records if record["phase"].startswith("speed")]
    base = [record for record in speed_records if record["attempt_number"] == 1]
    output: dict[str, Any] = {}
    for side_index, side in enumerate(("candidate", "benchmark")):
        first_results = [record["results"][side_index] for record in base]
        all_results = [record["results"][side_index] for record in speed_records]
        first_success = sum(result["primary_outcome"] in {"success", "content_failed"}
                            for result in first_results)
        system_errors = sum(result["primary_outcome"] in _SYSTEM_ERRORS for result in all_results)
        rate_limited = sum(result["primary_outcome"] == "rate_limited" for result in all_results)
        http_2xx = [result for result in all_results
                    if isinstance(result.get("status_code"), int)
                    and 200 <= result["status_code"] < 300]
        complete_streams = sum(
            result["normal_terminal"] and not result["truncated"]
            and int(result.get("token_count") or 0) > 0
            for result in http_2xx
        )
        chains: dict[str, list[dict[str, Any]]] = {}
        for record in speed_records:
            chains.setdefault(record["logical_id"], []).append(record["results"][side_index])
        retry_chains: list[tuple[list[dict[str, Any]], int]] = []
        limited_chains: list[tuple[list[dict[str, Any]], int]] = []
        for chain in chains.values():
            system_index = next((index for index, result in enumerate(chain)
                                 if result["primary_outcome"] in _SYSTEM_ERRORS), None)
            if system_index is not None and system_index < len(chain) - 1:
                retry_chains.append((chain, system_index))
            limited_index = next((index for index, result in enumerate(chain)
                                  if result["primary_outcome"] == "rate_limited"), None)
            if limited_index is not None:
                limited_chains.append((chain, limited_index))
        recovered = sum(any(
            result["primary_outcome"] in {"success", "content_failed"}
            for result in chain[index + 1:]
        ) for chain, index in retry_chains)
        limited_recovered = sum(any(
            result["primary_outcome"] in {"success", "content_failed"}
            for result in chain[index + 1:]
        ) for chain, index in limited_chains)
        counts: dict[str, int] = {}
        for result in all_results:
            outcome = result["primary_outcome"]
            counts[outcome] = counts.get(outcome, 0) + 1
        rate_limited_records = [record for record in speed_records
                                if record["results"][side_index]["primary_outcome"]
                                == "rate_limited"]
        rate_limited_items = {
            record["logical_id"] for record in rate_limited_records
        }
        error_rates = {
            outcome: count / len(all_results) if all_results else None
            for outcome, count in counts.items()
        }
        output[side] = {
            "first_request_success_rate": first_success / 10,
            "first_request_successes": first_success,
            "first_request_denominator": 10,
            "first_request_observed": len(first_results),
            "system_error_retry_recovery_rate": recovered / len(retry_chains)
            if retry_chains else None,
            "system_error_retry_chains": len(retry_chains),
            "rate_limit_recovery_rate": limited_recovered / len(limited_chains)
            if limited_chains else None,
            "rate_limited_attempts": rate_limited,
            "rate_limit_attempt_rate": rate_limited / len(all_results)
            if all_results else None,
            "rate_limited_item_count": len(rate_limited_items),
            "rate_limited_recovered_item_count": limited_recovered,
            "rate_limit_wait_seconds": sum(
                _rate_limit_wait(record) for record in rate_limited_records
            ),
            "stream_completeness_rate": complete_streams / len(http_2xx)
            if http_2xx else None,
            "stream_denominator": len(http_2xx),
            "system_error_rate": system_errors / len(all_results) if all_results else None,
            "system_error_count": system_errors,
            "formal_attempt_count": len(all_results),
            "outcome_counts": counts,
            "outcome_rates": error_rates,
            "content_error_count": counts.get("content_failed", 0),
        }
    latency = _latency_diagnostics(records)
    output["latency"] = latency
    shared_system_errors = [
        {
            "logical_id": record["logical_id"],
            "pair_id": record["pair_id"],
            "outcome": record["results"][0]["primary_outcome"],
        }
        for record in records
        if record["results"][0]["primary_outcome"] in _SYSTEM_ERRORS
        and record["results"][0]["primary_outcome"]
        == record["results"][1]["primary_outcome"]
    ]
    output["common_upstream_signal"] = {
        "present": len(shared_system_errors) >= 2
        or latency["common_latency_fluctuation_count"] >= 3,
        "shared_system_error_pairs": shared_system_errors,
        "shared_system_error_pair_count": len(shared_system_errors),
        "common_latency_fluctuation_count": latency[
            "common_latency_fluctuation_count"
        ],
        "alternative_explanations": ["测试设备异常", "共同网络路径波动"],
    }
    return output


def _majority(values: list[bool]) -> bool | None:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0] if values[0] == values[1] else None
    if len(values) >= 3:
        return sum(values) >= 2
    return None


def _ability_metrics(
    records: list[dict[str, Any]], ability_items: list[dict[str, Any]],
) -> dict[str, Any]:
    final_records = [record for record in _final_attempts(records)
                     if record["phase"].startswith("ability")]
    by_item: dict[str, list[dict[str, Any]]] = {}
    for record in final_records:
        by_item.setdefault(record["item_id"], []).append(record)
    manifest_items = {item["id"]: item for item in ability_items}
    item_results: dict[str, Any] = {}
    for item_id, item in manifest_items.items():
        rows = by_item.get(item_id, [])
        sides: dict[str, Any] = {}
        for side_index, side in enumerate(("candidate", "benchmark")):
            values = [record["results"][side_index].get("grade", {}).get("passed")
                      for record in rows]
            valid = [value for value in values if isinstance(value, bool)]
            sides[side] = {"valid_grades": len(valid), "values": valid,
                           "final": _majority(valid)}
        item_results[item_id] = {
            "category": item.get("dim"), "candidate": sides["candidate"],
            "benchmark": sides["benchmark"],
        }
    categories: dict[str, Any] = {}
    for category in ("代码", "结构化", "推理", "指令保持"):
        items = [value for value in item_results.values() if value["category"] == category]
        clear = bool(items) and all(
            item[side]["final"] is not None for item in items
            for side in ("candidate", "benchmark")
        )
        candidate_score = sum(item["candidate"]["final"] is True for item in items) / len(items) \
            if clear else None
        benchmark_score = sum(item["benchmark"]["final"] is True for item in items) / len(items) \
            if clear else None
        categories[category] = {
            "status": "available" if clear else "insufficient_data",
            "item_count": len(items),
            "candidate": candidate_score, "benchmark": benchmark_score,
            "difference": _difference(candidate_score, benchmark_score),
        }
    return {"items": item_results, "categories": categories}


def _highlights(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    by_logical: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_logical.setdefault(record["logical_id"], []).append(record)
    for attempts in by_logical.values():
        record = _final_attempts(attempts)[0]
        left, right = record["results"]
        differences = {
            "duration": _difference(left.get("duration"), right.get("duration")),
            "ttft": _difference(left.get("ttft"), right.get("ttft")),
            "tps": _difference(left.get("tps"), right.get("tps")),
        }
        grades = (left.get("grade", {}).get("passed"), right.get("grade", {}).get("passed"))
        exceptional = any(
            result["primary_outcome"] not in {"success", "content_failed"}
            or result.get("grade", {}).get("status") in {"invalid", "not_graded"}
            for attempt in attempts for result in attempt["results"]
        )
        percentages = [abs(float(value["percent"])) for value in differences.values()
                       if value["percent"] is not None]
        score = max(percentages) if percentages else None
        output.append({
            "logical_id": record["logical_id"], "item_id": record["item_id"],
            "pair_id": record["pair_id"], "candidate_outcome": left["primary_outcome"],
            "benchmark_outcome": right["primary_outcome"],
            "candidate_grade": grades[0], "benchmark_grade": grades[1],
            "differences": differences, "sort_value": score,
            "exceptional": exceptional,
            "attempt_count": len(attempts),
            "observed_outcomes": sorted({
                result["primary_outcome"]
                for attempt in attempts for result in attempt["results"]
            }),
        })
    exceptional = [item for item in output if item["exceptional"]]
    ranked = sorted(
        [item for item in output if not item["exceptional"]
         and item["sort_value"] is not None],
        key=lambda item: float(item["sort_value"]), reverse=True,
    )[:10]
    return exceptional + ranked


def _task_scope(task_id: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = [record["payload"] for record in paired_evidence.records(task_id)
                if record["record_type"] == "side_attempt"]
    auxiliary_stages = {"warmup", "identity", "retry_warmup"}
    auxiliary_pair_ids = {
        attempt.get("pair_id") for attempt in attempts
        if attempt.get("stage") in auxiliary_stages and attempt.get("pair_id")
    }
    retest_logical = {
        record["logical_id"] for record in records
        if record["phase"] == "ability_retest"
    }
    base_logical = {
        record["logical_id"] for record in records
        if record["phase"] in {"speed_round_1", "ability_base", "speed_round_2"}
    }
    candidate_requests = sum(attempt.get("side") == "candidate" for attempt in attempts)
    benchmark_requests = sum(attempt.get("side") == "benchmark" for attempt in attempts)
    return {
        "base_logical_pairs": len(base_logical),
        "ability_retest_logical_pairs": len(retest_logical),
        "formal_pair_attempts": len(records),
        "auxiliary_pair_attempts": len(auxiliary_pair_ids),
        "fidelity_endpoint_requests": sum(
            attempt.get("stage") == "fidelity" for attempt in attempts
        ),
        "candidate_endpoint_requests": candidate_requests,
        "benchmark_endpoint_requests": benchmark_requests,
        "total_endpoint_requests": candidate_requests + benchmark_requests,
    }


def _identity_summary(task_id: int) -> dict[str, Any]:
    attempts = [
        record["payload"] for record in paired_evidence.records(task_id)
        if record["record_type"] == "side_attempt"
        and record["payload"].get("stage") == "identity"
    ]
    summary: dict[str, Any] = {}
    for side in ("candidate", "benchmark"):
        side_attempts = [attempt for attempt in attempts if attempt.get("side") == side]
        selected = next((
            attempt for attempt in reversed(side_attempts)
            if attempt.get("primary_outcome") == "success"
        ), side_attempts[-1] if side_attempts else {})
        summary[side] = {
            "actual_model": selected.get("actual_model") or "",
            "system_fingerprint": selected.get("system_fingerprint") or "",
            "outcome": selected.get("primary_outcome") or "not_observed",
            "attempt_count": len(side_attempts),
        }
    return summary


def _dispatch_quality(task_id: int) -> dict[str, Any]:
    formal_stages = {
        "speed_round_1", "ability_base", "speed_round_2", "ability_retest",
    }


def _value_change(current: Any, previous: Any) -> dict[str, Any]:
    if current is None or previous is None:
        return {"current": current, "previous": previous, "absolute": None, "percent": None}
    absolute = float(current) - float(previous)
    percent = absolute / float(previous) * 100 if float(previous) != 0 else None
    return {
        "current": current, "previous": previous,
        "absolute": absolute, "percent": percent,
    }


def _previous_report_changes(
    paired: dict[str, Any], speed: dict[str, Any], stability: dict[str, Any],
    ability: dict[str, Any], highlights: list[dict[str, Any]],
) -> dict[str, Any] | None:
    parent_task_id = paired.get("parent_task_id")
    if not parent_task_id:
        return None
    parent_task = store.get("tasks", parent_task_id)
    previous = store.loads((parent_task or {}).get("report"), {})
    if not previous:
        return {"parent_task_id": parent_task_id, "status": "previous_report_unavailable"}
    speed_changes: dict[str, Any] = {}
    for side in ("candidate", "benchmark"):
        speed_changes[side] = {
            key: _value_change(
                (speed.get(side) or {}).get(key),
                ((previous.get("speed") or {}).get(side) or {}).get(key),
            )
            for key in ("ttft_median", "total_median", "total_p95", "tps_median")
        }
    stability_changes: dict[str, Any] = {}
    for side in ("candidate", "benchmark"):
        stability_changes[side] = {
            key: _value_change(
                (stability.get(side) or {}).get(key),
                ((previous.get("stability") or {}).get(side) or {}).get(key),
            )
            for key in (
                "first_request_success_rate", "system_error_rate",
                "rate_limit_recovery_rate", "stream_completeness_rate",
            )
        }
    previous_categories = (previous.get("ability") or {}).get("categories") or {}
    ability_changes = {
        category: {
            side: _value_change(
                values.get(side), (previous_categories.get(category) or {}).get(side)
            )
            for side in ("candidate", "benchmark")
        }
        for category, values in (ability.get("categories") or {}).items()
    }
    previous_exception_count = sum(
        bool(item.get("exceptional")) for item in previous.get("highlights") or []
    )
    exception_count = sum(bool(item.get("exceptional")) for item in highlights)
    return {
        "parent_task_id": parent_task_id,
        "status": "available",
        "speed": speed_changes,
        "stability": stability_changes,
        "ability": ability_changes,
        "exceptional_question_count": _value_change(
            exception_count, previous_exception_count
        ),
    }
    values = [
        float(record["payload"]["startup_skew"])
        for record in paired_evidence.records(task_id)
        if record["record_type"] == "dispatch_result"
        and record["payload"].get("stage") in formal_stages
        and record["payload"].get("startup_skew") is not None
    ]
    return {
        "sample_count": len(values),
        "startup_skew_median": _median(values),
        "startup_skew_p95": _percentile(values, 0.95),
    }


def _build_report(
    task_id: int, records: list[dict[str, Any]], *, complete: bool,
    status: str, stop_reason: str = "", task_root: str,
) -> dict[str, Any]:
    task = store.get("tasks", task_id)
    paired = store.get("paired_tasks", task_id, key="task_id")
    assert task and paired
    assets = paired_admission.secure_snapshots(paired)["assets"]
    speed = _speed_metrics(records)
    ability = _ability_metrics(records, assets["ability"])
    stability = _stability_metrics(records)
    highlights = _highlights(records)
    if not complete:
        speed["status"] = "incomplete_task"
        for side in ("candidate", "benchmark"):
            for key in (
                "first_request_success_rate", "system_error_retry_recovery_rate",
                "rate_limit_recovery_rate", "rate_limit_attempt_rate",
                "stream_completeness_rate", "system_error_rate",
            ):
                stability[side][key] = None
        stability["status"] = "incomplete_task"
    else:
        stability["status"] = "available"
    insufficient = speed["status"] != "available" or any(
        category["status"] != "available" for category in ability["categories"].values()
    )
    return {
        "task_id": task_id, "kind": paired_admission.PAIRED_KIND,
        "status": status, "complete": complete, "stop_reason": stop_reason,
        "snapshot": store.loads(task["snapshot"], {}),
        "identity": _identity_summary(task_id),
        "dispatch_quality": _dispatch_quality(task_id),
        "integrity": {
            "verified": True, "task_manifest_root": task_root,
            "input_manifest_root": task_root,
            "fidelity_manifest_root": paired.get("fidelity_manifest_root"),
        },
        "task_scope": {
            **_task_scope(task_id, records),
            "requests_used": paired["requests_used"],
            "tokens_used": paired["tokens_used"],
            "money_used": paired["money_used"],
        },
        "speed": speed,
        "stability": stability,
        "ability": ability,
        "highlights": highlights,
        "previous_report_changes": _previous_report_changes(
            paired, speed, stability, ability, highlights,
        ),
        "insufficient_metrics": insufficient,
        "conclusion": {
            "code": "manual_pending",
            "verdict": "等待人工准入结论",
            "allowed": ["admit", "do_not_admit", "continue_testing"]
            if complete else ["do_not_admit", "continue_testing"],
        },
        "created_at": time.time(),
    }


async def _stop(
    task_id: int, records: list[dict[str, Any]], reason: str, title: str,
) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if paired and paired["state"] not in paired_admission.TERMINAL_STATES:
        try:
            paired_admission.transition(task_id, "stopped", reason=title)
        except paired_evidence.EvidenceError:
            paired_admission.mark_evidence_storage_failed(task_id)
            return
    store.update(
        "paired_tasks", task_id, {"stop_reason": reason}, key="task_id"
    )
    store.update("tasks", task_id, {"finished_at": time.time()})
    try:
        paired_evidence.append(task_id, "task_stopped", {"reason": reason, "title": title})
        task_root = paired_evidence.seal_manifest(task_id, "task")
        store.update(
            "paired_tasks", task_id, {
                "task_manifest_root": task_root,
                "integrity_status": "verified",
                "integrity_error": "",
            }, key="task_id"
        )
        report = _build_report(
            task_id, records, complete=False, status="stopped",
            stop_reason=reason, task_root=task_root,
        )
        paired_admission.save_report_revision(task_id, report, title, None)
    except paired_evidence.EvidenceError:
        store.update(
            "paired_tasks", task_id,
            {"stop_reason": "evidence_storage_failed"}, key="task_id",
        )
    store.add_event(task_id, title, stage="停止", level="error")


async def _cancel(task_id: int, records: list[dict[str, Any]]) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if paired and paired["state"] not in paired_admission.TERMINAL_STATES:
        try:
            paired_admission.transition(task_id, "canceled", reason="用户取消")
        except paired_evidence.EvidenceError:
            paired_admission.mark_evidence_storage_failed(task_id)
            return
    store.update("tasks", task_id, {"finished_at": time.time()})
    try:
        paired_evidence.append(task_id, "task_canceled", {"reason": "user_canceled"})
        task_root = paired_evidence.seal_manifest(task_id, "task")
    except paired_evidence.EvidenceError:
        paired_admission.mark_evidence_storage_failed(task_id)
        return
    store.update(
        "paired_tasks", task_id, {
            "task_manifest_root": task_root,
            "integrity_status": "verified",
            "integrity_error": "",
        }, key="task_id"
    )
    report = _build_report(
        task_id, records, complete=False, status="canceled",
        stop_reason="user_canceled", task_root=task_root,
    )
    paired_admission.save_report_revision(task_id, report, "用户取消报告", None)


async def _run_fidelity(task_id: int, paired: dict[str, Any]) -> None:
    snapshots = paired_admission.secure_snapshots(paired)
    candidate = snapshots["candidate"]
    fingerprints = await _wait_acquire_resources(task_id, [candidate])
    renewer = asyncio.create_task(_renew_resources(task_id, fingerprints))
    active_start = time.monotonic()
    active_before = float(paired.get("active_seconds") or 0)
    try:
        paired_admission.transition(task_id, "fidelity", reason="执行候选端自动保真")
        _environment_snapshot(task_id, fingerprints)
        manifest = snapshots["assets"]
        aliases = paired_admission.model_aliases(candidate["model"])
        fidelity_items = list(manifest["fidelity"])
        for item_index, item in enumerate(fidelity_items):
            store.update("tasks", task_id, {
                "progress": store.dumps({
                    "done": 0, "total": 16,
                    "current": f"自动保真题 {item_index + 1}/{len(fidelity_items)}",
                }),
            })
            item_completed = False
            for attempt in range(1, paired_assets.FIDELITY_MAX_ATTEMPTS + 1):
                _deadline_remaining(active_start, active_before)
                client = _client()
                try:
                    result = await _dispatch_single(
                        task_id, client, candidate, item,
                        stage="fidelity", unit_id=item["instance_id"],
                        attempt_number=attempt, aliases=aliases,
                        timeout_seconds=min(
                            PAIRED_FIDELITY_TIMEOUT_SECONDS,
                            _deadline_remaining(active_start, active_before),
                        ),
                    )
                finally:
                    await client.aclose()
                if result["primary_outcome"] == "success":
                    review = _fidelity_review(result, item)
                    paired_evidence.append(task_id, "derived_result", review)
                    status = "符合机械预期" if review.get("matched") is True \
                        else "等待人工复核"
                    store.add_event(
                        task_id, f"{item['id']} 完成：{status}", stage="保真",
                    )
                    item_completed = True
                    break
                if result["primary_outcome"] == "identity_mismatch":
                    raise StopExecution(
                        "fidelity_identity_mismatch", "保真实际模型不匹配"
                    )
                if result["primary_outcome"] not in _RETRYABLE:
                    raise StopExecution("fidelity_hard_failure", "保真确定性失败")
                if attempt < paired_assets.FIDELITY_MAX_ATTEMPTS:
                    await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "fidelity_retry")
            if not item_completed:
                raise StopExecution("fidelity_retry_exhausted", "保真连续三次未恢复")
            if item_index < len(fidelity_items) - 1:
                await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "fidelity_item_interval")
        root = paired_evidence.seal_manifest(task_id, "fidelity")
        now = time.time()
        store.update("paired_tasks", task_id, {
            "fidelity_manifest_root": root,
            "fidelity_last_completed_at": now,
            "awaiting_truth_at": now,
            "active_seconds": active_before + time.monotonic() - active_start,
        }, key="task_id")
        paired_admission.transition(
            task_id, "awaiting_fidelity_truth",
            reason=f"{len(fidelity_items)} 道自动保真证据已封存",
        )
        store.update("tasks", task_id, {
            "progress": store.dumps({
                "done": 0, "total": 16,
                "current": f"等待用户判断 {len(fidelity_items)} 道保真证据",
            }),
        })
        store.add_event(
            task_id,
            f"{len(fidelity_items)} 道自动保真完成，连接已关闭，等待人工判断",
            stage="保真",
        )
        return
    finally:
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        _release_resources(task_id)


async def _run_dual(task_id: int, paired: dict[str, Any]) -> None:
    snapshots_map = paired_admission.secure_snapshots(paired)
    candidate, benchmark = snapshots_map["candidate"], snapshots_map["benchmark"]
    if not benchmark:
        raise StopExecution("benchmark_missing", "未选择标杆端")
    manifest = snapshots_map["assets"]
    request_policy = snapshots_map["request_policy"]
    warmup_request = _apply_request_policy(manifest["warmup"], request_policy)
    identity_request = _apply_request_policy(manifest["identity"], request_policy)
    snapshots = [candidate, benchmark]
    aliases = [
        paired_admission.model_aliases(candidate["model"]),
        paired_admission.model_aliases(benchmark["model"]),
    ]
    fingerprints = await _wait_acquire_resources(task_id, snapshots)
    renewer = asyncio.create_task(_renew_resources(task_id, fingerprints))
    telemetry = asyncio.create_task(_telemetry(task_id))
    active_start = time.monotonic()
    active_before = float(paired.get("active_seconds") or 0)
    dispatch_state: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    clients: list[httpx.AsyncClient] = []

    def deadline() -> float:
        return _deadline_remaining(active_start, active_before)

    try:
        _environment_snapshot(task_id, fingerprints)
        last_fidelity = paired.get("fidelity_last_completed_at")
        if last_fidelity:
            remaining = max(0.0, 5.0 - (time.time() - float(last_fidelity)))
            if PAIRED_COOLDOWN_SECONDS < 5 and remaining:
                remaining = PAIRED_COOLDOWN_SECONDS
            await _cooldown(task_id, remaining, "post_fidelity_connection_isolation")
        paired_admission.transition(task_id, "warmup", reason="创建双端独立客户端")
        clients = [_client(), _client()]
        clients, _ = await _aux_pair(
            task_id, clients, snapshots, warmup_request,
            "warmup", aliases, dispatch_state, deadline,
        )
        paired_admission.transition(task_id, "identity", reason="双端预热完成")
        clients, _ = await _aux_pair(
            task_id, clients, snapshots, identity_request,
            "identity", aliases, dispatch_state, deadline,
        )
        await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "after_identity")

        speed_round_1 = [item for item in manifest["speed"] if item["round"] == 1]
        speed_round_2 = [item for item in manifest["speed"] if item["round"] == 2]
        phases = [
            ("speed_round_1", speed_round_1),
            ("ability_base", manifest["ability"]),
            ("speed_round_2", speed_round_2),
        ]
        completed_logical = 0
        formal_started = False
        base_ability: list[dict[str, Any]] = []
        for phase, items in phases:
            paired_admission.transition(task_id, phase, reason=f"进入 {phase}")
            for item in items:
                if formal_started:
                    await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "between_formal_pairs")
                formal_started = True
                logical_id = item.get("instance_id") or item["id"]
                clients, attempts = await _formal_logical_pair(
                    task_id, clients, snapshots, item, phase=phase,
                    logical_id=logical_id, aliases=aliases,
                    dispatch_state=dispatch_state, deadline=deadline,
                    retry_warmup_request=warmup_request,
                    request_policy=request_policy,
                )
                records.extend(attempts)
                if phase == "ability_base":
                    base_ability.append(_final_attempts(attempts)[0])
                completed_logical += 1
                store.update("tasks", task_id, {"progress": store.dumps({
                    "done": completed_logical, "total": 16,
                    "current": logical_id,
                    "formal_attempts": len(records),
                })})

        retest_items: list[dict[str, Any]] = []
        by_id = {item["id"]: item for item in manifest["ability"]}
        for record in base_ability:
            grades = [result.get("grade", {}).get("passed") for result in record["results"]]
            if all(isinstance(value, bool) for value in grades) and grades[0] != grades[1]:
                retest_items.extend([by_id[record["item_id"]], by_id[record["item_id"]]])
        paired_admission.transition(task_id, "ability_retest", reason="执行能力差异同题复测")
        total_logical = 16 + len(retest_items)
        for index, item in enumerate(retest_items, 1):
            await _cooldown(task_id, PAIRED_COOLDOWN_SECONDS, "between_formal_pairs")
            logical_id = f"{item['id']}-RETEST-{index}"
            clients, attempts = await _formal_logical_pair(
                task_id, clients, snapshots, item, phase="ability_retest",
                logical_id=logical_id, aliases=aliases,
                dispatch_state=dispatch_state, deadline=deadline,
                retry_warmup_request=warmup_request,
                request_policy=request_policy,
            )
            records.extend(attempts)
            completed_logical += 1
            store.update("tasks", task_id, {"progress": store.dumps({
                "done": completed_logical, "total": total_logical,
                "current": logical_id, "formal_attempts": len(records),
            })})

        paired_admission.transition(task_id, "finalizing", reason="正式配对执行完成")
        store.update("paired_tasks", task_id, {
            "active_seconds": active_before + time.monotonic() - active_start,
        }, key="task_id")
        preliminary = _build_report(
            task_id, records, complete=True, status="finalizing",
            task_root="pending",
        )
        terminal = "completed_with_insufficient_metrics" \
            if preliminary["insufficient_metrics"] else "completed"
        paired_evidence.append(task_id, "report_inputs_finalized", {
            "formal_pair_attempts": len(records),
            "terminal_state": terminal,
            "algorithm_versions": {
                "tokenizer": paired_admission.TOKENIZER_VERSION,
                "grader": paired_admission.GRADER_VERSION,
                "report": "paired-report-v1.0.0",
            },
        })
        paired_admission.transition(task_id, terminal, reason="报告计算与完整性检查完成")
        task_root = paired_evidence.seal_manifest(task_id, "task")
        store.update(
            "paired_tasks", task_id, {
                "task_manifest_root": task_root,
                "integrity_status": "verified",
                "integrity_error": "",
            }, key="task_id"
        )
        final_report = _build_report(
            task_id, records, complete=True, status=terminal,
            task_root=task_root,
        )
        paired_admission.save_report_revision(task_id, final_report, "初始双端报告", None)
        store.update("tasks", task_id, {"finished_at": time.time()})
        store.add_event(task_id, "双端报告已封存，等待人工准入结论", stage="完成")
    except TaskCanceled:
        await _cancel(task_id, records)
    except StopExecution as exc:
        await _stop(task_id, records, exc.reason, exc.title)
    finally:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
        renewer.cancel()
        telemetry.cancel()
        await asyncio.gather(renewer, telemetry, return_exceptions=True)
        _release_resources(task_id)


async def execute(task_id: int) -> None:
    async with _execution_lock:
        paired = store.get("paired_tasks", task_id, key="task_id")
        if not paired:
            raise RuntimeError("paired_task_missing")
        try:
            if paired.get("benchmark_target_id"):
                await _run_dual(task_id, paired)
            else:
                await _run_fidelity(task_id, paired)
        except TaskCanceled:
            await _cancel(task_id, [])
        except StopExecution as exc:
            await _stop(task_id, [], exc.reason, exc.title)
        except paired_evidence.EvidenceError:
            cancel_runtime(task_id)
            paired_admission.mark_evidence_storage_failed(task_id)


async def cancel(task_id: int) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired or paired["state"] in paired_admission.TERMINAL_STATES:
        return
    await _cancel(task_id, [])


async def fail_execution(task_id: int, reason: str, title: str) -> None:
    paired = store.get("paired_tasks", task_id, key="task_id")
    if not paired or paired["state"] in paired_admission.TERMINAL_STATES:
        return
    records = [
        record["payload"] for record in paired_evidence.records(task_id)
        if record["record_type"] == "derived_result"
    ]
    await _stop(task_id, records, reason, title)
