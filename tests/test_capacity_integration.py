import asyncio
import unittest

import httpx

from features.capacity import api as capacity
from workbench import create_app


class CapacityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app()
        capacity.app.state.console = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        console = getattr(capacity.app.state, "console", None)
        if console:
            for job in list(console.jobs.values()):
                console.cancel(job.id)
            for job in list(console.jobs.values()):
                if job.thread:
                    await asyncio.to_thread(job.thread.join, 5)
        capacity.app.state.console = None
        await self.client.aclose()

    async def test_capacity_is_a_workbench_feature(self):
        platform = await self.client.get("/api/platform")
        self.assertEqual(platform.status_code, 200, platform.text)
        features = {item["id"]: item for item in platform.json()["features"]}
        self.assertEqual(features["capacity"]["url"], "/capacity/")
        page = await self.client.get("/capacity/")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("中转站 · 测试控制台", page.text)
        self.assertIn("/assets/common.js", page.text)

    async def test_mock_job_runs_through_integrated_api(self):
        state = await self.client.get("/capacity/api/state")
        self.assertEqual(state.status_code, 200, state.text)
        token = state.json()["csrf"]
        created = await self.client.post(
            "/capacity/api/jobs",
            headers={"X-Relay-UI": token},
            json={
                "mode": "account-test",
                "environment": "mock",
                "load_mode": "requests",
                "stages": [1],
                "samples": 1,
                "timeout": 1,
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        identifier = created.json()["id"]
        for _ in range(100):
            detail = await self.client.get(f"/capacity/api/jobs/{identifier}")
            if detail.json()["status"] != "running":
                break
            await asyncio.sleep(0.03)
        self.assertEqual(detail.json()["status"], "completed", detail.text)
        report = await self.client.get(f"/capacity/api/jobs/{identifier}/summary.json")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertNotIn("api_key", report.text.casefold())

    async def test_write_requires_page_session(self):
        response = await self.client.post(
            "/capacity/api/jobs",
            json={"mode": "account-test", "environment": "mock"},
        )
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
