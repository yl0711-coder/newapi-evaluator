"""独立编写的合成回归夹具；禁止读取真实渠道配置。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest
from unittest.mock import patch
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("diagnostic", ROOT / "hourly_channel_diagnostic.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


def outcome(text="14", usage=None, surface="chat", echo=None):
    body = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    if surface == "responses":
        body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "status": "completed", "reasoning": {"effort": echo}}
    if usage is not None:
        body["usage"] = usage
    return {"ok": True, "status_code": 200, "latency_ms": 20, "body": body}


def observation(value=None, package="reasoning", surface="chat", effort="low", round_no=1):
    return d.make_observation("synthetic", package, surface, effort, round_no,
                              value or outcome(), "2099-01-01T00:00:00+00:00", "Asia/Shanghai")


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ["DIAGNOSTIC_TEST_ROOT"])
        self.root = Path(self.directory.name)
        self.db = self.root / "diagnostic.sqlite3"
        self.config = d.load_config(ROOT / "config.example.json")

    def tearDown(self):
        self.directory.cleanup()

    def store(self, rows):
        conn = d.connect(self.db)
        conn.execute("INSERT INTO runs(id,started_at,hour_key,mock,status) VALUES(1,?,?,1,'completed')",
                     ("2099-01-01T00:00:00+00:00", "2099-01-01T08:00:00+0800"))
        for row in rows:
            d.save_observation(conn, 1, row)
        conn.close()

    def test_mock_complete_matrix_and_correct_answers(self):
        with contextlib.redirect_stdout(io.StringIO()):
            d.run_once(self.config, self.db, True)
        details = d.aggregate(self.db, True)
        self.assertEqual(len(details), 22)
        self.assertEqual(sum(r["requests"] for r in details), 110)
        self.assertEqual({r["model"] for r in details}, {"gpt-5.6-sol", "gpt-6-astra"})
        self.assertTrue(all(r["accuracy"] == 100 for r in details))
        self.assertEqual([r["match_rate"] for r in details if r["surface"] == "chat" and r["package"] == "reasoning"], [None]*6)
        self.assertTrue(all(r["verification_status"] == "verified" for r in details if r["package"] == "juice"))

    def test_missing_echo_is_unknown_and_chat_not_applicable(self):
        chat = observation()
        missing = observation(outcome(surface="responses"), surface="responses")
        mismatch = observation(outcome(surface="responses", echo="high"), surface="responses")
        self.assertIsNone(chat["matched"])
        self.assertIsNone(missing["matched"])
        self.assertEqual(mismatch["matched"], 0)
        self.store([chat, missing, mismatch])
        summary = d.aggregate(self.db)[0]
        self.assertEqual(summary["match_samples"], 1)
        self.assertEqual(summary["match_rate"], 0)
        self.assertEqual(summary["echo_distribution"], {"未返回": 1, "high": 1})

    def test_zero_missing_and_output_only_tokens(self):
        rows = [observation(outcome(usage={"total_tokens": 0})),
                observation(outcome(usage={"completion_tokens": 7})), observation()]
        self.store(rows)
        summary = d.aggregate(self.db)[0]
        self.assertEqual(summary["tokens"], 0)
        self.assertEqual(summary["token_samples"], 1)
        self.assertIsNone(rows[1]["total_tokens"])
        self.assertEqual(d.display(0), "0")
        self.assertEqual(d.display(None), "—")

    def test_all_missing_tokens_stay_null(self):
        self.store([observation(), observation()])
        self.assertIsNone(d.aggregate(self.db)[0]["tokens"])

    def test_reasoning_tokens_missing_null_zero_and_median(self):
        rows = [observation(outcome(usage=u)) for u in (
            {}, {"completion_tokens_details": {"reasoning_tokens": None}},
            {"completion_tokens_details": {"reasoning_tokens": 0}},
            {"completion_tokens_details": {"reasoning_tokens": 40}})]
        self.store(rows)
        summary = d.aggregate(self.db)[0]
        self.assertEqual(summary["reasoning_field_samples"], 3)
        self.assertEqual(summary["reasoning_null_samples"], 1)
        self.assertEqual(summary["reasoning_token_samples"], 2)
        self.assertEqual(summary["median_reasoning_tokens"], 20)

    def test_juice_threshold_includes_failed_and_unparsed_requests(self):
        rows = [observation(outcome(text=t), "juice") for t in ("8", "8", "8", "unknown")]
        rows.append(observation({"ok": False, "error": "http_429"}, "juice"))
        self.store(rows)
        summary = d.aggregate(self.db, True)[0]
        self.assertEqual(summary["verification_rate"], 60)
        self.assertEqual(summary["required_matches"], 3)
        self.assertEqual(summary["verification_status"], "verified")
        self.assertEqual(summary["parsed_samples"], 3)

    def test_juice_two_of_five_is_inconclusive_and_rejects_wrong_integer(self):
        rows = [observation(outcome(text=t), "juice") for t in ("8", "8", "40", "text", "9")]
        self.store(rows)
        summary = d.aggregate(self.db, True)[0]
        self.assertEqual(summary["verification_status"], "inconclusive")
        self.assertEqual(summary["juice_distribution"], {"8": 2, "40": 1, "9": 1})
        self.assertEqual(summary["verification_rate"], 40)

    def test_strict_numbers_and_sqlite_integer_bounds(self):
        for text in ("14 and 140", "the answer is 14", "140"):
            self.assertFalse(d.exact_number(text, "14"))
        self.assertTrue(d.exact_number("14.00", "14"))
        for text in ("8 16", "9"*10000, str(2**63)):
            self.assertIsNone(d.juice_value(text))
        self.assertEqual(d.juice_value("+8.00"), 8)

    def test_malformed_responses_are_observation_failures(self):
        bodies = [{"usage": "bad"}, {"usage": []}, {"choices": ["bad"]},
                  {"choices": [{"message": {"content": ["bad"]}}]},
                  {"choices": []}, {"error": {"message": "synthetic-marker"}}, []]
        for body in bodies:
            with self.subTest(shape=type(body).__name__):
                row = observation({"ok": True, "status_code": 200, "body": body})
                self.assertEqual(row["ok"], 0)
                self.assertIn(row["error"], ("invalid_response", "api_error"))

    def test_invalid_usage_is_not_silently_zero(self):
        for value in (True, -1, "40", float("nan"), 3.5, 2**70):
            row = observation(outcome(usage={"total_tokens": value}))
            self.assertEqual(row["ok"], 0)
            self.assertEqual(row["error"], "invalid_usage")

    def test_empty_and_incomplete_responses(self):
        value = outcome(text="")
        self.assertEqual(observation(value)["error"], "empty_response")
        value = outcome(); value["body"]["choices"][0]["finish_reason"] = "length"
        self.assertEqual(observation(value)["error"], "incomplete_response")
        value = outcome(surface="responses"); value["body"]["status"] = "incomplete"
        self.assertEqual(observation(value, surface="responses")["error"], "incomplete_response")

    def test_reported_usage_survives_invalid_or_truncated_answer(self):
        value = outcome(usage={"total_tokens": 77})
        value["body"]["choices"][0]["finish_reason"] = "length"
        row = observation(value)
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["total_tokens"], 77)
        self.store([row])
        summary = d.aggregate(self.db)[0]
        self.assertEqual(summary["tokens"], 77)
        self.assertEqual(summary["successes"], 0)

    def test_one_malformed_reply_does_not_stop_later_requests_or_channels(self):
        self.config["channels"].append({**self.config["channels"][0], "name": "second"})
        original = d.mock_call
        calls = 0
        def call(surface, effort, package):
            nonlocal calls
            calls += 1
            return {"ok": True, "body": {"usage": "bad"}} if calls == 1 else original(surface, effort, package)
        with patch.object(d, "mock_call", call), contextlib.redirect_stdout(io.StringIO()):
            d.run_once(self.config, self.db, True)
        summary = d.aggregate(self.db)
        self.assertEqual(sum(r["requests"] for r in summary), 220)
        self.assertEqual(sum(r["successes"] for r in summary), 219)

    def test_mock_live_data_separation(self):
        self.store([observation()])
        with patch.dict(os.environ, {"CHANNEL_A_API_KEY": "synthetic-credential"}):
            with self.assertRaises(RuntimeError):
                d.run_once(self.config, self.db, False)

    def test_concurrent_run_lock(self):
        with d.run_lock(self.db):
            with self.assertRaises(RuntimeError):
                d.run_once(self.config, self.db, True)

    def test_interrupt_keeps_completed_samples_and_status(self):
        original = d.mock_call
        calls = 0
        def call(surface, effort, package):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(surface, effort, package)
        with patch.object(d, "mock_call", call), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                d.run_once(self.config, self.db, True)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM runs").fetchone()[0], "interrupted")

    def test_legacy_rows_remain_readable_without_false_token_or_echo_claims(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, hour_key TEXT,
                          mock INTEGER, status TEXT, channels INTEGER, error TEXT);
        CREATE TABLE observations(id INTEGER PRIMARY KEY, run_id INTEGER, channel TEXT, package TEXT,
            surface TEXT, effort TEXT, round_no INTEGER, timestamp TEXT, hour_key TEXT, ok INTEGER,
            correct INTEGER, matched INTEGER, status_code INTEGER, latency_ms INTEGER,
            reasoning_tokens REAL, observed_juice INTEGER, total_tokens REAL, error TEXT);
        INSERT INTO runs VALUES(1,'2099-01-01',NULL,'2099-01-01T08:00:00+0800',1,'completed',1,NULL);
        INSERT INTO observations VALUES(1,1,'legacy','reasoning','chat','low',1,'2099-01-01',
            '2099-01-01T08:00:00+0800',1,1,1,200,20,0,NULL,7,NULL);
        """)
        conn.close()
        for _ in range(2):
            summary = d.aggregate(self.db, True)[0]
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["legacy_samples"], 1)
            self.assertIsNone(summary["tokens"])
            self.assertIsNone(summary["match_rate"])
        d.build_report(self.db, self.root / "report.html", "Asia/Shanghai")
        self.assertTrue((self.root / "report.html").exists())

    def test_report_escapes_alias_and_shows_detail_and_zero(self):
        row = observation(outcome(usage={"total_tokens": 0}))
        row["channel"] = "<script>synthetic</script>"
        self.store([row])
        report = self.root / "report.html"
        d.build_report(self.db, report, "Asia/Shanghai")
        text = report.read_text()
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("id='details'", text)
        self.assertIn("<td>0</td>", text)
        self.assertIn("不适用 / 未返回", text)

    def test_chart_keeps_points_but_does_not_bridge_missing_hours(self):
        base = {"channel": "synthetic", "package": "reasoning", "mock": True, "requests": 5}
        rows = [{**base, "hour": "2099-01-01T08:00:00+0800", "accuracy": 100},
                {**base, "hour": "2099-01-01T10:00:00+0800", "accuracy": 100}]
        svg = d.svg_line(rows, "accuracy", "test")
        self.assertEqual(svg.count("<circle"), 2)
        self.assertEqual(svg.count("<line"), 0)

    def test_config_validation_and_relative_data_resolution(self):
        path = self.root / "config.json"
        base = json.loads((ROOT / "config.example.json").read_text())
        for field, value in (("rounds", 0), ("rounds", True), ("juice_runs", 1.5),
                             ("timeout", float("nan")), ("retention_days", -1)):
            path.write_text(json.dumps({**base, field: value}))
            with self.assertRaises((ValueError, SystemExit)):
                d.load_config(path)
        base["data_dir"] = "external-data"
        path.write_text(json.dumps(base))
        self.assertEqual(d.load_config(path)["data_dir"], str(self.root / "external-data"))

    def test_endpoint_and_timezone_boundaries(self):
        self.assertEqual(d.endpoint("https://example.com/v1/responses", "/chat/completions"), "https://example.com/v1/chat/completions")
        for url in ("https://user:password@example.com", "https://example.com?token=synthetic", "file:///tmp/test", "https://example.com:bad"):
            with self.assertRaises(ValueError):
                d.validate_endpoint(url)
        cases = [("Asia/Shanghai", "2026-09-21T01:59:59+00:00", 1),
                 ("Asia/Kathmandu", "2026-09-21T01:14:30+00:00", 30),
                 ("America/New_York", "2026-11-01T05:59:00+00:00", 60),
                 ("America/New_York", "2026-03-08T06:59:00+00:00", 60)]
        for zone, now, seconds in cases:
            self.assertEqual(d.next_hour_sleep(zone, datetime.fromisoformat(now)), seconds)

    def test_report_cannot_overwrite_database(self):
        self.store([observation()])
        original = self.db.read_bytes()
        with self.assertRaises(ValueError):
            d.build_report(self.db, self.db, "Asia/Shanghai")
        self.assertEqual(self.db.read_bytes(), original)

    def test_runner_rejects_incomplete_and_failed_evidence(self):
        spec = importlib.util.spec_from_file_location("suite_runner", ROOT / "scripts/test_all.py")
        runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
        for exit_code, result, expected in (
            (0, {"status": "passed", "tests": 0}, "incomplete"),
            (1, {"status": "passed", "tests": 1}, "failed"),
            (0, {"status": "passed", "tests": 1, "skipped": 1}, "incomplete"),
            (0, {"status": "passed", "tests": 1, "failures": 1}, "failed"),
            (0, {"status": "passed", "tests": 1}, "passed")):
            self.assertEqual(runner.checked_status(exit_code, result), expected)
        failed, timed_out = runner.run_child([sys.executable, "-c", "raise SystemExit(3)"], dict(os.environ), 5)
        self.assertEqual(failed.returncode, 3)
        self.assertFalse(timed_out)
        expired, timed_out = runner.run_child([sys.executable, "-c", "import time; time.sleep(10)"], dict(os.environ), .1)
        self.assertTrue(timed_out)
        self.assertNotEqual(expired.returncode, 0)


if __name__ == "__main__":
    unittest.main()
