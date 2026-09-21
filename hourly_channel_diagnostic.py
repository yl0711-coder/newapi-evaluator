#!/usr/bin/env python3
"""独立的小时渠道稳定性诊断器。

默认每小时对配置中的每个渠道执行附件脚本的完整矩阵：
reasoning（responses/chat-completions × 3 档位 × rounds）和
Juice（5 档位 × runs）。结果只写入本目录的 SQLite 和静态 HTML 报告。
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_DATA = ROOT / "data"
DEFAULT_TIMEZONE = "Asia/Shanghai"
REASONING_LEVELS = ("low", "medium", "high")
JUICE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EXPECTED_JUICE = {"low": 8, "medium": 16, "high": 40, "xhigh": 128, "max": 960}
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
    config = json.loads(path.read_text(encoding="utf-8"))
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
        raise SystemExit(f"timezone 无效：{config['timezone']}") from exc
    if config["rounds"] < 1 or config["juice_runs"] < 1:
        raise SystemExit("rounds 和 juice_runs 必须大于 0")
    if not isinstance(config["channels"], list):
        raise SystemExit("channels 必须是数组")
    for field in ("timeout", "reasoning_timeout", "juice_timeout"):
        try:
            if float(config[field]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"{field} 必须是大于 0 的秒数") from exc
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
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": "hourly-channel-diagnostic/1.0",
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            status_code = int(response.status)
            if status_code != 200:
                return {"ok": False, "status_code": status_code, "error": f"http_{status_code}",
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict):
            return {"ok": False, "status_code": status_code, "error": "invalid_response",
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        return {"ok": True, "status_code": status_code, "body": body,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status_code": exc.code, "error": f"http_{exc.code}",
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "status_code": None, "error": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000)}


def response_fields(body: dict[str, Any], surface: str) -> dict[str, Any]:
    usage = body.get("usage") or {}
    if surface == "responses":
        detail = usage.get("output_tokens_details") or {}
        answer = "".join(
            str(part.get("text", ""))
            for item in (body.get("output") or [])
            if isinstance(item, dict) and item.get("type") == "message"
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("text")
        )
        reasoning = body.get("reasoning") or {}
        return {"answer": answer, "echoed_effort": reasoning.get("effort"),
                "reasoning_tokens": detail.get("reasoning_tokens"),
                "total_tokens": usage.get("total_tokens") or usage.get("output_tokens")}
    detail = usage.get("completion_tokens_details") or {}
    choices = body.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    content = message.get("content") or message.get("reasoning_content") or ""
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return {"answer": str(content), "echoed_effort": None,
            "reasoning_tokens": detail.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens") or usage.get("completion_tokens")}


def exact_number(text: str, expected: str) -> bool:
    return bool(re.fullmatch(r"\s*" + re.escape(expected) + r"(?:\.0+)?\s*", text or ""))


def juice_value(text: str) -> int | None:
    match = re.fullmatch(r"\s*([+-]?\d+)(?:\.0+)?\s*", text or "")
    return int(match.group(1)) if match else None


def mock_call(surface: str, effort: str) -> dict[str, Any]:
    if surface == "responses":
        return {"ok": True, "status_code": 200, "latency_ms": 80 + REASONING_LEVELS.index(effort) * 30,
                "body": {"model": "mock", "reasoning": {"effort": effort},
                         "output": [{"type": "message", "content": [{"text": "14"}]}],
                         "usage": {"total_tokens": 50, "output_tokens_details":
                                    {"reasoning_tokens": 20 + REASONING_LEVELS.index(effort) * 20}}}}
    expected = EXPECTED_JUICE[effort]
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
           "error": outcome.get("error")}
    if not outcome.get("ok"):
        return row
    fields = response_fields(outcome["body"], surface)
    row["total_tokens"] = fields.get("total_tokens")
    row["reasoning_tokens"] = fields.get("reasoning_tokens")
    if package == "reasoning":
        row["correct"] = int(exact_number(fields.get("answer", ""), "14"))
        row["matched"] = int(surface == "chat" or fields.get("echoed_effort") == effort)
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
    conn.commit()
    return conn


def save_observation(conn: sqlite3.Connection, run_id: int, row: dict[str, Any]) -> None:
    fields = ["run_id", "channel", "package", "surface", "effort", "round_no", "timestamp",
              "hour_key", "ok", "correct", "matched", "status_code", "latency_ms",
              "reasoning_tokens", "observed_juice", "total_tokens", "error"]
    conn.execute("INSERT INTO observations (" + ",".join(fields) + ") VALUES (" + ",".join("?" * len(fields)) + ")",
                 [run_id] + [row.get(field) for field in fields[1:]])


def run_once(config: dict[str, Any], db_path: Path, mock: bool = False) -> int:
    channels = [item for item in config.get("channels", []) if item.get("enabled", True)]
    if not channels:
        raise SystemExit("config.json 没有 enabled 渠道")
    conn = connect(db_path)
    started = now_utc()
    timezone_name = str(config["timezone"])
    cur = conn.execute("INSERT INTO runs (started_at, hour_key, mock, status, channels) VALUES (?,?,?,?,?)",
                       (started.isoformat(), hour_key(started, timezone_name), int(mock), "running", len(channels)))
    run_id = cur.lastrowid
    try:
        for channel in channels:
            name = str(channel.get("name", "unnamed"))
            key_env = str(channel.get("api_key_env", "OPENAI_API_KEY"))
            key = os.environ.get(key_env, "")
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
                        outcome = mock_call(surface, effort) if mock else call_json(url, key, payload, request_timeout)
                        timestamp = now_utc().isoformat()
                        save_observation(conn, run_id, make_observation(name, "reasoning", surface, effort, round_no, outcome, timestamp, timezone_name))
            for juice_round in range(1, int(config["juice_runs"]) + 1):
                offset = (juice_round - 1) % len(JUICE_EFFORTS)
                round_efforts = JUICE_EFFORTS[offset:] + JUICE_EFFORTS[:offset]
                for effort in round_efforts:
                    prompt = JUICE_PROMPTS[(juice_round - 1) % len(JUICE_PROMPTS)]
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                               "stream": False, "reasoning_effort": effort}
                    outcome = mock_call("chat", effort) if mock else call_json(chat_url, key, payload, config["juice_timeout"])
                    timestamp = now_utc().isoformat()
                    save_observation(conn, run_id, make_observation(name, "juice", "chat", effort, juice_round, outcome, timestamp, timezone_name))
            conn.commit()
            print(f"完成渠道：{name}", flush=True)
        conn.execute("UPDATE runs SET finished_at=?, status='completed' WHERE id=?", (now_utc().isoformat(), run_id))
        conn.commit()
    except Exception as exc:
        conn.execute("UPDATE runs SET finished_at=?, status='failed', error=? WHERE id=?", (now_utc().isoformat(), str(exc)[:500], run_id))
        conn.commit()
        raise
    finally:
        cutoff = (now_utc() - timedelta(days=int(config.get("retention_days", 14)))).isoformat()
        conn.execute("DELETE FROM observations WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        conn.commit()
        conn.close()
    return int(run_id)


def aggregate(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    rows = conn.execute("SELECT * FROM observations ORDER BY timestamp").fetchall()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(row["hour_key"], row["channel"], row["package"])].append(row)
    result = []
    for (hour, channel, package), values in groups.items():
        ok = [r for r in values if r["ok"]]
        correct = [r for r in ok if r["correct"] is not None]
        matched = [r for r in ok if r["matched"] is not None]
        latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]
        result.append({"hour": hour, "channel": channel, "package": package,
                       "requests": len(values), "successes": len(ok),
                       "success_rate": round(100 * len(ok) / len(values), 2) if values else None,
                       "accuracy": round(100 * sum(r["correct"] for r in correct) / len(correct), 2) if correct else None,
                       "match_rate": round(100 * sum(r["matched"] for r in matched) / len(matched), 2) if matched else None,
                       "median_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
                       "tokens": sum(r["total_tokens"] or 0 for r in values)})
    conn.close()
    return result


def svg_line(data: list[dict[str, Any]], metric: str, title: str) -> str:
    series = sorted({(r["channel"], r["package"]) for r in data})
    hours = sorted({r["hour"] for r in data})
    values = [r[metric] for r in data if r.get(metric) is not None]
    if not values:
        return f"<h3>{html.escape(title)}</h3><p>暂无可用数据</p>"
    width, height, left, top, plot_w, plot_h = 900, 300, 55, 30, 810, 220
    lo, hi = (0, 100) if metric in ("success_rate", "accuracy", "match_rate") else (0, max(values) * 1.15 or 1)
    def xy(index: int, value: float) -> tuple[float, float]:
        x = left + (plot_w * index / max(1, len(hours) - 1))
        y = top + plot_h - (float(value) - lo) / max(1, hi - lo) * plot_h
        return x, y
    parts = [f"<h3>{html.escape(title)}</h3><svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)}'>",
             f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top+plot_h}' stroke='#94a3b8'/>",
             f"<line x1='{left}' y1='{top+plot_h}' x2='{left+plot_w}' y2='{top+plot_h}' stroke='#94a3b8'/>"]
    colors = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2")
    for ci, (channel, package) in enumerate(series):
        points = []
        for i, hour in enumerate(hours):
            candidates = [r for r in data if r["channel"] == channel and r["package"] == package
                          and r["hour"] == hour and r.get(metric) is not None]
            if candidates:
                points.append(xy(i, candidates[-1][metric]))
        if points:
            coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            color = colors[ci % len(colors)]
            parts.append(f"<polyline points='{coords}' fill='none' stroke='{color}' stroke-width='2'/>")
            label = f"{channel}/{package}"
            parts.append(f"<text x='{left+8+ci*150}' y='18' fill='{color}' font-size='12'>{html.escape(label[:24])}</text>")
    parts.append(f"<text x='5' y='{top+8}' fill='#475569' font-size='11'>{hi:g}</text><text x='20' y='{top+plot_h}' fill='#475569' font-size='11'>{lo:g}</text></svg>")
    return "".join(parts)


def svg_heatmap(data: list[dict[str, Any]], timezone_name: str) -> str:
    series = sorted({(r["channel"], r["package"]) for r in data})
    cells: dict[tuple[tuple[str, str], int], list[int]] = defaultdict(lambda: [0, 0])
    for row in data:
        hour = int(parse_iso(row["hour"].replace("Z", "+00:00")).hour)
        cell = cells[((row["channel"], row["package"]), hour)]
        cell[0] += int(row["requests"] or 0)
        cell[1] += int(row.get("successes", 0) or 0)
    if not series:
        return "<p>暂无热力图数据</p>"
    cell = 28; left = 145; top = 28; width = left + 24 * cell + 10; height = top + len(series) * cell + 10
    parts = [f"<h3>按小时成功率热力图（{html.escape(timezone_name)}）</h3><svg viewBox='0 0 {width} {height}' role='img' aria-label='成功率热力图'>"]
    for h in range(24):
        parts.append(f"<text x='{left+h*cell+8}' y='18' font-size='9' fill='#475569'>{h:02d}</text>")
    for ci, (channel, package) in enumerate(series):
        y = top + ci * cell
        parts.append(f"<text x='2' y='{y+18}' font-size='10' fill='#334155'>{html.escape((channel + '/' + package)[:22])}</text>")
        for h in range(24):
            request_count, success_count = cells.get(((channel, package), h), (0, 0))
            value = 100 * success_count / request_count if request_count else None
            color = "#e2e8f0" if value is None else ("#fee2e2" if value < 80 else ("#fef3c7" if value < 95 else "#dcfce7"))
            label = "无数据" if value is None else f"{value:.1f}%"
            parts.append(f"<rect x='{left+h*cell}' y='{y}' width='{cell-2}' height='{cell-2}' rx='2' fill='{color}'><title>{html.escape(channel)}/{html.escape(package)} {h:02d}:00 {label}</title></rect>")
    return "".join(parts) + "</svg>"


def build_report(db_path: Path, output: Path, timezone_name: str) -> None:
    data = aggregate(db_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = "".join((svg_line(data, metric, title) for metric, title in (("success_rate", "成功率"), ("accuracy", "正确率/验证率"), ("match_rate", "匹配率"), ("median_latency_ms", "中位延迟（ms）"))))
    body += svg_heatmap(data, timezone_name)
    rows = "".join(f"<tr><td>{html.escape(r['hour'])}</td><td>{html.escape(r['channel'])}</td><td>{r['package']}</td><td>{r['requests']}</td><td>{r['success_rate'] if r['success_rate'] is not None else '-'}</td><td>{r['accuracy'] if r['accuracy'] is not None else '-'}</td><td>{r['match_rate'] if r['match_rate'] is not None else '-'}</td><td>{r['median_latency_ms'] if r['median_latency_ms'] is not None else '-'}</td><td>{r['tokens'] or '-'}</td></tr>" for r in reversed(data))
    anomalies = [r for r in data if (r.get("success_rate") is not None and r["success_rate"] < 100)
                 or (r.get("match_rate") is not None and r["match_rate"] < 100)
                 or (r.get("accuracy") is not None and r["accuracy"] < 100)]
    anomaly_rows = "".join(f"<tr><td>{html.escape(r['hour'])}</td><td>{html.escape(r['channel'])}</td><td>{r['package']}</td><td>{r['success_rate'] if r['success_rate'] is not None else '-'}</td><td>{r['match_rate'] if r['match_rate'] is not None else '-'}</td><td>{r['accuracy'] if r['accuracy'] is not None else '-'}</td><td>成功或匹配指标低于 100%</td></tr>" for r in reversed(anomalies)) or "<tr><td colspan='7'>当前没有低于 100% 的成功率、正确率或匹配率记录。</td></tr>"
    output.write_text("<!doctype html><meta charset='utf-8'><title>小时渠道异常诊断</title><style>body{font:14px system-ui;margin:24px;color:#1e293b}section{margin:24px 0;padding:16px;border:1px solid #e2e8f0;border-radius:8px}svg{width:100%;max-width:900px;background:#f8fafc}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #e2e8f0;padding:6px;text-align:left}</style><h1>小时渠道异常诊断</h1><p>生成时间：" + html.escape(now_utc().isoformat()) + "；缺失值保留为 -；Tokens 是接口返回 usage 的累计值。</p><section>" + body + "</section><section><h3>异常小时</h3><table><tr><th>小时</th><th>渠道</th><th>包</th><th>成功率</th><th>匹配率</th><th>正确率</th><th>判定</th></tr>" + anomaly_rows + "</table></section><section><h3>原始聚合</h3><table><tr><th>小时</th><th>渠道</th><th>包</th><th>请求数</th><th>成功率</th><th>正确率</th><th>匹配率</th><th>中位延迟 ms</th><th>Tokens</th></tr>" + rows + "</table></section>", encoding="utf-8")


def next_hour_sleep(timezone_name: str) -> float:
    zone = ZoneInfo(timezone_name)
    current = datetime.now(zone)
    next_hour = (current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max(1.0, next_hour.timestamp() - time.time())


def inspect_config(config: dict[str, Any]) -> None:
    """输出可交接的脱敏配置摘要，不读取或打印 API Key。"""
    channels = config.get("channels", [])
    print(json.dumps({
        "channels": [
            {
                "name": str(item.get("name", "unnamed")),
                "enabled": bool(item.get("enabled", True)),
                "protocol": str(item.get("protocol", "openai")),
                "host": urllib.parse.urlsplit(str(item.get("base_url", ""))).hostname or "-",
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
    }, ensure_ascii=False, indent=2))


def main() -> int:
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
    if args.command == "report":
        build_report(db_path, report_path, str(config["timezone"]))
        print(f"报告：{report_path}")
        return 0
    if args.command == "run-once":
        if not args.mock and not args.confirm_live:
            raise SystemExit("真实渠道运行需要显式添加 --confirm-live；本地验收请添加 --mock")
        try:
            run_once(config, db_path, args.mock)
        except (RuntimeError, ValueError) as exc:
            print(f"本轮失败：{exc}", file=sys.stderr)
            return 1
        build_report(db_path, report_path, str(config["timezone"]))
        print(f"报告：{report_path}")
        return 0
    if not args.mock and not args.confirm_live:
        raise SystemExit("真实渠道运行需要显式添加 --confirm-live；本地验收请添加 --mock")
    print("小时调度已启动；首次执行将在下一个整点。Ctrl-C 停止。", flush=True)
    while True:
        time.sleep(next_hour_sleep(str(config["timezone"])))
        try:
            run_once(config, db_path, args.mock)
            build_report(db_path, report_path, str(config["timezone"]))
        except Exception as exc:
            print(f"本轮失败：{exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
