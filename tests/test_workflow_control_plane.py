"""Behavioral regressions for the registered, bounded workflow gate."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from scripts import verify_workflow_control_plane as gate
from scripts.test_manifest import SuiteSpec


def python_output(*, count=2, registered=2, failures=0, skipped=0, collection_errors=0):
    collection = {"registered_cases": registered, "collection_errors": collection_errors,
                  "modules": [{"module": "synthetic_cases", "case_count": registered,
                               "collection_errors": collection_errors}]}
    result = {"executed": count, "failed": failures, "errors": 0,
              "unexpected_successes": 0, "skipped": skipped}
    footer = f"FAILED (failures={failures})" if failures else f"OK (skipped={skipped})" if skipped else "OK"
    return ("WORKFLOW_COLLECTION=" + json.dumps(collection) + "\n"
            + f"Ran {count} tests in 0.001s\n\n{footer}\n"
            + "WORKFLOW_RESULT=" + json.dumps(result) + "\n")


def python_spec(name="workflow-python", command=()):
    return SuiteSpec(name, command or (sys.executable, "-c", "pass"), 2,
                     "workflow-unittest", ("synthetic_cases",))


class WorkflowClassificationTests(unittest.TestCase):
    def test_collection_execution_and_skip_evidence_are_required(self):
        self.assertEqual(gate.classify_suite(python_spec(), 0, python_output())["status"], "passed")
        for output in (python_output(count=0, registered=0), python_output(count=1),
                       python_output(skipped=1), "Ran 2 tests\n\nOK\n", ""):
            with self.subTest(output=output):
                self.assertEqual(gate.classify_suite(python_spec(), 0, output)["status"], "incomplete")

    def test_real_failure_and_dependency_gap_have_different_states(self):
        failed = gate.classify_suite(python_spec(), 1, python_output(failures=1))
        self.assertEqual((failed["status"], failed["failure_count"]), ("failed", 1))
        contradictory = python_output().replace("\nOK\n", "\nFAILED (failures=1)\n")
        self.assertEqual(gate.classify_suite(python_spec(), 0, contradictory)["status"], "failed")
        for code, output in [(127, "Required executable unavailable\n"),
                             (1, "Cannot find module 'playwright'\n"),
                             (1, python_output(collection_errors=1))]:
            with self.subTest(output=output):
                self.assertEqual(gate.classify_suite(python_spec(), code, output)["status"], "incomplete")
        self.assertEqual(gate.classify_suite(python_spec(), -9, "partial\n")["reason"], "child_signal")

    def test_non_python_checks_require_observable_results(self):
        cases = [
            ("syntax", {"status": "passed", "checks": 3, "skipped": 0}),
            ("security", {"passed": True, "files_checked": 3, "findings": []}),
            ("browser", {"status": "passed", "checks": 3, "mockRequests": 2}),
        ]
        for kind, data in cases:
            spec = SuiteSpec(kind, ("synthetic",), 1, kind)
            with self.subTest(kind=kind):
                self.assertEqual(gate.classify_suite(spec, 0, json.dumps(data))["status"], "passed")
                self.assertEqual(gate.classify_suite(spec, 0, "{}")["status"], "incomplete")
                self.assertEqual(gate.classify_suite(spec, 1, json.dumps(data))["status"], "failed")

    def test_known_failure_survives_timeout_cancellation_and_signal_exit(self):
        for code, reason, expected_reason in [(-15, "timeout", "timeout"),
                                              (None, "cancelled", "cancelled"),
                                              (None, None, "exit_unknown"), (-9, None, "child_signal")]:
            with self.subTest(code=code, reason=reason):
                result = gate.classify_suite(python_spec(), code, python_output(failures=1), reason=reason)
                self.assertEqual((result["status"], result["failure_count"], result["reason"]), ("failed", 1, expected_reason))
                self.assertEqual(gate.aggregate_status([result, {"status": "not_run"}]), "failed")
        partial = "ModuleNotFoundError: synthetic_dependency\nFAIL: first (fixture.Test.first)\nRan 1 test in 0.01s\nFAILED (failures=1)\n"
        self.assertEqual(gate.classify_suite(python_spec(), None, partial, reason="cancelled")["status"], "failed")

    def test_browser_assertion_without_success_counts_survives_timeout_or_cancel(self):
        spec = SuiteSpec("monitor-control-browser", ("node", "synthetic"), 1, "browser")
        output = json.dumps({"status": "failed", "error": "AssertionError"})
        for code, reason in [(-15, "timeout"), (None, "cancelled")]:
            with self.subTest(code=code, reason=reason):
                result = gate.classify_suite(spec, code, output, reason=reason)
                self.assertEqual((result["status"], result["failure_count"], result["reason"]), ("failed", 1, reason))
                self.assertFalse(result["failure_count_exact"])
        counted = json.dumps({"status": "failed", "failure_count": 2})
        self.assertEqual(gate.classify_suite(spec, None, counted, reason="cancelled")["failure_count"], 2)
        dependency = json.dumps({"status": "failed", "error": "ModuleNotFoundError"})
        self.assertEqual(gate.classify_suite(spec, None, dependency, reason="cancelled")["status"], "incomplete")
        self.assertEqual(gate.classify_suite(spec, -15, "partial output", reason="timeout")["status"], "incomplete")

    def test_loader_reports_actual_cases_and_preserves_failure_with_import_gap(self):
        module = types.ModuleType("synthetic_workflow_registered")

        class FailingCase(unittest.TestCase):
            def test_synthetic_failure(self):
                self.fail("synthetic assertion")

        module.FailingCase = FailingCase
        modules = (module.__name__, "synthetic_workflow_module_absent")
        stream = io.StringIO()
        with patch.dict(sys.modules, {module.__name__: module}), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = gate.run_python_tests(modules)
        spec = SuiteSpec("python", ("synthetic",), 1, "workflow-unittest", modules)
        result = gate.classify_suite(spec, code, stream.getvalue())
        self.assertEqual((result["registered_cases"], result["executed"], result["collection_errors"]), (2, 2, 1))
        self.assertEqual((result["status"], result["failure_count"]), ("failed", 1))


class WorkflowExecutionTests(unittest.TestCase):
    def run_plan(self, root, specs, *, budget=5):
        output = root / "evidence"
        with patch.object(gate, "workflow_control_plane", return_value=specs), contextlib.redirect_stdout(io.StringIO()):
            code = gate.main(["--output", str(output), "--artifact-root", str(root), "--overall-budget", str(budget)])
        return code, json.loads((output / "summary.json").read_text()), output

    def test_summary_is_preregistered_and_failure_survives_later_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            summary = root / "evidence" / "summary.json"
            first = ("import json; from pathlib import Path; "
                     f"v=json.loads(Path({str(summary)!r}).read_text()); "
                     "assert list(v['suites'])==['bad','good']; "
                     "assert v['suites']['good']['status']=='not_run'; "
                     "print('synthetic assertion',flush=True); raise SystemExit(3)")
            specs = [SuiteSpec("bad", (sys.executable, "-c", first), 1, "web"),
                     SuiteSpec("good", (sys.executable, "-c", "print('UI contract tests passed: synthetic')"), 1, "web")]
            code, value, output = self.run_plan(root, specs)
            self.assertEqual((code, value["status"]), (1, "failed"))
            self.assertEqual(value["counts"], {"passed": 1, "failed": 1, "incomplete": 0, "not_run": 0})
            self.assertIn("synthetic assertion", (output / "bad.log").read_text())

    def test_missing_executable_has_summary_and_does_not_mask_other_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            specs = [SuiteSpec("absent", (str(root / "absent-executable"),), 1, "web"),
                     SuiteSpec("good", (sys.executable, "-c", "print('UI contract tests passed: synthetic')"), 1, "web")]
            code, value, output = self.run_plan(root, specs)
            self.assertEqual((code, value["status"]), (1, "incomplete"))
            self.assertEqual(value["suites"]["absent"]["reason"], "missing_dependency")
            self.assertEqual(value["suites"]["good"]["status"], "passed")
            self.assertIn("Required executable unavailable", (output / "absent.log").read_text())

    def test_zero_collection_and_skips_remain_incomplete_in_the_durable_summary(self):
        for text in (python_output(count=0, registered=0), python_output(skipped=1)):
            with self.subTest(output=text), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                spec = python_spec(command=(sys.executable, "-c", f"print({text!r})"))
                code, value, _output = self.run_plan(root, [spec])
                self.assertEqual((code, value["status"], value["suites"][spec.suite_id]["status"]),
                                 (1, "incomplete", "incomplete"))

    def test_timeout_keeps_both_output_channels_and_enforces_overall_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = "import sys,time; print('partial stdout',flush=True); print('partial stderr',file=sys.stderr,flush=True); time.sleep(30)"
            specs = [SuiteSpec("slow", (sys.executable, "-c", script), 20, "web"),
                     SuiteSpec("later", (sys.executable, "-c", "print('must not run')"), 20, "web")]
            code, value, output = self.run_plan(root, specs, budget=.2)
            self.assertEqual((code, value["status"]), (1, "incomplete"))
            self.assertEqual(value["suites"]["slow"]["reason"], "timeout")
            self.assertEqual(value["suites"]["later"]["status"], "not_run")
            self.assertEqual(value["suites"]["later"]["reason"], "overall_budget")
            log = (output / "slow.log").read_text()
            self.assertIn("partial stdout", log)
            self.assertIn("partial stderr", log)

    def test_assertion_before_timeout_keeps_failed_overall_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = "import time; print('FAIL: first (fixture.Test.first)\\nRan 1 test in 0.01s\\nFAILED (failures=1)',flush=True); time.sleep(30)"
            specs = [SuiteSpec("failed-then-slow", (sys.executable, "-c", script), 20, "web"),
                     SuiteSpec("later", (sys.executable, "-c", "pass"), 20, "web")]
            code, value, _output = self.run_plan(root, specs, budget=.2)
            row = value["suites"]["failed-then-slow"]
            self.assertEqual((code, value["status"], row["status"], row["reason"], row["failure_count"]),
                             (1, "failed", "failed", "timeout", 1))
            self.assertTrue(row["timed_out"])
            self.assertEqual(value["suites"]["later"]["status"], "not_run")

    def test_existing_and_repository_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "existing"
            old.mkdir()
            marker = old / "summary.json"
            marker.write_text("unchanged")
            for output in (old, gate.ROOT / "gate-evidence"):
                with self.subTest(output=output), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    gate.main(["--output", str(output), "--artifact-root", str(root)])
                self.assertEqual(exc.exception.code, 2)
            self.assertEqual(marker.read_text(), "unchanged")

    def test_sigint_preserves_partial_summary_and_only_stops_owned_child(self):
        self.check_sigint(False)

    def test_assertion_before_sigint_keeps_failed_overall_result(self):
        self.check_sigint(True)

    def check_sigint(self, known_failure):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ready = root / "ready.json"
            output = root / "evidence"
            failure_output = "FAIL: first (fixture.Test.first)\nRan 1 test in 0.01s\nFAILED (failures=1)"
            child = ("import json,os,sys,time; from pathlib import Path; "
                     + "print('partial stdout',flush=True); print('partial stderr',file=sys.stderr,flush=True); "
                     + (f"print({failure_output!r},flush=True); " if known_failure else "")
                     + f"Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid()}})); time.sleep(30)")
            wrapper = f'''import sys
from pathlib import Path
sys.path.insert(0, {str(gate.ROOT)!r})
from scripts import verify_workflow_control_plane as runner
from scripts.test_manifest import SuiteSpec
runner.workflow_control_plane=lambda python, output: [
    SuiteSpec('slow', (sys.executable, '-c', {child!r}), 30, 'web'),
    SuiteSpec('later', (sys.executable, '-c', 'raise SystemExit(99)'), 30, 'web')]
sys.exit(runner.main(['--output', {str(output)!r}, '--artifact-root', {str(root)!r}]))
'''
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            process = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("owned child did not become ready")
                    time.sleep(.02)
                pid = json.loads(ready.read_text())["pid"]
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=8)
                self.assertEqual(process.returncode, 130)
                value = json.loads((output / "summary.json").read_text())
                self.assertTrue(value["cancelled"])
                self.assertEqual(value["status"], "failed" if known_failure else "incomplete")
                self.assertEqual(value["suites"]["slow"]["status"], "failed" if known_failure else "incomplete")
                self.assertEqual(value["suites"]["slow"]["reason"], "cancelled")
                self.assertEqual(value["suites"]["later"]["status"], "not_run")
                self.assertIsNone(unrelated.poll())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                text = (output / "slow.log").read_text()
                self.assertIn("partial stdout", text)
                self.assertIn("partial stderr", text)
            finally:
                for owned in (process, unrelated):
                    if owned.poll() is None:
                        os.killpg(owned.pid, signal.SIGKILL)
                    owned.communicate(timeout=5)
