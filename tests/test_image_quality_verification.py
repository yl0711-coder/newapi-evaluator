import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.verify_image_quality import ROOT, classify, run_process


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
        output = json.dumps({"status": "failed", "checks": [{"mode": "all", "status": "passed"},
                                                               {"mode": "image-quality", "status": "failed"}]})
        self.assertEqual(classify(0, output, "container")["status"], "failed")
        missing = json.dumps({"status": "incomplete", "checks": [], "failure_count": 0})
        self.assertEqual(classify(1, "FAILED (failures=1)\n" + missing, "container")["status"], "failed")

    def test_bounded_subprocess_keeps_error_exit_and_output(self):
        code, output, timed_out = run_process([sys.executable, "-c", "import sys; print('synthetic failure',file=sys.stderr); sys.exit(3)"], os.environ, 3)
        self.assertEqual(code, 3)
        self.assertIn("synthetic failure", output)
        self.assertFalse(timed_out)

    def test_timeout_terminates_only_owned_process(self):
        code, _output, timed_out = run_process([sys.executable, "-c", "import time; time.sleep(10)"], os.environ, 0.1)
        self.assertTrue(timed_out)
        self.assertNotEqual(code, 0)

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
