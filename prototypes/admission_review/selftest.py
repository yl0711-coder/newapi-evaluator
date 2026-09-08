from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from prototypes.admission_review.api import create_app
from prototypes.admission_review.cli import sanitized_snapshot
from prototypes.admission_review.storage import Store


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="admission-feishu-framework-")
        self.store = Store(Path(self.temp.name) / "framework.db")
        self.app = create_app(self.store)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.store.close()
        self.temp.cleanup()

    async def start(self) -> tuple[int, str]:
        secret = "synthetic-" + "credential-for-redaction-test"
        response = await self.client.post("/api/runs", json={
            "channel_name": "候选渠道",
            "base_url": "https://relay.example/v1?secret=drop-me#fragment",
            "api_key": secret,
            "model": "demo-model",
            "protocol": "openai",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("drop-me", response.text)
        return response.json()["id"], secret

    async def test_full_framework_flow_is_human_gated_and_outbox_only(self) -> None:
        run_id, secret = await self.start()
        early = await self.client.post(f"/api/runs/{run_id}/review", json={
            "decision": "qualified", "reviewer": "tester", "note": "too early",
        })
        self.assertEqual(early.status_code, 409)

        finished = await self.client.post(f"/api/runs/{run_id}/test-result", json={
            "outcome": "completed",
            "summary": {"request_count": 10, "success_count": 10, "p95_ms": 820},
        })
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["status"], "awaiting_review")

        reviewed = await self.client.post(f"/api/runs/{run_id}/review", json={
            "decision": "qualified", "reviewer": "tester", "note": "人工确认合格",
        })
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["review_decision"], "qualified")

        repeated = await self.client.post(f"/api/runs/{run_id}/review", json={
            "decision": "qualified", "reviewer": "tester", "note": "same decision",
        })
        self.assertEqual(repeated.status_code, 200, repeated.text)
        jobs = (await self.client.get("/api/outbox")).json()
        self.assertFalse(jobs["delivery_enabled"])
        self.assertEqual(len(jobs["jobs"]), 1)
        self.assertEqual(jobs["jobs"][0]["status"], "awaiting_field_mapping")
        self.assertEqual(jobs["jobs"][0]["payload"]["fields"], {})

        self.store.close()
        for path in Path(self.temp.name).glob("framework.db*"):
            self.assertNotIn(secret.encode(), path.read_bytes())

    async def test_reject_decision_is_recorded_and_cannot_be_reversed_silently(self) -> None:
        run_id, _ = await self.start()
        await self.client.post(f"/api/runs/{run_id}/test-result", json={
            "outcome": "failed", "summary": {"request_count": 5, "success_count": 2},
        })
        rejected = await self.client.post(f"/api/runs/{run_id}/review", json={
            "decision": "not_qualified", "reviewer": "owner", "note": "人工判定不合格",
        })
        self.assertEqual(rejected.status_code, 200, rejected.text)
        reversal = await self.client.post(f"/api/runs/{run_id}/review", json={
            "decision": "qualified", "reviewer": "owner", "note": "change",
        })
        self.assertEqual(reversal.status_code, 409)

    async def test_summary_rejects_sensitive_fields_and_likely_credentials(self) -> None:
        run_id, _ = await self.start()
        for summary in (
            {"api_key": "hidden"},
            {"API Key": "hidden"},
            {"message": "Bearer " + "a" * 26},
        ):
            response = await self.client.post(f"/api/runs/{run_id}/test-result", json={
                "outcome": "completed", "summary": summary,
            })
            self.assertEqual(response.status_code, 400, response.text)

    async def test_validation_errors_never_echo_credentials(self) -> None:
        secret = "q" * 4_100
        response = await self.client.post("/api/runs", json={
            "channel_name": "候选渠道",
            "base_url": "https://relay.example/v1",
            "api_key": secret,
            "model": "demo-model",
            "protocol": "openai",
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn('"input"', response.text)

    async def test_one_command_snapshot_is_sanitized(self) -> None:
        run_id, secret = await self.start()
        run = self.store.get_run(run_id)
        assert run is not None
        snapshot = sanitized_snapshot(run)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(snapshot["channel_alias"], f"candidate-{run_id}")
        self.assertRegex(snapshot["masked_host"], r"^https://host-[0-9a-f]{12}$")
        self.assertNotIn("relay.example", encoded)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("channel_name", snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
