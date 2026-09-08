"""Field-mapping seam for the future Feishu Bitable integration.

No business field is selected yet.  Human review creates an outbox intent, and
this module will become the only place that turns a reviewed run into Feishu
fields after the owner confirms the exact record schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FieldMapping:
    feishu_field: str
    value: Callable[[dict[str, Any]], Any]


# Deliberately empty until the owner supplies the fields to record.
FEISHU_FIELD_MAPPINGS: tuple[FieldMapping, ...] = ()


def build_outbox_payload(run: dict[str, Any]) -> dict[str, Any]:
    fields = {
        item.feishu_field: item.value(run)
        for item in FEISHU_FIELD_MAPPINGS
    }
    return {
        "schema_version": 1,
        "record_key": f"admission-run:{run['id']}",
        "source_run_id": run["id"],
        "mapping_status": (
            "ready" if FEISHU_FIELD_MAPPINGS else "awaiting_field_definition"
        ),
        "fields": fields,
    }
