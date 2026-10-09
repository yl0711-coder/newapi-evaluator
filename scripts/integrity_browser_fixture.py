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
    schedule=storage.get_schedule(int(sys.argv[2]))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    run_id=layered.create_day(schedule,datetime.now(ZoneInfo(schedule["timezone"])).date())
    # Move only fixture windows, then drive the real executor without its clock loop.
    # Plan remains disabled in the application to prevent competing test dispatch.
    now=time.time()
    with storage.cursor() as cur:
        cur.execute("UPDATE layered_slots SET due=?,deadline=? WHERE run_id=?",(now+3600,now+7200,run_id))
    rows=layered.slots(run_id)
    channel_id=schedule["layered_config"]["registry_channel_ids"][0]
    for slot in rows:
        if slot["method"] in {"modeltrace","canary"}:
            layered.update_slot(slot["slot_key"],"pending",channel_id=channel_id)
    # Serial real HTTP/projection/persistence. Test does not substitute any scorer.
    for slot in layered.slots(run_id):
        await integrity.execute_slot(slot)
    integrity.refresh_run(run_id)
    report=storage.get_run(run_id)
    print(json.dumps({"run_id":run_id,"status":report["status"],"attempted":report["summary"]["attempted"]}))


if __name__=="__main__":
    asyncio.run(main())
