"""端到端自测：假上游 + 真执行器，跑完三个入口的完整流程并检查报告。

用法：python selftest_api.py
测试数据写在 data/selftest.db，**不碰正式的 data/platform.db**。
"""
import os

# 必须在 import app.* 之前设置：config.py 在导入时就把 DB_PATH 定下来了。
# 自测从空状态开始断言、每次跑前清库，共用正式库的话会把真实渠道与标杆删掉。
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

BASE = "http://127.0.0.1:8099"
UP = "http://127.0.0.1:8098"
GOOD_KEY = "sk-test-good-key-123"


def serve(app_path: str, port: int) -> threading.Thread:
    cfg = uvicorn.Config(app_path, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t


def wait(url: str, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            httpx.get(url, timeout=2, trust_env=False)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"服务未起来：{url}")


def poll(cli: httpx.Client, task_id: int, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = cli.get(f"{BASE}/api/tasks/{task_id}").json()
        if t["status"] not in ("queued", "running"):
            return t
        time.sleep(0.4)
    raise RuntimeError("任务超时未结束")


def main() -> int:
    serve("mock_upstream:app", 8098)
    serve("app.main:app", 8099)
    wait(f"{UP}/v1/models")
    wait(f"{BASE}/api/summary")
    fails = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
        if not cond:
            fails.append(name)

    with httpx.Client(timeout=30, trust_env=False) as cli:
        login(cli, BASE)
        # 1. 空状态提示
        print("\n[1] 首页空状态")
        health = cli.get(f"{BASE}/api/health").json()
        check("服务端与定时调度器均在运行",
              health["status"] == "ok" and health["scheduler_running"] is True
              and health["metric_collector_running"] is True
              and "capacity" in health, health)
        s = cli.get(f"{BASE}/api/summary").json()
        check("无渠道时提示去配一个", "先去配一个" in s["hints"]["admission"], s["hints"])
        check("巡检未启用", s["hints"]["inspect"] == "未启用")

        # 2. 导入解析不回传明文 Key
        print("\n[2] 导入解析")
        imp = cli.post(f"{BASE}/api/import", json={"text":
            f'{{"name":"假上游","base_url":"{UP}","model":"gpt-4o-mini",'
            f'"api_key":"{GOOD_KEY}"}}'}).json()
        check("识别出地址与模型", imp["base_url"] == UP and imp["model"] == "gpt-4o-mini")
        check("不回传明文 Key", "api_key" not in imp, list(imp))
        check("Key 已脱敏", "***" in imp["key_masked"] and GOOD_KEY not in imp["key_masked"])

        partial = cli.post(f"{BASE}/api/import", json={"text": '{"name":"x"}'})
        partial_data = partial.json()
        check("字段不全时保留已识别信息", partial.status_code == 200
              and partial_data["name"] == "x" and partial_data["base_url"] == ""
              and partial_data["model"] == "", partial_data)

        loose = cli.post(f"{BASE}/api/import", json={"text":
            "地址：https://loose.example/v1/chat/completions，密钥 sk-loose-123"})
        loose_data = loose.json()
        check("普通文本可提取地址和 sk- 密钥", loose.status_code == 200
              and loose_data["base_url"] == "https://loose.example"
              and loose_data["has_key"] and "***" in loose_data["key_masked"], loose_data)
        admission_html = cli.get(f"{BASE}/admission.html").text
        check("新准入页保留粘贴连接信息自动提取入口",
              'id="f-import-text"' in admission_html
              and 'id="btn-import-config"' in admission_html, admission_html[:500])

        # 2.5 渠道连接先保存，多个模型复用同一份连接配置
        print("\n[2.5] 渠道与模型分层保存")
        channel = cli.post(f"{BASE}/api/channels", json={
            "name": "复用连接", "base_url": UP, "api_key": GOOD_KEY,
            "protocol": "openai"}).json()
        check("保存渠道不需要模型", bool(channel["id"]) and channel["models"] == [], channel)
        imported_channel = cli.post(f"{BASE}/api/channels/import", json={"text":
            f'BASE_URL={UP}\nAPI_KEY={GOOD_KEY}\nNAME=复用连接'} )
        check("粘贴连接可安全创建渠道且不回传明文 Key",
              imported_channel.status_code == 200
              and imported_channel.json()["id"] == channel["id"]
              and GOOD_KEY not in imported_channel.text, imported_channel.text)
        first_model = cli.post(f"{BASE}/api/channels/{channel['id']}/models", json={
            "name": "复用连接 · 模型一", "model": "gpt-4o-mini"}).json()
        second_model = cli.post(f"{BASE}/api/channels/{channel['id']}/models", json={
            "name": "复用连接 · 模型二", "model": "gpt-4o"}).json()
        check("同一渠道可添加多个模型", first_model["channel_id"] == channel["id"]
              and second_model["channel_id"] == channel["id"])
        listed_channel = next(x for x in cli.get(f"{BASE}/api/channels").json()
                              if x["id"] == channel["id"])
        check("渠道列表只返回脱敏 Key", "key_enc" not in str(listed_channel)
              and GOOD_KEY not in str(listed_channel) and len(listed_channel["models"]) == 2,
              listed_channel)
        removed = cli.delete(f"{BASE}/api/channels/{channel['id']}")
        check("管理页可删除渠道及其模型", removed.status_code == 200
              and all(x["id"] != channel["id"]
                      for x in cli.get(f"{BASE}/api/channels").json()), removed.text)

        # 3. 预估
        print("\n[3] 提交前预估")
        est = cli.get(f"{BASE}/api/estimate?kind=admission").json()
        check("给出请求数/tokens/并发/费用",
              all(est[k] for k in ("requests", "tokens", "concurrency")) and est["cost"] > 0, est)

        # 4. 接入检测（好 Key，应通过并被记录）
        print("\n[4] 接入检测 - 正常渠道")
        tgt = cli.post(f"{BASE}/api/targets", json={
            "name": "假上游 · gpt-4o-mini", "base_url": UP, "model": "gpt-4o-mini",
            "api_key": GOOD_KEY, "protocol": "openai", "source": "import",
            "edited_fields": [],
        }).json()
        check("新建目标立即保存在配置中", tgt["recorded"] is True)
        check("目标接口不含 key_enc", "key_enc" not in tgt)

        sub = cli.post(f"{BASE}/api/tasks", json={
            "kind": "admission", "target_id": tgt["id"]}).json()
        check("提交立即返回任务号", bool(sub["task_id"]) and sub["status"] == "queued", sub)

        t = poll(cli, sub["task_id"])
        rep = t["report"]
        check("任务成功结束", t["status"] == "success", t["status"])
        check("生成报告", rep is not None)
        check("结论为推荐准入", rep["conclusion"]["code"] == "recommend",
              rep["conclusion"] if rep else None)
        check("成功率 100%", rep["metrics"]["pass_rate"] == 1.0, rep["metrics"]["pass_rate"])
        cap = rep["metrics"].get("capability") or {}
        check("跑出四维能力向量", len(cap.get("dims") or {}) == 4,
              list((cap.get("dims") or {})))
        check("准入向量与固定四维轴完全一致",
              list(cap.get("dims") or {}) == cap.get("dim_order"),
              list((cap.get("dims") or {})))
        check("四维综合分接近满分",
              cap.get("overall") is not None and cap["overall"] >= 0.95,
              cap.get("overall"))
        check("硬门槛全过", (rep["metrics"].get("gates") or {}).get("passed") is True,
              rep["metrics"].get("gates"))
        check("报告里无明文 Key", GOOD_KEY not in str(rep))
        check("报告含三层内容", all(k in rep for k in ("conclusion", "metrics", "evidence")))
        check("有行动建议", len(rep["conclusion"]["actions"]) > 0)
        check("时间线有记录", len(t["events"]) >= 5, len(t["events"]))

        tgt2 = [x for x in cli.get(f"{BASE}/api/targets").json() if x["id"] == tgt["id"]]
        check("通过后仍保留在配置中", len(tgt2) == 1 and tgt2[0]["recorded"] is True)
        check("已存基线", tgt2[0]["has_baseline"] is True)

        # 5. 导出
        print("\n[5] 报告导出")
        for fmt, needle in (("md", "# 测试报告"), ("html", "<!DOCTYPE html>"), ("json", '"conclusion"')):
            r = cli.get(f"{BASE}/api/tasks/{sub['task_id']}/export?fmt={fmt}")
            check(f"导出 {fmt}", r.status_code == 200 and needle in r.text
                  and GOOD_KEY not in r.text, r.status_code)

        # 6. 坏 Key → 应判暂不准入且不被记录
        print("\n[6] 接入检测 - 错误 Key")
        badt = cli.post(f"{BASE}/api/targets", json={
            "name": "坏 Key 渠道", "base_url": UP, "model": "gpt-4o-mini",
            "api_key": "sk-wrong-key-000", "protocol": "openai"}).json()
        bt = poll(cli, cli.post(f"{BASE}/api/tasks", json={
            "kind": "admission", "target_id": badt["id"]}).json()["task_id"])
        brep = bt["report"]
        check("坏 Key 判暂不准入", brep["conclusion"]["code"] == "reject",
              brep["conclusion"]["code"])
        check("失败归类为鉴权", "鉴权" in str(brep["metrics"]["reason_counts"]),
              brep["metrics"]["reason_counts"])
        check("建议里提到检查 Key",
              any("Key" in a for a in brep["conclusion"]["actions"]),
              brep["conclusion"]["actions"])
        still = [x for x in cli.get(f"{BASE}/api/targets").json() if x["id"] == badt["id"]]
        check("未通过模型仍保留在配置列表", len(still) == 1 and still[0]["status"] == "reject")

        # 7. 降智复核（与基线一致 → 未见降智）
        print("\n[7] 降智复核")
        dt = poll(cli, cli.post(f"{BASE}/api/tasks", json={
            "kind": "degrade", "target_id": tgt["id"]}).json()["task_id"])
        drep = dt["report"]
        check("复核完成", dt["status"] == "success", dt["status"])
        check("与基线对比结论", any("基线" in r for r in drep["conclusion"]["reasons"]),
              drep["conclusion"]["reasons"])
        check("未判降智", drep["conclusion"]["code"] in ("recommend", "observe"),
              drep["conclusion"]["code"])

        # 8. 定时计划 + 手动抽检
        print("\n[8] 定期巡检")
        selected = cli.put(f"{BASE}/api/targets/{tgt['id']}/scheduled-test",
                           json={"enabled": True}).json()
        check("工作台可勾选定时测试模型", selected["scheduled_test_enabled"] is True)
        ins = cli.post(f"{BASE}/api/scheduled-tests", json={
            "name": "每日巡检", "report_time": "04:00",
            "enabled": True,
        }).json()
        check("可创建定时计划且前端取得统一提前量",
              ins["report_time"] == "04:00" and ins["test_lead_minutes"] == 30, ins)
        s2 = cli.get(f"{BASE}/api/summary").json()
        check("首页显示已启用", "已启用 1" in s2["hints"]["inspect"], s2["hints"]["inspect"])
        workspace_data = cli.get(f"{BASE}/api/workspace").json()
        workspace_models = [
            model for channel_group in workspace_data["unassigned"]
            for model in channel_group["models"]
        ]
        workspace_models.extend(
            model
            for platform_group in workspace_data["platform_groups"]
            for channel_group in platform_group["channel_groups"]
            for model in channel_group["models"]
        )
        workspace_target = next(model for model in workspace_models if model["id"] == tgt["id"])
        check("工作台返回定时勾选状态", workspace_target["scheduled_test_enabled"] is True)
        it = poll(cli, cli.post(f"{BASE}/api/targets/{tgt['id']}/inspect-now").json()["task_id"])
        check("手动抽检完成", it["status"] == "success", it["status"])

        # 9. 重试保留原记录
        print("\n[9] 重试与历史")
        before = cli.get(f"{BASE}/api/tasks/{bt['id']}").json()
        rt = cli.post(f"{BASE}/api/tasks/{bt['id']}/retry").json()
        check("重试建新任务", rt["task_id"] != bt["id"])
        newt = poll(cli, rt["task_id"])
        check("新任务记录父任务", newt["parent_task_id"] == bt["id"])
        after = cli.get(f"{BASE}/api/tasks/{bt['id']}").json()
        check("原失败记录未被覆盖",
              after["status"] == before["status"]
              and after["report"] == before["report"]
              and after["report"]["conclusion"]["code"] == "reject",
              f'{before["status"]} -> {after["status"]}')

        tasks = cli.get(f"{BASE}/api/tasks?limit=100").json()
        check("历史任务可列出", len(tasks) == 5, len(tasks))
        check("可按结论筛选",
              all(x["verdict_code"] == "reject"
                  for x in cli.get(f"{BASE}/api/tasks?verdict=reject").json()))
        check("可按类型筛选",
              all(x["kind"] == "degrade"
                  for x in cli.get(f"{BASE}/api/tasks?kind=degrade").json()))

        # 10. 边界：不存在的任务 / 未接入渠道做降智
        print("\n[10] 错误处理")
        check("不存在任务返回 404",
              cli.get(f"{BASE}/api/tasks/999999").status_code == 404)
        check("已结束任务不能取消",
              cli.post(f"{BASE}/api/tasks/{sub['task_id']}/cancel").status_code == 400)

        # 11. 云端必须拒绝压测；真实本地执行链由 selftest_local_runner.py 覆盖。
        print("\n[11] 压力测试")
        load_sub = cli.post(f"{BASE}/api/tasks", json={
            "kind": "load", "target_id": tgt["id"],
            "load_levels": [5], "load_requests_per_level": 10,
        })
        check("未选择配对本地执行器时云端拒绝压测",
              load_sub.status_code == 400 and "本地执行器" in load_sub.json()["detail"],
              load_sub.text)
        dynamic_estimate = cli.get(
            f"{BASE}/api/estimate?kind=load&load_levels=5,10&load_requests_per_level=15"
        ).json()
        check("压力测试请求数按页面实际档位动态计算",
              dynamic_estimate["requests"] == 30
              and dynamic_estimate["concurrency"] == 10, dynamic_estimate)

        # 平台倍率组、模型包批量准入、工作台层级、标杆槽位与迁移由
        # selftest_workspace_v2.py 作为当前唯一规格继续覆盖。
        print("\n" + "=" * 46)
        print(f"通过 {len(fails) == 0}，失败项：{fails if fails else '无'}")
        return 1 if fails else 0

        # 12. 批量接入：一个 API 一次创建多个模型任务，重复模型自动去重
        print("\n[12] 批量模型检测")
        batch_channel = cli.post(f"{BASE}/api/channels", json={
            "name": "批量连接", "base_url": UP, "api_key": GOOD_KEY,
            "protocol": "openai"}).json()
        custom_rate = cli.post(f"{BASE}/api/rate-groups", json={
            "multiplier": 1.2}).json()
        rates = cli.get(f"{BASE}/api/rate-groups").json()
        check("可新建并持久化自定义倍率分组",
              custom_rate["multiplier"] == 1.2
              and any(rate["multiplier"] == 1.2 for rate in rates), rates)
        batch_group = cli.post(f"{BASE}/api/groups", json={
            "name": "批量模型-1.2x组", "multiplier": 1.2}).json()
        peer_group = cli.post(f"{BASE}/api/groups", json={
            "name": "另一模型-1.2x组", "multiplier": 1.2}).json()
        bench_dims = {"指令与推理": 0.8, "代码与工具": 0.8, "上下文表达": 0.8,
                      "速度": 0.8, "稳定性": 0.8}
        first_benchmark = cli.post(f"{BASE}/api/benchmarks", json={
            "name": "批量模型 1.2x 标杆", "group_id": batch_group["id"],
            "dims": bench_dims})
        second_benchmark = cli.post(f"{BASE}/api/benchmarks", json={
            "name": "另一模型 1.2x 标杆", "group_id": peer_group["id"],
            "dims": bench_dims})
        same_rate_groups = cli.get(f"{BASE}/api/groups").json()
        same_rate_groups = {g["id"]: g for g in same_rate_groups
                            if g["id"] in {batch_group["id"], peer_group["id"]}}
        check("同一倍率下不同模型可分别保存标杆",
              first_benchmark.status_code == 200 and second_benchmark.status_code == 200
              and same_rate_groups[batch_group["id"]]["benchmark_id"]
                  == first_benchmark.json()["id"]
              and same_rate_groups[peer_group["id"]]["benchmark_id"]
                  == second_benchmark.json()["id"]
              and first_benchmark.json()["groups"][0]["id"] == batch_group["id"]
              and second_benchmark.json()["groups"][0]["id"] == peer_group["id"],
              {"first": first_benchmark.text, "second": second_benchmark.text,
               "groups": same_rate_groups})
        missing_rate = cli.post(
            f"{BASE}/api/channels/{batch_channel['id']}/admission-tasks",
            json={"models": ["gpt-4o-mini"]})
        check("批量准入必须选择倍率", missing_rate.status_code == 422,
              missing_rate.text)
        batch = cli.post(f"{BASE}/api/channels/{batch_channel['id']}/admission-tasks", json={
            "models": ["gpt-4o-mini", "gpt-4o", "gpt-4o-mini"],
            "multiplier": 1.2}).json()
        check("同一 API 可批量创建去重后的检测任务",
              len(batch["task_ids"]) == 2 and batch["status"] == "queued", batch)
        batch_done = [poll(cli, task_id) for task_id in batch["task_ids"]]
        check("批量任务均可完成", all(task["status"] in ("success", "partial", "failed")
              for task in batch_done), [task["status"] for task in batch_done])
        grouped_models = cli.get(f"{BASE}/api/channels").json()
        grouped_models = next(c for c in grouped_models if c["id"] == batch_channel["id"])["models"]
        groups = {group["id"]: group for group in cli.get(f"{BASE}/api/groups").json()}
        automatic_groups = {model["model"]: groups[model["group_id"]]
                            for model in grouped_models}
        check("每个模型按所选倍率自动进入自己的模型-倍率组",
              len({model["group_id"] for model in grouped_models}) == 2
              and {group["name"] for group in automatic_groups.values()}
                  == {"gpt-4o-mini-1.2x组", "gpt-4o-1.2x组"}
              and all(group["multiplier"] == 1.2
                      for group in automatic_groups.values()),
              automatic_groups)
        mini_group_id = automatic_groups["gpt-4o-mini"]["id"]
        auto_golden = cli.post(
            f"{BASE}/api/groups/{mini_group_id}/golden",
            json={"task_id": batch["task_ids"][0]})
        wrong_group_golden = cli.post(
            f"{BASE}/api/groups/{peer_group['id']}/golden",
            json={"task_id": batch["task_ids"][0]})
        check("从报告保存标杆会自动绑定任务所属模型-倍率组",
              auto_golden.status_code == 200
              and auto_golden.json()["benchmark"]["source_task_id"] == batch["task_ids"][0],
              auto_golden.text)
        check("同倍率下不能把其他模型任务误设为本组标杆",
              wrong_group_golden.status_code == 400, wrong_group_golden.text)

        first_batch_task = cli.get(f"{BASE}/api/tasks/{batch['task_ids'][0]}").json()
        check("批量准入固定使用当前准入题库",
              first_batch_task["kind"] == "admission"
              and first_batch_task["pack_version"] == "api-evaluator-selected-2026.06.17",
              first_batch_task)
        manual_comparison = cli.post(
            f"{BASE}/api/tasks/{batch['task_ids'][0]}/benchmark-comparison",
            json={"benchmark_id": auto_golden.json()["benchmark"]["id"]})
        check("发起时未选标杆的历史报告可手动选标杆对比",
              manual_comparison.status_code == 200
              and manual_comparison.json()["result"]["qualified"] is True,
              manual_comparison.text)
        compared_task = cli.get(
            f"{BASE}/api/tasks/{batch['task_ids'][0]}").json()
        check("手选标杆的固定对比结果持久化到报告列表数据",
              compared_task["selected_benchmark_id"]
                  == auto_golden.json()["benchmark"]["id"]
              and compared_task["selected_benchmark_comparison"]["benchmark_name"]
                  == auto_golden.json()["benchmark"]["name"],
              compared_task)

        # 12.5 工作台聚合与渠道倍率删除
        print("\n[12.5] 模型工作台")
        workspace_channel = cli.post(f"{BASE}/api/channels", json={
            "name": "工作台渠道", "base_url": UP, "api_key": GOOD_KEY,
            "protocol": "anthropic"}).json()
        workspace_group_one = cli.post(f"{BASE}/api/groups", json={
            "name": "工作台模型一-1x组", "multiplier": 1}).json()
        workspace_group_low = cli.post(f"{BASE}/api/groups", json={
            "name": "工作台模型二-0.7x组", "multiplier": 0.7}).json()
        workspace_model_one = cli.post(
            f"{BASE}/api/channels/{workspace_channel['id']}/models", json={
                "name": "工作台渠道 · 模型一", "model": "workspace-model-one",
                "group_id": workspace_group_one["id"]}).json()
        workspace_model_low = cli.post(
            f"{BASE}/api/channels/{workspace_channel['id']}/models", json={
                "name": "工作台渠道 · 模型二", "model": "workspace-model-low",
                "group_id": workspace_group_low["id"]}).json()
        workspace_connection_two = cli.post(f"{BASE}/api/channels", json={
            "name": "工作台渠道", "base_url": UP,
            "api_key": "sk-workspace-other-key", "protocol": "anthropic"}).json()
        workspace_model_low_two = cli.post(
            f"{BASE}/api/channels/{workspace_connection_two['id']}/models", json={
                "name": "工作台渠道 · 模型三", "model": "workspace-model-low-two",
                "group_id": workspace_group_low["id"]}).json()
        workspace_task = poll(cli, cli.post(f"{BASE}/api/tasks", json={
            "kind": "admission", "target_id": workspace_model_low["id"]}).json()["task_id"])
        upstream_update = cli.put(
            f"{BASE}/api/targets/{workspace_model_low['id']}/upstream-multiplier",
            json={"upstream_multiplier": 2}).json()
        cli.put(f"{BASE}/api/targets/{workspace_model_low['id']}/scheduled-test",
                json={"enabled": True})
        online_group = cli.post(f"{BASE}/api/scheduled-report-groups", json={
            "model_family": "workspace-model-low", "online_multiplier": 1}).json()
        online_assignment = cli.put(
            f"{BASE}/api/scheduled-report-groups/{online_group['id']}/targets",
            json={"target_ids": [workspace_model_low["id"]]}).json()
        workspace_data = cli.get(f"{BASE}/api/workspace").json()
        workspace_matches = [channel for channel in workspace_data["channels"]
                             if channel["name"] == "工作台渠道"]
        workspace_view = workspace_matches[0]
        workspace_low_view = next(model for model in workspace_view["models"]
                                  if model["id"] == workspace_model_low["id"])
        check("工作台自动合并同名渠道并保留独立连接",
              len(workspace_matches) == 1
              and set(workspace_view["channel_ids"])
                  == {workspace_channel["id"], workspace_connection_two["id"]}
              and len(workspace_view["connections"]) == 2
              and {connection["key_masked"] for connection in workspace_view["connections"]}
                  == {workspace_channel["key_masked"], workspace_connection_two["key_masked"]},
              workspace_view)
        check("工作台一次返回渠道倍率模型和最近报告",
              {model["evaluation_multiplier"] for model in workspace_view["models"]} == {1, 0.7}
              and len(workspace_view["models"]) == 3
              and workspace_low_view["reports"][0]["id"] == workspace_task["id"]
              and workspace_low_view["score"] is not None,
              workspace_view)
        check("上游倍率与评测档位和上线倍率完全解耦",
              upstream_update["upstream_multiplier"] == 2
              and online_group["online_multiplier"] == 1
              and online_assignment["target_ids"] == [workspace_model_low["id"]]
              and workspace_low_view["upstream_multiplier"] == 2
              and workspace_low_view["evaluation_multiplier"] == 0.7
              and workspace_low_view["online_groups"] == [{
                  "model_family": "workspace-model-low", "online_multiplier": 1}],
              workspace_low_view)
        deleted_rate = cli.delete(
            f"{BASE}/api/workspace/channels/rates/0.7",
            params={"channel_name": "工作台渠道"})
        check("删除同名渠道倍率会覆盖其全部独立连接",
              deleted_rate.status_code == 200
              and deleted_rate.json()["deleted_models"] == 2
              and deleted_rate.json()["archived_reports"] >= 1
              and cli.get(f"{BASE}/api/tasks/{workspace_task['id']}").json()["report"]
                  == workspace_task["report"],
              deleted_rate.text)
        only_rate = cli.delete(
            f"{BASE}/api/workspace/channels/rates/1",
            params={"channel_name": "工作台渠道"})
        check("渠道唯一评测档位不可单独删除",
              only_rate.status_code == 400
              and "唯一评测档位" in only_rate.json()["detail"], only_rate.text)
        remaining_workspace = cli.get(f"{BASE}/api/workspace").json()
        remaining_channel = next(channel for channel in remaining_workspace["channels"]
                                 if channel["name"] == "工作台渠道")
        check("倍率删除不影响同渠道其他倍率",
              [model["id"] for model in remaining_channel["models"]]
                  == [workspace_model_one["id"]], remaining_channel)
        deleted_logical_channel = cli.delete(
            f"{BASE}/api/workspace/channels",
            params={"channel_name": "工作台渠道"})
        after_logical_delete = cli.get(f"{BASE}/api/workspace").json()
        check("删除工作台渠道会删除全部同名连接并保留历史报告",
              deleted_logical_channel.status_code == 200
              and deleted_logical_channel.json()["deleted_connections"] == 2
              and all(channel["name"] != "工作台渠道"
                      for channel in after_logical_delete["channels"])
              and cli.get(f"{BASE}/api/tasks/{workspace_task['id']}").json()["report"]
                  == workspace_task["report"],
              deleted_logical_channel.text)

        # 13. 分组回收站、冲突恢复与渠道自由改组
        print("\n[13] 分组无损删除与撤销")
        mini_target = next(model for model in grouped_models
                           if model["model"] == "gpt-4o-mini")
        mini_group = automatic_groups["gpt-4o-mini"]
        report_before_delete = cli.get(
            f"{BASE}/api/tasks/{batch['task_ids'][0]}").json()["report"]
        wrong_name = cli.delete(
            f"{BASE}/api/groups/{mini_group['id']}",
            params={"confirm_name": "错误名称"})
        check("删除接口二次校验完整分组名", wrong_name.status_code == 400,
              wrong_name.text)
        deleted = cli.delete(
            f"{BASE}/api/groups/{mini_group['id']}",
            params={"confirm_name": mini_group["name"]})
        check("含渠道和标杆的分组可进入回收站", deleted.status_code == 200,
              deleted.text)
        released_target = next(target for target in cli.get(f"{BASE}/api/targets").json()
                               if target["id"] == mini_target["id"])
        check("删除只释放渠道分组，不删除测试记录",
              released_target["group_id"] is None
              and cli.get(f"{BASE}/api/tasks/{batch['task_ids'][0]}").json()["report"]
                  == report_before_delete)
        check("独立标杆档案仍然存在",
              any(benchmark["id"] == auto_golden.json()["benchmark"]["id"]
                  for benchmark in cli.get(f"{BASE}/api/benchmarks").json()))
        restored = cli.post(
            f"{BASE}/api/deleted-groups/{mini_group['id']}/restore").json()
        check("立即撤销完整恢复空闲渠道和标杆",
              restored["status"] == "restored"
              and restored["restored_member_ids"] == [mini_target["id"]]
              and restored["benchmark_status"] == "restored", restored)

        cli.delete(
            f"{BASE}/api/groups/{mini_group['id']}",
            params={"confirm_name": mini_group["name"]})
        reassigned = cli.put(
            f"{BASE}/api/targets/{mini_target['id']}/group",
            json={"model": "gpt-4o-mini", "multiplier": 1.2}).json()
        benchmark_id = auto_golden.json()["benchmark"]["id"]
        cli.put(f"{BASE}/api/groups/{peer_group['id']}/benchmark",
                json={"benchmark_id": benchmark_id})
        conflict_restore = cli.post(
            f"{BASE}/api/deleted-groups/{mini_group['id']}/restore").json()
        check("同模型同倍率组已重建时安全合并",
              conflict_restore["status"] == "merged"
              and conflict_restore["group"]["id"] == reassigned["group_id"],
              conflict_restore)
        check("撤销不抢回已迁移渠道或已占用标杆",
              conflict_restore["skipped_member_ids"] == [mini_target["id"]]
              and conflict_restore["benchmark_status"] == "in_use",
              conflict_restore)
        transferred = cli.put(
            f"{BASE}/api/groups/{reassigned['group_id']}/benchmark",
            json={"benchmark_id": benchmark_id}).json()
        peer_after_transfer = next(group for group in cli.get(f"{BASE}/api/groups").json()
                                   if group["id"] == peer_group["id"])
        check("用户可显式转移保留的标杆",
              transferred["group"]["benchmark_id"] == benchmark_id
              and peer_after_transfer["benchmark_id"] is None,
              {"transferred": transferred, "peer": peer_after_transfer})

        custom_assignment = cli.put(
            f"{BASE}/api/targets/{mini_target['id']}/group",
            json={"model": "自定义分组模型", "multiplier": 0.7}).json()
        custom_group = next(group for group in cli.get(f"{BASE}/api/groups").json()
                            if group["id"] == custom_assignment["group_id"])
        check("渠道可用自定义模型和任意倍率建组",
              custom_group["name"] == "自定义分组模型-0.7x组"
              and custom_group["multiplier"] == 0.7, custom_group)
        check("分组模型输入不修改实际调用模型",
              custom_assignment["model"] == "gpt-4o-mini", custom_assignment)
        ungrouped = cli.delete(
            f"{BASE}/api/targets/{mini_target['id']}/group").json()
        check("渠道可主动移出分组且保留配置",
              ungrouped["group_id"] is None and ungrouped["model"] == "gpt-4o-mini",
              ungrouped)

        disposable = cli.post(f"{BASE}/api/groups", json={
            "name": "临时模型-0.7x组", "multiplier": 0.7}).json()
        cli.delete(f"{BASE}/api/groups/{disposable['id']}",
                   params={"confirm_name": disposable["name"]})
        bad_purge = cli.delete(
            f"{BASE}/api/deleted-groups/{disposable['id']}",
            params={"confirm_name": "错误名称"})
        purged = cli.delete(
            f"{BASE}/api/deleted-groups/{disposable['id']}",
            params={"confirm_name": disposable["name"]})
        check("彻底删除快照也需要名称确认",
              bad_purge.status_code == 400 and purged.status_code == 200,
              {"bad": bad_purge.text, "purged": purged.text})
        expiring = cli.post(f"{BASE}/api/groups", json={
            "name": "过期快照模型-0.7x组", "multiplier": 0.7}).json()
        cli.delete(f"{BASE}/api/groups/{expiring['id']}",
                   params={"confirm_name": expiring["name"]})
        from app import store as app_store
        app_store.update("deleted_groups", expiring["id"], {"expires_at": time.time() - 1})
        after_expiry = cli.get(f"{BASE}/api/deleted-groups").json()
        check("超过 30 天的快照自动清理",
              all(group["id"] != expiring["id"] for group in after_expiry), after_expiry)

    print("\n" + "=" * 46)
    print(f"通过 {len(fails) == 0}，失败项：{fails if fails else '无'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
