import asyncio
import unittest
from unittest.mock import patch

from features.stability.app import scheduler, transport


class StabilityConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scheduler._probe_semaphore = None
        scheduler._probe_loop = None

    async def asyncTearDown(self):
        scheduler._probe_semaphore = None
        scheduler._probe_loop = None

    async def test_concurrent_channels_share_the_two_request_global_limit(self):
        active = 0
        peak_active = 0
        completed = 0

        async def fake_probe(_client, _channel, probe):
            nonlocal active, peak_active, completed
            active += 1
            peak_active = max(peak_active, active)
            try:
                await asyncio.sleep(0.01)
                completed += 1
                return {
                    "probe_id": probe["id"], "probe_name": probe["name"], "ok": True,
                    "status": "completed", "latency_ms": 10, "ttft_ms": 2,
                    "tokens_per_second": 20.0, "output_tokens": 10,
                    "finish_reason": "stop", "actual_model": "demo-model",
                    "usage_complete": True, "model_mismatch": False, "error": "",
                }
            finally:
                active -= 1

        snapshot = {
            "rounds": 1, "round_interval_seconds": 0,
            "min_success_rate": 0.95, "max_timeout_rate": 0.05,
            "max_stream_break_rate": 0, "max_p95_ms": 30000,
            "speed_threshold_mode": "off",
        }
        channel = {
            "name": "channel", "base_url": "https://example.com/v1",
            "model": "demo-model", "protocol": "openai", "api_key": "test-key",
        }

        with patch.object(transport, "run_probe", new=fake_probe), \
                patch.object(scheduler.storage, "add_probe_result"), \
                patch.object(scheduler.storage, "extend_lease"):
            await asyncio.gather(
                scheduler._measure_channel(1, {**channel, "id": 1}, snapshot),
                scheduler._measure_channel(2, {**channel, "id": 2}, snapshot),
            )

        self.assertEqual(completed, len(transport.PROBES) * 2)
        self.assertEqual(peak_active, scheduler.MAX_CONCURRENT_PROBES)
        self.assertEqual(scheduler.MAX_CONCURRENT_PROBES, 2)


if __name__ == "__main__":
    unittest.main()
