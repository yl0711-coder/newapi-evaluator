"""Owned browser fixture; replaces only Feishu's external HTTP transport."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import uvicorn
from fastapi import HTTPException
from shared import feishu_channels
from tests.test_feishu_channels import FeishuFixture, record
from workbench import create_app

fixture = FeishuFixture([record(), record("recMissing", baseurl="")])
feishu_channels.FeishuReader = fixture.reader
app = create_app("channels")


@app.get("/fixture/state")
async def state():
    return {"mockRequests": len(fixture.calls)}


@app.post("/fixture/{scenario}")
async def scenario(scenario: str):
    if scenario == "offline":
        fixture.pages = [[record(是否在线="已下线")]]
    elif scenario == "rotate":
        fixture.pages = [[record(apikey="synthetic-rotated-key", 渠道名称="Renamed")]]
    elif scenario == "failure":
        fixture.failure_page = 0
    else:
        raise HTTPException(400, "Unknown synthetic scenario")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="error", access_log=False)
