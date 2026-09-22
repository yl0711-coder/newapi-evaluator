"""只绑定回环地址，使用合成凭据验证实际 HTTP、CLI、落库和报告。"""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from test_domain import DomainTests, ROOT, d


class HttpTests(DomainTests):
    # 本类只收集这里定义的 HTTP 用例，领域用例由独立测试组执行。
    def test_http_matrix_cli_database_and_report(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                surface = "responses" if self.path.endswith("/responses") else "chat"
                effort = body["reasoning"]["effort"] if surface == "responses" else body["reasoning_effort"]
                juice = body.get("stream") is False
                calls.append((surface, effort, juice))
                self.send_response(200); self.end_headers()
                if len(calls) == 1:
                    self.wfile.write(b"[" * 10000 + b"0" + b"]" * 10000)
                    return
                if len(calls) == 2:
                    response = {"usage": "malformed-synthetic"}
                elif surface == "responses":
                    response = {"output": [{"type": "message", "content": [{"text": "14"}]}],
                                "reasoning": {"effort": effort}, "usage": {"total_tokens": 5}}
                else:
                    expected = {"low": "8", "medium": "16", "high": "40", "xhigh": "128", "max": "960"}
                    response = {"choices": [{"message": {"content": expected[effort] if juice else "14"}}],
                                "usage": {"total_tokens": 5}}
                self.wfile.write(json.dumps(response).encode())
        with local_server(Handler) as url:
            config = {**self.config, "channels": [{**self.config["channels"][0], "base_url": url,
                       "api_key_env": "DIAGNOSTIC_SYNTHETIC_KEY"}], "data_dir": str(self.root / "wire")}
            path = self.root / "config.json"; path.write_text(json.dumps(config))
            environment = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
            environment.update(DIAGNOSTIC_SYNTHETIC_KEY="synthetic-credential", HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1", NO_PROXY="")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "hourly_channel_diagnostic.py"),
                                     "run-once", "--config", str(path), "--confirm-live"],
                                    env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 110)
        self.assertEqual(calls[:6], [("responses", "low", False), ("responses", "medium", False),
                                    ("responses", "high", False), ("chat", "low", False),
                                    ("chat", "medium", False), ("chat", "high", False)])
        self.assertEqual([c[1] for c in calls[30:40]], ["low", "medium", "high", "xhigh", "max", "medium", "high", "xhigh", "max", "low"])
        summaries = d.aggregate(self.root / "wire/diagnostic.sqlite3", True)
        self.assertEqual(sum(r["successes"] for r in summaries), 108)
        errors = {key: value for r in summaries for key, value in r["error_distribution"].items()}
        self.assertEqual(errors.get("RecursionError"), 1)
        self.assertEqual(errors.get("invalid_response"), 1)
        self.assertTrue((self.root / "wire/report.html").exists())
        for file in (self.root / "wire").iterdir():
            self.assertNotIn(b"synthetic-credential", file.read_bytes())
            self.assertNotIn(b"malformed-synthetic", file.read_bytes())

    def test_http_error_redirect_and_bad_json_are_sanitized(self):
        requested = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requested.append(self.path)
                code = {"/redirect": 302, "/limited": 429}.get(self.path, 200)
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(b"synthetic-sensitive-marker")
        with local_server(Handler) as url:
            for suffix, error in (("/redirect", "http_302"), ("/limited", "http_429"), ("/invalid", "JSONDecodeError")):
                result = d.call_json(url + suffix, "synthetic-credential", {}, 2)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], error)
                self.assertNotIn("synthetic-sensitive-marker", json.dumps(result))
        self.assertNotIn("/must-not-follow", requested)

    def test_http_timeout_and_non_utf8(self):
        released = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/slow":
                    released.wait(2)
                    return
                self.send_response(200); self.end_headers(); self.wfile.write(b"\xff")
        with local_server(Handler) as url:
            try:
                self.assertFalse(d.call_json(url + "/slow", "synthetic-credential", {}, .05)["ok"])
            finally:
                released.set()
            result = d.call_json(url + "/encoding", "synthetic-credential", {}, 2)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "UnicodeDecodeError")

    def test_http_timeout_is_total_deadline_even_when_body_trickles(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200); self.end_headers()
                try:
                    for _ in range(50):
                        self.wfile.write(b" "); self.wfile.flush(); time.sleep(.03)
                except OSError:
                    pass
        with local_server(Handler) as url:
            started = time.monotonic()
            result = d.call_json(url + "/trickle", "synthetic-credential", {}, .08)
            elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "TimeoutError")
        self.assertLess(elapsed, .4)

    def test_live_gate_and_inspect_never_send_request(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({**self.config, "data_dir": str(self.root / "never-created")}))
        base = [sys.executable, "-B", str(ROOT / "hourly_channel_diagnostic.py")]
        blocked = subprocess.run(base + ["run-once", "--config", str(path)], capture_output=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertFalse((self.root / "never-created").exists())
        inspected = subprocess.run(base + ["inspect", "--config", str(path)], capture_output=True, timeout=10)
        self.assertEqual(inspected.returncode, 0)
        summary = json.loads(inspected.stdout)
        self.assertEqual(summary["requests_per_channel"], 110)
        self.assertEqual(len(summary["config_fingerprint"]), 64)
        self.assertNotIn(b"https://example.com", inspected.stdout)

    def test_daemon_controlled_clock_two_runs_then_stop(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({**self.config, "rounds": 1, "juice_runs": 1,
                                           "data_dir": str(self.root / "scheduled")}))
        calls = 0
        def advance(_):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt
        with patch.object(sys, "argv", ["diagnostic", "daemon", "--mock", "--config", str(config_path)]), \
                patch.object(d.time, "sleep", advance), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(), 130)
        summaries = d.aggregate(self.root / "scheduled/diagnostic.sqlite3")
        self.assertEqual(sum(r["requests"] for r in summaries), 44)


@contextlib.contextmanager
def local_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(HttpTests(name) for name in HttpTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
