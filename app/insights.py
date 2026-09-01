"""生产只读指标采集、聚合、排行与供给缺口建议。"""
from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from . import egress, metric_store, store
from .security import decrypt, scrub

PROTOCOL_VERSION = "1"
COLLECTOR_TICK_SECONDS = 15
MINUTE_RETENTION_SECONDS = 30 * 86400
HOUR_RETENTION_SECONDS = 180 * 86400
DAY_RETENTION_SECONDS = 730 * 86400
RANKING_WINDOW_SECONDS = 7 * 86400
RANKING_MIN_ATTEMPTS = 200
RANKING_MIN_SUCCESSES = 100
RANKING_MIN_ACTIVE_DAYS = 3
STALE_MIN_SECONDS = 180

DIMENSION_FIELDS = (
    "platform_group", "model_family", "model", "usage_profile", "channel",
    "supply_source", "output_length_band",
)
COUNT_FIELDS = (
    "request_count", "attempt_count", "success_count", "failure_count",
    "retry_count", "failover_count", "auth_error_count", "rate_limit_count",
    "timeout_count", "stream_break_count", "upstream_5xx_count",
    "network_error_count", "protocol_error_count", "input_tokens", "output_tokens",
    "active_users",
)
FLOAT_FIELDS = (
    "ttft_p50_ms", "ttft_p95_ms", "latency_p50_ms", "latency_p95_ms",
    "generation_tps", "output_length_p50", "output_length_p95",
    "top_user_request_share",
)
SENSITIVE_FIELDS = {
    "prompt", "messages", "content", "response", "completion", "api_key",
    "key", "authorization", "user_id", "raw_user_id", "request_body",
    "response_body",
}

_collector_task: asyncio.Task | None = None
_collecting: set[int] = set()
_last_maintenance_day = ""


