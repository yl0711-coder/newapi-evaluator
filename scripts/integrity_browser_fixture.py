"""Owned Mock browser fixture setup and timing driver; not a product endpoint.

Only a fresh external data root is accepted. All upstream connections are guarded
and restricted to this test's loopback server; no real data or notifications.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
data=Path(os.environ["PLATFORM_DATA_DIR"]).resolve()
if data==ROOT or ROOT in data.parents or not data.name.startswith("integrity-ui-"):
    raise RuntimeError("owned external integrity-ui data required")


async def main():
    from shared.registry import get_registry
    from features.integrity.service import ReviewService
    from features.stability.app import storage,layered,integrity
    from tests.test_kbf_review import synthetic_reference
    if sys.argv[1]=="references":
        packages={method:synthetic_reference(method,count=4) for method in ("hlwy","kbf")}
        (data/"references.json").write_text(json.dumps(packages))
        print(json.dumps({method:package["package_hash"] for method,package in packages.items()}))
        return
    if sys.argv[1] == "timetable":
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        from features.stability.app import timetable, timetable_executor as executor
        schedule = storage.get_schedule(int(sys.argv[2]))
        if schedule["enabled"] or schedule["plan_version"] != timetable.VERSION:
            raise RuntimeError("disabled owned timetable fixture required")
        day = datetime.now(ZoneInfo(schedule["timezone"])).date() + timedelta(days=2)
        midnight = timetable.local_epoch(day, "00:00", ZoneInfo(schedule["timezone"]))
        targets = [{"registry_channel_id": i, "model": "gpt-6-astra", "protocol": "responses"} for i in schedule["layered_config"]["registry_channel_ids"]]
        with patch("time.time", return_value=midnight):
            storage.save_schedule_targets({**schedule, "enabled": True}, targets)
        schedule = storage.get_schedule(schedule["id"])
        run_id = timetable.reconcile(schedule, day, now=midnight)
        # A future source revision prevents the concurrently running UI service
        # from dispatching today's graph. This process drives only future Mock slots.
        for due in sorted({r["due"] for r in timetable.slots(run_id)}):
            with patch("time.time", return_value=due):
                while True:
                    slot = executor.select_ready(due)
                    if slot is None:
                        break
                    await executor.execute_probe(slot)
        executor.refresh_run(run_id)
        result = storage.get_run(run_id)
        storage.save_schedule_targets({**storage.get_schedule(schedule["id"]), "enabled": False}, targets)
        print(json.dumps({"run_id": run_id, "date": str(day), "status": result["status"], "attempted": result["summary"]["attempted"]}))
        return
    schedule=storage.get_schedule(int(sys.argv[2]))
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    if schedule["enabled"] or schedule["plan_version"] != layered.VERSION:
        raise RuntimeError("disabled owned layered fixture required")
    zone=ZoneInfo(schedule["timezone"])
    day=datetime.now(zone).date()+timedelta(days=2)
    while day.weekday()>=5:
        day+=timedelta(days=1)
    midnight=layered.epoch(day,"00:00",zone)
    targets=[{"registry_channel_id":i,"model":model,"protocol":"responses"}
             for i in schedule["layered_config"]["registry_channel_ids"]
             for model in ("gpt-6-astra","gpt-6.1-sol")]
    with patch("time.time",return_value=midnight):
        storage.save_schedule_targets({**schedule,"enabled":True},targets)
        # This source plan belongs to the controlled future fixture day. The UI
        # service's real wall clock must not build a competing current-day run.
        with storage.cursor() as cur:
            cur.execute("UPDATE schedules SET created_at=? WHERE id=?",(midnight,schedule["id"]))
        schedule=storage.get_schedule(schedule["id"])
        run_id=layered.create_day(schedule,day)
    try:
        channel_id=schedule["layered_config"]["registry_channel_ids"][0]
        for slot in layered.slots(run_id):
            if slot["method"] in {"modeltrace","canary"}:
                layered.update_slot(slot["slot_key"],"pending",channel_id=channel_id)
        # Serial real HTTP/projection/persistence in each original fixture slot's
        # own window. Source plan is enabled; sender and scorer stay unchanged.
        for slot in layered.slots(run_id):
            with patch("time.time",return_value=slot["due"]):
                await integrity.execute_slot(slot)
        with patch("time.time",return_value=max(slot["due"] for slot in layered.slots(run_id))+1):
            integrity.refresh_run(run_id)
            report=storage.get_run(run_id)
        print(json.dumps({"run_id":run_id,"status":report["status"],"attempted":report["summary"]["attempted"]}))
    finally:
        storage.save_schedule_targets({**storage.get_schedule(schedule["id"]),"enabled":False},targets)


if __name__=="__main__":
    asyncio.run(main())
