"""签名告警、幂等合并、证据与保守归因自测。"""
import hashlib
import hmac
import json
import os
import time

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "127.0.0.1,localhost")

from fastapi.testclient import TestClient  # noqa: E402

from app import main, metric_store, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


def signed(secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {"X-Monitor-Timestamp": str(timestamp), "X-Monitor-Signature": digest,
            "Content-Type": "application/json"}


def main_test() -> None:
    secret = "monitor-secret-2026"
    with TestClient(main.app) as client:
        login(client)
        probe_list = client.get("/api/incidents/probe-locations")
        check("静态探针路由不会被事件 ID 路由误匹配",
              probe_list.status_code == 200 and probe_list.json() == [],
              probe_list.text)
        source_response = client.post("/api/incidents/sources", json={
            "name": "监测平台", "secret": secret, "enabled": True,
        })
        source = source_response.json()
        check("可创建独立签名监测告警源",
              source_response.status_code == 200 and source["secret_masked"] != secret,
              source_response.text)

        now = int(time.time())
        metric_source_id = metric_store.insert("metric_sources", {
            "name": "事件证据", "endpoint": "http://127.0.0.1:8090/metrics",
            "token_enc": "", "protocol_version": "1", "enabled": 0,
            "poll_interval_seconds": 60, "cursor": "", "last_error": "",
            "created_at": now, "updated_at": now,
        })
        metric_store.insert("metric_buckets", {
            "source_id": metric_source_id,
            "bucket_start": now // 60 * 60, "platform_group": "Codex 1x",
            "model_family": "Codex", "model": "gpt-5.6-sol",
            "usage_profile": "coding", "channel": "告警渠道",
            "supply_source": "", "output_length_band": "medium",
            "request_count": 100, "attempt_count": 110, "success_count": 10,
            "failure_count": 100, "retry_count": 10, "failover_count": 0,
            "auth_error_count": 0, "rate_limit_count": 100,
            "timeout_count": 0, "stream_break_count": 0,
            "upstream_5xx_count": 0, "network_error_count": 0,
            "protocol_error_count": 0, "input_tokens": 0, "output_tokens": 0,
            "active_users": 8, "received_at": now, "updated_at": now,
        })
        alert = {
            "event_id": "evt-001", "event_time": now,
            "channel": "告警渠道", "model": "gpt-5.6-sol",
            "platform_group": "Codex 1x", "symptom": "HTTP 429 spike",
            "severity": "critical", "affected_users": 8,
            "metadata": {"window": "5m", "prompt": "must-not-store",
                         "route_attempts": [{
                             "chain_id": "chain-001", "happened_at": now,
                             "anonymous_user_hash": "h1:" + "a" * 64,
                             "attempts": [{"channel": "告警渠道", "status_code": 429}],
                         }]},
        }
        body = json.dumps(alert, ensure_ascii=False, separators=(",", ":")).encode()
        client.cookies.clear()
        rejected = client.post(
            f"/api/incidents/webhook/{source['id']}", content=body,
            headers={"X-Monitor-Timestamp": str(now), "X-Monitor-Signature": "bad",
                     "Content-Type": "application/json"},
        )
        check("错误签名告警被拒绝", rejected.status_code == 401, rejected.text)
        accepted = client.post(
            f"/api/incidents/webhook/{source['id']}", content=body,
            headers=signed(secret, body, now),
        )
        check("签名 Webhook 无需登录但必须验签",
              accepted.status_code == 200 and accepted.json()["status"] == "accepted",
              accepted.text)
        duplicate = client.post(
            f"/api/incidents/webhook/{source['id']}", content=body,
            headers=signed(secret, body, now),
        )
        check("相同事件 ID 幂等去重",
              duplicate.json()["status"] == "duplicate"
              and duplicate.json()["incident_id"] == accepted.json()["incident_id"],
              duplicate.text)

        login(client)
        detail = client.get(f"/api/incidents/{accepted.json()['incident_id']}").json()
        check("告警窗口会关联分钟指标并形成证据",
              any(item["evidence_type"] == "minute_metrics" for item in detail["evidence"]),
              detail)
        route_rows = store.query(
            "SELECT * FROM incident_route_chains WHERE incident_id=?", (detail["id"],)
        )
        route_evidence = next(
            item for item in detail["evidence"] if item["evidence_type"] == "route_chain"
        )
        check("验签告警可携带限量匿名路由链并只保留 30 天",
              len(route_rows) == 1 and route_rows[0]["expires_at"] > now + 29 * 86400
              and route_evidence["detail"]["available"], route_rows)
        check("429 证据归因为上游限流并保留人工复核",
              detail["attribution"]["cause"] == "upstream_rate_limit"
              and detail["attribution"]["needs_human_review"], detail["attribution"])
        check("归因明确禁止自动停用、切路由或封禁用户",
              "不会自动" in detail["attribution"]["automation_boundary"],
              detail["attribution"])
        raw_alert = store.query(
            "SELECT metadata_json FROM incident_alerts WHERE incident_id=?",
            (detail["id"],),
        )[0]["metadata_json"]
        check("告警敏感正文不会进入事件证据", "must-not-store" not in raw_alert, raw_alert)

        unsafe = {**alert, "event_id": "evt-unsafe", "metadata": {
            "route_attempts": [{"chain_id": "bad", "happened_at": now,
                                "user_id": "raw-user"}],
        }}
        unsafe_body = json.dumps(unsafe, ensure_ascii=False, separators=(",", ":")).encode()
        client.cookies.clear()
        unsafe_response = client.post(
            f"/api/incidents/webhook/{source['id']}", content=unsafe_body,
            headers=signed(secret, unsafe_body, now),
        )
        check("匿名路由链出现原始用户 ID 时整条拒绝",
              unsafe_response.status_code == 400, unsafe_response.text)

    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
