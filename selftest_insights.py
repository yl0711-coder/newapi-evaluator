"""生产指标增量、健康、排行、供给缺口和保留策略自测。"""
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


def metric_bucket(
    bucket_start: int, channel: str, attempts: int, successes: int, *,
    requests: int = 0, latency: float = 800, ttft: float = 100,
    generation_tps: float = 50, active_users: int = 20,
) -> dict:
    return {
        "bucket_start": bucket_start,
        "platform_group": "Codex 1x",
        "model_family": "Codex",
        "model": "gpt-5.6-sol",
        "usage_profile": "coding",
        "channel": channel,
        "supply_source": "供应商-A" if channel in {"稳定渠道", "快速渠道"} else "",
        "output_length_band": "medium",
        "request_count": requests,
        "attempt_count": attempts,
        "success_count": successes,
        "failure_count": attempts - successes,
        "timeout_count": 0,
        "stream_break_count": 0,
        "latency_p95_ms": latency,
        "ttft_p95_ms": ttft,
        "generation_tps": generation_tps,
        "active_users": active_users,
        "top_user_request_share": .1,
    }


def main_test() -> None:
    now_minute = int(time.time()) // 60 * 60
    days = [now_minute - offset * 86400 for offset in (0, 1, 2)]
    with TestClient(main.app) as client:
        login(client)
        source_response = client.post("/api/insights/sources", json={
            "name": "只读指标测试源",
            "endpoint": "http://127.0.0.1:8090/api/metrics/minute",
            "protocol_version": "1",
            "enabled": False,
            "poll_interval_seconds": 60,
        })
        source = source_response.json()
        check("可配置版本化只读指标源", source_response.status_code == 200
              and source["protocol_version"] == "1" and not source["enabled"], source)

        buckets = []
        for bucket_start in days:
            buckets.append(metric_bucket(
                bucket_start, "稳定渠道", 100, 99, requests=100,
                latency=800, ttft=100, generation_tps=50,
            ))
            buckets.append(metric_bucket(
                bucket_start, "快速渠道", 100, 98,
                latency=400, ttft=50, generation_tps=80,
            ))
        buckets.append(metric_bucket(
            days[0], "低样本渠道", 10, 10,
            latency=200, ttft=20, generation_tps=120,
        ))
        buckets.append(metric_bucket(
            now_minute - 120, "稳定渠道", 10, 10,
            latency=790, ttft=98, generation_tps=51,
        ))
        payload = {"version": "1", "next_cursor": "cursor-1", "buckets": buckets}
        first = client.post(
            f"/api/insights/sources/{source['id']}/ingest", json=payload)
        check("分钟桶可批量增量入库", first.status_code == 200
              and first.json()["upserted_count"] == 8, first.text)
        check("生产指标写入独立 SQLite 而非业务库",
              metric_store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"] == 8
              and store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"] == 0)
        row_count = metric_store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"]
        repeated = client.post(
            f"/api/insights/sources/{source['id']}/ingest", json=payload)
        check("游标重复拉取不会重复入库", repeated.status_code == 200
              and metric_store.query("SELECT COUNT(*) n FROM metric_buckets")[0]["n"] == row_count)

        replacement = metric_bucket(
            days[1], "稳定渠道", 120, 119, requests=120,
            latency=780, ttft=95, generation_tps=52,
        )
        out_of_order = client.post(
            f"/api/insights/sources/{source['id']}/ingest", json={
                "version": "1", "next_cursor": "cursor-2", "buckets": [replacement],
            })
        updated = metric_store.query(
            "SELECT attempt_count FROM metric_buckets WHERE source_id=? "
            "AND bucket_start=? AND channel='稳定渠道'",
            (source["id"], days[1]),
        )[0]
        check("乱序到达会合并到原分钟桶", out_of_order.status_code == 200
              and updated["attempt_count"] == 120 and metric_store.query(
                  "SELECT COUNT(*) n FROM metric_buckets")[0]["n"] == row_count, updated)

        forbidden = {**metric_bucket(days[0], "泄漏渠道", 1, 1), "prompt": "secret"}
        blocked = client.post(
            f"/api/insights/sources/{source['id']}/ingest", json={
                "version": "1", "next_cursor": "bad", "buckets": [forbidden],
            })
        check("包含 Prompt 等敏感字段的指标协议被拒绝", blocked.status_code == 400,
              blocked.text)

        overview = client.get("/api/insights/overview?minutes=5").json()
        check("实时健康展示请求、成功率、P95 与 TTFT",
              overview["rows"] and all(
                  key in overview["rows"][0] for key in (
                      "request_count", "success_rate", "latency_p95_ms", "ttft_p95_ms")),
              overview)
        quality = overview["collection_quality"][0]
        check("缺失分钟桶可检测并给出完整率",
              quality["missing_count"] > 0 and quality["completeness"] < 1, quality)
        filtered = client.get(
            "/api/insights/overview?minutes=5&channel=稳定渠道").json()
        check("健康数据支持渠道筛选",
              filtered["rows"] and {row["channel"] for row in filtered["rows"]} == {"稳定渠道"})

        rankings = client.get("/api/insights/rankings?days=7").json()
        check("需求排行单独按组输出前三",
              rankings["demand_top"] and rankings["demand_top"][0]["model"] == "gpt-5.6-sol")
        check("稳定排行使用样本门槛和置信下界",
              rankings["stability_top"][0]["channel"] == "稳定渠道"
              and "success_rate_lower_bound" in rankings["stability_top"][0],
              rankings["stability_top"])
        check("速度排行只在同模型用途和输出长度内比较",
              rankings["speed_top"][0]["channel"] == "快速渠道"
              and rankings["speed_top"][0]["output_length_band"] == "medium",
              rankings["speed_top"])
        check("低样本渠道不进入正式排行",
              any(row["channel"] == "低样本渠道" for row in rankings["insufficient"])
              and not any(row["channel"] == "低样本渠道"
                          for row in rankings["stability_top"] + rankings["speed_top"]))

        gaps = client.post("/api/insights/supply-gaps/refresh").json()
        gap = next(row for row in gaps if row["model"] == "gpt-5.6-sol")
        check("关键高需求按三个低相关合格渠道计算缺口",
              gap["required_channels"] == 3 and gap["qualified_channels"] == 2,
              gap)
        check("相同申报供应来源只提示疑似共同故障域",
              gap["suspected_fault_domains"]
              and "疑似" in gap["suspected_fault_domains"][0]["assessment"], gap)

        channel = client.post("/api/channels", json={
            "name": "复盘新渠道", "base_url": "http://127.0.0.1:8091/v1",
            "api_key": "sk-review-test", "protocol": "openai",
        }).json()
        review_response = client.post(
            f"/api/insights/supply-gaps/{gap['id']}/reviews", json={
                "channel_id": channel["id"], "production_channel": "复盘新渠道",
                "activated_at": time.time() - 31 * 86400,
            },
        )
        check("供给建议可关联真实上线渠道并安排 7/30 天复盘",
              review_response.status_code == 200
              and review_response.json()["status"] == "scheduled", review_response.text)
        review_id = review_response.json()["id"]
        seven = client.post(
            f"/api/insights/recommendation-reviews/{review_id}/run?horizon_days=7"
        ).json()
        thirty = client.post(
            f"/api/insights/recommendation-reviews/{review_id}/run?horizon_days=30"
        ).json()
        check("复盘证据不足时明确标记且不伪造效果",
              seven["review_7d"]["classification"] == "insufficient_evidence"
              and thirty["review_30d"]["classification"] == "insufficient_evidence",
              thirty)
        check("30 天复盘完成后保留前后指标与四项差值",
              thirty["status"] == "completed"
              and set(thirty["review_30d"]["deltas"]) == {
                  "success_rate", "latency_p95_ms", "switch_rate", "concentration"},
              thirty)

        old_start = now_minute - 31 * 86400
        old_payload = {"version": "1", "next_cursor": "cursor-3", "buckets": [
            metric_bucket(old_start, "历史渠道", 20, 20),
        ]}
        client.post(f"/api/insights/sources/{source['id']}/ingest", json=old_payload)
        retention = client.post("/api/insights/retention/run").json()
        old_count = metric_store.query(
            "SELECT COUNT(*) n FROM metric_buckets WHERE bucket_start=?", (old_start,)
        )[0]["n"]
        day_rollup = metric_store.query(
            "SELECT COUNT(*) n FROM daily_metrics WHERE period_start=?",
            (old_start // 86400 * 86400,),
        )[0]["n"]
        check("分钟数据到期删除前已形成长期日聚合",
              retention["minute_rows_deleted"] >= 1 and old_count == 0 and day_rollup >= 1,
              retention)

    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
