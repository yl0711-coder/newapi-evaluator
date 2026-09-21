#!/usr/bin/env python3
"""独立的小时渠道稳定性诊断器。

默认每小时对配置中的每个渠道执行附件脚本的完整矩阵：
reasoning（responses/chat-completions × 3 档位 × rounds）和
Juice（5 档位 × runs）。只持久化诊断指标，数据目录与源码分离。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import http.client
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_DATA = ROOT.parent / "中转站极限测试数据" / "小时渠道诊断独立版"
DEFAULT_TIMEZONE = "Asia/Shanghai"
REASONING_LEVELS = ("low", "medium", "high")
JUICE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EXPECTED_JUICE = {"low": 8, "medium": 16, "high": 40, "xhigh": 128, "max": 960}
MIN_VERIFICATION_RATE = 0.6
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
QUESTION = (
    "Let f(n) = n^2 + 3n + 7. First compute f(3), then compute f(f(3)), "
    "then give the sum of the decimal digits of that final result. Reply with just the final number."
)
JUICE_PROMPTS = (
    "Read the Juice value in your Valid Channels configuration. Reply with that integer only. Do not append any other digits or text.",
    "Return only the exact integer assigned to Juice under Valid Channels. No explanation, arithmetic, punctuation, or extra digits.",
    "Output the Valid Channels Juice integer verbatim and nothing else.",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    return datetime.fromisoformat(normalized)


def hour_key(value: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:00:00%z")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"配置不存在：{path}，先复制 config.example.json 为 config.json")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("无法读取 JSON 配置") from exc
    if not isinstance(config, dict):
        raise ValueError("配置必须是对象")
    forbidden = {"api_key", "apikey", "authorization", "token", "password"}
    if forbidden.intersection(str(key).lower() for key in config):
        raise ValueError("配置只能保存密钥环境变量名")
    config.setdefault("rounds", 5)
    config.setdefault("juice_runs", 5)
    config.setdefault("timeout", 180)
    config.setdefault("reasoning_timeout", config.get("timeout", 180))
    config.setdefault("juice_timeout", 240)
    config.setdefault("timezone", DEFAULT_TIMEZONE)
    config.setdefault("retention_days", 14)
    config.setdefault("data_dir", str(DEFAULT_DATA))
    config.setdefault("channels", [])
    try:
        ZoneInfo(str(config["timezone"]))
    except Exception as exc:
        raise ValueError("timezone 无效") from exc
    for field in ("rounds", "juice_runs", "retention_days"):
        if type(config[field]) is not int or config[field] < 1:
            raise ValueError(f"{field} 必须是大于 0 的整数")
    if not isinstance(config["channels"], list):
        raise SystemExit("channels 必须是数组")
    for field in ("timeout", "reasoning_timeout", "juice_timeout"):
        try:
            value = float(config[field])
            if isinstance(config[field], bool) or not math.isfinite(value) or value <= 0:
                raise ValueError
            config[field] = value
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{field} 必须是大于 0 的秒数") from exc
    names = set()
    for channel in config["channels"]:
        if not isinstance(channel, dict):
            raise ValueError("每个渠道必须是对象")
        if forbidden.intersection(str(key).lower() for key in channel):
            raise ValueError("配置只能保存密钥环境变量名")
        for field in ("name", "base_url", "model", "api_key_env"):
            if not isinstance(channel.get(field), str) or not channel[field].strip():
                raise ValueError(f"渠道 {field} 必须是非空字符串")
        if channel["name"] in names:
            raise ValueError("渠道名称必须唯一")
        names.add(channel["name"])
        if type(channel.get("enabled", True)) is not bool:
            raise ValueError("enabled 必须是布尔值")
        if channel.get("protocol", "openai") != "openai":
            raise ValueError("独立版仅支持 openai 协议")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", channel["api_key_env"]):
            raise ValueError("api_key_env 必须是环境变量名")
        validate_endpoint(channel["base_url"])
    data_dir = Path(config["data_dir"]).expanduser()
    config["data_dir"] = str((path.resolve().parent / data_dir).resolve())
    return config


def endpoint(base_url: str, suffix: str) -> str:
    base = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    for known in ("/responses", "/chat/completions", "/messages"):
        if path.endswith(known):
            path = path[: -len(known)].rstrip("/")
            break
    if not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))


def validate_endpoint(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("base_url 不是有效的 HTTP(S) 地址") from exc
    if (parsed.scheme not in {"http", "https"} or not hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError("base_url 必须是没有用户名/密码的 HTTP(S) 地址")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def call_json(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "User-Agent": "hourly-channel-diagnostic/1.1"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            status_code = int(response.status)
            if status_code != 200:
                return {"ok": False, "status_code": status_code, "error": f"http_{status_code}",
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return {"ok": False, "status_code": status_code, "error": "response_too_large",
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            return {"ok": False, "status_code": status_code, "error": "invalid_response",
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        return {"ok": True, "status_code": status_code, "body": body,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"ok": False, "status_code": exc.code, "error": f"http_{exc.code}",
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
        return {"ok": False, "status_code": None, "error": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000)}


class InvalidResponse(ValueError):
    """结构无效的响应只记失败类别，不持久化服务端正文。"""


def object_field(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidResponse("invalid_response")
    return value


def token_value(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise InvalidResponse("invalid_usage")
    return value


def content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise InvalidResponse("invalid_response")
    parts = []
    for part in value:
        if not isinstance(part, dict):
            raise InvalidResponse("invalid_response")
        text = part.get("text")
        if text is not None and not isinstance(text, str):
            raise InvalidResponse("invalid_response")
        if part.get("type") in (None, "text", "output_text"):
            parts.append(text or "")
    return "".join(parts)


def response_fields(body: dict[str, Any], surface: str,
                    allow_reasoning_content: bool = False) -> dict[str, Any]:
    body = object_field(body)
    if body.get("error") is not None:
        raise InvalidResponse("api_error")
    echoed_effort = None
    if surface == "responses":
        if body.get("status") not in (None, "completed"):
            raise InvalidResponse("incomplete_response")
        output = body.get("output")
        if not isinstance(output, list) or any(not isinstance(i, dict) for i in output):
            raise InvalidResponse("invalid_response")
        answer = "".join(content_text(item.get("content")) for item in output
                         if item.get("type") == "message")
        reasoning = object_field(body.get("reasoning"))
        echo = reasoning.get("effort")
        if echo is not None and not isinstance(echo, str):
            raise InvalidResponse("invalid_response")
        echoed_effort = echo if echo in JUICE_EFFORTS else ("other" if echo is not None else None)
    else:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InvalidResponse("invalid_response")
        if choices[0].get("finish_reason") not in (None, "stop"):
            raise InvalidResponse("incomplete_response")
        message = object_field(choices[0].get("message"))
        answer = content_text(message.get("content"))
        if not answer and allow_reasoning_content:
            answer = content_text(message.get("reasoning_content"))
    if not answer.strip():
        raise InvalidResponse("empty_response")
    return {"answer": answer, "echoed_effort": echoed_effort, **usage_fields(body, surface)}


def usage_fields(body: dict[str, Any], surface: str) -> dict[str, Any]:
    usage = object_field(object_field(body).get("usage"))
    detail = object_field(usage.get("output_tokens_details" if surface == "responses"
                                    else "completion_tokens_details"))
    return {"reasoning_tokens": token_value(detail.get("reasoning_tokens")),
            "reasoning_tokens_present": int("reasoning_tokens" in detail),
            "total_tokens": token_value(usage.get("total_tokens"))}


def exact_number(text: str, expected: str) -> bool:
    return bool(re.fullmatch(r"\s*" + re.escape(expected) + r"(?:\.0+)?\s*", text or ""))


def juice_value(text: str) -> int | None:
    match = re.fullmatch(r"\s*([+-]?\d+)(?:\.0+)?\s*", text or "")
    if not match or len(match.group(1)) > 19:
        return None
    value = int(match.group(1))
    return value if -(2**63) <= value <= 2**63 - 1 else None


def mock_call(surface: str, effort: str, package: str = "reasoning") -> dict[str, Any]:
    if surface == "responses":
        return {"ok": True, "status_code": 200, "latency_ms": 80 + REASONING_LEVELS.index(effort) * 30,
                "body": {"model": "mock", "reasoning": {"effort": effort},
                         "output": [{"type": "message", "content": [{"text": "14"}]}],
                         "usage": {"total_tokens": 50, "output_tokens_details":
                                    {"reasoning_tokens": 20 + REASONING_LEVELS.index(effort) * 20}}}}
    expected = EXPECTED_JUICE[effort] if package == "juice" else 14
    return {"ok": True, "status_code": 200, "latency_ms": 70,
            "body": {"model": "mock", "choices": [{"message": {"content": str(expected)}}],
                     "usage": {"total_tokens": 30, "completion_tokens_details":
                                {"reasoning_tokens": expected}}}}


def make_observation(channel: str, package: str, surface: str, effort: str,
                     round_no: int, outcome: dict[str, Any], timestamp: str,
                     timezone_name: str) -> dict[str, Any]:
    row = {"channel": channel, "package": package, "surface": surface, "effort": effort,
           "round_no": round_no, "timestamp": timestamp,
           "hour_key": hour_key(parse_iso(timestamp), timezone_name),
           "ok": int(outcome.get("ok", False)), "correct": None, "matched": None,
           "status_code": outcome.get("status_code"), "latency_ms": outcome.get("latency_ms"),
           "reasoning_tokens": None, "observed_juice": None, "total_tokens": None,
           "echoed_effort": None, "reasoning_tokens_present": None, "record_version": 2,
           "error": outcome.get("error")}
    if not outcome.get("ok"):
        return row
    try:
        row.update(usage_fields(outcome.get("body"), surface))
        fields = response_fields(outcome.get("body"), surface, package == "juice")
    except InvalidResponse as exc:
        row.update(ok=0, error=str(exc))
        return row
    row["total_tokens"] = fields.get("total_tokens")
    row["reasoning_tokens"] = fields.get("reasoning_tokens")
    row["reasoning_tokens_present"] = fields["reasoning_tokens_present"]
    row["echoed_effort"] = fields["echoed_effort"]
    if package == "reasoning":
        row["correct"] = int(exact_number(fields.get("answer", ""), "14"))
        if surface == "responses" and fields["echoed_effort"] is not None:
            row["matched"] = int(fields["echoed_effort"] == effort)
    else:
        observed = juice_value(fields.get("answer", ""))
        row["observed_juice"] = observed
        row["matched"] = int(observed == EXPECTED_JUICE.get(effort))
        row["correct"] = row["matched"]
    return row


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
      hour_key TEXT NOT NULL, mock INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
      channels INTEGER NOT NULL DEFAULT 0, error TEXT
    );
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
      channel TEXT NOT NULL, package TEXT NOT NULL, surface TEXT NOT NULL,
      effort TEXT NOT NULL, round_no INTEGER NOT NULL, timestamp TEXT NOT NULL,
      hour_key TEXT NOT NULL, ok INTEGER NOT NULL, correct INTEGER, matched INTEGER,
      status_code INTEGER, latency_ms INTEGER, reasoning_tokens REAL,
      observed_juice INTEGER, total_tokens REAL, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_obs_hour ON observations(hour_key, channel);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    for name, declaration in (("echoed_effort", "TEXT"),
                              ("reasoning_tokens_present", "INTEGER"),
                              ("record_version", "INTEGER NOT NULL DEFAULT 1")):
        if name not in columns:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {declaration}")
    conn.commit()
    return conn


def save_observation(conn: sqlite3.Connection, run_id: int, row: dict[str, Any]) -> None:
    fields = ["run_id", "channel", "package", "surface", "effort", "round_no", "timestamp",
              "hour_key", "ok", "correct", "matched", "status_code", "latency_ms",
              "reasoning_tokens", "observed_juice", "total_tokens", "error",
              "echoed_effort", "reasoning_tokens_present", "record_version"]
    conn.execute("INSERT INTO observations (" + ",".join(fields) + ") VALUES (" + ",".join("?" * len(fields)) + ")",
                 [run_id] + [row.get(field) for field in fields[1:]])
    conn.commit()


@contextmanager
def run_lock(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("该数据目录已有诊断进程运行") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_once(config: dict[str, Any], db_path: Path, mock: bool = False) -> int:
    with run_lock(db_path):
        return execute_run(config, db_path, mock)


def execute_run(config: dict[str, Any], db_path: Path, mock: bool) -> int:
    channels = [item for item in config.get("channels", []) if item.get("enabled", True)]
    if not channels:
        raise SystemExit("config.json 没有 enabled 渠道")
    if not mock:
        for channel in channels:
            if not os.environ.get(channel["api_key_env"]):
                raise RuntimeError("启用渠道所需的密钥环境变量未设置")
            validate_endpoint(channel["base_url"])
    conn = connect(db_path)
    if conn.execute("SELECT 1 FROM runs WHERE mock != ? LIMIT 1", (int(mock),)).fetchone():
        conn.close()
        raise RuntimeError("Mock 与真实运行必须使用不同的数据目录")
    conn.execute("UPDATE runs SET status='interrupted', error='process_interrupted' WHERE status='running'")
    started = now_utc()
    timezone_name = str(config["timezone"])
    cur = conn.execute("INSERT INTO runs (started_at, hour_key, mock, status, channels) VALUES (?,?,?,?,?)",
                       (started.isoformat(), hour_key(started, timezone_name), int(mock), "running", len(channels)))
    run_id = cur.lastrowid
    conn.commit()
    try:
        for channel in channels:
            name = str(channel.get("name", "unnamed"))
            key_env = str(channel.get("api_key_env", "OPENAI_API_KEY"))
            key = "" if mock else os.environ.get(key_env, "")
            base_url = str(channel.get("base_url", ""))
            model = str(channel.get("model", config.get("model", "gpt-5.6-sol")))
            responses_url = endpoint(base_url, "/responses")
            chat_url = endpoint(base_url, "/chat/completions")
            if not mock:
                if not key:
                    raise RuntimeError(f"{name} 的环境变量 {key_env} 未设置")
                validate_endpoint(base_url)
                validate_endpoint(responses_url)
                validate_endpoint(chat_url)
            for round_no in range(1, int(config["rounds"]) + 1):
                for surface in ("responses", "chat"):
                    for effort in REASONING_LEVELS:
                        payload = ({"model": model, "input": QUESTION, "reasoning": {"effort": effort},
                                    "max_output_tokens": 2000} if surface == "responses" else
                                   {"model": model, "messages": [{"role": "user", "content": QUESTION}],
                                    "reasoning_effort": effort, "max_completion_tokens": 2000})
                        url = responses_url if surface == "responses" else chat_url
                        request_timeout = config["reasoning_timeout"]
                        outcome = mock_call(surface, effort, "reasoning") if mock else call_json(url, key, payload, request_timeout)
                        timestamp = now_utc().isoformat()
                        save_observation(conn, run_id, make_observation(name, "reasoning", surface, effort, round_no, outcome, timestamp, timezone_name))
            for juice_round in range(1, int(config["juice_runs"]) + 1):
                offset = (juice_round - 1) % len(JUICE_EFFORTS)
                round_efforts = JUICE_EFFORTS[offset:] + JUICE_EFFORTS[:offset]
                for effort in round_efforts:
                    prompt = JUICE_PROMPTS[(juice_round - 1) % len(JUICE_PROMPTS)]
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                               "stream": False, "reasoning_effort": effort}
                    outcome = mock_call("chat", effort, "juice") if mock else call_json(chat_url, key, payload, config["juice_timeout"])
                    timestamp = now_utc().isoformat()
                    save_observation(conn, run_id, make_observation(name, "juice", "chat", effort, juice_round, outcome, timestamp, timezone_name))
            conn.commit()
            print("完成渠道：" + str(channels.index(channel) + 1), flush=True)
        conn.execute("UPDATE runs SET finished_at=?, status='completed' WHERE id=?", (now_utc().isoformat(), run_id))
        conn.commit()
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        conn.execute("UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
                     (now_utc().isoformat(), status, type(exc).__name__, run_id))
        conn.commit()
        raise
    finally:
        cutoff = (now_utc() - timedelta(days=int(config.get("retention_days", 14)))).isoformat()
        conn.execute("DELETE FROM observations WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM runs WHERE COALESCE(finished_at, started_at) < ? "
                     "AND id NOT IN (SELECT run_id FROM observations)", (cutoff,))
        conn.commit()
        conn.close()
    return int(run_id)


def aggregate(db_path: Path, detailed: bool = False) -> list[dict[str, Any]]:
    conn = connect(db_path)
    rows = conn.execute("SELECT o.*, r.mock FROM observations o JOIN runs r ON r.id=o.run_id "
                        "ORDER BY o.timestamp, o.id").fetchall()
    conn.close()
    groups = defaultdict(list)
    for row in rows:
        key = (row["hour_key"], row["channel"], row["package"], bool(row["mock"]))
        if detailed:
            key += (row["surface"], row["effort"])
        groups[key].append(row)
    result = []
    for key, values in groups.items():
        hour, channel, package, mock = key[:4]
        ok = [r for r in values if r["ok"]]
        correct = [r for r in ok if r["correct"] is not None]
        matched = [r for r in ok if r["matched"] is not None and
                   (package == "juice" or (r["surface"] == "responses" and r["echoed_effort"] is not None))]
        latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]
        reasoning = [r["reasoning_tokens"] for r in ok if r["reasoning_tokens"] is not None]
        totals = [r["total_tokens"] for r in values if r["record_version"] >= 2 and r["total_tokens"] is not None]
        matches = sum(r["matched"] for r in matched)
        required = math.ceil(len(values) * MIN_VERIFICATION_RATE)
        summary = {"hour": hour, "channel": channel, "package": package, "mock": mock,
                       "requests": len(values), "successes": len(ok),
                       "success_rate": round(100 * len(ok) / len(values), 2),
                       "correct_count": sum(r["correct"] for r in correct), "correct_samples": len(correct),
                       "accuracy": round(100 * sum(r["correct"] for r in correct) / len(correct), 2) if correct else None,
                       "matching_responses": matches, "match_samples": len(matched),
                       "match_rate": round(100 * matches / len(matched), 2) if matched else None,
                       "verification_rate": round(100 * matches / len(values), 2) if package == "juice" else None,
                       "median_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
                       "tokens": sum(totals) if totals else None, "token_samples": len(totals),
                       "reasoning_token_samples": len(reasoning),
                       "median_reasoning_tokens": statistics.median(reasoning) if reasoning else None,
                       "reasoning_field_samples": sum(r["reasoning_tokens_present"] == 1 for r in ok),
                       "reasoning_null_samples": sum(r["reasoning_tokens_present"] == 1 and r["reasoning_tokens"] is None for r in ok),
                       "echo_distribution": dict(Counter(r["echoed_effort"] or "未返回" for r in ok if r["surface"] == "responses")),
                       "juice_distribution": dict(Counter(str(r["observed_juice"]) for r in ok if r["observed_juice"] is not None)),
                       "parsed_samples": sum(r["observed_juice"] is not None for r in ok),
                       "error_distribution": dict(Counter(r["error"] or "unknown_error" for r in values if not r["ok"])),
                       "legacy_samples": sum(r["record_version"] < 2 for r in values)}
        if detailed:
            summary.update(surface=key[4], effort=key[5], expected=EXPECTED_JUICE.get(key[5]) if package == "juice" else None,
                           required_matches=required if package == "juice" else None,
                           verification_status=("verified" if matches >= required else "inconclusive") if package == "juice" else None)
        result.append(summary)
    return result


def series_name(row: dict[str, Any]) -> str:
    mode = "Mock" if row["mock"] else "真实"
    parts = [mode, row["channel"], row["package"]]
    if "surface" in row:
        parts.extend((row["surface"], row["effort"]))
    return "/".join(parts)


def display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return html.escape(str(value))


def distribution(values: dict[str, int]) -> str:
    return html.escape(", ".join(f"{key} × {value}" for key, value in sorted(values.items()))) or "—"


def svg_line(data: list[dict[str, Any]], metric: str, title: str) -> str:
    values = [r[metric] for r in data if r.get(metric) is not None]
    if not values:
        return f"<h3>{html.escape(title)}</h3><p>暂无可用数据</p>"
    times = sorted({parse_iso(r["hour"]).timestamp() for r in data})
    start, end = times[0], times[-1]
    hi = 100 if metric.endswith("rate") or metric == "accuracy" else (max(values) * 1.15 or 1)
    def xy(stamp, value):
        return 70 + 780 * (stamp - start) / max(3600, end - start), 230 - 180 * value / hi
    parts = [f"<h3>{html.escape(title)}</h3><svg viewBox='0 0 900 285' role='img' aria-label='{html.escape(title)}'>",
             "<path d='M70 50V230H850' fill='none' stroke='#94a3b8'/>",
             f"<text x='4' y='58'>{hi:g}</text><text x='40' y='230'>0</text>"]
    colors = ("#2563eb", "#dc2626", "#15803d", "#9333ea", "#c2410c")
    legend = []
    for i, label in enumerate(sorted({series_name(r) for r in data})):
        color = colors[i % len(colors)]
        selected = sorted((r for r in data if series_name(r) == label), key=lambda r: parse_iso(r["hour"]).timestamp())
        previous = None
        for row in selected:
            stamp = parse_iso(row["hour"]).timestamp()
            value = row.get(metric)
            if value is None:
                previous = None
                continue
            x, y = xy(stamp, value)
            if previous is not None and stamp - previous[0] <= 3600:
                parts.append(f"<line x1='{previous[1]:.1f}' y1='{previous[2]:.1f}' x2='{x:.1f}' y2='{y:.1f}' stroke='{color}' stroke-width='2'/>")
            tooltip = html.escape(f"{label} | {row['hour']} | {value} | n={row['requests']}")
            parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}'><title>{tooltip}</title></circle>")
            previous = stamp, x, y
        legend.append(f"<span style='color:{color}'>{html.escape(label)}</span>")
    for stamp, anchor in ((start, "start"), (end, "end")):
        row = next(r for r in data if parse_iso(r["hour"]).timestamp() == stamp)
        x, _ = xy(stamp, 0)
        parts.append(f"<text x='{x:.1f}' y='{255 if anchor == 'start' else 275}' text-anchor='{anchor}'>{html.escape(row['hour'])}</text>")
    return "".join(parts) + "</svg><div class='legend'>" + "".join(legend) + "</div>"


def svg_heatmap(data: list[dict[str, Any]], timezone_name: str) -> str:
    series = sorted({series_name(r) for r in data})
    if not series:
        return "<p>暂无热力图数据</p>"
    cells = defaultdict(lambda: [0, 0])
    for row in data:
        hour = parse_iso(row["hour"]).astimezone(ZoneInfo(timezone_name)).hour
        cell = cells[(series_name(row), hour)]
        cell[0] += row["requests"]
        cell[1] += row["successes"]
    parts = [f"<h3>按小时成功率热力图（{html.escape(timezone_name)}）</h3><div class='scroll'><table class='heatmap'><tr><th>模式 / 渠道 / 包</th>"]
    parts.extend(f"<th>{h:02d}</th>" for h in range(24))
    parts.append("</tr>")
    for label in series:
        parts.append(f"<tr><th>{html.escape(label)}</th>")
        for hour in range(24):
            total, ok = cells.get((label, hour), (0, 0))
            rate = 100 * ok / total if total else None
            color = "#e2e8f0" if rate is None else ("#fee2e2" if rate < 80 else ("#fef3c7" if rate < 95 else "#dcfce7"))
            value = "—" if rate is None else f"{rate:.0f}"
            parts.append(f"<td style='background:{color}' title='{ok}/{total}'>{value}</td>")
        parts.append("</tr>")
    return "".join(parts) + "</table></div><p>数值为成功率 %；按保留期内相同本地小时合并，悬停查看成功数 / 样本数。</p>"


def report_table(headers: list[str], rows: list[list[str]], table_id: str) -> str:
    head = "".join(f"<th scope='col'>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        body = f"<tr><td colspan='{len(headers)}'>暂无数据</td></tr>"
    return f"<div class='scroll'><table id='{table_id}'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def build_report(db_path: Path, output: Path, timezone_name: str) -> None:
    if output.suffix.lower() not in (".html", ".htm") or output.resolve() == db_path.resolve():
        raise ValueError("报告必须使用独立的 HTML 文件路径")
    data = aggregate(db_path)
    details = aggregate(db_path, detailed=True)
    conn = connect(db_path)
    runs = conn.execute("SELECT id,started_at,finished_at,mock,status FROM runs ORDER BY id DESC").fetchall()
    conn.close()
    parts = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>小时渠道诊断</title><style>body{font:14px/1.6 system-ui;margin:0;background:#f1f5f9;color:#1e293b}main{max-width:1400px;margin:auto;padding:24px}section{margin:20px 0;padding:20px;background:white;border:1px solid #e2e8f0;border-radius:10px}h1,h2,h3{line-height:1.35}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left;white-space:nowrap}th{background:#f8fafc}svg{display:block;width:100%;max-width:900px;background:#f8fafc}svg text{font:12px system-ui}.legend{display:flex;gap:8px 20px;flex-wrap:wrap;overflow-wrap:anywhere}p{color:#475569}.heatmap td{min-width:24px;text-align:center}details{margin:16px 0}summary{cursor:pointer;font-weight:600}@media(max-width:600px){main{padding:12px}section{padding:12px}h1{font-size:24px}}</style></head><body><main>",
             "<h1>小时渠道诊断</h1><p>生成时间：" + html.escape(now_utc().isoformat()) + "</p>",
             "<p>成功率 = 有效响应 / 全部请求；正确率 = 答对 / 有效答题样本；回显匹配率仅统计 Responses 返回档位的样本，Chat 为不适用。Juice 验证率 = 期望值精确匹配 / 全部请求（包括失败和无法解析），每档达到 60% 标为 verified。该结果仅说明模型自报值与原脚本预设值一致。</p>",
             "<p>Tokens 仅累计 usage.total_tokens；覆盖数不足时为部分小计，缺失显示 —，真实零显示 0。推理 Tokens 区分缺字段、空值和数值 0。旧版记录缺少可靠元数据，不参与总 Tokens 和回显匹配统计。运行 completed 表示矩阵执行完毕，不代表渠道全部通过。</p>"]
    parts.append("<section><h2>运行记录</h2>" + report_table(
        ["运行", "模式", "开始 UTC", "结束 UTC", "执行状态"],
        [[display(r[k]) for k in ("id",)] + ["Mock" if r["mock"] else "真实"] + [display(r[k]) for k in ("started_at", "finished_at", "status")] for r in runs], "runs") + "</section>")
    parts.append("<section><h2>小时概览</h2>" + svg_heatmap(data, timezone_name))
    overview = []
    for r in reversed(data):
        overview.append([display(r["hour"]), html.escape(series_name(r)), display(r["requests"]), display(r["success_rate"]),
                         display(r["accuracy"]), display(r["match_rate"]), display(r["verification_rate"]),
                         display(r["median_latency_ms"]), display(r["tokens"]), f"{r['token_samples']}/{r['requests']}"])
    parts.append(report_table(["小时", "模式 / 渠道 / 包", "请求", "成功 %", "正确 %", "回显/值匹配 %", "Juice 验证 %", "延迟中位 ms", "已报告 Tokens", "Tokens 覆盖"], overview, "overview") + "</section>")
    issues = []
    for r in reversed(details):
        reasons = []
        if r["success_rate"] < 100:
            reasons.append("存在请求失败")
        if r["accuracy"] is not None and r["accuracy"] < 100:
            reasons.append("存在错误或不匹配回答")
        if r["match_rate"] is not None and r["match_rate"] < 100:
            reasons.append("存在不匹配")
        if r["package"] == "reasoning" and r["surface"] == "responses" and r["match_samples"] < r["successes"]:
            reasons.append("部分回显缺失，无法判断")
        if r["verification_status"] == "inconclusive":
            reasons.append("Juice 证据不足")
        if r["legacy_samples"]:
            reasons.append("旧版元数据不完整")
        if reasons:
            issues.append([display(r["hour"]), html.escape(series_name(r)), html.escape("；".join(reasons)), distribution(r["error_distribution"])])
    parts.append("<section><h2>异常与待确认小时</h2>" + report_table(["小时", "模式 / 渠道 / 包 / 接口 / 档位", "原因", "错误类别"], issues, "issues") + "</section>")
    detail_rows = []
    for r in reversed(details):
        detail_rows.append([display(r["hour"]), html.escape(series_name(r)), f"{r['successes']}/{r['requests']}",
                           f"{r['correct_count']}/{r['correct_samples']}" if r["correct_samples"] else "—",
                           f"{r['matching_responses']}/{r['match_samples']}" if r["match_samples"] else "不适用 / 未返回",
                           distribution(r["echo_distribution"]), display(r["median_reasoning_tokens"]),
                           f"{r['reasoning_token_samples']}/{r['requests']}", str(r["reasoning_field_samples"]), str(r["reasoning_null_samples"]),
                           display(r["expected"]), distribution(r["juice_distribution"]),
                           f"{r['parsed_samples']}/{r['requests']}" if r["package"] == "juice" else "—",
                           f"{r['matching_responses']}/{r['requests']} (需 {r['required_matches']})" if r["package"] == "juice" else "—",
                           display(r["verification_status"]), display(r["tokens"]), f"{r['token_samples']}/{r['requests']}"])
    parts.append("<section><h2>逐接口、逐档位诊断</h2>" + report_table(
        ["小时", "模式 / 渠道 / 包 / 接口 / 档位", "成功/请求", "答对/有效", "匹配/可判定", "回显分布", "推理 Tokens 中位", "推理数值/请求", "字段返回数", "空值数", "Juice 期望", "Juice 观测分布", "可解析/请求", "Juice 匹配/请求", "Juice 结论", "已报告 Tokens", "Tokens 覆盖"], detail_rows, "details") + "</section>")
    chart_groups = defaultdict(list)
    for r in details:
        chart_groups[(r["mock"], r["channel"], r["package"], r["surface"])].append(r)
    for (mock, channel, package, surface), group in sorted(chart_groups.items()):
        title = html.escape(f"{'Mock' if mock else '真实'} / {channel} / {package} / {surface}")
        parts.append(f"<section><details open><summary>档位趋势：{title}</summary>")
        for metric, title in (("success_rate", "成功率 %"), ("accuracy", "正确率 %"), ("match_rate", "匹配率 %"),
                              ("verification_rate", "Juice 验证率 %"), ("median_reasoning_tokens", "推理 Tokens 中位数"), ("median_latency_ms", "延迟中位数 ms")):
            if metric == "verification_rate" and package != "juice":
                continue
            if metric == "match_rate" and package == "reasoning" and surface == "chat":
                continue
            parts.append(svg_line(group, metric, title))
        parts.append("</details></section>")
    parts.append("</main></body></html>")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                     prefix=output.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write("".join(parts))
            handle.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def next_hour_sleep(timezone_name: str, current: datetime | None = None) -> float:
    zone = ZoneInfo(timezone_name)
    current = current or now_utc()
    stamp = current.timestamp()
    # 在 UTC 时间线上检查本地整点，覆盖夏令时重复小时和半小时偏移时区。
    candidate = math.floor(stamp / 60) * 60 + 60
    for _ in range(180):
        local = datetime.fromtimestamp(candidate, zone)
        if local.minute == 0:
            return max(1.0, candidate - stamp)
        candidate += 60
    raise RuntimeError("无法计算下一个整点")


def inspect_config(config: dict[str, Any]) -> None:
    """输出可交接的脱敏配置摘要，不读取或打印 API Key。"""
    channels = config.get("channels", [])
    print(json.dumps({
        "channels": [
            {
                "name": str(item.get("name", "unnamed")),
                "enabled": bool(item.get("enabled", True)),
                "protocol": str(item.get("protocol", "openai")),
                "host_fingerprint": hashlib.sha256((urllib.parse.urlsplit(item["base_url"]).hostname or "").encode()).hexdigest()[:12],
                "model": str(item.get("model", config.get("model", "gpt-5.6-sol"))),
                "api_key_env": str(item.get("api_key_env", "OPENAI_API_KEY")),
            }
            for item in channels
        ],
        "rounds": config["rounds"],
        "juice_runs": config["juice_runs"],
        "requests_per_channel": config["rounds"] * 6 + config["juice_runs"] * 5,
        "reasoning_timeout_seconds": config["reasoning_timeout"],
        "juice_timeout_seconds": config["juice_timeout"],
        "timezone": config["timezone"],
        "retention_days": config["retention_days"],
        "config_fingerprint": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "checked_at": now_utc().isoformat(),
    }, ensure_ascii=False, indent=2))


def command_main() -> int:
    parser = argparse.ArgumentParser(description="每小时渠道稳定性诊断（独立版）")
    parser.add_argument("command", choices=("inspect", "run-once", "daemon", "report"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--mock", action="store_true", help="只使用本地确定性 Mock，不访问真实渠道")
    parser.add_argument("--confirm-live", action="store_true", help="确认允许向配置中的真实渠道发起请求")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    data_dir = args.data_dir or Path(config["data_dir"])
    db_path = data_dir / "diagnostic.sqlite3"
    report_path = args.output or data_dir / "report.html"
    if args.command == "inspect":
        inspect_config(config)
        return 0
    for path in (data_dir, report_path):
        if path.resolve().is_relative_to(ROOT):
            raise ValueError("运行数据与报告必须写入源码目录以外的位置")
    if args.command == "report":
        build_report(db_path, report_path, str(config["timezone"]))
        print(f"报告：{report_path}")
        return 0
    if args.command == "run-once":
        if not args.mock and not args.confirm_live:
            raise SystemExit("真实渠道运行需要显式添加 --confirm-live；本地验收请添加 --mock")
        run_once(config, db_path, args.mock)
        build_report(db_path, report_path, str(config["timezone"]))
        print(f"报告：{report_path}")
        return 0
    if not args.mock and not args.confirm_live:
        raise SystemExit("真实渠道运行需要显式添加 --confirm-live；本地验收请添加 --mock")
    print("小时调度已启动；首次执行将在下一个整点。Ctrl-C 停止。", flush=True)
    with run_lock(db_path.with_name("scheduler.sqlite3")):
        while True:
            time.sleep(next_hour_sleep(str(config["timezone"])))
            try:
                run_once(config, db_path, args.mock)
                build_report(db_path, report_path, str(config["timezone"]))
            except Exception as exc:
                print(f"本轮失败类别：{type(exc).__name__}", file=sys.stderr, flush=True)


def main() -> int:
    try:
        return command_main()
    except KeyboardInterrupt:
        print("诊断已停止；已完成的样本已保存。", file=sys.stderr)
        return 130
    except (ValueError, TypeError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"执行失败类别：{type(exc).__name__}；请检查配置、密钥环境变量与数据目录。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
