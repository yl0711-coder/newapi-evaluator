import hashlib
import json
import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from features.channel_monitor.storage import encode as channel_encode
from features.daily_checks.domain import encode as daily_encode
from features.integrity.durable import _json as integrity_json, clean_projection
from features.integrity.strategies import canonical_hash as integrity_canonical_hash
from features.model_coverage.monitor import body_hash, canonical, sign
from features.model_coverage.monitor import _json as monitor_json
from features.model_coverage.workflow import WorkflowQueue
from features.stability.app.layered import canonical_hash as layered_canonical_hash
from features.admission.request_contract import fingerprint as admission_request_fingerprint
from features.protocol_admission.catalog import fingerprint as protocol_fingerprint
from features.token_usage.storage import encode as token_encode
from features.token_usage.domain import condition_hash, digest as token_digest
from features.volcengine_admission.catalog import fingerprint as volcengine_fingerprint
from shared.json_codec import encode_json


class _SQLiteStore:
    """Minimal isolated SQLite owner for stores that only require ``connect``."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


class JsonCodecTests(unittest.TestCase):
    VALUE = {"z": "雪", "a": [1, 1.25, True, None], "nested": {"β": "é", "count": 2}}
    LEGACY_TEXT = '{"a":[1,1.25,true,null],"nested":{"count":2,"β":"é"},"z":"雪"}'
    LEGACY_DIGEST = "890bd62fd995fe70a299190a59d9e41762d9f6660a58b4cc5b26b6793e2b8ea4"

    def test_shared_and_legacy_names_match_golden_text_and_digest(self):
        encoders = (encode_json, channel_encode, daily_encode, token_encode, integrity_json, monitor_json)
        for encoder in encoders:
            with self.subTest(encoder=encoder.__module__):
                self.assertIsInstance(encoder(self.VALUE), str)
                self.assertEqual(encoder(self.VALUE), self.LEGACY_TEXT)
                self.assertEqual(hashlib.sha256(encoder(self.VALUE).encode("utf-8")).hexdigest(), self.LEGACY_DIGEST)

    def test_non_finite_numbers_are_rejected_by_all_legacy_names(self):
        encoders = (encode_json, channel_encode, daily_encode, token_encode, integrity_json, monitor_json)
        for encoder in encoders:
            for number in (math.nan, math.inf, -math.inf):
                with self.subTest(encoder=encoder.__module__, number=number):
                    with self.assertRaises(ValueError):
                        encoder({"number": number})

    def test_utf8_bytes_are_unchanged_for_every_legacy_consumer(self):
        expected_bytes = self.LEGACY_TEXT.encode("utf-8")
        encoders = (encode_json, channel_encode, daily_encode, token_encode, integrity_json, monitor_json)
        for encoder in encoders:
            with self.subTest(encoder=encoder.__module__):
                encoded = encoder(self.VALUE)
                self.assertEqual(encoded.encode("utf-8"), expected_bytes)
                self.assertEqual(hashlib.sha256(encoded.encode("utf-8")).hexdigest(), self.LEGACY_DIGEST)

    def test_durable_projection_bytes_and_digest_are_pinned(self):
        projection = clean_projection({
            "status": "completed", "valid": True, "request_id": "synthetic:req/1",
            "vector": [1, 1.25, -2.5], "format_errors": ["none"], "row_counts": [1, 2],
        })
        expected = '{"format_errors":["none"],"request_id":"synthetic:req/1","row_counts":[1,2],"status":"completed","valid":true,"vector":[1,1.25,-2.5]}'
        self.assertEqual(integrity_json(projection), expected)
        self.assertEqual(integrity_json(projection).encode("utf-8"), expected.encode("utf-8"))
        self.assertEqual(hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                         "bfc501d261f8133469da16b410104dd86030788ac0a506c36d2ca41f0fb2b115")

    def test_monitor_body_hash_and_hmac_cover_exact_utf8_bytes(self):
        body = {"z": "雪", "a": [1, 1.25, True, None]}
        raw = '{"z":"雪","a":[1,1.25,true,null]}'
        self.assertEqual(body_hash(body),
                         "b0849b9362b976f0d89cd11f735c730626ada26e72b4626e1c739ed7bfe8c673")
        raw_digest = "4fc2e8282fb1100179f0ba77feda196c009f6893e07b07938f5839e86f18fcb9"
        self.assertEqual(hashlib.sha256(raw.encode("utf-8")).hexdigest(), raw_digest)
        timestamp = "2026-10-18T00:00:00Z"
        nonce = "synthetic-nonce-001"
        path = "/internal/v1/jobs?x=é"
        secret = b"synthetic-monitor-secret-012345678901"
        expected_canonical = ("POST\n/internal/v1/jobs?x=é\n2026-10-18T00:00:00Z\n"
                              "synthetic-nonce-001\n" + raw_digest).encode("utf-8")
        self.assertEqual(canonical("post", path, timestamp, nonce, raw.encode("utf-8")), expected_canonical)
        self.assertEqual(sign(secret, "post", path, timestamp, nonce, raw.encode("utf-8")),
                         "v1=79fbb6db6747f3b32fce36684be9a23be0adcb1fc4e350ac747054d36f918284")

    def test_similar_hash_contracts_keep_their_own_golden_bytes(self):
        self.assertEqual(integrity_canonical_hash({"n": 1.0, "nested": [2.0, 2.5], "u": "雪"}),
                         "4ba09f9b6d5cd6ba31d5502e6a4aedf21e854c8ed277fd0078d7f6f6ad278a13")
        self.assertEqual(condition_hash({"minor_ratio": 1.25, "minor_difference": 256}),
                         "c22e303de8e75a326f6f97b324110dad26ec9e8b9eed0c8666843b66d9b8cf06")
        for name, function in (
            ("layered", layered_canonical_hash),
            ("admission_request", admission_request_fingerprint),
            ("token", token_digest),
            ("protocol", protocol_fingerprint),
            ("volcengine", volcengine_fingerprint),
        ):
            with self.subTest(contract=name):
                self.assertEqual(function(self.VALUE), self.LEGACY_DIGEST)

    def test_old_daily_and_token_db_json_remains_readable(self):
        legacy_spec = '{"notify":true,"profiles":[],"version":"online-channel-daily-v1"}'
        with tempfile.TemporaryDirectory(prefix="json-codec-db-") as root:
            registry = _SQLiteStore(Path(root) / "daily.db")
            from features.daily_checks.storage import Store as DailyStore
            daily = DailyStore(registry)
            with registry.connect() as db:
                db.execute("INSERT INTO daily_check_plan VALUES(1,?,?,?,?)",
                           (legacy_spec, "legacy-revision", 1, 1.0))
            self.assertEqual(daily.plan()["version"], "online-channel-daily-v1")
            self.assertEqual(daily.plan()["profiles"], [])

            from features.token_usage.storage import Store as TokenStore
            token = TokenStore(Path(root) / "token")
            old_snapshot = '{"model":"synthetic-model","protocol":"openai"}'
            old_thresholds = '{"minor_difference":128,"minor_ratio":1.1}'
            with token.connect() as db:
                db.execute("INSERT INTO bindings VALUES(?,?,?,?,?)",
                           ("synthetic-model", "openai", old_snapshot, "legacy-revision", old_thresholds))
            self.assertEqual(token.binding("synthetic-model", "openai"), {
                "model": "synthetic-model", "protocol": "openai",
                "snapshot": {"model": "synthetic-model", "protocol": "openai"},
                "revision": "legacy-revision", "thresholds": {"minor_difference": 128, "minor_ratio": 1.1},
            })

    def test_old_durable_and_monitor_db_json_remains_readable(self):
        # These rows emulate the pre-C1 pretty/default JSON text.  The read path
        # must parse it without rewriting the stored bytes or requiring a migration.
        from shared.registry import Registry
        from features.integrity.durable import IntegrityStore
        from features.integrity.execution import ProbeRequest
        from features.model_coverage.monitor import MonitorStore

        with tempfile.TemporaryDirectory(prefix="json-codec-legacy-") as root:
            registry = Registry(Path(root) / "registry")
            durable = IntegrityStore(registry)
            request = ProbeRequest("synthetic:req", {"prompt": "synthetic", "max_tokens": 1}, 1, 1)
            target = {"registry_channel_id": 1, "connection_fingerprint": "a" * 64,
                      "model": "synthetic-model", "canonical_model": "synthetic-model", "protocol": "openai"}
            job = durable.enqueue(idempotency_key="synthetic-legacy", target_snapshot=target,
                                  strategy={"strategy_id": "synthetic", "version": "synthetic-v1"}, requests=[request],
                                  limits={"max_requests": 1, "max_input_tokens": 8, "max_output_tokens": 8},
                                  deadline=time.time() + 60)
            with registry.connect() as db:
                db.execute("UPDATE integrity_jobs SET target_json=?,strategy_json=?,manifest_json=?,limits_json=?,skipped_json=? WHERE job_id=?",
                           ('{"protocol": "openai", "model": "synthetic-model", "canonical_model": "synthetic-model", "connection_fingerprint": "' + "a" * 64 + '", "registry_channel_id": 1}',
                            '{"version": "雪", "strategy_id": "synthetic"}',
                            '[{"request_id": "synthetic:req", "identity_hash": "' + request.manifest()["identity_hash"] + '", "input_tokens_reserved": 1, "output_tokens_reserved": 1}]',
                            '{"max_requests": 1, "max_input_tokens": 8, "max_output_tokens": 8}', '[]', job["job_id"]))
            loaded = durable.job(job["job_id"])
            self.assertEqual(loaded["target_snapshot"]["model"], "synthetic-model")
            self.assertEqual(loaded["strategy"]["version"], "雪")
            self.assertEqual(loaded["request_manifest"][0]["request_id"], "synthetic:req")

            monitor = MonitorStore(registry)
            legacy_channels = '[{"channel_identity": "synthetic-channel", "models": ["synthetic-model"]}]'
            with registry.connect() as db:
                db.execute("INSERT INTO monitor_inventories VALUES(?,?,?,?,?,?)",
                           ("synthetic-inventory", "legacy-digest", 1.0, 1.0, "synthetic-newapi", legacy_channels))
            self.assertEqual(monitor.inventory_channel("synthetic-inventory", "synthetic-channel"), {
                "channel_identity": "synthetic-channel", "models": ["synthetic-model"]})

    def test_workflow_queue_keeps_legacy_default_separator_digest(self):
        payload = self.VALUE
        expected = "9abc23d38cb41ff47ea5332b44f17672f9cba770898f983871db9dfed65ce659"
        with tempfile.TemporaryDirectory(prefix="json-codec-workflow-") as root:
            queue = WorkflowQueue(_SQLiteStore(Path(root) / "workflow.db"))
            task = queue.enqueue("synthetic", payload, idempotency_key="synthetic-queue-001")
            self.assertEqual(task["payload"]["payload_hash"], expected)
            self.assertEqual(task["payload"]["payload"], payload)


class AdmissionJobDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_job_identity_digest_uses_legacy_default_json_bytes(self):
        from shared.registry import Registry
        from features.admission.api import CompareInput
        from features.admission.jobs import AdmissionManager

        with tempfile.TemporaryDirectory(prefix="json-codec-admission-") as root:
            root = Path(root)
            registry = Registry(root / "registry")
            reference = registry.save({"base_url": "https://reference.example/v1",
                                       "api_key": "synthetic-reference-key", "multiplier": 1})
            manager = AdmissionManager(root / "admission" / "reports.db", registry)
            body = CompareInput(
                candidate={"base_url": "https://candidate.example/v1", "api_key": "synthetic-candidate-key",
                           "model": "synthetic-model", "protocol": "openai"},
                reference={"channel_id": reference["id"], "model": "synthetic-model", "protocol": "openai"},
                rounds=1,
            )
            result = await manager.enqueue(body, idempotency_key="synthetic-admission-job")
            task = manager.store.queue.get(result["id"])
            configuration = manager.store.get(result["id"])["configuration"]
            identity_configuration = {
                **configuration,
                **{side: {key: value for key, value in configuration[side].items() if key != "name"}
                   for side in ("candidate", "reference")},
            }
            # Keep this expectation independent of the production digest helper:
            # the historical admission contract uses sorted keys, UTF-8, and
            # json.dumps' default separators (with spaces).
            expected = hashlib.sha256(json.dumps(identity_configuration, sort_keys=True,
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
            self.assertEqual(task["payload"]["payload"]["configuration_digest"], expected)
