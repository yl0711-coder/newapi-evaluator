import unittest
from unittest.mock import patch

from features.stability.app import scheduler


class ReportFormattingTests(unittest.TestCase):
    def test_requested_groups_reuse_results_and_render_compact_report(self):
        run = {"snapshot": {"report_groups": [
            {"family": "Claude", "label": "0.5x", "registry_channel_ids": [2, 16, 8], "always_normal": False},
            {"family": "Claude", "label": "1.3x", "registry_channel_ids": [2, 16, 8], "always_normal": False},
            {"family": "Claude", "label": "3.5x", "registry_channel_ids": [], "always_normal": True},
            {"family": "Codex", "label": "0.7x", "registry_channel_ids": [3, 5, 13, 14], "always_normal": False},
            {"family": "Codex", "label": "1.2x", "registry_channel_ids": [6, 12, 1], "always_normal": False},
        ]}}
        healthy = {"verdict": "pass", "reasons": [], "failures": {}}
        slow = {
            "verdict": "fail", "reasons": ["P95 延迟高于历史中位数"], "failures": {},
            "p95_latency_ms": 2400, "p95_ttft_ms": 800, "p50_tokens_per_second": 18.2,
            "speed_assessment": {"status": "slow", "current_p95_ms": 2400,
                                 "baseline_median_p95_ms": 1200, "threshold_p95_ms": 1800},
        }
        summary = {"channels": [
            *({"registry_channel_id": value, **healthy} for value in (2, 16, 8, 6, 12, 1)),
            *({"registry_channel_id": value, **slow} for value in (3, 5, 13, 14)),
        ]}
        report = scheduler.render_notification(run, summary)
        self.assertTrue(report.startswith("""【Claude】
0.5x|正常
1.3x|正常
3.5x|正常

【Codex】
0.7x|异常（速度缓慢）
1.2x|正常"""), report)
        self.assertIn("速度缓慢渠道：", report)
        self.assertIn("P95 2.40 秒；历史中位数 1.20 秒；慢速线 1.80 秒", report)
        self.assertIn("不稳定渠道：\n无", report)

    def test_missing_expected_channel_is_not_reported_as_normal(self):
        run = {"snapshot": {"report_groups": [{
            "family": "Codex", "label": "0.7x", "registry_channel_ids": [3, 5], "always_normal": False,
        }]}}
        summary = {"channels": [{"registry_channel_id": 3, "verdict": "pass", "reasons": [], "failures": {}}]}
        report = scheduler.render_notification(run, summary)
        self.assertTrue(report.startswith("【Codex】\n0.7x|异常（未测试）"), report)

    def test_adaptive_speed_waits_for_history_then_uses_median_ratio(self):
        snapshot = {"speed_threshold_mode": "adaptive", "speed_baseline_min_runs": 5, "speed_slow_ratio": 1.5}
        summary = {"total": 18, "verdict": "pass", "reasons": [], "p95_success_latency_ms": 1600}
        with patch.object(scheduler.storage, "channel_latency_baseline", return_value={
            "sample_count": 4, "median_p95_latency_ms": 1000,
        }):
            collecting = scheduler._apply_speed_threshold(1, "demo", summary, snapshot)
        self.assertEqual(collecting["speed_assessment"]["status"], "collecting")
        self.assertEqual(collecting["verdict"], "pass")

        with patch.object(scheduler.storage, "channel_latency_baseline", return_value={
            "sample_count": 5, "median_p95_latency_ms": 1000,
        }):
            slow = scheduler._apply_speed_threshold(1, "demo", summary, snapshot)
        self.assertEqual(slow["speed_assessment"]["threshold_p95_ms"], 1500)
        self.assertEqual(slow["speed_assessment"]["status"], "slow")
        self.assertEqual(slow["verdict"], "fail")

    def test_unstable_channel_report_contains_concrete_rates_and_counts(self):
        run = {"snapshot": {"report_groups": [{
            "family": "Codex", "label": "0.7x", "registry_channel_ids": [3], "always_normal": False,
        }]}}
        channel = {
            "registry_channel_id": 3, "channel_name": "#3 example.com", "model": "gpt5.6sol",
            "verdict": "fail", "reasons": ["成功率低于阈值", "超时率超过阈值"],
            "failures": {"timeout": 2}, "total": 18, "completed": 16, "pass_rate": 0.8889,
            "timeout_count": 2, "timeout_rate": 0.1111, "streaming_total": 9,
            "stream_break_count": 1, "stream_break_rate": 0.1111, "p95_latency_ms": 42000,
        }
        report = scheduler.render_notification(run, {"channels": [channel]})
        self.assertIn("不稳定渠道：", report)
        self.assertIn("成功 16/18（88.9%）；超时 2/18（11.1%）；断流 1/9（11.1%）；P95 42.00 秒", report)


if __name__ == "__main__":
    unittest.main()
