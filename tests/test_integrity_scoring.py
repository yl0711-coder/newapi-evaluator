"""Independent statistical expectations and fixed-bank/denominator contracts."""
import copy
import unittest

from features.integrity.strategies import get_strategy, load_bank
from features.integrity.scoring import (score_strategy, source_verdict, one_sided_mcnemar,
    holm_bonferroni, project_observation, compare_paired_outcomes)
from features.integrity.evidence import analyze_account_evidence, validate_analysis_output
from tests.integrity_fixtures import account_evidence


class IntegrityScoringTests(unittest.TestCase):
    def test_bank_versions_request_ceiling_and_unlisted_model(self):
        daily, imported = get_strategy("modeltrace"), get_strategy("nerfed")
        self.assertEqual(len(load_bank(daily)["models"]), 17)
        self.assertEqual(len(load_bank(imported)["models"]), 16)
        self.assertNotEqual(daily.asset_hash, imported.asset_hash)
        self.assertEqual(imported.execution_mode, "import_only")
        self.assertEqual((daily.max_requests, daily.max_retries), (3, 0))
        result = score_strategy(daily, [], expected_model="gpt-6.1-sol")
        self.assertEqual(result["source_verdict"], "UNLISTED")
        self.assertFalse(result["identity_authenticated"])
        with self.assertRaises(ValueError):
            score_strategy(daily, [{"text": ""}]*4)

    def test_nerfed_threshold_requires_two_valid_answers(self):
        rows = [{"model": "other", "probability": .8, "score": .5},
                {"model": "expected", "probability": .2, "score": 0}]
        self.assertEqual(source_verdict("expected", rows, 1)["source_verdict"], "SUSPICIOUS")
        self.assertEqual(source_verdict("expected", rows, 2)["source_verdict"], "MISMATCH")
        self.assertEqual(source_verdict("unlisted", rows, 3)["source_verdict"], "UNLISTED")
        low = copy.deepcopy(rows); low[0]["probability"] = .79
        self.assertEqual(source_verdict("expected", low, 3)["source_verdict"], "SUSPICIOUS")

    def test_exact_mcnemar_holm_and_invalid_count_as_wrong(self):
        self.assertEqual(one_sided_mcnemar(10, 0), 1/1024)
        self.assertEqual(one_sided_mcnemar(0, 0), 1)
        old = {str(i): True for i in range(10)}
        compared = compare_paired_outcomes(old, {k: None for k in old})
        self.assertEqual((compared["paired_items"], compared["invalid_current_items"], compared["regressions"]), (10,10,10))
        self.assertEqual(compared["status"], "degraded")
        corrected = holm_bonferroni([.001,.02,.04,.2])
        self.assertEqual([v["reject"] for v in corrected], [True,False,False,False])
        self.assertAlmostEqual(corrected[1]["adjusted_p_value"], .06)

    def test_complete_canary_current_only_baseline_and_condition_drift(self):
        manifest = get_strategy("canary")
        self.assertEqual(len(manifest.probes), 192)
        self.assertEqual(len({p.family for p in manifest.probes}), 4)
        condition = {"provider":"synthetic", "protocol":"responses", "model":"gpt-6-astra",
                     "parameters":{"effort":"low"}, "budget":{"requests":192}}
        outputs = [{"probe_id":p.probe_id, "text":str(p.expected)} for p in manifest.probes]
        base = score_strategy(manifest, outputs, conditions=condition)
        self.assertEqual((base["status"],base["score"],base["comparison"]), ("current_only",1,None))
        equal = score_strategy(manifest, outputs, baseline=base, conditions=condition)
        self.assertEqual(equal["comparison"]["overall"]["one_sided_p_value"], 1)
        incomplete = score_strategy(manifest, outputs[:-1], baseline=base, conditions=condition)
        self.assertEqual((incomplete["status"],incomplete["not_run"]), ("incomplete",1))
        invalid = [{**v,"status":"truncated"} for v in outputs]
        loss = score_strategy(manifest, invalid, baseline=base, conditions=condition)
        self.assertEqual((loss["invalid"],loss["comparison"]["overall"]["paired_items"]), (192,192))
        drift = score_strategy(manifest, outputs, baseline=base, conditions={**condition,"protocol":"openai"})
        self.assertEqual(drift["status"], "invalid_comparison")
        changed = {**base,"scorer_hash":"0"*64}
        self.assertEqual(score_strategy(manifest, outputs, baseline=changed, conditions=condition)["status"], "invalid_comparison")

    def test_offline_evidence_whitelist_metadata_unknown_and_order(self):
        evidence = account_evidence()
        result = validate_analysis_output(analyze_account_evidence(evidence))
        self.assertEqual((result["status"],result["metadata_status"]), ("unknown","unavailable"))
        evidence["events"] = [{"type":"turn_context","turn_id":"t2","timestamp":"2026-10-08T02:00:00Z","effort":"low"},
                              {"type":"turn_context","turn_id":"t1","timestamp":"2026-10-08T01:00:00Z","effort":"high"}]
        result = validate_analysis_output(analyze_account_evidence(evidence))
        self.assertEqual(result["metadata"]["changes"][0]["direction"], "down")
        self.assertEqual(result["metadata"]["changes"][0]["settings_evidence"], "unknown")
        evidence["events"][0]["content"] = "synthetic forbidden body"
        with self.assertRaises(ValueError):
            analyze_account_evidence(evidence)

    def test_transport_invalid_projection_never_counts_valid(self):
        manifest = get_strategy("modeltrace")
        observed = project_observation(manifest, manifest.probes[0].probe_id,
                                       {"text":"42 "*300,"finish_reason":"length"})
        self.assertFalse(observed["valid"])
        result = score_strategy(manifest, [observed], expected_model="gpt-6-astra")
        self.assertEqual((result["valid_answers"],result["invalid_answers"],result["requested_answers"]), (0,1,1))

    def test_canary_malformed_json_is_invalid_and_retains_denominator(self):
        manifest = get_strategy("canary")
        for text in ("9"*5000, "["*2000+"0"+"]"*2000):
            observed = project_observation(manifest, manifest.probes[0].probe_id, {"text":text})
            self.assertFalse(observed["valid"])
            scored = score_strategy(manifest, [observed])
            # The complete capability bank is the denominator; absent answers
            # also fail, while not_run separately exposes missing collection.
            self.assertEqual(scored["invalid"], 192)
            self.assertEqual(scored["not_run"], 191)
