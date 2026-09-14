import unittest

from features.admission import reporting


def measurement(round_number, question_id, side, *, ok=True, status="completed", total=1000, category="", model="demo"):
    return {
        "type": "side_finished",
        "round": round_number,
        "round_role": "warmup" if round_number == 1 else "evaluated",
        "pair_id": f"run:{round_number}:{question_id}",
        "question_id": question_id,
        "side": side,
        "ok": ok,
        "status": status,
        "error_category": category,
        "request_started_ms": 1000 if side == "candidate" else 1004,
        "first_answer_ms": total // 2,
        "total_ms": total,
        "actual_model": model,
    }


def base_report():
    return {
        "version": 2,
        "run_id": "run",
        "created_at": "2026-09-13T00:00:00Z",
        "status": "completed",
        "rounds": 2,
        "warmup_rounds": 1,
        "candidate": {"base_url": "https://candidate.example/v1", "model": "demo", "protocol": "openai"},
        "reference": {"base_url": "https://reference.example/v1", "model": "demo", "protocol": "openai"},
        "questions": [{"id": "q1", "title": "题一", "difficulty": "easy", "prompt": ""}],
        "measurements": [],
        "responses": [],
    }


class AdmissionReportingTests(unittest.TestCase):
    def test_warmup_is_excluded_and_candidate_failure_is_attributed(self):
        report = base_report()
        report["measurements"] = [
            measurement(1, "q1", "candidate"), measurement(1, "q1", "reference"),
            measurement(2, "q1", "candidate", ok=False, status="error", category="read_timeout"),
            measurement(2, "q1", "reference"),
        ]
        result = reporting.finalize_report(report)
        self.assertEqual(len(result["pairs"]), 2)
        self.assertEqual(result["summary"]["counts"]["candidate_only"], 1)
        self.assertEqual(result["summary"]["evidence"]["evaluated_pairs"], 1)
        self.assertEqual(result["summary"]["overall"]["status"], "candidate_deviation")

    def test_matching_failures_are_shared_not_candidate_faults(self):
        report = base_report()
        report["measurements"] = [
            measurement(2, "q1", "candidate", ok=False, status="error", category="http_429"),
            measurement(2, "q1", "reference", ok=False, status="error", category="http_429"),
        ]
        result = reporting.finalize_report(report)
        self.assertEqual(result["pairs"][0]["attribution"], "shared")
        self.assertEqual(result["summary"]["counts"]["candidate_only"], 0)
        self.assertEqual(result["summary"]["counts"]["shared"], 1)

    def test_response_model_mismatch_is_visible_without_proving_identity(self):
        report = base_report()
        report["measurements"] = [
            measurement(2, "q1", "candidate", model="different"),
            measurement(2, "q1", "reference", model="demo"),
        ]
        result = reporting.finalize_report(report)
        self.assertEqual(result["summary"]["model_identity"]["candidate"]["status"], "mismatch")
        self.assertEqual(result["summary"]["model_identity"]["reference"]["status"], "matched")

    def test_faster_but_shorter_answers_are_not_presented_as_a_plain_speed_win(self):
        report = base_report()
        report["questions"].append({"id":"q2","title":"题二","difficulty":"hard","prompt":""})
        for question_id in ("q1", "q2"):
            report["measurements"].extend([
                measurement(2, question_id, "candidate", total=500),
                measurement(2, question_id, "reference", total=1000),
            ])
            report["responses"].extend([
                {"round":2,"question_id":question_id,"side":"candidate","content":"短答"},
                {"round":2,"question_id":question_id,"side":"reference","content":"较完整的参照端回答内容"},
            ])
        result = reporting.finalize_report(report)
        self.assertEqual(result["summary"]["speed"]["status"], "candidate_faster_shorter")

    def test_ordinary_report_excludes_raw_answers_prompts_and_urls(self):
        report = base_report()
        report["measurements"] = [measurement(2, "q1", side) for side in reporting.SIDES]
        report["responses"] = [
            {"round": 2, "question_id": "q1", "side": side, "content": "raw", "reasoning": "trace"}
            for side in reporting.SIDES
        ]
        public = reporting.public_report(report)
        self.assertNotIn("responses", public)
        self.assertNotIn("questions", public)
        self.assertNotIn("base_url", public["candidate"])
        self.assertNotIn("base_url", public["reference"])


if __name__ == "__main__":
    unittest.main()
