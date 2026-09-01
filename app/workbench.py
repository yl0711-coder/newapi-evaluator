"""模型家族、平台倍率组和逐模型标杆的统一领域服务。"""
import time
from typing import Any

from . import hardbank, itembank, lifecycle, store


def family_out(row: dict[str, Any]) -> dict[str, Any]:
    models = store.query(
        "SELECT * FROM model_family_models WHERE family_id=? "
        "ORDER BY sort_order,display_name,id", (row["id"],))
    return {
        "id": row["id"], "name": row["name"],
        "models": [{
            "id": model["id"], "model": model["model"],
            "display_name": model["display_name"],
            "enabled": bool(model["enabled"]),
            "sort_order": model["sort_order"],
            "lifecycle_status": model["lifecycle_status"],
            "lifecycle_name": lifecycle.MODEL_STATES[model["lifecycle_status"]],
            "allowed_transitions": lifecycle.allowed_transitions(
                "model", model["lifecycle_status"]),
            "replacement_model_id": model["replacement_model_id"],
        } for model in models],
        "aliases": lifecycle.aliases(int(row["id"])),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_families() -> list[dict[str, Any]]:
    return [family_out(row) for row in
            store.query("SELECT * FROM model_families WHERE archived_at IS NULL "
                        "ORDER BY name,id")]


def family(family_id: int) -> dict[str, Any] | None:
    row = store.get("model_families", family_id)
    return family_out(row) if row else None


def enabled_family_models(family_id: int) -> list[dict[str, Any]]:
    return store.query(
        "SELECT * FROM model_family_models WHERE family_id=? AND enabled=1 "
        "AND lifecycle_status IN ('experimental','enabled','observe') "
        "ORDER BY sort_order,display_name,id", (family_id,))


def ensure_platform_slots(platform_group_id: int, family_id: int) -> None:
    now = time.time()
    for model in store.query(
        "SELECT model FROM model_family_models WHERE family_id=? ORDER BY sort_order,id",
        (family_id,),
    ):
        if not store.query(
            "SELECT id FROM platform_group_benchmarks "
            "WHERE platform_group_id=? AND model=?",
            (platform_group_id, model["model"]),
        ):
            store.insert("platform_group_benchmarks", {
                "platform_group_id": platform_group_id,
                "model": model["model"], "benchmark_id": None,
                "created_at": now, "updated_at": now,
            })


def benchmark_out(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    hard = store.loads(row.get("hard"), None)
    return {
        "id": row["id"], "name": row["name"],
        "pack_version": row["pack_version"],
        "model": row.get("benchmark_model") or row["model_hint"],
        "dims": store.loads(row["dims"], {}),
        "overall": row["overall"], "tolerance": row["tolerance"],
        "source_task_id": row["source_task_id"],
        "stale": row["pack_version"] != itembank.PACK_VERSION,
        "hard": hard,
        "hard_stale": bool(hard and hard.get("hard_version")
                           and hard.get("hard_version") != hardbank.HARD_VERSION),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def platform_group_out(row: dict[str, Any]) -> dict[str, Any]:
    family_row = store.get("model_families", row["family_id"])
    slots = store.query(
        "SELECT slots.*,family_models.display_name,family_models.enabled," 
        "family_models.lifecycle_status,family_models.replacement_model_id,"
        "family_models.sort_order FROM platform_group_benchmarks slots "
        "LEFT JOIN model_family_models family_models "
        "ON family_models.family_id=? AND family_models.model=slots.model "
        "WHERE slots.platform_group_id=? "
        "ORDER BY COALESCE(family_models.sort_order,10000),slots.model",
        (row["family_id"], row["id"]),
    )
    slot_output = []
    for slot in slots:
        benchmark = store.get("benchmarks", slot["benchmark_id"]) \
            if slot["benchmark_id"] else None
        slot_output.append({
            "id": slot["id"], "model": slot["model"],
            "display_name": slot["display_name"] or slot["model"],
            "enabled": bool(slot["enabled"]),
            "lifecycle_status": slot["lifecycle_status"],
            "lifecycle_name": lifecycle.MODEL_STATES.get(
                slot["lifecycle_status"], slot["lifecycle_status"]),
            "replacement_model_id": slot["replacement_model_id"],
            "benchmark": benchmark_out(benchmark),
        })
    return {
        "id": row["id"], "family_id": row["family_id"],
        "family_name": (family_row or {}).get("name", ""),
        "online_multiplier": row["multiplier"],
        "label": f"{(family_row or {}).get('name', '')} {float(row['multiplier']):g}x",
        "models": slot_output,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_platform_groups() -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT platform_groups.* FROM platform_groups "
        "JOIN model_families ON model_families.id=platform_groups.family_id "
        "WHERE platform_groups.archived_at IS NULL "
        "AND model_families.archived_at IS NULL "
        "ORDER BY model_families.name,platform_groups.multiplier")
    return [platform_group_out(row) for row in rows]


def platform_group(group_id: int) -> dict[str, Any] | None:
    row = store.get("platform_groups", group_id)
    if not row:
        return None
    ensure_platform_slots(group_id, row["family_id"])
    return platform_group_out(row)


def slot(platform_group_id: int, model: str) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM platform_group_benchmarks "
        "WHERE platform_group_id=? AND model=?", (platform_group_id, model))
    return rows[0] if rows else None


def benchmark_for_target(target: dict[str, Any]) -> dict[str, Any] | None:
    group_id = target.get("platform_group_id")
    if not group_id:
        return None
    benchmark_slot = slot(int(group_id), target["model"])
    if not benchmark_slot or not benchmark_slot["benchmark_id"]:
        return None
    return store.get("benchmarks", int(benchmark_slot["benchmark_id"]))


def source_channel(source_task_id: int | None) -> str:
    if not source_task_id:
        return ""
    task = store.get("tasks", source_task_id)
    if not task:
        return ""
    snapshot = store.loads(task["snapshot"], {})
    return snapshot.get("channel_name") or snapshot.get("group_name") \
        or task["target_name"].split(" · ")[0]
