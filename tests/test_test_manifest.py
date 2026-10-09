import json
from pathlib import Path
import subprocess
import sys
import unittest

from scripts.test_manifest import admission, diagnosis, image_quality, workflow_control_plane, integrity, manifest_dict


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("/EXTERNAL_TEST_EVIDENCE")


class TestManifestTests(unittest.TestCase):
    def test_plans_have_unique_bounded_suites(self):
        plans = [
            admission(sys.executable, OUTPUT),
            admission(sys.executable, OUTPUT, sha="a" * 40),
            diagnosis(sys.executable, OUTPUT),
            diagnosis(sys.executable, OUTPUT, sha="b" * 40),
            image_quality(sys.executable, OUTPUT),
            workflow_control_plane(sys.executable, OUTPUT),
            integrity(sys.executable, OUTPUT),
        ]
        for suites in plans:
            self.assertTrue(suites)
            self.assertEqual(len({suite.suite_id for suite in suites}), len(suites))
            self.assertTrue(all(suite.timeout_seconds > 0 and suite.command for suite in suites))

    def test_candidate_manifest_adds_legacy_suite_only_for_sha(self):
        development = {suite.suite_id for suite in admission(sys.executable, OUTPUT)}
        candidate = {suite.suite_id for suite in admission(sys.executable, OUTPUT, sha="a" * 40)}
        self.assertNotIn("legacy-acceptance", development)
        self.assertIn("legacy-acceptance", candidate)

    def test_inspection_entrypoint_is_read_only_and_json(self):
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_test_plan.py", "admission"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
        self.assertEqual(value["plan"], "admission")
        self.assertEqual(value["suite_count"], len(admission(sys.executable, OUTPUT)))
        self.assertEqual(value["rules_version"], "1.0")
        self.assertTrue(all(row["suite_id"] for row in value["suites"]))

    def test_verify_entrypoints_import_manifest_when_run_as_scripts(self):
        # Direct script execution puts scripts/ (not the repository root) on sys.path.
        for script in ("verify_admission.py", "verify_diagnosis.py", "verify_image_quality.py", "verify_workflow_control_plane.py", "verify_integrity.py"):
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, f"scripts/{script}", "--help"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr[-500:])
                self.assertIn("--output", completed.stdout)

    def test_manifest_paths_are_external_placeholders_in_inspection(self):
        value = manifest_dict(diagnosis(sys.executable, OUTPUT), plan="diagnosis")
        command = next(row["command"] for row in value["suites"] if row["suite_id"] == "workbench-e2e")
        self.assertIn("/EXTERNAL_TEST_EVIDENCE/e2e", command)

    def test_workflow_registration_contains_execution_and_transport_regressions(self):
        suites = workflow_control_plane(sys.executable, OUTPUT)
        self.assertEqual([suite.suite_id for suite in suites], [
            "workflow-python", "workflow-syntax", "workflow-security", "workflow-web", "workflow-browser", "monitor-control-browser"])
        self.assertIn("tests.test_monitor_execution", suites[0].expected_modules)
        self.assertIn("tests.test_monitor_transport", suites[0].expected_modules)
        self.assertIn("tests.test_workflow_control_plane", suites[0].expected_modules)
        self.assertEqual(manifest_dict(suites, plan="workflow-control-plane")["suites"][0]["expected_modules"], list(suites[0].expected_modules))

    def test_integrity_plan_preserves_full_workbench_and_adds_consumers(self):
        suites = integrity(sys.executable, OUTPUT)
        ids = {row.suite_id for row in suites}
        self.assertTrue({row.suite_id for row in admission(sys.executable, OUTPUT)} <= ids)
        self.assertTrue({"integrity-inspect", "integrity-browser", "integrity-timetable-browser", "monitor-control-browser", "build-container"} <= ids)
        completed = subprocess.run([sys.executable, "scripts/inspect_test_plan.py", "integrity"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0)
        data = json.loads(completed.stdout)
        self.assertEqual(data["suite_count"], len(suites))
        self.assertEqual(data["plan"], "integrity")

    def test_integrity_container_and_inspection_results_are_classified(self):
        from scripts.verify_image_quality import classify
        from scripts.verify_diagnosis import classify as domain_classify
        container = next(s for s in integrity(sys.executable, OUTPUT) if s.suite_id == "build-container")
        self.assertEqual(container.kind, "container")
        report = {"status": "passed", "checks": [{"mode": m, "status": "passed"} for m in ("all", "image-quality")], "failure_count": 0}
        self.assertEqual(classify(0, json.dumps(report), container.kind)["status"], "passed")
        report["failure_count"] = 1
        self.assertEqual(classify(0, json.dumps(report), container.kind)["status"], "failed")
        inspected = subprocess.run([sys.executable, "scripts/inspect_integrity.py"], cwd=ROOT,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(domain_classify(inspected.returncode, inspected.stdout, "inspect")["status"], "passed")


if __name__ == "__main__":
    unittest.main()
