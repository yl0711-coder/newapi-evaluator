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
import shlex
import yaml

from scripts.test_all import GroupSpec, _unittest_counts, classify_group, registered_groups, run_group


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

    def test_final_framework_counts_do_not_add_failure_titles(self):
        samples = [
            ("FAIL: example (fixture.Test.example)\nRan 1 test\nFAILED (failures=1)\n", ([1], 0, 1)),
            ("FAIL: one\nERROR: two\nRan 2 tests\nFAILED (failures=1, errors=1)\n", ([2], 0, 2)),
            ("Ran 2 tests\nFAILED (failures=1)\nRan 3 tests\nFAILED (errors=2)\n", ([2, 3], 0, 3)),
            ("FAIL: t (fixture.Test.t) (i=1)\nFAIL: t (fixture.Test.t) (i=2)\nRan 1 test\nFAILED (failures=2)\n", ([1], 0, 2)),
            ("FAILED\n", ([], 0, 1)),
            ("FAIL: one\nERROR: two\n", ([], 0, 2)),
            ("Ran 3 tests\nOK (skipped=1)\n", ([3], 1, 0)),
            ("Ran 3 tests\nOK\nFAIL: interrupted (fixture.Test.interrupted)\n", ([3], 0, 1)),
        ]
        for output, expected in samples:
            with self.subTest(output=output):
                self.assertEqual(_unittest_counts(output), expected)

    def test_subtests_distinguish_failure_records_from_failed_tests(self):
        output = "FAIL: t (fixture.Test.t) (i=1)\nFAIL: t (fixture.Test.t) (i=2)\nRan 1 test\nFAILED (failures=2)\n"
        result = classify_group(registered_groups()[0], 1, output)
        self.assertEqual((result["failed"], result["failed_test_count"]), (2, 1))
        self.assertTrue(result["failure_count_exact"])
        self.assertFalse(classify_group(registered_groups()[0], 1, "FAILED\n")["failure_count_exact"])

    def test_missing_executable_and_signal_exit_are_incomplete(self):
        spec = registered_groups()[0]
        self.assertEqual(classify_group(spec, 127, "Required executable unavailable\n")["status"], "incomplete")
        self.assertEqual(classify_group(spec, -9, "partial output\n")["status"], "incomplete")

    def test_known_failure_survives_timeout_cancellation_and_unknown_exit(self):
        spec = registered_groups()[0]
        output = "FAIL: first (fixture.Test.first)\nRan 1 test in 0.1s\nFAILED (failures=1)\n"
        for code, options, reason in [(-15, {"timed_out": True}, "timeout"),
                                      (None, {"reason": "cancelled"}, "cancelled"),
                                      (None, {}, "exit_unknown"), (-9, {}, "child_signal"),
                                      (0, {"reason": "overall_budget"}, "overall_budget")]:
            with self.subTest(code=code, options=options):
                result = classify_group(spec, code, output, **options)
                self.assertEqual((result["status"], result["failed"], result["reason"]), ("failed", 1, reason))

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
sys.argv = ["test_all", "--output", {str(output)!r}, "--artifact-root", {str(Path(folder))!r}]
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


class TestEntryPointContracts(unittest.TestCase):
    def run_entrypoint(self, kind, exit_code=0):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="test-entrypoint-") as folder:
            temporary = Path(folder)
            binaries = temporary / "bin"
            binaries.mkdir()
            mapped_tmp = temporary / "mapped-tmp"
            mapped_tmp.mkdir()
            ledger = temporary / "entrypoint.json"
            python = binaries / "python"
            synthetic_check = "print('Ran 1 test in 0.1s'); print(); print('OK')"
            python.write_text("#!" + sys.executable + "\n" + f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(root)!r})
from scripts import test_all as runner
from scripts.verify_image_quality import test_artifact_root
sys.platform = 'linux'
artifact_root = test_artifact_root(repository=runner.ROOT)
assert sys.argv[1] == 'scripts/test_all.py'
assert sys.argv[2] == '--output'
assert Path(sys.argv[3]) == artifact_root / 'evidence'
assert not Path(sys.argv[3]).exists()
Path(os.environ['ENTRYPOINT_LEDGER']).write_text(json.dumps({{'root':str(artifact_root), 'bytecode':os.environ.get('PYTHONDONTWRITEBYTECODE')}}))
if {exit_code}:
    raise SystemExit({exit_code})
runner.registered_groups = lambda python=None: (runner.GroupSpec('synthetic', (sys.executable, '-c', {synthetic_check!r}), 2, 'unittest'),)
raise SystemExit(runner.main(sys.argv[2:]))
''')
            python.chmod(0o755)
            env = {"PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
                   "ENTRYPOINT_LEDGER": str(ledger), "ENTRYPOINT_TEMP": str(mapped_tmp),
                   "RUNNER_TEMP": str(mapped_tmp), "PYTHONDONTWRITEBYTECODE": "1"}
            if kind == "release":
                workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
                step = next(step for step in workflow["jobs"]["verify"]["steps"] if step["name"] == "Run automated tests")
                self.assertEqual(step["shell"], "bash")
                command = ["bash", "-c", step["run"]]
            else:
                recipe = (root / "Makefile").read_text().split("\ntest:\n\t", 1)[1].splitlines()[0]
                argv = shlex.split(recipe.replace("$$", "$"))
                self.assertEqual(argv[:7], ["docker", "compose", "run", "--rm", "workbench", "sh", "-ec"])
                # Map the container's /tmp into this test's isolated filesystem.
                mktemp = binaries / "mktemp"
                mktemp.write_text("#!" + sys.executable + "\n" + '''import os, sys, tempfile
assert sys.argv[1:] == ['-d', '/tmp/eval-tests.XXXXXX']
print(tempfile.mkdtemp(prefix='eval-tests.', dir=os.environ['ENTRYPOINT_TEMP']))
''')
                mktemp.chmod(0o755)
                command = argv[5:]
            completed = subprocess.run(command, env=env, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, exit_code, completed.stdout + completed.stderr)
            value = json.loads(ledger.read_text())
            artifact_root = Path(value["root"])
            self.assertEqual(artifact_root.parent, mapped_tmp.resolve())
            self.assertEqual(value["bytecode"], "1")
            if exit_code == 0:
                summary = json.loads((artifact_root / "evidence/summary.json").read_text())
                self.assertEqual(summary["status"], "passed")
                self.assertEqual(summary["groups"][0]["executed"], 1)

    def test_ci_and_makefile_prepare_external_linux_roots(self):
        for kind in ("release", "makefile"):
            with self.subTest(kind=kind):
                self.run_entrypoint(kind)

    def test_ci_and_makefile_preserve_test_failures(self):
        for kind in ("release", "makefile"):
            with self.subTest(kind=kind):
                self.run_entrypoint(kind, exit_code=17)


if __name__ == "__main__":
    unittest.main()
