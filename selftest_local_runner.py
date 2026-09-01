"""本地执行器配对、密文领取、取消和签名回传自测。"""
import asyncio
import base64
import os
from typing import Any

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "example.com")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import local_runners, main, runner, store  # noqa: E402
import local_runner  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def private_text(key: object) -> str:
    return b64(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ))


def public_text(key: object) -> str:
    return b64(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    ))


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


async def load_round_task_counts() -> tuple[int, int]:
    active_requests = 0
    peak_active_requests = 0
    created_tasks = 0

    async def fake_request(*_args: object) -> dict[str, Any]:
        nonlocal active_requests, peak_active_requests
        active_requests += 1
        peak_active_requests = max(peak_active_requests, active_requests)
        try:
            await asyncio.sleep(0)
            return {"ok": True, "status_code": 200, "latency": 0.01,
                    "first_token": 0.005, "output_bytes": 1, "error": ""}
        finally:
            active_requests -= 1

    original_request = local_runner.one_request
    original_create_task = local_runner.asyncio.create_task

    def count_created_task(coro: object, **kwargs: object) -> asyncio.Task[object]:
        nonlocal created_tasks
        created_tasks += 1
        return original_create_task(coro, **kwargs)

    local_runner.one_request = fake_request
    local_runner.asyncio.create_task = count_created_task
    try:
        await local_runner.run_round(
            {"protocol": "openai", "model": "test", "base_url": "https://example.com",
             "api_key": "test"},
            {"mode": "closed", "max_in_flight": 4, "stream": True},
            level=4, count=800,
        )
    finally:
        local_runner.one_request = original_request
        local_runner.asyncio.create_task = original_create_task
    return created_tasks, peak_active_requests


def main_test() -> None:
    encryption = X25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate()
    with TestClient(main.app) as client:
        login(client)
        code = client.post("/api/local-runners/pairing-codes", json={
            "name": "测试电脑",
        }).json()
        client.cookies.clear()
        paired_response = client.post("/api/runner-agent/pair", json={
            "pairing_code": code["pairing_code"],
            "encryption_public_key": public_text(encryption),
            "signing_public_key": public_text(signing),
            "version": "1.0.0", "capabilities": {"open_loop": True},
        })
        paired = paired_response.json()
        check("一次性配对码可注册独立密钥对",
              paired_response.status_code == 200 and paired["runner_token"], paired_response.text)
        reused = client.post("/api/runner-agent/pair", json={
            "pairing_code": code["pairing_code"],
            "encryption_public_key": public_text(encryption),
            "signing_public_key": public_text(signing),
            "version": "1.0.0", "capabilities": {},
        })
        check("配对码只能使用一次", reused.status_code == 400, reused.text)
        agent_headers = {"Authorization": f"Bearer {paired['runner_token']}"}

        login(client)
        target = client.post("/api/targets", json={
            "name": "本地压测目标", "base_url": "https://example.com",
            "model": "gpt-local-load", "api_key": "sk-local-only",
            "protocol": "openai",
        }).json()
        missing_runner = client.post("/api/tasks", json={
            "kind": "load", "target_id": target["id"], "load_levels": [1],
            "load_requests_per_level": 10,
        })
        check("云端禁止无本地执行器的压力任务", missing_runner.status_code == 400,
              missing_runner.text)
        original_submit = runner.submit
        submitted_to_cloud: list[int] = []

        async def should_not_submit(task_id: int) -> None:
            submitted_to_cloud.append(task_id)

        runner.submit = should_not_submit
        created = client.post("/api/tasks", json={
            "kind": "load", "target_id": target["id"], "load_levels": [1],
            "load_requests_per_level": 10, "local_runner_id": paired["runner_id"],
        }).json()
        runner.submit = original_submit
        check("压力任务只生成本地执行器作业而不进入云端 Worker",
              created["runner_job"]["status"] == "queued" and not submitted_to_cloud,
              created)

        client.cookies.clear()
        poll = client.post(
            f"/api/runner-agent/{paired['runner_id']}/poll", headers=agent_headers,
        ).json()["job"]
        envelope_text = str(poll["encrypted_credentials"])
        check("领取接口只返回执行器公钥密文而不含渠道 Key",
              "sk-local-only" not in envelope_text and "ciphertext" in envelope_text, poll)
        credentials = local_runner.decrypt_credentials({
            "encryption_private_key": private_text(encryption),
        }, poll["encrypted_credentials"])
        check("只有持有本地私钥的执行器能解出短期任务凭据",
              credentials["api_key"] == "sk-local-only" and credentials["expires_at"],
              credentials)
        report = {"load": {"rounds": [{
            "concurrency": 1, "requests": 10, "success_rate": 1,
            "throughput": 2.5, "p95_ttft": .1, "p95_latency": .4,
            "tokens_per_second": 50, "speed_decline": 0,
            "cache_signal": "未检测", "stable": True,
            "errors": {"429": 0}, "generator_saturated": False,
        }], "safe_concurrency": 1, "stopped_early": False,
            "capacity_verdict": "扫描范围内未见饱和", "cache_signal": "未检测"}}
        telemetry = {"cpu_count": 8, "generator_saturated": False,
                     "network_response_bytes": 1200}
        invalid = client.post(
            f"/api/runner-agent/{paired['runner_id']}/results", headers=agent_headers,
            json={"job_id": poll["job_id"], "status": "success", "report": report,
                  "telemetry": telemetry, "signature": "a" * 40},
        )
        check("伪造执行器结果签名被拒绝", invalid.status_code == 400, invalid.text)
        signature = b64(signing.sign(local_runners.result_signing_bytes(
            poll["job_id"], poll["result_nonce"], "success", report, telemetry
        )))
        accepted = client.post(
            f"/api/runner-agent/{paired['runner_id']}/results", headers=agent_headers,
            json={"job_id": poll["job_id"], "status": "success", "report": report,
                  "telemetry": telemetry, "signature": signature},
        )
        task = store.get("tasks", created["task_id"])
        job = store.get("runner_jobs", poll["job_id"])
        saved_report = store.loads(task["report"], {})
        check("签名结果通过后完成云端任务并清理凭据密文",
              accepted.status_code == 200 and task["status"] == "success"
              and job["encrypted_credentials_json"] == "{}"
              and saved_report["security"]["signature_verified"], accepted.text)
        check("结果保留本机 CPU、网络和发生器饱和指标",
              saved_report["metrics"]["local_runner"]["cpu_count"] == 8,
              saved_report["metrics"]["local_runner"])

    created_tasks, peak_active_requests = asyncio.run(load_round_task_counts())
    check("本地压测不为排队请求创建协程",
          created_tasks <= 4 and peak_active_requests <= 4,
          {"created_tasks": created_tasks, "peak_active_requests": peak_active_requests})

    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
