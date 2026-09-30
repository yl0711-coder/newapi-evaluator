"""Regression tests for the bounded workbench execution layer."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
import json
import subprocess

from scripts.test_all import GroupSpec, classify_group, registered_groups, run_group


class TestAllExecutionTests(unittest.TestCase):
    def test_internal_groups_have_stable_ids_and_budgets(self):
        groups = registered_groups(sys.executable)
        self.assertEqual(
            [group.group_id for group in groups],
            [
                "admission-selftest",
                "stability-selftest",
                "reasoning-selftest",
                "unittest-discover",
            ],
        )
        self.assertEqual(len({group.group_id for group in groups}), len(groups))
        self.assertTrue(all(group.timeout_seconds > 0 for group in groups))

    def test_classification_keeps_failure_and_timeout_distinct(self):
        unittest_group = registered_groups(sys.executable)[0]
        passing = classify_group(unittest_group, 0, "Ran 34 tests in 0.1s\n\nOK\n")
        self.assertEqual(passing["status"], "passed")
        failed = classify_group(unittest_group, 1, "Ran 34 tests in 0.1s\nFAILED (failures=1)\n")
        self.assertEqual(failed["status"], "failed")
        timed_out = classify_group(unittest_group, -15, "partial output\n", timed_out=True)
        self.assertEqual(timed_out["status"], "incomplete")

    def test_manual_group_requires_complete_check_footer(self):
        manual = registered_groups(sys.executable)[1]
        output = ("".join("  OK  synthetic\n" for _ in range(22)) + "\n失败项：无\n")
        self.assertEqual(classify_group(manual, 0, output)["status"], "passed")
        self.assertEqual(classify_group(manual, 0, output.replace("失败项：无", "失败项：bad"))["status"], "incomplete")

    def test_timeout_terminates_only_owned_process_group(self):
        with tempfile.TemporaryDirectory(prefix="test-all-process-") as folder:
            pid_file = Path(folder) / "child.pid"
            script = (
                "import pathlib, subprocess, sys, time; "
                f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(30)"
            )
            code, _output, timed_out = run_group(
                (sys.executable, "-c", script),
                {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[1])},
                0.2,
            )
            self.assertTrue(timed_out)
            self.assertIsNotNone(code)
            deadline = time.monotonic() + 2
            child_pid = int(pid_file.read_text())
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("owned child process survived timeout cleanup")

    def test_cancellation_records_active_and_later_groups(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="test-all-cancel-") as folder:
            output = Path(folder) / "evidence"
            wrapper = f'''import sys
from pathlib import Path
sys.path.insert(0, {str(root)!r})
from scripts import test_all as runner
runner.registered_groups = lambda python=None: (
    runner.GroupSpec("slow", (python or sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"), 60, "unittest"),
    runner.GroupSpec("later", (python or sys.executable, "-c", "print('must not run')"), 60, "unittest"),
)
sys.argv = ["test_all", "--output", {str(output)!r}]
sys.exit(runner.main())
'''
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    line = process.stdout.readline()
                    if line.startswith("=== slow ==="):
                        process.send_signal(signal.SIGTERM)
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("synthetic test group did not start")
                process.communicate(timeout=8)
                self.assertEqual(process.returncode, 130)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["status"], "incomplete")
                self.assertTrue(summary["cancelled"])
                self.assertEqual(
                    [(row["group_id"], row["status"]) for row in summary["groups"]],
                    [("slow", "incomplete"), ("later", "not_run")],
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
