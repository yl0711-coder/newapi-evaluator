"""渠道导入与倍率快照的合成夹具，不读取用户文档。"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import unittest

from test_domain import DomainTests, outcome, observation, d
import channel_catalog
import manage


DOCUMENT = '''Alpha-0.4x
model_provider = "OpenAI"
model = "synthetic-model"
[model_providers.OpenAI]
base_url = "https://alpha.example/v1"
experimental_bearer_token = "sk-synthetic"
http_headers = { "x-openai-actor-authorization" = "synthetic-header" }
Alpha0.25x
https://alpha.example/v1 sk-second
Beta1x
https://beta.example/v1 sk-third
'''


class CatalogTests(DomainTests):
    def test_import_preserves_separate_rates_and_private_headers(self):
        channels, keys = channel_catalog.parse_document(DOCUMENT)
        self.assertEqual([c["multiplier"] for c in channels], [.4, .25, 1])
        self.assertEqual(len({c["id"] for c in channels}), 3)
        self.assertTrue(all(not c["enabled"] for c in channels))
        self.assertEqual(sum(not c["model_confirmed"] for c in channels), 2)
        self.assertEqual(keys[channels[0]["api_key_env"]]["headers"], {"x-openai-actor-authorization": "synthetic-header"})
        self.assertNotIn("sk-synthetic", json.dumps(channels))

    def test_default_model_must_be_explicit_to_confirm(self):
        channels, _ = channel_catalog.parse_document(DOCUMENT, "chosen-model")
        self.assertEqual(channels[0]["model"], "synthetic-model")
        self.assertEqual(channels[1]["model"], "chosen-model")
        self.assertTrue(all(c["model_confirmed"] for c in channels))

    def test_duplicate_or_ambiguous_sections_are_rejected(self):
        for text in (DOCUMENT + DOCUMENT, "Alpha0.4x\nhttps://alpha.example sk-first sk-second",
                     "Alpha0.4x\nhttps://name:secret@alpha.example sk-first"):
            with self.assertRaises(ValueError) as caught:
                channel_catalog.parse_document(text)
            self.assertNotIn("sk-", str(caught.exception))
            self.assertNotIn("secret@", str(caught.exception))

    def test_private_import_is_idempotent_and_report_contains_rates(self):
        source = self.root / "source.txt"; source.write_text(DOCUMENT)
        state = self.root / "state"
        for _ in range(2):
            result = manage.import_document(source, state)
            self.assertEqual(result["total"], 3)
        for name in ("config.json", "credentials.json"):
            self.assertEqual(stat.S_IMODE((state/name).stat().st_mode), 0o600)
        config = d.load_config(state / "config.json")
        self.assertEqual(len(config["channels"]), 3)
        report = (state / "reports/report.html").read_text()
        for label in ("Alpha-0.4x", "Alpha0.25x", "Beta1x", "0.4×", "0.25×", "1×",
                      "gpt-5.6-sol", "gpt-6-astra"):
            self.assertIn(label, report)
        for sensitive in ("sk-synthetic", "synthetic-header", "https://alpha.example"):
            self.assertNotIn(sensitive, report)

    def test_enable_uses_global_models_and_disable_preserves_keys(self):
        source = self.root / "source.txt"; source.write_text(DOCUMENT)
        state = self.root / "state"; manage.import_document(source, state)
        original = (state/"credentials.json").read_bytes()
        manage.configure_enabled(state, [], True)
        self.assertTrue(all(c["enabled"] for c in d.load_config(state/"config.json")["channels"]))
        self.assertEqual(d.load_config(state/"config.json")["test_models"], list(d.DEFAULT_TEST_MODELS))
        manage.configure_enabled(state, [], False)
        self.assertTrue(all(not c["enabled"] for c in d.load_config(state/"config.json")["channels"]))
        self.assertEqual((state/"credentials.json").read_bytes(), original)

    def test_credentials_reject_world_readable_or_header_override(self):
        path = self.root / "credentials.json"
        manage.write_private(path, {"credentials": {"EXAMPLE_KEY": {"api_key": "synthetic", "headers": {}}}})
        self.assertEqual(d.load_credentials(path)["EXAMPLE_KEY"]["api_key"], "synthetic")
        path.chmod(0o644)
        with self.assertRaises(ValueError): d.load_credentials(path)
        manage.write_private(path, {"credentials": {"EXAMPLE_KEY": {"api_key": "synthetic", "headers": {"Authorization": "override"}}}})
        with self.assertRaises(ValueError): d.load_credentials(path)

    def test_multiplier_and_model_snapshot_separate_same_hour(self):
        rows=[]
        for multiplier, model in ((.4, "model-a"), (.25, "model-a"), (.25, "model-b")):
            row = d.make_observation("same-channel", "reasoning", "chat", "low", 1, outcome(),
                                     "2099-01-01T00:00:00+00:00", "Asia/Shanghai",
                                     {"id": "stable-id", "provider": "Alpha", "multiplier": multiplier, "model": model})
            rows.append(row)
        self.store(rows)
        summaries = d.aggregate(self.db, True)
        self.assertEqual(len(summaries), 3)
        self.assertEqual([(r["multiplier"], r["model"]) for r in summaries], [(.4,"model-a"),(.25,"model-a"),(.25,"model-b")])
        d.build_report(self.db, self.root/"report.html", "Asia/Shanghai")
        report=(self.root/"report.html").read_text()
        self.assertIn("0.4×", report)
        self.assertIn("0.25×", report)

    def test_old_rows_keep_unknown_rate(self):
        self.store([observation()])
        summary=d.aggregate(self.db)[0]
        self.assertIsNone(summary["multiplier"])
        self.assertIn("倍率未记录", d.series_name(summary))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CatalogTests(name) for name in CatalogTests.__dict__ if name.startswith("test_"))
