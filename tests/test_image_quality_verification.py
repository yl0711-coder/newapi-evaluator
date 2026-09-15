import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.verify_image_quality import ROOT, aggregate_status, classify, run_process


def framework_output(counts=(2, 3, 4), statuses=("OK", "OK", "OK")):
    return ("test_image_quality\n" + "".join(
        f"Ran {count} tests in 0.001s\n\n{status}\n" for count, status in zip(counts, statuses))
        + "  OK synthetic manual check\n" * 22 + "All engine and integration checks passed.\n")


class VerificationTests(unittest.TestCase):
    def test_nonzero_exit_cannot_be_overridden_by_success_text(self):
        result = classify(1, "UI contract tests passed:", "web")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_count"], 1)

    def test_empty_or_unknown_output_is_incomplete(self):
        for kind in ["syntax", "inspect", "security", "web", "e2e", "browser", "container"]:
            self.assertEqual(classify(0, "", kind)["status"], "incomplete")
            self.assertEqual(classify(0, "{}", kind)["status"], "incomplete")

    def test_each_framework_suite_must_collect_and_pass(self):
        self.assertEqual(classify(0, framework_output(), "unittest")["status"], "passed")
        for index in range(3):
            counts = [2, 3, 4]
            counts[index] = 0
            self.assertEqual(classify(0, framework_output(counts), "unittest")["status"], "incomplete")
            for outcome, expected in [("OK (skipped=1)", "incomplete"), ("", "incomplete"),
                                       ("FAILED (failures=2)", "failed")]:
                statuses = ["OK"] * 3
                statuses[index] = outcome
                result = classify(0, framework_output(statuses=statuses), "unittest")
                self.assertEqual(result["status"], expected)
                if expected == "failed":
                    self.assertEqual(result["failure_count"], 2)
        self.assertEqual(classify(0, "Ran 4 tests\nOK\ntest_image_quality", "unittest")["status"], "incomplete")

    def test_dependency_gap_is_incomplete_but_assertion_failure_stays_failed(self):
        self.assertEqual(classify(127, "Required executable unavailable", "browser")["status"], "incomplete")
        self.assertEqual(classify(1, "ModuleNotFoundError: synthetic_dependency", "unittest")["status"], "incomplete")
        result = classify(1, framework_output(statuses=("FAILED (errors=1)", "OK", "OK (skipped=1)")), "unittest")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_count"], 1)

    def test_container_framework_failure_cannot_hide_behind_mode_names(self):
        output = json.dumps({"status": "failed", "failure_count": 3, "checks": [{"mode": "all", "status": "passed"},
                                                               {"mode": "image-quality", "status": "failed"}]})
        result = classify(0, output, "container")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_count"], 3)
        missing = json.dumps({"status": "incomplete", "checks": [], "failure_count": 0})
        self.assertEqual(classify(1, "FAILED (failures=1)\n" + missing, "container")["status"], "failed")

    def test_source_change_cannot_override_known_failure(self):
        self.assertEqual(aggregate_status([{"status": "failed"}], source_changed=True), "failed")
        self.assertEqual(aggregate_status([{"status": "passed"}], source_changed=True), "incomplete")
        self.assertEqual(aggregate_status([{"status": "passed"}, {"status": "not_run"}]), "incomplete")

    def test_bounded_subprocess_keeps_error_exit_and_output(self):
        code, output, timed_out = run_process([sys.executable, "-c", "import sys; print('synthetic failure',file=sys.stderr); sys.exit(3)"], os.environ, 3)
        self.assertEqual(code, 3)
        self.assertIn("synthetic failure", output)
        self.assertFalse(timed_out)

    def test_timeout_terminates_only_owned_process(self):
        code, _output, timed_out = run_process([sys.executable, "-c", "import time; time.sleep(10)"], os.environ, 0.1)
        self.assertTrue(timed_out)
        self.assertNotEqual(code, 0)

    def check_cancellation(self, number, full_entrypoint=False):
        with tempfile.TemporaryDirectory(prefix="image-cancel-") as folder:
            root = Path(folder)
            ledger = root / "child.json"
            child = '''import json, os, time
from pathlib import Path
path = Path(os.environ["SYNTHETIC_CHILD_LEDGER"])
pending = path.with_suffix(".tmp")
pending.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
pending.replace(path)
time.sleep(30)
'''
            env = {**os.environ, "PYTHONPATH": str(ROOT), "SYNTHETIC_CHILD_LEDGER": str(ledger)}
            if full_entrypoint:
                node = root / "node"
                node.write_text("#!" + sys.executable + "\n" + child)
                node.chmod(0o755)
                env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
                command = [sys.executable, str(ROOT / "scripts/verify_image_quality.py"), "--output", str(root / "verification")]
            else:
                wrapper = "import os, sys\nfrom scripts.verify_image_quality import run_process\nrun_process([sys.executable, '-c', " + repr(child) + "], os.environ, 30)"
                command = [sys.executable, "-c", wrapper]
            process = subprocess.Popen(command, env=env, cwd=ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            owned = None
            try:
                deadline = time.monotonic() + 5
                while not ledger.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("synthetic child did not become ready")
                    time.sleep(0.02)
                owned = json.loads(ledger.read_text())
                process.send_signal(number)
                process.communicate(timeout=8)
                self.assertNotEqual(process.returncode, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(owned["pid"], 0)
                if full_entrypoint:
                    report = json.loads((root / "verification/checks.json").read_text())
                    self.assertTrue(report["cancelled"])
                    self.assertEqual(report["status"], "incomplete")
                    self.assertEqual(len(report["checks"]), 8)
                    self.assertEqual(report["checks"][0]["reason"], "cancelled")
                    self.assertTrue(all(row["status"] == "not_run" for row in report["checks"][1:]))
            finally:
                for pgid in ([owned["pgid"]] if owned else []) + [process.pid]:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate(timeout=5)

    def test_user_interrupt_cleans_detached_child_process(self):
        for number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=number):
                self.check_cancellation(number)

    def test_cancelled_entrypoint_records_remaining_groups_as_not_run(self):
        self.check_cancellation(signal.SIGTERM, full_entrypoint=True)

    def run_container_mock(self, scenario):
        # The actual container script runs; only the external Docker daemon is replaced.
        with tempfile.TemporaryDirectory(prefix="image-cleanup-") as folder:
            root = Path(folder)
            binary = root / "docker"
            binary.write_text("#!" + sys.executable + "\n" + '''import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["SYNTHETIC_DOCKER_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if os.environ["SYNTHETIC_DOCKER_SCENARIO"] == "unavailable" and args[0] == "info":
    sys.exit(1)
if os.environ["SYNTHETIC_DOCKER_SCENARIO"] == "cleanup_failure" and args[0] == "rm":
    sys.exit(9)
if args[0] == "port":
    time.sleep(30)
''')
            binary.chmod(0o755)
            log = root / "docker-calls.jsonl"
            output = root / "container"
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                   "SYNTHETIC_DOCKER_LOG": str(log), "SYNTHETIC_DOCKER_SCENARIO": scenario}
            code, text, timed_out = run_process(
                [sys.executable, str(ROOT / "scripts/image_quality_container.py"), "--output", str(output)], env, 2)
            self.assertEqual(timed_out, scenario != "unavailable")
            self.assertNotEqual(code, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            owned = json.loads((output / "resources.json").read_text())
            report = json.loads((output / "checks.json").read_text())
            if scenario == "unavailable":
                self.assertEqual(len(calls), 1)
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["incomplete"], ["DockerUnavailable"])
                self.assertEqual(classify(code, text, "container")["status"], "incomplete")
                return
            self.assertIn(["rm", "--force", owned["containers"][0]], calls)
            self.assertIn(["image", "rm", owned["image"]], calls)
            self.assertEqual(sum(row[0] == "rm" for row in calls), 1)
            expected = "failed" if scenario == "cleanup_failure" else "incomplete"
            self.assertEqual(report["status"], expected)
            self.assertEqual(report["failure_count"], int(scenario == "cleanup_failure"))
            self.assertEqual(report["incomplete"], ["interrupted"])
            self.assertEqual(classify(code, text, "container")["status"], expected)

    def test_outer_timeout_cleans_owned_docker_resources(self):
        self.run_container_mock("timeout")

    def test_cleanup_failure_is_reported_even_after_timeout(self):
        self.run_container_mock("cleanup_failure")

    def test_missing_docker_is_incomplete_without_resource_cleanup(self):
        self.run_container_mock("unavailable")
