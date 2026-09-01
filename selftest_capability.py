"""当前简单渠道测试端到端自测：题库口径、执行证据、参数协商与硬门槛。"""
import os

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "127.0.0.1,localhost")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from selftest_session import login  # noqa: E402

from app import itembank, test_catalog  # noqa: E402

BASE = "http://127.0.0.1:8095"
UPSTREAM = "http://127.0.0.1:8094"
GOOD_KEY = "sk-test-good-key-123"

failures: list[str] = []


def serve(app_path: str, port: int) -> None:
    config = uvicorn.Config(app_path, host="127.0.0.1", port=port, log_level="error")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()


def wait(url: str) -> None:
    for _attempt in range(80):
        try:
            httpx.get(url, timeout=2, trust_env=False)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"服务未起来：{url}")


def poll(client: httpx.Client, task_id: int, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"{BASE}/api/tasks/{task_id}").json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.5)
    raise RuntimeError("任务超时未结束")


def knobs(**values: bool) -> None:
    httpx.post(f"{UPSTREAM}/__reset", timeout=10)
    if values:
        httpx.post(f"{UPSTREAM}/__control", json=values, timeout=10)


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(name)


def make_target(
    client: httpx.Client, name: str, key: str = GOOD_KEY, *,
    protocol: str = "openai", model: str = "gpt-4o-mini",
) -> dict:
    return client.post(f"{BASE}/api/targets", json={
        "name": name,
        "base_url": UPSTREAM,
        "model": model,
        "api_key": key,
        "protocol": protocol,
    }).json()


def admit(client: httpx.Client, target: dict) -> dict:
    created = client.post(f"{BASE}/api/tasks", json={
        "kind": "admission",
        "target_id": target["id"],
    }).json()
    return poll(client, created["task_id"])


def main() -> int:
    serve("mock_upstream:app", 8094)
    serve("app.main:app", 8095)
    wait(f"{UPSTREAM}/v1/models")
    wait(f"{BASE}/api/summary")
    knobs()

    with httpx.Client(timeout=90, trust_env=False) as client:
        login(client, BASE)

        dimensions = client.get(f"{BASE}/api/dimensions").json()
        dimension_names = [value["name"] for value in dimensions["dims"]]
        check("能力分类固定为四类", dimension_names == itembank.DIM_ORDER, dimension_names)
        check("当前能力题共六道",
              sum(value["item_count"] for value in dimensions["dims"]) == 6,
              dimensions["dims"])

        placement = client.get(f"{BASE}/api/placement-config").json()
        check("落位配置引用当前四类能力",
              placement["dim_order"] == itembank.DIM_ORDER, placement["dim_order"])
        check("落位门槛为 90%", placement["qualify_ratio"] == 0.90)
        check("超时率硬门槛为 5%", placement["timeout_rate_limit"] == 0.05)

        estimate = client.get(f"{BASE}/api/estimate?kind=admission").json()
        check("简单渠道测试引用当前题库",
              estimate["version"] == itembank.PACK_VERSION and not estimate["has_hard"],
              estimate)
        check("一次简单渠道测试固定十二个请求", estimate["requests"] == 12, estimate)

        strong = make_target(client, "当前题库强模型")
        task = admit(client, strong)
        report = task["report"]
        metrics = report["metrics"]
        capability = metrics["capability"]
        admission = metrics["admission"]
        check("任务形成四类能力结果",
              capability["dim_order"] == itembank.DIM_ORDER
              and list(capability["dims"]) == itembank.DIM_ORDER,
              capability["dims"])
        check("六道能力题全部形成判分",
              admission["valid_items"] == len(test_catalog.admission_items())
              and admission["passed_items"] == len(test_catalog.admission_items()),
              admission)
        check("五道固定速度题全部形成样本",
              admission["speed_samples"] == len(test_catalog.fixed_speed_items()),
              admission)
        check("简单渠道测试通过", admission["status"] == "passed", admission)
        check("硬门槛只含鉴权和超时率",
              {item["name"] for item in metrics["gates"]["items"]}
              == {"QA-PROTO-01 鉴权与模型可达性", "超时率"},
              metrics["gates"])
        check("全部证据与计划请求数一致",
              len(report["evidence"]) == estimate["requests"], len(report["evidence"]))
        check("能力题均为二级难度", set(capability["by_level"]) == {"2"},
              capability["by_level"])
        check("当前能力总分接近满分", capability["overall"] >= 0.95,
              capability["overall"])

        knobs(reject_temperature=True)
        anthropic = make_target(
            client,
            "Anthropic 参数协商模型",
            protocol="anthropic",
            model="claude-sonnet-5",
        )
        adaptive = admit(client, anthropic)
        evidence = adaptive["report"]["evidence"]
        negotiated = [
            item for item in evidence
            if "drop_temperature" in item.get("retry_reasons", [])
        ]
        observations = client.get(f"{UPSTREAM}/__observations").json()
        check("temperature 拒绝只触发一次",
              observations["temperature_rejections"] == 1, observations)
        check("首次流式请求完成参数协商",
              len(negotiated) == 1
              and negotiated[0]["attempts"] == 2
              and negotiated[0]["effective_parameters"].get("temperature") == "omitted",
              negotiated)
        negotiation_index = evidence.index(negotiated[0]) if negotiated else len(evidence)
        remaining = evidence[negotiation_index + 1:]
        check("后续请求复用已确认参数",
              bool(remaining)
              and all(item["attempts"] == 1 for item in remaining)
              and all(item["effective_parameters"].get("temperature") == "omitted"
                      for item in remaining),
              remaining)

        cloud_load = client.post(f"{BASE}/api/tasks", json={
            "kind": "load",
            "target_id": anthropic["id"],
            "load_levels": [1],
            "load_requests_per_level": 10,
            "load_cooldown_seconds": 0,
            "load_stream": True,
        })
        check("云端压力测试仍受本地执行器边界约束",
              cloud_load.status_code == 400
              and "本地执行器" in cloud_load.json()["detail"],
              cloud_load.text)

        knobs()
        invalid = make_target(client, "无效凭据渠道", key="sk-wrong-key-000")
        rejected = admit(client, invalid)
        rejected_report = rejected["report"]
        rejected_gates = rejected_report["metrics"]["gates"]
        check("鉴权失败使硬门槛不通过",
              rejected_gates["passed"] is False
              and "QA-PROTO-01 鉴权与模型可达性" in rejected_gates["failed"],
              rejected_gates)
        check("鉴权失败形成暂不准入建议",
              rejected_report["conclusion"]["code"] == "reject",
              rejected_report["conclusion"])
        recommendation = client.get(
            f"{BASE}/api/tasks/{rejected['id']}/recommendation"
        ).json()
        check("未指定平台组时不自动分组",
              recommendation["result"]["status"] == "unassigned"
              and recommendation["result"]["target_group_id"] is None,
              recommendation["result"])

    print(f"通过 {not failures}，失败项：{failures if failures else '无'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
