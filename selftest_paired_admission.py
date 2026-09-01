"""双端准入核心契约自检：受控本地模拟端、非正式证据和权限边界。"""
from __future__ import annotations

import os

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_METRIC_DB_NAME", "metrics-selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "127.0.0.1,localhost")
os.environ["TEST_PAIRED_SELFTEST_MODE"] = "1"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import auth, paired_evidence, store  # noqa: E402
from selftest_session import login  # noqa: E402


BASE = "http://127.0.0.1:8119"
UPSTREAM = "http://127.0.0.1:8118"
KEY = "sk-test-good-key-123"
TERMINAL_STATES = {
    "completed", "completed_with_insufficient_metrics", "stopped", "canceled",
}
failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(name)


def serve(app_path: str, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(
        app_path, host="127.0.0.1", port=port, log_level="error",
    ))
    threading.Thread(target=server.run, daemon=True).start()


def wait(url: str) -> None:
    for _ in range(100):
        try:
            response = httpx.get(url, timeout=1, trust_env=False)
            if response.status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"服务未启动：{url}")


def paired_task(client: httpx.Client, task_id: int) -> dict[str, Any]:
    response = client.get(f"{BASE}/api/paired-admission/tasks/{task_id}")
    response.raise_for_status()
    return response.json()


