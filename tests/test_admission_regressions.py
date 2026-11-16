"""Full-question-set regressions for the three v2.0.1 admission fixes; synthetic only."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from features.admission import api, main as engine, reporting, storage


EXPECTED_PROBES = {
    "injection-echo": "CCMAX_SENTINEL_7F31",
    "injection-priority": '{"source":"client","extra":false}',
    "injection-repeat": "CCMAX_REPEAT_2A91",
    "injection-identity-sentinel": "CCMAX_IDENTITY_3C72",
    "injection-identity-conflict": "CCMAX_IDENTITY_CONFLICT_91B4",
    "injection-identity-answer": "CCMAX_IDENTITY_ONLY_5D18",
}
DEVIATING_PROBES = {"injection-identity-sentinel", "injection-identity-conflict"}


def complete_report(rounds=2, *, deviating=()):
    questions = [{key: q[key] for key in ("id", "title", "difficulty", "prompt")}
                 for q in engine.questions_for(True)]
    report = {"version": 2, "created_at": "2026-11-16T00:00:00Z", "status": "completed",
              "rounds": rounds, "warmup_rounds": 1 if rounds >= 2 else 0,
              "questions": questions, "measurements": [], "responses": []}
    for side in ("candidate", "reference"):
        report[side] = {"base_url": "https://synthetic.example/v1", "model": "synthetic", "protocol": "openai"}
    for number in range(1, rounds + 1):
        for question in questions:
            for side in ("candidate", "reference"):
                metadata = {"round": number, "question_id": question["id"], "side": side,
                            "round_role": "warmup" if number <= report["warmup_rounds"] else "evaluated"}
                text = EXPECTED_PROBES.get(question["id"], "Synthetic answer")
                if side == "candidate" and question["id"] in deviating:
                    text = "I am Claude."
                report["measurements"].append({**metadata, "ok": True, "status": "completed",
                                               "total_ms": 100, "first_answer_ms": 20})
                report["responses"].append({**metadata, "content": text})
    return report


class InjectionCoverageRegressions(unittest.TestCase):
    def summary(self, report):
        return reporting.finalize_report(report)["summary"]["injection_probe"]

    def test_full_real_question_set_has_zero_evidence_when_both_sides_conform(self):
        summary = self.summary(complete_report())
        self.assertTrue(summary["full_coverage"])
        self.assertEqual(summary["classification"], "zero_injection_evidence")

    def test_full_real_question_set_detects_repeated_candidate_only_deviation(self):
        summary = self.summary(complete_report(deviating=DEVIATING_PROBES))
        self.assertEqual(summary["classification"], "micro_injection_evidence")
        self.assertEqual(set(summary["qualifying_probe_ids"]), DEVIATING_PROBES)
        self.assertEqual(summary["repeated_rounds"], 2)  # Warmup is still instruction evidence.

    def test_missing_measurement_or_response_is_insufficient(self):
        for field in ("measurements", "responses"):
            with self.subTest(field=field):
                report = complete_report()
                report[field] = [row for row in report[field]
                                 if not (row["round"] == 2 and row["question_id"] == "injection-echo"
                                         and row["side"] == "reference")]
                summary = self.summary(report)
                self.assertFalse(summary["full_coverage"])
                self.assertEqual(summary["classification"], "indeterminate")

    def test_missing_declared_round_is_not_mistaken_for_full_coverage(self):
        report = complete_report(3)
        for field in ("measurements", "responses"):
            report[field] = [row for row in report[field] if row["round"] != 3]
        self.assertFalse(self.summary(report)["full_coverage"])
        self.assertEqual(self.summary(report)["classification"], "indeterminate")

    def test_one_round_or_one_deviating_probe_remains_insufficient(self):
        for report in (complete_report(1), complete_report(deviating={"injection-identity-sentinel"})):
            with self.subTest(rounds=report["rounds"]):
                self.assertEqual(self.summary(report)["classification"], "indeterminate")

    def test_failure_cancellation_and_running_snapshots_never_claim_a_classification(self):
        for status in ("canceled", "failed", "running"):
            with self.subTest(status=status):
                report = complete_report(deviating=DEVIATING_PROBES)
                report["status"] = status
                self.assertEqual(self.summary(report)["classification"], "indeterminate")
        report = complete_report()
        next(row for row in report["measurements"] if row["question_id"] == "injection-echo")["ok"] = False
        self.assertEqual(self.summary(report)["classification"], "indeterminate")

    def test_legacy_probe_reports_remain_readable_and_unclassified(self):
        report = complete_report()
        report["questions"] = [q for q in report["questions"] if not q["id"].startswith("injection-identity-")]
        for field in ("measurements", "responses"):
            report[field] = [row for row in report[field] if not row["question_id"].startswith("injection-identity-")]
        self.assertEqual(self.summary(report)["classification"], "indeterminate")


class AdmissionReportLimitRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="admission-regressions-")
        storage.close()
        self.path = patch.object(storage, "DB_PATH", Path(self.temp.name) / "reports.db")
        self.path.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        storage.close()
        self.path.stop()
        self.temp.cleanup()

    async def test_complete_two_three_and_five_round_reports_save_and_read_back(self):
        for rounds in (2, 3, 5):
            with self.subTest(rounds=rounds):
                report = complete_report(rounds)
                report["pairs"] = reporting.build_pairs(report)
                response = await self.client.post("/api/reports", json=report)
                self.assertEqual(response.status_code, 201, response.text)
                saved = response.json()
                detail = (await self.client.get(f"/api/reports/{saved['id']}")).json()
                self.assertEqual(len(detail["responses"]), 12 * rounds * 2)
                self.assertEqual(len(detail["pairs"]), 12 * rounds)
                self.assertEqual(detail["summary"]["injection_probe"]["classification"], "zero_injection_evidence")
                ordinary = (await self.client.get(f"/api/reports/{saved['id']}/ordinary")).json()
                self.assertNotIn("responses", ordinary)
                self.assertEqual(ordinary["summary"]["injection_probe"], detail["summary"]["injection_probe"])

    async def test_global_row_limits_still_reject_one_extra_record(self):
        for field in ("measurements", "responses", "pairs"):
            with self.subTest(field=field):
                report = complete_report(5)
                report["pairs"] = reporting.build_pairs(report)
                report[field].append(deepcopy(report[field][0]))
                self.assertEqual((await self.client.post("/api/reports", json=report)).status_code, 422)

    async def test_canceled_partial_report_is_still_accepted(self):
        report = complete_report(3)
        report["status"] = "canceled"
        report["measurements"] = report["measurements"][:1]
        report["responses"] = report["responses"][:1]
        response = await self.client.post("/api/reports", json=report)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["summary"]["injection_probe"]["classification"], "indeterminate")

    async def test_existing_report_byte_budget_is_not_bypassed_by_larger_row_limit(self):
        with patch.object(storage, "MAX_REPORT_BYTES", 256):
            response = await self.client.post("/api/reports", json=complete_report(3))
        self.assertEqual(response.status_code, 413)


class AdmissionHtmlExportRegressions(unittest.TestCase):
    def test_html_exports_include_the_page_conclusion_reason_and_escape_text(self):
        source = Path(__file__).resolve().parents[1] / "features/admission/web/app.js"
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = vm.createContext({cacheSummaryText: () => '-', endpointText: () => 'synthetic',
  reportStatus: {completed: 'completed'}});
vm.runInContext(source.match(/^const escapeHtml = .*$/m)[0], context);
const helperStart = source.indexOf('function injectionSummary(');
if (helperStart >= 0) vm.runInContext(source.slice(helperStart, source.indexOf('\nfunction ', helperStart + 1)), context);
const start = source.indexOf('function reportHtml(');
vm.runInContext(source.slice(start, source.indexOf('\nconst queueCards', start)), context);
for (const [classification, label] of [['micro_injection_evidence','发现微注入证据'],
  ['zero_injection_evidence','未发现注入证据'], ['indeterminate','证据不足']]) {
  const summary = {injection_probe: {classification, reason: 'Synthetic <img src=x onerror=alert(1)>'}};
  for (const technical of [false, true]) {
    const html = context.reportHtml({summary, pairs: [], candidate: {}, reference: {}}, technical);
    assert.ok(html.includes('注入保真专项') && html.includes(label));
    assert.ok(html.includes('Synthetic &lt;img'));
    assert.ok(!html.includes('<img src=x'));
  }
}
assert.ok(!context.reportHtml({summary: {}, pairs: []}, false).includes('注入保真专项'));
console.log(JSON.stringify({cases: 7, passed: true}));
'''
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
