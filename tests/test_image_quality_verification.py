import os
import sys
import unittest

from scripts.verify_image_quality import classify, run_process


class VerificationTests(unittest.TestCase):
    def test_nonzero_exit_cannot_be_overridden_by_success_text(self):
        result = classify(1, "UI contract tests passed:", "web")
        self.assertEqual(result["status"], "failed")

    def test_empty_or_unknown_output_never_counts_as_a_check(self):
        for kind in ["syntax", "inspect", "security", "web", "e2e", "browser", "container"]:
            self.assertEqual(classify(0, "", kind)["status"], "failed")
            self.assertEqual(classify(0, "{}", kind)["status"], "failed")

    def test_missing_framework_suites_or_skips_fail(self):
        for text in ["Ran 0 tests\nOK", "Ran 1 test\nOK (skipped=1)",
                     "Ran 4 tests\nOK\ntest_image_quality"]:
            self.assertEqual(classify(0, text, "unittest")["status"], "failed")

    def test_bounded_subprocess_keeps_error_exit_and_output(self):
        code, output, timed_out = run_process([sys.executable, "-c", "import sys; print('synthetic failure',file=sys.stderr); sys.exit(3)"], os.environ, 3)
        self.assertEqual(code, 3)
        self.assertIn("synthetic failure", output)
        self.assertFalse(timed_out)

    def test_timeout_terminates_only_owned_process(self):
        code, _output, timed_out = run_process([sys.executable, "-c", "import time; time.sleep(10)"], os.environ, 0.1)
        self.assertTrue(timed_out)
        self.assertNotEqual(code, 0)
