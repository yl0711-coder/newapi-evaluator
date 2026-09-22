"""控制 API 的合成回归夹具；不启动真实诊断进程。"""
import importlib
import unittest

from fastapi.testclient import TestClient


control_server = importlib.import_module("control_server")


class FakeController:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"state": "stopped", "enabled_channels": 0, "total_channels": 2,
                "test_models": ["gpt-5.6-sol", "gpt-6-astra"], "last_run": None}

    def enable_all(self):
        self.calls.append("enable_all")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def run_once(self):
        self.calls.append("run_once")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.client = TestClient(control_server.create_app(self.controller, "synthetic-token"))

    def test_status_is_readable_without_token_and_has_no_secret(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["test_models"], ["gpt-5.6-sol", "gpt-6-astra"])
        self.assertNotIn("token", response.text.lower())

    def test_mutations_require_control_token(self):
        self.assertEqual(self.client.post("/api/enable-all").status_code, 403)
        self.assertEqual(self.client.post("/api/enable-all", headers={"X-Control-Token": "wrong"}).status_code, 403)
        response = self.client.post("/api/enable-all", headers={"X-Control-Token": "synthetic-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.controller.calls, ["enable_all"])

    def test_start_and_stop_are_forwarded_after_authorization(self):
        headers = {"X-Control-Token": "synthetic-token"}
        self.assertEqual(self.client.post("/api/start", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/stop", headers=headers).status_code, 200)
        self.assertEqual(self.controller.calls, ["start", "stop"])

    def test_run_once_is_forwarded_after_authorization(self):
        response = self.client.post("/api/run-once", headers={"X-Control-Token": "synthetic-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.controller.calls, ["run_once"])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ControlTests(name) for name in ControlTests.__dict__ if name.startswith("test_"))
