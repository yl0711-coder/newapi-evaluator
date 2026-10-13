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
    def test_remediation_running_one_pair_never_claims_final_similarity(self):
        report = base_report()
        report["status"] = "running"
        report["questions"] += [{"id": "q2", "title": "题二", "difficulty": "hard", "prompt": ""}]
        report["measurements"] = [measurement(2, "q1", side) for side in reporting.SIDES]
        summary = reporting.finalize_report(report)["summary"]
        self.assertEqual(summary["overall"]["status"], "in_progress")
        self.assertEqual(summary["evidence"]["planned_pairs"], 2)
        self.assertFalse(summary["evidence"]["coverage_complete"])

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

    def test_token_high_risk_remains_visible_when_all_question_pairs_are_normal(self):
        report = base_report()
        report["measurements"] = [measurement(2, "q1", side) for side in reporting.SIDES]
        report["token_usage"] = {
            "conclusion": "high_risk", "severity": "high_risk", "billing_status": "unverified",
            "label": "输入 Token 用量上报显著偏高；实际计费待核实", "status": "pending",
            "candidate_input_tokens": 126000, "reference_input_tokens": 9000,
            "pairs": [{"ratio": 14}],
        }
        technical = reporting.finalize_report(report)
        self.assertEqual(technical["summary"]["overall"]["status"], "token_usage_high_risk")
        self.assertEqual(technical["token_usage"]["candidate_input_tokens"], 126000)
        public = reporting.public_report(report)
        self.assertEqual(public["token_usage"]["conclusion"], "high_risk")
        self.assertEqual(public["token_usage"]["billing_status"], "unverified")
        self.assertNotIn("pairs", public["token_usage"])
        self.assertNotIn("candidate_input_tokens", public["summary"]["token_usage"])

    def test_missing_token_evidence_does_not_become_runtime_equivalence(self):
        report = base_report()
        report["measurements"] = [measurement(2, "q1", side) for side in reporting.SIDES]
        report["token_usage"] = {"conclusion": "unable_to_determine", "label": "Token 检测无法判定"}
        self.assertEqual(reporting.finalize_report(report)["summary"]["overall"]["status"], "insufficient")
        report["token_usage"]["conclusion"] = "pending_confirmation"
        self.assertEqual(reporting.finalize_report(report)["summary"]["overall"]["status"], "insufficient")

    def test_old_report_without_token_evidence_preserves_its_summary(self):
        report = base_report()
        report["measurements"] = [measurement(2, "q1", side) for side in reporting.SIDES]
        public = reporting.public_report(report)
        self.assertEqual(public["summary"]["overall"]["status"], "runtime_similar")
        self.assertNotIn("token_usage", public)


if __name__ == "__main__":
    unittest.main()
