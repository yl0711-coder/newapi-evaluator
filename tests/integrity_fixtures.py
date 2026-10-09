"""Self-authored synthetic production/account evidence; never customer data."""
import time

from features.model_coverage.monitor import MonitorStore
from features.model_coverage.production import ProductionCoverage


def production_binding(registry, channel_id, *, source="synthetic-source", version="synthetic-v1",
                       identity=None, generated_at=None, status="online", eval_channel_id=None):
    identity = identity or f"synthetic-channel-{channel_id}"
    MonitorStore(registry).bind_identity(channel_id, identity)
    return ProductionCoverage(registry).import_snapshot({
        "source": source, "version": version, "generated_at": generated_at or time.time(),
        "items": [{"channel_identity": identity, "model": model, "protocol": "responses",
                   "production_status": status, "eval_channel_id": eval_channel_id or channel_id}
                  for model in ("gpt-6-astra", "gpt-6.1-sol")]})


def account_evidence():
    return {"schema": "integrity-official-account-evidence/v1", "account_alias": "synthetic-account",
            "expected_model": "gpt-6-astra", "authorization": {"authorized": True,
            "basis": "self_authored", "source": "synthetic_regression_fixture"},
            "provenance": {"source": "official_account", "trusted": True, "complete": True},
            "events_coverage_complete": False, "events": [], "catalog": []}
