"""认证、SSRF 防护和任务恢复自测。"""
import asyncio
import os
import time

os.environ.setdefault("TEST_DB_NAME", "selftest.db")
os.environ.setdefault("TEST_BOOTSTRAP_USERNAME", "selftest-admin")
os.environ.setdefault("TEST_BOOTSTRAP_PASSWORD", "selftest-password-2026")
os.environ.setdefault("TEST_COOKIE_SECURE", "0")
os.environ.setdefault("TEST_EGRESS_ALLOWLIST", "127.0.0.1,localhost")

from fastapi.testclient import TestClient  # noqa: E402
import httpx  # noqa: E402

from app import auth, egress, main, runner, store  # noqa: E402
from selftest_session import login  # noqa: E402

failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failed.append(name)


def exercise_http_boundary() -> None:
    with TestClient(main.app) as client:
        unauthorized = client.get("/api/summary")
        check("未登录不能读取业务接口", unauthorized.status_code == 401, unauthorized.text)

        bad = client.post("/api/auth/login", json={
            "username": "selftest-admin", "password": "wrong-password",
        })
        check("错误密码不建立会话", bad.status_code == 401, bad.text)

        state = login(client)
        csrf = state["csrf_token"]
        client.headers.pop("X-CSRF-Token")
        denied = client.post("/api/import", json={"text": '{"name":"x"}'})
        check("写请求缺少 CSRF 时被拒绝", denied.status_code == 403, denied.text)
        client.headers.update({"X-CSRF-Token": csrf})
        accepted = client.post("/api/import", json={"text": '{"name":"x"}'})
        check("带有效 CSRF 的写请求可进入接口", accepted.status_code == 200, accepted.text)

        audit_rows = client.get("/api/audit-events").json()
        check("登录和写操作留下审计事件", any(
            row["action"] == "auth.login" and row["result"] == "success"
            for row in audit_rows), audit_rows[:3])
        filtered = client.get("/api/audit-events", params={
            "actor": "selftest-admin", "result": "success",
        }).json()
        check("审计支持按人员和结果筛选", filtered and all(
            row["actor"] == "selftest-admin" and row["result"] == "success"
            for row in filtered), filtered[:3])

        raw_subject = "real-user-or-token-should-never-persist"
        binding = client.post("/api/insights/usage-bindings", json={
            "subject_type": "token", "subject_id": raw_subject,
            "subject_label": "客服测试组", "primary_profile": "customer_service",
            "secondary_profiles": ["coding", "customer_service"],
        }).json()
        exported = client.get("/api/insights/usage-bindings/export").json()
        stored = store.get("usage_profile_bindings", binding["id"])
        check("用途对象只保存不可逆匿名指纹",
              raw_subject not in str(stored) and raw_subject not in str(exported)
              and stored["subject_hash"].startswith("h1:"), stored)
        check("用途导出只含匿名映射且主次用途去重",
              exported["bindings"][0]["primary_profile"] == "customer_service"
              and exported["bindings"][0]["secondary_profiles"] == ["coding"]
              and "subject_label" not in exported["bindings"][0], exported)
        policy = client.get("/api/security/egress-policy").json()
        check("页面只读展示部署级出站边界", policy["max_redirects"] == 3
              and "页面不开放动态放行" in policy["management"], policy)

        secondary_id = auth.create_user("secondary-admin", "secondary-password-2026")
        secondary = TestClient(main.app)
        secondary_login = secondary.post("/api/auth/login", json={
            "username": "secondary-admin", "password": "secondary-password-2026",
        })
        secondary_login.raise_for_status()
        secondary_state = secondary_login.json()
        secondary.headers.update({"X-CSRF-Token": secondary_state["csrf_token"]})
        auth.set_user_active("secondary-admin", False)
        check("停用单个账号会撤销其会话且不影响其他账号",
              secondary.get("/api/auth/me").status_code == 401
              and client.get("/api/auth/me").status_code == 200,
              store.get("users", secondary_id))
        auth.set_user_active("secondary-admin", True)
        secondary.close()


def exercise_egress_policy() -> None:
    try:
        egress.validate_url("http://169.254.169.254/latest/meta-data")
    except egress.EgressDenied:
        metadata_blocked = True
    else:
        metadata_blocked = False
    check("云元数据地址默认被拦截", metadata_blocked)
    check("显式白名单允许本地测试上游",
          egress.validate_url("http://127.0.0.1:8098") == "http://127.0.0.1:8098")
    try:
        egress.validate_url("file:///etc/passwd")
    except egress.EgressDenied:
        scheme_blocked = True
    else:
        scheme_blocked = False
    check("非 HTTP 协议不能作为上游", scheme_blocked)
    oversized = httpx.Response(
        200, content=b"x" * (egress.EGRESS_MAX_RESPONSE_BYTES + 1),
        request=httpx.Request("GET", "https://example.com/data"),
    )
    try:
        egress.ensure_response_size(oversized)
    except egress.EgressDenied:
        size_blocked = True
    else:
        size_blocked = False
    check("无 Content-Length 的超大响应也会被拒绝", size_blocked)
    check("出站客户端统一限制重定向次数",
          egress.EGRESS_MAX_REDIRECTS == 3)


async def exercise_recovery() -> None:
    now = time.time()
    queued_id = store.insert("tasks", {
        "kind": "inspect", "status": "queued", "snapshot": "{}",
        "created_at": now,
    })
    running_id = store.insert("tasks", {
        "kind": "inspect", "status": "running", "snapshot": "{}",
        "created_at": now, "started_at": now,
    })
    original = runner._run_task
    calls: list[int] = []

    async def fake_run(task_id: int) -> None:
        calls.append(task_id)
        await asyncio.sleep(0.02)
        store.update("tasks", task_id, {"status": "success", "finished_at": time.time()})

    runner._run_task = fake_run
    try:
        await runner.start()
        await asyncio.sleep(0.12)
        check("重启后排队任务自动恢复且只执行一次",
              store.get("tasks", queued_id)["status"] == "success"
              and calls.count(queued_id) == 1, calls)
        check("崩溃前执行中的任务明确标为中断",
              store.get("tasks", running_id)["status"] == "interrupted",
              store.get("tasks", running_id))
    finally:
        await runner.stop()
        runner._run_task = original


def main_test() -> None:
    exercise_http_boundary()
    exercise_egress_policy()
    asyncio.run(exercise_recovery())
    print("\n失败项：" + ("、".join(failed) if failed else "无"))


if __name__ == "__main__":
    main_test()
