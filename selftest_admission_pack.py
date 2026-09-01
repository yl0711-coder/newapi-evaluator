"""简单渠道对比标准自测，不访问网络。"""
import sys

from app import grading, packs, report, runner, test_catalog


failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(name)


def completed_step(item: dict, *, latency: float, first_token: float,
                   grade_status: str = "passed") -> dict:
    return {
        "step": item["id"], "ok": True, "reason": "", "detail": "流式完成",
        "latency": latency, "first_token": first_token,
        "usage": {"prompt": 20, "completion": 60},
        "extra": {
            "item": item["id"], "method_id": item["method_id"],
            "score_domain": item["score_domain"], "dim": item.get("dim"),
            "item_weight": float(item.get("weight") or 1.0), "variant": 1,
            "chunks": 8, "reply": item["mock_answer"],
            "performance_probe": bool(item.get("performance_probe")),
            "tokens_per_second": 30.0,
            "grade": {"status": grade_status,
                      "score": 1.0 if grade_status == "passed" else 0.0,
                      "failure_codes": [] if grade_status == "passed"
                      else ["selftest_wrong_answer"]},
            "eligibility": {"ability": "eligible", "stability": "eligible",
                            "performance": "eligible"},
        },
    }


ability = test_catalog.admission_items("gpt-test", test_catalog.DEFAULT_SEED)
speed = test_catalog.fixed_speed_items()
estimate = packs.estimate("admission", 2.0, 8.0, include_hard=False)
specs = runner._expand(packs.get_pack("admission"), model="gpt-test",
                       seed=test_catalog.DEFAULT_SEED)
model_specs = [spec for spec in specs if spec["kind"] != "auth"]

auth = {
    "step": "QA-PROTO-01 鉴权与模型可达性", "ok": True,
    "reason": "", "detail": "通过", "latency": 0.05, "first_token": 0.0,
    "usage": {"prompt": 0, "completion": 0},
    "extra": {"method_id": "QA-PROTO-01"},
}
steps = [auth]
steps.extend(completed_step(item, latency=1.0 + index / 10,
                            first_token=0.2 + index / 100,
                            grade_status="failed" if index == 0 else "passed")
             for index, item in enumerate(ability))
steps.extend(completed_step(item, latency=value, first_token=value / 10)
             for item, value in zip(speed, (2.0, 2.2, 2.4, 2.6, 2.8)))
metrics = report.compute_metrics(steps, 0.0, 0.0)
built = report.build(
    {"id": 1, "kind": "admission", "created_at": 1.0},
    packs.get_pack("admission"), steps,
    {"model": "gpt-test", "protocol": "openai", "price_in": 0.0,
     "price_out": 0.0},
)

check("固定 6 道智力题", len(ability) == 6, len(ability))
check("智力题覆盖代码、结构化、推理和指令保持",
      {"代码", "结构化", "推理", "指令保持"}
      == {item["dim"] for item in ability})
check("智力门槛为 6 道通过至少 5 道",
      test_catalog.INTELLIGENCE_REQUIRED_PASSES == 5)
check("固定 5 道速度题", len(speed) == 5, len(speed))
check("速度题使用相同输出预算",
      {item["max_tokens"] for item in speed}
      == {test_catalog.FIXED_SPEED_MAX_TOKENS})
check("速度题配置具有固定版本",
      all(test_catalog.FIXED_SPEED_VERSION in item["comparison_key"] for item in speed))
check("所有模型请求都由流式执行类型展开",
      {spec["kind"] for spec in model_specs} == {"eval", "fixed_speed"}, model_specs)
check("简单测试固定 12 个请求", estimate["requests"] == 12, estimate)
check("简单测试没有动态补测", "max_rescue_requests" not in estimate, estimate)
check("所有题均形成流式证据",
      metrics["admission"]["streamed_questions"] == 11, metrics["admission"])
check("智力达到 5/6 即通过",
      metrics["admission"]["passed_items"] == 5
      and metrics["admission"]["intelligence_passed"],
      metrics["admission"])
check("固定速度只统计速度题",
      metrics["fixed_speed"]["samples"] == 5
      and metrics["fixed_speed"]["p50_latency"] == 2.4
      and metrics["fixed_speed"]["p95_latency"] == 2.8,
      metrics["fixed_speed"])
check("全部流式完成时稳定率为 100%",
      metrics["admission"]["stability_rate"] == 1.0
      and metrics["admission"]["stability_passed"], metrics["admission"])
check("答错一题不污染渠道稳定性",
      metrics["admission"]["passed_items"] == 5
      and metrics["admission"]["stability_rate"] == 1.0,
      metrics["admission"])
check("能力、稳定性和固定速度证据完整时最终通过",
      metrics["admission"]["status"] == "passed", metrics["admission"])
check("最终建议使用简单标准结论",
      built["conclusion"]["code"] == "recommend"
      and any("智力门槛通过" in reason for reason in built["conclusion"]["reasons"]),
      built["conclusion"])
check("逐题展示答错但不把它写成断流",
      built["items"][1]["ok"] is False
      and built["metrics"]["admission"]["stability_rate"] == 1.0,
      built["items"][1])
check("任务摘要使用固定速度题 P95",
      "固定速度题 P95 2.8s" in report.summary_line(built), report.summary_line(built))

for item in [*ability, *speed]:
    check(f"{item['id']} 使用注册判分器",
          item["grader"]["id"] in grading.registered_graders(), item["grader"])

print(f"通过 {not failures}，失败项：{failures if failures else '无'}")
sys.exit(1 if failures else 0)
