"""配对式本地压力执行器。

首次配对：python local_runner.py pair http://平台地址 配对码
持续运行：python local_runner.py run http://平台地址
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import protocol
from app.local_runners import result_signing_bytes

VERSION = "1.0.0"
DEFAULT_STATE = Path(__file__).resolve().parent / "data" / "local-runner-state.json"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def raw_private(key: Any) -> str:
    return b64(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ))


def raw_public(key: Any) -> str:
    return b64(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError("尚未配对，请先运行 pair 命令")
    return json.loads(path.read_text(encoding="utf-8"))


def pair(server: str, code: str, state_path: Path) -> None:
    encryption = X25519PrivateKey.generate()
    signing = Ed25519PrivateKey.generate()
    response = httpx.post(server.rstrip("/") + "/api/runner-agent/pair", json={
        "pairing_code": code,
        "encryption_public_key": raw_public(encryption.public_key()),
        "signing_public_key": raw_public(signing.public_key()),
        "version": VERSION,
        "capabilities": capabilities(),
    }, timeout=20)
    response.raise_for_status()
    result = response.json()
    save_state(state_path, {
        "runner_id": result["runner_id"], "runner_token": result["runner_token"],
        "encryption_private_key": raw_private(encryption),
        "signing_private_key": raw_private(signing),
        "paired_server": server.rstrip("/"),
    })
    print(f"配对完成：执行器 #{result['runner_id']}。私钥仅保存在 {state_path}")


def capabilities() -> dict[str, Any]:
    return {
        "protocol": "local-load-v1", "cpu_count": os.cpu_count() or 1,
        "streaming": True, "open_loop": True, "closed_loop": True,
        "python": sys.version.split()[0], "platform": sys.platform,
    }


def decrypt_credentials(state: dict[str, Any], envelope: dict[str, str]) -> dict[str, Any]:
    if envelope.get("version") != "x25519-aesgcm-v1":
        raise RuntimeError("不支持的凭据密文版本")
    private = X25519PrivateKey.from_private_bytes(unb64(state["encryption_private_key"]))
    ephemeral = X25519PublicKey.from_public_bytes(unb64(envelope["ephemeral_public_key"]))
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=unb64(envelope["salt"]),
        info=b"api-evaluator-local-runner-job-v1",
    ).derive(private.exchange(ephemeral))
    plaintext = AESGCM(key).decrypt(
        unb64(envelope["nonce"]), unb64(envelope["ciphertext"]), None
    )
    credentials = json.loads(plaintext)
    if time.time() >= credentials["expires_at"]:
        raise RuntimeError("短期任务凭据已经过期")
    return credentials


async def one_request(
    client: httpx.AsyncClient, credentials: dict[str, Any], config: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    first_token = 0.0
    output_bytes = 0
    status_code = 0
    error = ""
    profile = config.get("prompt_profile", "simple")
    prompts = {
        "simple": "只回复 OK。",
        "reasoning": "用不超过三句话说明为什么重试必须具有幂等性。",
        "coding": "写一个 Python 函数，对整数列表稳定去重并保留原顺序。",
    }
    prompt = f"{prompts.get(profile, prompts['simple'])}\n请求序号：{sequence}"
    max_tokens = config.get("max_tokens") or {"simple": 64, "reasoning": 512, "coding": 1600}.get(profile, 64)
    body = protocol.chat_payload(
        credentials["protocol"], credentials["model"], prompt,
        stream=bool(config.get("stream", True)), max_tokens=max_tokens,
        variant=protocol.payload_variant(credentials["protocol"], credentials["model"]),
    )
    try:
        async with client.stream(
            "POST", protocol.chat_url(credentials["protocol"], credentials["base_url"]),
            headers=protocol.headers(credentials["protocol"], credentials["api_key"]),
            json=body,
        ) as response:
            status_code = response.status_code
            async for chunk in response.aiter_bytes():
                if chunk and not first_token:
                    first_token = time.perf_counter() - started
                output_bytes += len(chunk)
            response.raise_for_status()
    except Exception as exc:
        error = type(exc).__name__
    latency = time.perf_counter() - started
    return {
        "ok": 200 <= status_code < 300 and not error,
        "status_code": status_code, "latency": latency,
        "first_token": first_token, "output_bytes": output_bytes, "error": error,
    }


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]


async def run_round(
    credentials: dict[str, Any], config: dict[str, Any], level: int, count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    mode = config.get("mode", "closed")
    maximum = min(int(config.get("max_in_flight", 250)), level if mode == "closed" else 1000)
    scheduled = time.perf_counter()
    process_start = time.process_time()
    drift = 0.0
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(
        max_connections=maximum, max_keepalive_connections=maximum,
    )) as client:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=maximum)
        results: list[dict[str, Any]] = []

        async def worker() -> None:
            while True:
                index = await queue.get()
                try:
                    results.append(await one_request(client, credentials, config, index))
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(maximum)]
        try:
            for index in range(count):
                if mode == "open":
                    due = scheduled + index / level
                    delay = due - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await queue.put(index)
                    drift = max(drift, time.perf_counter() - due)
                else:
                    await queue.put(index)
            await queue.join()
        finally:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    elapsed = max(time.perf_counter() - scheduled, .001)
    process_used = time.process_time() - process_start
    successes = [item for item in results if item["ok"]]
    latencies = [item["latency"] for item in successes]
    first_tokens = [item["first_token"] for item in successes if item["first_token"]]
    output_bytes = sum(item["output_bytes"] for item in results)
    generator_saturated = process_used / elapsed / max(os.cpu_count() or 1, 1) > .85 \
        or (mode == "open" and drift > max(.2, 2 / level))
    row = {
        "concurrency": level, "requests": count,
        "success_rate": round(len(successes) / count, 6),
        "throughput": round(count / elapsed, 3),
        "p95_ttft": round(percentile(first_tokens, .95), 4),
        "p95_latency": round(percentile(latencies, .95), 4),
        "tokens_per_second": 0, "speed_decline": 0,
        "cache_signal": "未检测", "stable": False,
        "errors": {
            "429": sum(item["status_code"] == 429 for item in results),
            "5xx": sum(item["status_code"] >= 500 for item in results),
            "network": sum(bool(item["error"]) for item in results),
        },
        "generator_saturated": generator_saturated,
    }
    telemetry = {
        "cpu_process_share": round(process_used / elapsed / max(os.cpu_count() or 1, 1), 4),
        "network_response_bytes": output_bytes, "scheduler_drift_seconds": round(drift, 4),
        "generator_saturated": generator_saturated, "elapsed_seconds": round(elapsed, 4),
    }
    return row, telemetry


async def execute_job(
    server: str, state: dict[str, Any], job: dict[str, Any], headers: dict[str, str],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    credentials = decrypt_credentials(state, job["encrypted_credentials"])
    try:
        config = job["config"]["load"]
        rounds = []
        telemetry_rounds = []
        base_latency = None
        safe = 0
        for level in config["levels"]:
            status = httpx.get(
                f"{server}/api/runner-agent/{state['runner_id']}/jobs/{job['job_id']}",
                headers=headers, timeout=10,
            ).json()
            if status.get("cancel_requested"):
                return "cancelled", {"load": {"rounds": rounds, "safe_concurrency": safe}}, {
                    "rounds": telemetry_rounds, "cancelled_by_user": True,
                }
            row, telemetry = await run_round(
                credentials, config, int(level), int(config["requests_per_level"])
            )
            if base_latency is None and row["p95_latency"]:
                base_latency = row["p95_latency"]
            row["speed_decline"] = round(
                max(0.0, row["p95_latency"] / base_latency - 1), 4
            ) if base_latency else 0
            row["stable"] = row["success_rate"] >= .95 and not row["errors"]["429"] \
                and row["speed_decline"] <= .3 and not row["generator_saturated"]
            rounds.append(row)
            telemetry_rounds.append({"level": level, **telemetry})
            if row["stable"]:
                safe = level
            else:
                break
            if config.get("cooldown_seconds"):
                await asyncio.sleep(config["cooldown_seconds"])
        load = {
            "rounds": rounds, "safe_concurrency": safe,
            "stopped_early": bool(rounds and not rounds[-1]["stable"]),
            "capacity_verdict": "本地发生器饱和" if any(
                item["generator_saturated"] for item in rounds
            ) else "已完成本地负载扫描",
            "cache_signal": "未检测",
        }
        return "success", {"load": load, "cost": 0}, {
            "runner_version": VERSION, "rounds": telemetry_rounds,
            "cpu_count": os.cpu_count() or 1,
            "network_response_bytes": sum(
                item["network_response_bytes"] for item in telemetry_rounds),
            "generator_saturated": any(
                item["generator_saturated"] for item in telemetry_rounds),
            "max_cpu_process_share": max(
                (item["cpu_process_share"] for item in telemetry_rounds), default=0),
        }
    finally:
        credentials.clear()


async def run_loop(server: str, state_path: Path) -> None:
    state = load_state(state_path)
    expected = state.get("paired_server")
    server = server.rstrip("/")
    if expected and expected != server:
        raise RuntimeError(f"该执行器配对的是 {expected}，拒绝连接其他平台")
    headers = {"Authorization": f"Bearer {state['runner_token']}"}
    print(f"本地执行器 #{state['runner_id']} 已启动，主动轮询 {server}")
    while True:
        try:
            response = httpx.post(
                f"{server}/api/runner-agent/{state['runner_id']}/poll",
                headers=headers, timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            job = payload.get("job")
            if not job:
                await asyncio.sleep(payload.get("poll_after_seconds", 5))
                continue
            try:
                status, report, telemetry = await execute_job(server, state, job, headers)
            except Exception as exc:
                status, report, telemetry = "failed", {"error_type": type(exc).__name__}, {
                    "runner_version": VERSION, "execution_error": type(exc).__name__,
                }
            signing = Ed25519PrivateKey.from_private_bytes(unb64(state["signing_private_key"]))
            signature = b64(signing.sign(result_signing_bytes(
                job["job_id"], job["result_nonce"], status, report, telemetry
            )))
            result = httpx.post(
                f"{server}/api/runner-agent/{state['runner_id']}/results",
                headers=headers, json={"job_id": job["job_id"], "status": status,
                                       "report": report, "telemetry": telemetry,
                                       "signature": signature}, timeout=20,
            )
            result.raise_for_status()
            print(f"任务 #{job['task_id']} 已签名回传：{status}")
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"轮询失败：{type(exc).__name__}；5 秒后重试", file=sys.stderr)
            await asyncio.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="API 中转站本地压力执行器")
    parser.add_argument("command", choices=("pair", "run"))
    parser.add_argument("server")
    parser.add_argument("pairing_code", nargs="?")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.command == "pair":
        if not args.pairing_code:
            parser.error("pair 命令需要配对码")
        pair(args.server, args.pairing_code, args.state)
    else:
        asyncio.run(run_loop(args.server, args.state))


if __name__ == "__main__":
    main()
