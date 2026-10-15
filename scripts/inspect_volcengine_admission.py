"""Inspect the fixed Ark model/specification contract without opening runtime data."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.volcengine_admission import catalog


def inspect_contract():
    metadata = catalog.meta()
    models = [catalog.normalize_item(row["model_id"], row["default_spec"])
              for row in metadata["catalog"]]
    value = {
        "contract_version": metadata["contract_version"],
        "verified_at": metadata["verified_at"],
        "host_alias": "official-ark-beijing",
        "protocol": "ark-v3",
        "default_mode": metadata["default_mode"],
        "model_count": len(models),
        "default_model_count": sum(row["default_selected"] for row in metadata["catalog"]),
        "models": models,
        "generation_attempts_per_model": 1,
        "generation_retries": 0,
        "resume_scope": "query_known_video_task_ids_only",
        "key_storage": "process_memory_only",
        "billing_status": "unverified",
        "sources": metadata["sources"],
    }
    return {**value, "fingerprint": catalog.fingerprint(value), "requests_sent": 0,
            "database_opened": False, "credentials_read": False, "notifications_sent": 0}


if __name__ == "__main__":
    print(json.dumps(inspect_contract(), ensure_ascii=False, indent=2))