def wait_for_state(
    client: httpx.Client, task_id: int, expected: set[str], timeout: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = paired_task(client, task_id)
        state = latest["paired"]["state"]
        if state in expected or state in TERMINAL_STATES:
            return latest
        time.sleep(0.1)
    raise RuntimeError(
        f"双端准入状态等待超时：{task_id} -> {latest.get('paired', {}).get('state')}"
    )


def create_target(client: httpx.Client, name: str, group_name: str) -> dict[str, Any]:
    response = client.post(f"{BASE}/api/targets", json={
        "name": name,
        "base_url": UPSTREAM,
        "model": "gpt-4o-mini",
        "api_key": KEY,
        "protocol": "openai",
        "group_name": group_name,
        "source": "manual",
    })
    response.raise_for_status()
    return response.json()


def login_as(client: httpx.Client, username: str, password: str) -> None:
    response = client.post(f"{BASE}/api/auth/login", json={
        "username": username,
        "password": password,
    })
    response.raise_for_status()
    client.headers.update({"X-CSRF-Token": response.json()["csrf_token"]})


def ordered_subsequence(values: list[str], expected: list[str]) -> bool:
    remaining = iter(values)
    return all(any(value == wanted for value in remaining) for wanted in expected)


serve("mock_upstream:app", 8118)
serve("app.main:app", 8119)
wait(f"{UPSTREAM}/v1/models")
wait(f"{BASE}/api/health")

with httpx.Client(timeout=60, trust_env=False) as client:
    login(client, BASE)

    readiness_response = client.get(f"{BASE}/api/paired-admission/readiness")
    readiness = readiness_response.json()
    check("核心资产、适配器、判分器、表结构和证据 HMAC 均已就绪",
          readiness_response.status_code == 200 and readiness.get("ready") is True,
          readiness)
    check("开发自检入口明确标记为非正式且不使用负载阈值",
          readiness.get("formal") is False
          and readiness.get("load_thresholds_used") is False,
          readiness)

    candidate = create_target(client, "准入候选端", "candidate-route")
    benchmark = create_target(client, "准入标杆端", "benchmark-route")
    estimate = client.get(
        f"{BASE}/api/paired-admission/estimate",
        params={"candidate_target_id": candidate["id"]},
    ).json()
    check("六道保真题资产与 534 个理论端请求上限已进入规模预览",
          estimate.get("asset_version") == "paired-admission-v1.2.0"
          and estimate.get("fidelity_logical_items") == 6
          and estimate.get("maximum_fidelity_endpoint_requests") == 18
          and estimate.get("maximum_endpoint_requests") == 534,
          estimate)

    secondary_id = auth.create_user(
        "paired-no-role", "paired-no-role-password-2026", "无双端角色用户",
    )
    check("无角色测试账号没有隐式双端授权",
          not store.query(
              "SELECT id FROM user_roles WHERE user_id=? AND revoked_at IS NULL",
              (secondary_id,),
          ))
    with httpx.Client(timeout=30, trust_env=False) as denied_client:
        login_as(denied_client, "paired-no-role", "paired-no-role-password-2026")
        denied = denied_client.post(f"{BASE}/api/paired-admission/tasks", json={
            "candidate_target_id": candidate["id"],
            "idempotency_key": "selftest-denied-create",
        })
        check("未授权用户不能创建双端准入任务",
              denied.status_code == 403, denied.text)

    create_payload = {
        "candidate_target_id": candidate["id"],
        "idempotency_key": "selftest-create-paired-task",
    }
    created_response = client.post(
        f"{BASE}/api/paired-admission/tasks", json=create_payload,
    )
    check("授权 operator 可以创建双端准入任务",
          created_response.status_code == 200, created_response.text)
    created = created_response.json()
    task_id = created["task"]["id"]
    replayed_create = client.post(
        f"{BASE}/api/paired-admission/tasks", json=create_payload,
    )
    conflicting_create = client.post(
        f"{BASE}/api/paired-admission/tasks", json={
            **create_payload, "candidate_target_id": benchmark["id"],
        },
    )
    check("相同创建命令幂等重放只返回原任务",
          replayed_create.status_code == 200
          and replayed_create.json()["task"]["id"] == task_id
          and store.query(
              "SELECT COUNT(*) AS n FROM paired_tasks WHERE created_by=?",
              (created["paired"]["created_by"],),
          )[0]["n"] == 1,
          {"status": replayed_create.status_code,
           "body": replayed_create.text[:200]})
    check("同一创建幂等键绑定不同参数时返回并发冲突",
          conflicting_create.status_code == 409, conflicting_create.text)

    awaiting = wait_for_state(client, task_id, {"awaiting_fidelity_truth"})
    paired = awaiting["paired"]
    check("自动保真证据封存后等待人工判断",
          paired["state"] == "awaiting_fidelity_truth"
          and bool(paired.get("fidelity_manifest_root")), paired)
    check("自检任务和任务快照均保持非正式",
          paired["formal"] == 0 and awaiting["snapshot"].get("formal") is False,
          {"paired": paired.get("formal"), "snapshot": awaiting["snapshot"].get("formal")})

    evidence_before_truth = client.get(
        f"{BASE}/api/paired-admission/tasks/{task_id}/evidence",
        params={"include_raw": True, "purpose": "核心自检：核对保真证据"},
    )
    evidence_before_truth.raise_for_status()
    fidelity_evidence = evidence_before_truth.json()
    fidelity_attempts = [
        record["payload"] for record in fidelity_evidence["records"]
        if record["record_type"] == "side_attempt"
        and record["payload"].get("stage") == "fidelity"
    ]
    fidelity_checks = [
        record["payload"] for record in fidelity_evidence["records"]
        if record["record_type"] == "derived_result"
        and record["payload"].get("stage") == "fidelity"
    ]
    fidelity_intervals = [
        record["payload"] for record in fidelity_evidence["records"]
        if record["record_type"] == "cooldown"
        and record["payload"].get("reason") == "fidelity_item_interval"
    ]
    check("保真阶段证据链和加密原始块可完整读取",
          fidelity_evidence["integrity"]["ok"] is True
          and len(fidelity_attempts) == 6
          and KEY not in str(fidelity_evidence), fidelity_evidence.get("integrity"))
    check("六道保真题形成五项机械匹配和一项人工复核摘要",
          len(fidelity_checks) == 6
          and sum(item.get("check_status") == "matched"
                  for item in fidelity_checks) == 5
          and sum(item.get("check_status") == "human_review_required"
                  for item in fidelity_checks) == 1,
          fidelity_checks)
    check("六道保真题之间固定记录五次计划 5 秒冷却",
          len(fidelity_intervals) == 5
          and all(item.get("planned_seconds") == 5 for item in fidelity_intervals),
          fidelity_intervals)

    expected_version = paired["state_version"]
    decision_payload = {
        "value": "true",
        "reason": "本地模拟响应及身份字段与冻结配置一致",
        "evidence_refs": [paired["fidelity_manifest_root"]],
        "expected_state_version": expected_version,
        "idempotency_key": f"selftest-fidelity-{task_id}",
    }
    cross_command_conflict = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/fidelity-decision",
        json={**decision_payload,
              "idempotency_key": create_payload["idempotency_key"]},
    )
    check("同一操作者不能把创建命令幂等键复用于人工保真命令",
          cross_command_conflict.status_code == 409,
          cross_command_conflict.text)
    decision_response = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/fidelity-decision",
        json=decision_payload,
    )
    check("fidelity_reviewer 可以把完整保真证据人工定义为 true",
          decision_response.status_code == 200, decision_response.text)
    decided = decision_response.json()
    decision_count = store.query(
        "SELECT COUNT(*) AS n FROM paired_fidelity_decisions WHERE task_id=?",
        (task_id,),
    )[0]["n"]

    duplicate_decision = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/fidelity-decision",
        json=decision_payload,
    )
    conflicting_decision = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/fidelity-decision",
        json={**decision_payload, "expected_state_version": expected_version + 1},
    )
    after_duplicate = paired_task(client, task_id)
    duplicate_count = store.query(
        "SELECT COUNT(*) AS n FROM paired_fidelity_decisions WHERE task_id=?",
        (task_id,),
    )[0]["n"]
    check("相同人工保真命令幂等重放不会重复落决策或推进状态",
          duplicate_decision.status_code == 200
          and decision_count == duplicate_count == 1
          and after_duplicate["paired"]["state_version"]
          == decided["paired"]["state_version"],
          {"status": duplicate_decision.status_code,
           "decision_count": duplicate_count,
           "state_version": after_duplicate["paired"]["state_version"]})
    check("同一人工保真幂等键绑定不同预期版本时返回并发冲突",
          conflicting_decision.status_code == 409, conflicting_decision.text)
    check("开发自检中的人工 true 不生成可复用 truth",
          decided["paired"]["state"] == "awaiting_pair_start"
          and decided.get("truth") is None
          and not store.query(
              "SELECT id FROM paired_fidelity_truths WHERE source_task_id=?",
              (task_id,),
          ), decided.get("truth"))

    start_version = decided["paired"]["state_version"]
    start_payload = {
        "benchmark_target_id": benchmark["id"],
        "scale_confirmed": True,
        "request_limit": None,
        "token_limit": None,
        "money_limit": None,
        "expected_state_version": start_version,
        "idempotency_key": f"selftest-start-{task_id}",
    }
    start_response = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/start", json=start_payload,
    )
    check("确认规模后可以启动双端正式流程",
          start_response.status_code == 200, start_response.text)

    duplicate_start = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/start", json=start_payload,
    )
    conflicting_start = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/start",
        json={**start_payload, "request_limit": 100},
    )
    check("相同启动命令幂等重放不会创建或调度第二个任务",
          duplicate_start.status_code == 200
          and store.query(
              "SELECT COUNT(*) AS n FROM paired_tasks WHERE task_id=?", (task_id,),
          )[0]["n"] == 1,
          {"status": duplicate_start.status_code, "body": duplicate_start.text[:200]})
    check("同一启动幂等键绑定不同预算参数时返回并发冲突",
          conflicting_start.status_code == 409, conflicting_start.text)

    finished = wait_for_state(client, task_id, TERMINAL_STATES, timeout=180)
    report = finished.get("report") or {}
    check("本地模拟双端完成 16 个基础逻辑配对并封存报告",
          finished["paired"]["state"] in {
              "completed", "completed_with_insufficient_metrics",
          }
          and report.get("complete") is True
          and report.get("task_scope", {}).get("base_logical_pairs") == 16
          and report.get("task_scope", {}).get("formal_pair_attempts") == 16,
          {"state": finished["paired"]["state"],
           "scope": report.get("task_scope"), "stop": report.get("stop_reason")})
    check("报告只展示配对差异并等待人工结论",
          report.get("conclusion", {}).get("code") == "manual_pending"
          and report.get("speed", {}).get("sample_count") == 10
          and report.get("stability", {}).get("candidate", {}).get(
              "first_request_denominator") == 10,
          {"conclusion": report.get("conclusion"), "speed": report.get("speed")})

    evidence_response = client.get(
        f"{BASE}/api/paired-admission/tasks/{task_id}/evidence",
        params={"include_raw": True, "purpose": "核心自检：验证完整证据链"},
    )
    evidence_response.raise_for_status()
    evidence = evidence_response.json()
    records = evidence["records"]
    sequences = [record["record_seq"] for record in records]
    transitions = [
        record["payload"].get("to") for record in records
        if record["record_type"] == "state_transition"
    ]
    check("证据序号连续、前序哈希可验证且任务根已封存",
          evidence["integrity"]["ok"] is True
          and sequences == list(range(1, len(sequences) + 1))
          and bool(finished["paired"].get("task_manifest_root"))
          and report.get("integrity", {}).get("task_manifest_root")
          == finished["paired"].get("task_manifest_root"),
          {"integrity": evidence["integrity"], "record_count": len(records)})
    check("主状态严格按保真、预热、身份、两轮速度、能力和封存顺序推进",
          ordered_subsequence(transitions, [
              "fidelity", "awaiting_fidelity_truth", "awaiting_pair_start",
              "queued", "warmup", "identity", "speed_round_1", "ability_base",
              "speed_round_2", "ability_retest", "finalizing",
              finished["paired"]["state"],
          ]), transitions)

    formal_cooldowns = [
        record["payload"] for record in records
        if record["record_type"] == "cooldown"
        and record["payload"].get("reason") == "between_formal_pairs"
    ]
    check("16 道基础题之间固定记录 15 次计划 5 秒冷却",
          len(formal_cooldowns) == 15
          and all(item.get("planned_seconds") == 5 for item in formal_cooldowns)
          and all(0 < item.get("selftest_actual_seconds", 0) < 5
                  for item in formal_cooldowns), formal_cooldowns)

    raw_blocks = store.query(
        "SELECT id,key_ciphertext,ciphertext,content_hash FROM paired_raw_blocks "
        "WHERE task_id=? ORDER BY id", (task_id,),
    )
    check("原始请求、响应和流分片以带内容哈希的加密数据块保存",
          bool(raw_blocks)
          and all(row["key_ciphertext"] and row["ciphertext"] and row["content_hash"]
                  for row in raw_blocks)
          and KEY not in str(raw_blocks), len(raw_blocks))

    conclusion_response = client.post(
        f"{BASE}/api/paired-admission/tasks/{task_id}/conclusion", json={
            "verdict": "do_not_admit",
            "reason": "开发自检不得形成正式人工结论",
            "evidence_refs": [report["integrity"]["task_manifest_root"]],
            "report_version": finished["paired"]["report_version"],
            "expected_conclusion_version": 0,
            "idempotency_key": f"selftest-conclusion-{task_id}",
        },
    )
    check("开发自检任务拒绝写入任何正式准入结论",
          conclusion_response.status_code == 400
          and not store.query(
              "SELECT id FROM paired_conclusions WHERE task_id=?", (task_id,),
          ), conclusion_response.text)

    first_evidence = store.query(
        "SELECT id,payload_json FROM paired_evidence_records "
        "WHERE task_id=? ORDER BY record_seq LIMIT 1", (task_id,),
    )[0]
    store.execute(
        "UPDATE paired_evidence_records SET payload_json=? WHERE id=?",
        ('{"tampered":true}', first_evidence["id"]),
    )
    corrupted = paired_evidence.verify(task_id)
    check("证据被篡改后哈希/HMAC 校验确定性失败且不会被静默覆盖",
          corrupted["ok"] is False
          and corrupted.get("reason") == "hash_or_hmac", corrupted)

print(f"失败项：{failures if failures else '无'}")
sys.exit(1 if failures else 0)
