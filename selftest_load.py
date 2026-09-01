"""压力测试汇总自测：阶梯并发、停止条件、安全并发和缓存观察。"""
import sys

from app import probes, report


fails: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name
          + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def load_step(level: int, index: int, *, ok: bool = True,
              speed: float = 50.0, latency: float = 1.0,
              repeated: bool = False) -> dict:
    return probes.result(
        f"并发 {level}·#{index}", ok,
        reason="" if ok else probes.LIMIT,
        latency=latency, first_token=latency / 2,
        usage={"prompt": 20, "completion": 20 if ok else 0},
        extra={
            "load": True,
            "load_level": level,
            "load_round": 1 if level == 5 else 2,
            "cache_candidate": repeated,
            "round_elapsed": 2.0,
            "tokens_per_second": speed if ok else 0.0,
            "chunks": 2 if ok else 0,
        },
    )


steps = []
for i in range(10):
    steps.append(load_step(
        5, i, repeated=i < 5,
        latency=0.2 if i < 5 else 1.0,
    ))
for i in range(10):
    steps.append(load_step(
        10, i, ok=i != 9, speed=20.0,
        repeated=i < 5, latency=0.5 if i < 5 else 1.1,
    ))

metrics = report.compute_metrics(steps, 2.0, 8.0)
load = metrics["load"]
round5, round10 = load["rounds"]

check("汇总两个并发档位", [row["concurrency"] for row in load["rounds"]] == [5, 10])
check("低负载档稳定", round5["stable"] is True, round5)
check("错误率或速度衰减触发停止", round10["stable"] is False, round10)
check("最大安全并发取最后稳定档", load["safe_concurrency"] == 5,
      load["safe_concurrency"])
check("报告标记提前停止", load["stopped_early"] is True)
check("正确计算成功率", round10["success_rate"] == 0.9,
      round10["success_rate"])
check("重复请求显著更快时只标疑似缓存",
      load["cache_signal"] == "疑似有缓存", load["cache_signal"])
check("缓存比值可追溯", round5["cache_latency_ratio"] == 0.2,
      round5["cache_latency_ratio"])


def performance_step(
    name: str, ok: bool, reason: str, speed: float, latency: float, reply: str,
    grade_status: str,
) -> dict:
    return probes.result(
        name, ok, reason=reason, latency=latency, first_token=1.0,
        usage={"prompt": 10, "completion": 10 if reply else 0},
        extra={"chunks": 3, "performance_probe": True,
               "tokens_per_second": speed, "reply": reply,
               "grade": {"status": grade_status}},
    )


speed_metrics = report.compute_metrics([
    performance_step("正确答案", True, "", 20.0, 2.0, "正确", "passed"),
    performance_step(
        "错误答案", False, probes.MISMATCH, 10.0, 3.0, "错误", "failed"),
    performance_step(
        "协议报错", False, probes.PROTO, 1000.0, 99.0, "", "not_graded"),
], 2.0, 8.0)
check("传输速度统计完整响应，答案错误仍计入",
      speed_metrics["avg_tokens_per_second"] == 15.0,
      speed_metrics["avg_tokens_per_second"])
check("固定速度样本只包含内容判分通过的响应",
      speed_metrics["performance"]["samples"] == 1
      and speed_metrics["performance"]["granular_samples"] == 1,
      speed_metrics["performance"])
check("固定速度延迟排除内容不合格与协议报错",
      speed_metrics["performance"]["p95_latency"] == 2.0,
      speed_metrics["performance"])

load_speed_steps = [
    load_step(1, 1, speed=20.0),
    load_step(1, 2, ok=False, speed=1000.0),
]
load_speed_steps[1]["extra"]["tokens_per_second"] = 1000.0
load_speed_metrics = report.compute_metrics(load_speed_steps, 2.0, 8.0)
check("负载轮次速度排除失败报错",
      load_speed_metrics["load"]["rounds"][0]["tokens_per_second"] == 20.0,
      load_speed_metrics["load"]["rounds"][0])

print("=" * 50)
print(f"通过 {len(fails) == 0}，失败项：{fails if fails else '无'}")
sys.exit(1 if fails else 0)