def source_out(row: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    stale_after = max(STALE_MIN_SECONDS, int(row["poll_interval_seconds"]) * 3)
    last_success = row.get("last_success_at")
    return {
        "id": row["id"], "name": row["name"], "endpoint": row["endpoint"],
        "protocol_version": row["protocol_version"], "enabled": bool(row["enabled"]),
        "poll_interval_seconds": row["poll_interval_seconds"],
        "has_token": bool(row["token_enc"]), "cursor": row["cursor"],
        "last_attempt_at": row["last_attempt_at"], "last_success_at": last_success,
        "last_error": row["last_error"],
        "data_delay_seconds": round(now - last_success, 1) if last_success else None,
        "stale": bool(row["enabled"] and (not last_success or now - last_success > stale_after)),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def validate_source_endpoint(endpoint: str) -> str:
    egress.validate_url(endpoint)
    query_keys = {key.casefold() for key, _ in parse_qsl(urlsplit(endpoint).query)}
    if query_keys & {"token", "key", "api_key", "authorization", "access_token"}:
        raise ValueError("指标接口凭据不能放在 URL 查询参数中，请使用独立 Token 字段")
    return endpoint.rstrip("/")


async def start() -> None:
    global _collector_task
    if _collector_task and not _collector_task.done():
        return
    _collector_task = asyncio.create_task(_collector_loop(), name="metric-collector")


async def stop() -> None:
    global _collector_task
    if not _collector_task:
        return
    _collector_task.cancel()
    await asyncio.gather(_collector_task, return_exceptions=True)
    _collector_task = None


def is_running() -> bool:
    return bool(_collector_task and not _collector_task.done())


async def _collector_loop() -> None:
    global _last_maintenance_day
    while True:
        try:
            now = time.time()
            for source in metric_store.query("SELECT * FROM metric_sources WHERE enabled=1 ORDER BY id"):
                last_attempt = source.get("last_attempt_at") or 0
                if source["id"] not in _collecting \
                        and now - last_attempt >= source["poll_interval_seconds"]:
                    asyncio.create_task(_collect_due_source(source["id"]))
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _last_maintenance_day:
                await asyncio.to_thread(run_retention)
                await asyncio.to_thread(refresh_supply_gaps)
                await asyncio.to_thread(run_due_recommendation_reviews)
                _last_maintenance_day = today
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(COLLECTOR_TICK_SECONDS)


async def _collect_due_source(source_id: int) -> None:
    try:
        await collect_source(source_id)
    except Exception:
        return


async def collect_source(source_id: int) -> dict[str, Any]:
    if source_id in _collecting:
        return {"status": "already_running", "source_id": source_id}
    source = metric_store.get("metric_sources", source_id)
    if not source:
        raise ValueError("指标数据源不存在")
    _collecting.add(source_id)
    started = time.time()
    cursor = source["cursor"] or ""
    run_id = metric_store.insert("metric_collection_runs", {
        "source_id": source_id, "cursor_before": cursor, "cursor_after": cursor,
        "status": "running", "started_at": started,
    })
    metric_store.update("metric_sources", source_id, {"last_attempt_at": started})
    received = upserted = pages = 0
    try:
        token = decrypt(source["token_enc"]) if source["token_enc"] else ""
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            max_redirects=egress.EGRESS_MAX_REDIRECTS,
            event_hooks=egress.event_hooks(),
        ) as client:
            while True:
                response = await client.get(source["endpoint"], headers=headers, params={
                    "version": source["protocol_version"], "cursor": cursor, "limit": 1000,
                })
                response.raise_for_status()
                egress.ensure_response_size(response)
                payload = response.json()
                result = ingest_payload(source, payload)
                received += result["received_count"]
                upserted += result["upserted_count"]
                next_cursor = result["next_cursor"]
                previous_cursor = cursor
                pages += 1
                if next_cursor != cursor:
                    cursor = next_cursor
                    metric_store.update("metric_sources", source_id, {"cursor": cursor})
                if not result["has_more"]:
                    break
                if pages >= 20 or next_cursor == previous_cursor:
                    raise ValueError("指标接口分页未前进或单轮超过 20 页")
        finished = time.time()
        metric_store.update("metric_sources", source_id, {
            "cursor": cursor, "last_success_at": finished, "last_error": "",
            "updated_at": finished,
        })
        metric_store.update("metric_collection_runs", run_id, {
            "cursor_after": cursor, "received_count": received,
            "upserted_count": upserted, "status": "success", "finished_at": finished,
        })
        return {"status": "success", "source_id": source_id, "pages": pages,
                "received_count": received, "upserted_count": upserted,
                "next_cursor": cursor}
    except Exception as exc:
        finished = time.time()
        try:
            exposed_token = decrypt(source["token_enc"]) if source["token_enc"] else ""
        except Exception:
            exposed_token = ""
        error = scrub(str(exc), exposed_token)[:500]
        metric_store.update("metric_sources", source_id, {
            "last_error": error, "updated_at": finished,
        })
        metric_store.update("metric_collection_runs", run_id, {
            "cursor_after": cursor, "received_count": received,
            "upserted_count": upserted, "status": "failed", "error": error,
            "finished_at": finished,
        })
        raise
    finally:
        _collecting.discard(source_id)


def _contains_sensitive_fields(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold().replace("-", "_") in SENSITIVE_FIELDS:
                return True
            if _contains_sensitive_fields(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_fields(item) for item in value)
    return False


def _nonnegative_int(bucket: dict[str, Any], field: str) -> int:
    value = bucket.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    if int(value) != value:
        raise ValueError(f"{field} 必须是整数")
    return int(value)


def _optional_nonnegative_float(bucket: dict[str, Any], field: str) -> float | None:
    value = bucket.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return float(value)


def _normalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    required = ("bucket_start", "platform_group", "model", "channel")
    missing = [field for field in required if bucket.get(field) in (None, "")]
    if missing:
        raise ValueError(f"分钟指标缺少字段：{'、'.join(missing)}")
    raw_start = bucket["bucket_start"]
    if isinstance(raw_start, bool) or not isinstance(raw_start, (int, float)):
        raise ValueError("bucket_start 必须是 Unix 秒时间戳")
    bucket_start = int(raw_start) // 60 * 60
    if bucket_start < 946684800 or bucket_start > time.time() + 3600:
        raise ValueError("bucket_start 超出合理范围")
    row = {
        "bucket_start": bucket_start,
        "platform_group": str(bucket["platform_group"]).strip(),
        "model_family": str(bucket.get("model_family") or "").strip(),
        "model": str(bucket["model"]).strip(),
        "usage_profile": str(bucket.get("usage_profile") or "general").strip().casefold(),
        "channel": str(bucket["channel"]).strip(),
        "supply_source": str(bucket.get("supply_source") or "").strip(),
        "output_length_band": str(bucket.get("output_length_band") or "unknown").strip().casefold(),
    }
    if row["usage_profile"] not in {"general", "agent", "coding", "customer_service"}:
        row["usage_profile"] = "general"
    for field in COUNT_FIELDS:
        row[field] = _nonnegative_int(bucket, field)
    for field in FLOAT_FIELDS:
        row[field] = _optional_nonnegative_float(bucket, field)
    share = row["top_user_request_share"] or 0.0
    if share > 1:
        raise ValueError("top_user_request_share 必须在 0 到 1 之间")
    row["top_user_request_share"] = share
    if row["success_count"] + row["failure_count"] > row["attempt_count"]:
        raise ValueError("成功数与失败数之和不能超过尝试数")
    return row


def ingest_payload(source: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or _contains_sensitive_fields(payload):
        raise ValueError("指标协议包含 Prompt、Key、原始用户或完整响应等禁止字段")
    version = str(payload.get("version") or "")
    if version != source["protocol_version"] or version != PROTOCOL_VERSION:
        raise ValueError(
            f"指标协议版本不兼容：收到 {version or '空'}，需要 {source['protocol_version']}"
        )
    buckets = payload.get("buckets")
    if not isinstance(buckets, list) or len(buckets) > 1000:
        raise ValueError("buckets 必须是最多 1000 条的数组")
    now = time.time()
    normalized = [_normalize_bucket(bucket) for bucket in buckets if isinstance(bucket, dict)]
    if len(normalized) != len(buckets):
        raise ValueError("buckets 中存在非对象记录")
    columns = ("source_id", *DIMENSION_FIELDS, *COUNT_FIELDS, *FLOAT_FIELDS,
               "bucket_start", "received_at", "updated_at")
    conflict = ("source_id", "bucket_start", "platform_group", "model",
                "usage_profile", "channel", "output_length_band")
    updated = [field for field in columns if field not in conflict and field != "received_at"]
    sql = (
        f"INSERT INTO metric_buckets ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT ({','.join(conflict)}) DO UPDATE SET "
        + ",".join(f"{field}=excluded.{field}" for field in updated)
    )
    periods: set[tuple[int, int]] = set()
    for bucket in normalized:
        values = {"source_id": source["id"], **bucket, "received_at": now, "updated_at": now}
        metric_store.execute(sql, tuple(values[column] for column in columns))
        periods.add((bucket["bucket_start"] // 3600 * 3600,
                     bucket["bucket_start"] // 86400 * 86400))
    for hour_start, day_start in periods:
        _refresh_rollup("hourly_metrics", hour_start, 3600)
        _refresh_rollup("daily_metrics", day_start, 86400)
    return {
        "cursor_before": source["cursor"] or "",
        "next_cursor": str(payload.get("next_cursor") or source["cursor"] or ""),
        "has_more": bool(payload.get("has_more")),
        "received_count": len(buckets), "upserted_count": len(normalized),
    }


def _weighted(rows: list[dict[str, Any]], field: str, weight_field: str = "success_count") -> float | None:
    pairs = [(row[field], max(1, row[weight_field])) for row in rows if row.get(field) is not None]
    if not pairs:
        return None
    return round(sum(value * weight for value, weight in pairs) / sum(weight for _, weight in pairs), 3)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {field: sum(int(row.get(field) or 0) for row in rows) for field in COUNT_FIELDS}
    result["active_users"] = max((int(row.get("active_users") or 0) for row in rows), default=0)
    for field in FLOAT_FIELDS:
        result[field] = _weighted(rows, field, "request_count" if field == "top_user_request_share" else "success_count")
    result["approximate_quantiles"] = True
    return result


def _refresh_rollup(table: str, period_start: int, seconds: int) -> None:
    rows = metric_store.query(
        "SELECT * FROM metric_buckets WHERE bucket_start>=? AND bucket_start<?",
        (period_start, period_start + seconds),
    )
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(str(row[field]) for field in (
            "platform_group", "model_family", "model", "usage_profile", "channel",
            "output_length_band",
        ))
        grouped[key].append(row)
    metric_store.execute(f"DELETE FROM {table} WHERE period_start=?", (period_start,))
    for key, group_rows in grouped.items():
        metric_store.insert(table, {
            "period_start": period_start, "platform_group": key[0],
            "model_family": key[1], "model": key[2], "usage_profile": key[3],
            "channel": key[4], "output_length_band": key[5],
            "metrics_json": store.dumps(_aggregate(group_rows)), "updated_at": time.time(),
        })


def _filtered_rows(start: int, filters: dict[str, str]) -> list[dict[str, Any]]:
    clauses = ["bucket_start>=?"]
    params: list[Any] = [start]
    for field in ("platform_group", "model_family", "model", "usage_profile", "channel"):
        value = filters.get(field, "").strip()
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    return metric_store.query(
        "SELECT * FROM metric_buckets WHERE " + " AND ".join(clauses)
        + " ORDER BY bucket_start", tuple(params),
    )


def health_overview(filters: dict[str, str], minutes: int = 5) -> dict[str, Any]:
    now = time.time()
    rows = _filtered_rows(int(now - max(1, min(minutes, 60)) * 60), filters)
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["platform_group"], row["model_family"], row["model"],
                 row["usage_profile"], row["channel"])].append(row)
    health_rows = []
    for key, values in grouped.items():
        metrics = _aggregate(values)
        attempts = metrics["attempt_count"]
        success_rate = metrics["success_count"] / attempts if attempts else None
        timeout_rate = metrics["timeout_count"] / attempts if attempts else None
        if success_rate is None:
            status = "no_data"
        elif success_rate < .95 or (timeout_rate or 0) > .05:
            status = "critical"
        elif success_rate < .99 or (timeout_rate or 0) > .01:
            status = "warning"
        else:
            status = "healthy"
        health_rows.append({
            "platform_group": key[0], "model_family": key[1], "model": key[2],
            "usage_profile": key[3], "channel": key[4], "status": status,
            "request_count": metrics["request_count"], "attempt_count": attempts,
            "success_count": metrics["success_count"],
            "success_rate": round(success_rate, 4) if success_rate is not None else None,
            "timeout_count": metrics["timeout_count"],
            "stream_break_count": metrics["stream_break_count"],
            "rate_limit_count": metrics["rate_limit_count"],
            "upstream_5xx_count": metrics["upstream_5xx_count"],
            "latency_p95_ms": metrics["latency_p95_ms"],
            "ttft_p95_ms": metrics["ttft_p95_ms"],
            "latest_bucket": max(row["bucket_start"] for row in values),
        })
    sources = [source_out(row) for row in metric_store.query("SELECT * FROM metric_sources ORDER BY id")]
    latest = max((row["bucket_start"] for row in rows), default=None)
    source_quality = [collection_quality(source["id"]) for source in sources]
    return {
        "generated_at": now, "latest_bucket": latest,
        "data_delay_seconds": round(now - latest, 1) if latest else None,
        "stale": (any(source["stale"] for source in sources if source["enabled"])
                  or latest is None or now - latest > STALE_MIN_SECONDS),
        "sources": sources, "collection_quality": source_quality, "rows": sorted(
            health_rows, key=lambda row: (row["platform_group"], row["model"], row["channel"])),
        "filters": filters,
    }


def collection_quality(source_id: int, hours: int = 24) -> dict[str, Any]:
    cutoff = int(time.time() - max(1, min(hours, 24 * 30)) * 3600)
    rows = metric_store.query(
        "SELECT DISTINCT bucket_start FROM metric_buckets "
        "WHERE source_id=? AND bucket_start>=? ORDER BY bucket_start",
        (source_id, cutoff),
    )
    if not rows:
        return {"source_id": source_id, "expected_buckets": 0, "received_buckets": 0,
                "missing_buckets": [], "completeness": None}
    starts = [int(row["bucket_start"]) for row in rows]
    expected = (starts[-1] - starts[0]) // 60 + 1
    received = set(starts)
    missing = [bucket for bucket in range(starts[0], starts[-1] + 60, 60)
               if bucket not in received]
    return {
        "source_id": source_id, "expected_buckets": expected,
        "received_buckets": len(received), "missing_buckets": missing[:100],
        "missing_count": len(missing),
        "completeness": round(len(received) / expected, 6) if expected else None,
    }


def _wilson_lower(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts <= 0:
        return 0.0
    p = successes / attempts
    denominator = 1 + z * z / attempts
    centre = p + z * z / (2 * attempts)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * attempts)) / attempts)
    return max(0.0, (centre - margin) / denominator)


def rankings(days: int = 7) -> dict[str, Any]:
    window_days = max(1, min(days, 30))
    rows = _filtered_rows(int(time.time() - window_days * 86400), {})
    demand_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    stability_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    speed_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        demand_groups[(row["platform_group"], row["model"], row["usage_profile"])].append(row)
        stability_groups[(row["platform_group"], row["model"], row["channel"])].append(row)
        speed_groups[(row["platform_group"], row["model"], row["usage_profile"],
                      row["output_length_band"], row["channel"])].append(row)

    demand = []
    for key, values in demand_groups.items():
        requests = sum(row["request_count"] for row in values)
        users = max((row["active_users"] for row in values), default=0)
        concentration = _weighted(values, "top_user_request_share", "request_count") or 0
        demand.append({
            "platform_group": key[0], "model": key[1], "usage_profile": key[2],
            "request_count": requests, "active_users": users,
            "top_user_request_share": round(concentration, 4),
            "broad_demand_score": round(requests * (1 - concentration), 2),
        })
    demand_top = []
    for platform_group in sorted({row["platform_group"] for row in demand}):
        ranked = sorted(
            (row for row in demand if row["platform_group"] == platform_group),
            key=lambda row: (-row["broad_demand_score"], -row["active_users"], row["model"]),
        )[:3]
        demand_top.extend({**row, "rank": index + 1} for index, row in enumerate(ranked))

    stability_candidates = []
    insufficient = []
    eligibility: dict[tuple[str, str, str], bool] = {}
    for key, values in stability_groups.items():
        attempts = sum(row["attempt_count"] for row in values)
        successes = sum(row["success_count"] for row in values)
        days_active = len({row["bucket_start"] // 86400 for row in values if row["attempt_count"]})
        eligible = (attempts >= RANKING_MIN_ATTEMPTS and successes >= RANKING_MIN_SUCCESSES
                    and days_active >= RANKING_MIN_ACTIVE_DAYS)
        eligibility[key] = eligible
        if not eligible:
            insufficient.append({
                "platform_group": key[0], "model": key[1], "channel": key[2],
                "attempt_count": attempts, "success_count": successes,
                "active_days": days_active, "reason": "样本不足",
            })
            continue
        daily: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        for row in values:
            day = row["bucket_start"] // 86400
            daily[day][0] += row["success_count"]
            daily[day][1] += row["attempt_count"]
        daily_rates = [success / total for success, total in daily.values() if total]
        variation = statistics.pstdev(daily_rates) if len(daily_rates) > 1 else 0.0
        timeout_rate = sum(row["timeout_count"] for row in values) / attempts
        break_rate = sum(row["stream_break_count"] for row in values) / attempts
        lower = _wilson_lower(successes, attempts)
        stability_candidates.append({
            "platform_group": key[0], "model": key[1], "channel": key[2],
            "attempt_count": attempts, "success_count": successes, "active_days": days_active,
            "success_rate_lower_bound": round(lower, 6),
            "timeout_rate": round(timeout_rate, 6), "stream_break_rate": round(break_rate, 6),
            "cross_day_variation": round(variation, 6),
            "stability_score": round(lower - timeout_rate - break_rate - variation, 6),
        })
    stability_top = []
    cohorts = {(row["platform_group"], row["model"]) for row in stability_candidates}
    for cohort in sorted(cohorts):
        ranked = sorted(
            (row for row in stability_candidates
             if (row["platform_group"], row["model"]) == cohort),
            key=lambda row: (-row["stability_score"], -row["attempt_count"], row["channel"]),
        )[:3]
        stability_top.extend({**row, "rank": index + 1} for index, row in enumerate(ranked))

    speed_candidates = []
    for key, values in speed_groups.items():
        if not eligibility.get((key[0], key[1], key[4]), False):
            continue
        successes = sum(row["success_count"] for row in values)
        if successes < 30:
            continue
        speed_candidates.append({
            "platform_group": key[0], "model": key[1], "usage_profile": key[2],
            "output_length_band": key[3], "channel": key[4], "success_count": successes,
            "ttft_p95_ms": _weighted(values, "ttft_p95_ms"),
            "latency_p95_ms": _weighted(values, "latency_p95_ms"),
            "generation_tps": _weighted(values, "generation_tps"),
        })
    speed_top = []
    speed_cohorts = {(row["platform_group"], row["model"], row["usage_profile"],
                      row["output_length_band"]) for row in speed_candidates}
    for cohort in sorted(speed_cohorts):
        ranked = sorted(
            (row for row in speed_candidates if (
                row["platform_group"], row["model"], row["usage_profile"],
                row["output_length_band"],
            ) == cohort),
            key=lambda row: (
                row["latency_p95_ms"] if row["latency_p95_ms"] is not None else math.inf,
                row["ttft_p95_ms"] if row["ttft_p95_ms"] is not None else math.inf,
                -(row["generation_tps"] or 0), row["channel"],
            ),
        )[:3]
        speed_top.extend({**row, "rank": index + 1} for index, row in enumerate(ranked))
    return {
        "window_days": window_days,
        "eligibility": {"min_attempts": RANKING_MIN_ATTEMPTS,
                        "min_successes": RANKING_MIN_SUCCESSES,
                        "min_active_days": RANKING_MIN_ACTIVE_DAYS},
        "demand_top": demand_top, "stability_top": stability_top,
        "speed_top": speed_top, "insufficient": insufficient,
    }


def refresh_supply_gaps() -> list[dict[str, Any]]:
    rows = _filtered_rows(int(time.time() - RANKING_WINDOW_SECONDS), {})
    demands: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    channel_rows: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        dimension = (row["platform_group"], row["model"], row["usage_profile"])
        demands[dimension].append(row)
        channel_rows[(*dimension, row["channel"])].append(row)
    now = time.time()
    day = int(now) // 86400
    week_start = (day - (datetime.fromtimestamp(now, timezone.utc).weekday())) * 86400
    output = []
    for dimension, values in demands.items():
        requests = sum(row["request_count"] for row in values)
        users = max((row["active_users"] for row in values), default=0)
        critical = dimension[2] in {"agent", "coding", "customer_service"}
        if requests >= 1000 or users >= 50 or (critical and requests >= 300):
            demand_level, required = "high", 3
        elif requests >= 100 or users >= 10:
            demand_level, required = "normal", 2
        else:
            demand_level, required = "low", 0
        candidates = []
        for key, channel_values in channel_rows.items():
            if key[:3] != dimension:
                continue
            attempts = sum(row["attempt_count"] for row in channel_values)
            successes = sum(row["success_count"] for row in channel_values)
            active_days = len({row["bucket_start"] // 86400 for row in channel_values
                               if row["attempt_count"]})
            timeout_rate = sum(row["timeout_count"] for row in channel_values) / attempts \
                if attempts else 1
            break_rate = sum(row["stream_break_count"] for row in channel_values) / attempts \
                if attempts else 1
            qualified = (
                attempts >= RANKING_MIN_ATTEMPTS and successes >= RANKING_MIN_SUCCESSES
                and active_days >= RANKING_MIN_ACTIVE_DAYS
                and _wilson_lower(successes, attempts) >= .95
                and timeout_rate <= .02 and break_rate <= .01
            )
            supply_source = next((row["supply_source"] for row in reversed(channel_values)
                                  if row["supply_source"]), "")
            candidates.append({"channel": key[3], "attempts": attempts,
                               "qualified": qualified, "supply_source": supply_source})
        qualified_count = sum(candidate["qualified"] for candidate in candidates)
        total_attempts = sum(candidate["attempts"] for candidate in candidates)
        concentration = max((candidate["attempts"] for candidate in candidates), default=0) \
            / total_attempts if total_attempts else 0
        source_channels: dict[str, list[str]] = defaultdict(list)
        for candidate in candidates:
            if candidate["supply_source"]:
                source_channels[candidate["supply_source"]].append(candidate["channel"])
        suspected = [{"declared_source": source, "channels": channels,
                      "assessment": "申报来源相同，疑似存在共同故障域"}
                     for source, channels in source_channels.items() if len(channels) > 1]
        reasons = []
        if required and qualified_count < required:
            reasons.append(f"{demand_level} 需求需要 {required} 个合格渠道，目前只有 {qualified_count} 个")
        if concentration > .7:
            reasons.append(f"单渠道尝试占比 {concentration:.0%}，供应过度集中")
        if suspected:
            reasons.append("多个渠道申报同一供应来源，冗余可能低于渠道数量")
        if demand_level == "low":
            reasons.append("当前需求较低，仅观察，不为凑数补充渠道")
        feedback_rows = store.query(
            "SELECT reviews.review_30d_json FROM recommendation_reviews reviews "
            "JOIN supply_gap_recommendations gaps ON gaps.id=reviews.recommendation_id "
            "WHERE gaps.platform_group=? AND gaps.model=? AND gaps.usage_profile=? "
            "AND reviews.review_30d_json!='{}' ORDER BY reviews.updated_at DESC LIMIT 1",
            dimension,
        )
        if feedback_rows:
            feedback = store.loads(feedback_rows[0]["review_30d_json"], {})
            label = {
                "effective": "有效", "no_clear_effect": "无明显效果",
                "negative": "负面", "insufficient_evidence": "证据不足",
            }.get(feedback.get("classification"), "待复核")
            reasons.append(f"最近一次 30 天供给复盘为“{label}”，已作为本周建议背景，不自动修改评分规则")
        payload = {
            "week_start": week_start, "platform_group": dimension[0], "model": dimension[1],
            "usage_profile": dimension[2], "demand_level": demand_level,
            "request_count": requests, "active_users": users,
            "required_channels": required, "qualified_channels": qualified_count,
            "concentration": round(concentration, 6),
            "status": "observe" if demand_level == "low" else "candidate",
            "reasons_json": store.dumps(reasons),
            "suspected_fault_domains_json": store.dumps(suspected),
            "created_at": now, "updated_at": now,
        }
        conflict = ("week_start", "platform_group", "model", "usage_profile")
        columns = tuple(payload)
        updates = [column for column in columns if column not in conflict and column != "created_at"]
        store.execute(
            f"INSERT INTO supply_gap_recommendations ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT ({','.join(conflict)}) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}" for column in updates),
            tuple(payload[column] for column in columns),
        )
        if reasons:
            stored = store.query(
                "SELECT id FROM supply_gap_recommendations WHERE week_start=? "
                "AND platform_group=? AND model=? AND usage_profile=?",
                (week_start, *dimension),
            )[0]
            output.append({**payload, "id": stored["id"], "reasons": reasons,
                           "suspected_fault_domains": suspected})
    return output


def list_supply_gaps() -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM supply_gap_recommendations ORDER BY week_start DESC,id")
    output = []
    for row in rows:
        reasons = store.loads(row.pop("reasons_json"), [])
        suspected = store.loads(row.pop("suspected_fault_domains_json"), [])
        output.append({**row, "reasons": reasons, "suspected_fault_domains": suspected})
    return output


def create_recommendation_review(
    recommendation_id: int, channel_id: int, production_channel: str,
    activated_at: float, created_by: int,
) -> dict[str, Any]:
    if not store.get("supply_gap_recommendations", recommendation_id):
        raise ValueError("供给缺口建议不存在")
    if not store.get("channels", channel_id):
        raise ValueError("渠道不存在")
    now = time.time()
    review_id = store.insert("recommendation_reviews", {
        "recommendation_id": recommendation_id, "channel_id": channel_id,
        "production_channel": production_channel.strip(),
        "activated_at": activated_at, "status": "scheduled",
        "baseline_json": "{}", "review_7d_json": "{}", "review_30d_json": "{}",
        "next_review_at": activated_at + 7 * 86400, "created_by": created_by,
        "created_at": now, "updated_at": now,
    })
    return recommendation_review_out(store.get("recommendation_reviews", review_id))


def recommendation_review_out(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        raise ValueError("复盘记录不存在")
    return {
        **row,
        "baseline": store.loads(row["baseline_json"], {}),
        "review_7d": store.loads(row["review_7d_json"], {}),
        "review_30d": store.loads(row["review_30d_json"], {}),
    }


def list_recommendation_reviews() -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT reviews.*,gaps.platform_group,gaps.model,gaps.usage_profile," 
        "channels.name channel_name FROM recommendation_reviews reviews "
        "JOIN supply_gap_recommendations gaps ON gaps.id=reviews.recommendation_id "
        "JOIN channels ON channels.id=reviews.channel_id "
        "ORDER BY reviews.created_at DESC,reviews.id DESC"
    )
    return [recommendation_review_out(row) for row in rows]


def _review_window(
    gap: dict[str, Any], start: float, end: float, production_channel: str,
) -> dict[str, Any]:
    rows = [row for row in _filtered_rows(int(start), {
        "platform_group": gap["platform_group"], "model": gap["model"],
        "usage_profile": gap["usage_profile"],
    }) if row["bucket_start"] < end]
    metrics = _aggregate(rows)
    attempts = metrics["attempt_count"]
    channel_attempts: dict[str, int] = defaultdict(int)
    for row in rows:
        channel_attempts[row["channel"]] += row["attempt_count"]
    return {
        "start": start, "end": end, "attempt_count": attempts,
        "success_rate": round(metrics["success_count"] / attempts, 6) if attempts else None,
        "latency_p95_ms": metrics["latency_p95_ms"],
        "switch_rate": round(metrics["failover_count"] / metrics["request_count"], 6)
        if metrics["request_count"] else None,
        "concentration": round(max(channel_attempts.values(), default=0) / attempts, 6)
        if attempts else None,
        "new_channel_attempts": channel_attempts.get(production_channel, 0),
        "active_days": len({row["bucket_start"] // 86400 for row in rows}),
    }


def _classify_review(before: dict[str, Any], after: dict[str, Any], complete: bool) -> str:
    if not complete or before["attempt_count"] < 200 or after["attempt_count"] < 200 \
            or after["new_channel_attempts"] < 50:
        return "insufficient_evidence"
    success_delta = (after["success_rate"] or 0) - (before["success_rate"] or 0)
    latency_delta = None
    if before["latency_p95_ms"] and after["latency_p95_ms"] is not None:
        latency_delta = after["latency_p95_ms"] / before["latency_p95_ms"] - 1
    concentration_delta = (after["concentration"] or 0) - (before["concentration"] or 0)
    if success_delta <= -.01 or (latency_delta is not None and latency_delta >= .15) \
            or concentration_delta >= .1:
        return "negative"
    improved = success_delta >= .005 or (latency_delta is not None and latency_delta <= -.05) \
        or concentration_delta <= -.1
    regressed = success_delta < -.003 or (latency_delta is not None and latency_delta > .05)
    return "effective" if improved and not regressed else "no_clear_effect"


def run_recommendation_review(review_id: int, horizon_days: int) -> dict[str, Any]:
    if horizon_days not in {7, 30}:
        raise ValueError("复盘周期只能是 7 或 30 天")
    row = store.get("recommendation_reviews", review_id)
    if not row:
        raise ValueError("复盘记录不存在")
    gap = store.get("supply_gap_recommendations", row["recommendation_id"])
    assert gap is not None
    seconds = horizon_days * 86400
    now = time.time()
    before = _review_window(
        gap, row["activated_at"] - seconds, row["activated_at"],
        row["production_channel"],
    )
    after_end = min(now, row["activated_at"] + seconds)
    after = _review_window(
        gap, row["activated_at"], after_end, row["production_channel"],
    )
    complete = now >= row["activated_at"] + seconds
    classification = _classify_review(before, after, complete)
    result = {
        "horizon_days": horizon_days, "complete": complete,
        "classification": classification, "before": before, "after": after,
        "deltas": {
            "success_rate": None if before["success_rate"] is None or after["success_rate"] is None
            else round(after["success_rate"] - before["success_rate"], 6),
            "latency_p95_ms": None if before["latency_p95_ms"] is None or after["latency_p95_ms"] is None
            else round(after["latency_p95_ms"] - before["latency_p95_ms"], 3),
            "switch_rate": None if before["switch_rate"] is None or after["switch_rate"] is None
            else round(after["switch_rate"] - before["switch_rate"], 6),
            "concentration": None if before["concentration"] is None or after["concentration"] is None
            else round(after["concentration"] - before["concentration"], 6),
        },
        "rule_notice": "单次复盘只反馈供给建议，不自动修改测试评分规则。",
        "generated_at": now,
    }
    patch: dict[str, Any] = {
        "baseline_json": store.dumps(before), "updated_at": now,
    }
    if horizon_days == 7:
        patch.update({
            "review_7d_json": store.dumps(result),
            "status": "waiting_30d", "next_review_at": row["activated_at"] + 30 * 86400,
        })
    else:
        patch.update({
            "review_30d_json": store.dumps(result),
            "status": "completed", "next_review_at": row["activated_at"] + 30 * 86400,
        })
    store.update("recommendation_reviews", review_id, patch)
    return recommendation_review_out(store.get("recommendation_reviews", review_id))


def run_due_recommendation_reviews() -> list[dict[str, Any]]:
    now = time.time()
    due = store.query(
        "SELECT * FROM recommendation_reviews WHERE status!='completed' "
        "AND next_review_at<=? ORDER BY next_review_at,id", (now,)
    )
    output = []
    for row in due:
        horizon = 7 if row["status"] == "scheduled" else 30
        output.append(run_recommendation_review(row["id"], horizon))
    return output


def trend(filters: dict[str, str], hours: int = 24) -> list[dict[str, Any]]:
    rows = _filtered_rows(int(time.time() - max(1, min(hours, 24 * 30)) * 3600), filters)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["bucket_start"]].append(row)
    return [{"bucket_start": bucket, **_aggregate(values)}
            for bucket, values in sorted(grouped.items())]


def run_retention() -> dict[str, Any]:
    started = time.time()
    run_id = metric_store.insert("metric_retention_runs", {"status": "running", "started_at": started})
    try:
        now = int(time.time())
        periods = metric_store.query("SELECT DISTINCT bucket_start/3600*3600 hour_start,"
                              "bucket_start/86400*86400 day_start FROM metric_buckets")
        for period in periods:
            _refresh_rollup("hourly_metrics", int(period["hour_start"]), 3600)
            _refresh_rollup("daily_metrics", int(period["day_start"]), 86400)
        before_minute = metric_store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"]
        before_hour = metric_store.query("SELECT COUNT(*) n FROM hourly_metrics")[0]["n"]
        before_day = metric_store.query("SELECT COUNT(*) n FROM daily_metrics")[0]["n"]
        metric_store.execute("DELETE FROM metric_buckets WHERE bucket_start<?", (now - MINUTE_RETENTION_SECONDS,))
        metric_store.execute("DELETE FROM hourly_metrics WHERE period_start<?", (now - HOUR_RETENTION_SECONDS,))
        metric_store.execute("DELETE FROM daily_metrics WHERE period_start<?", (now - DAY_RETENTION_SECONDS,))
        after_minute = metric_store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"]
        after_hour = metric_store.query("SELECT COUNT(*) n FROM hourly_metrics")[0]["n"]
        after_day = metric_store.query("SELECT COUNT(*) n FROM daily_metrics")[0]["n"]
        result = {
            "status": "success", "hourly_rows": after_hour, "daily_rows": after_day,
            "minute_rows_deleted": before_minute - after_minute,
            "hourly_rows_deleted": before_hour - after_hour,
            "daily_rows_deleted": before_day - after_day, "finished_at": time.time(),
        }
        metric_store.update("metric_retention_runs", run_id, result)
        return result
    except Exception as exc:
        metric_store.update("metric_retention_runs", run_id, {
            "status": "failed", "error": str(exc)[:500], "finished_at": time.time(),
        })
        raise
