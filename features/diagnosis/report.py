from collections import Counter
from statistics import median


def report(run):
    groups = []
    case = run["plan"]["case"]
    for variant in dict.fromkeys(row["variant"] for row in run["results"]):
        rows = [row for row in run["results"] if row["variant"] == variant]
        sent = [r for r in rows if r["outcome"] not in ("pending", "not_sent")]
        successful = [r for r in sent if r.get("request_ok")]
        latencies = [r["latency_ms"] for r in successful if r.get("latency_ms") is not None]
        first = [r["ttft_ms"] for r in successful if r.get("ttft_ms") is not None]
        latency = median(latencies) if latencies else None
        ttft = median(first) if first else None
        comparable = {"total": latency, "first_content": ttft}.get(case["latency_kind"])
        historical = case["latency_ms"]
        ratio = comparable / historical if comparable is not None and historical and historical > 0 else None
        groups.append({"variant": variant, "planned": len(rows), "attempted": len(sent),
                       "request_ok": len(successful), "completed": sum(r["outcome"] == "completed" for r in rows),
                       "outcomes": dict(Counter(r["outcome"] for r in rows)),
                       "http_statuses": dict(Counter(str(r.get("http_status")) for r in sent)),
                       "median_success_latency_ms": latency, "median_success_ttft_ms": ttft,
                       "historical_latency_ratio": ratio,
                       "usage_missing": sum(r.get("usage_input_tokens") is None or r.get("usage_output_tokens") is None for r in sent)})
    observations = []
    baseline = groups[0]
    for group in groups[1:]:
        if group["attempted"] and baseline["attempted"]:
            observations.append(f"{group['variant']}：正常结束 {group['completed']}/{group['attempted']}；"
                                f"基准组 {baseline['completed']}/{baseline['attempted']}。")
    return {"schema_version": 1, "run": run, "groups": groups, "observations": observations,
            "conclusion": "这是合成请求的小样本特征对照，仅提供排查线索；不能判断原内容是否触发拒绝，也不能据此确定故障归属。",
            "metric_notes": ["request_ok 包含正常结束及明确达到输出上限；completed 只计正常结束。",
                             "耗时中位数仅使用 request_ok 样本，失败请求保留逐条时间；不报告小样本 p95。",
                             "首段耗时从发出请求至首个可见文本；非流式为收到完整正文后的时间。",
                             "usage 缺失保持 null；Mock usage 为夹具数值；Token 预算是估算而非费用硬上限。",
                             "取消/进程中断可能已到达上游，unknown 不自动重发；没有历史时间序列时不还原并发。"]}


def markdown(value):
    lines = ["# 请求特征诊断报告", "", value["conclusion"], "",
             f"运行：{value['run']['id']} · 状态：{value['run']['state']}", "",
             f"计划指纹：{value['run']['plan']['fingerprint']}", "",
             "| 对照组 | 已尝试 | 正常结束 | 达到输出上限 | 成功总耗时中位数 ms |", "| --- | ---: | ---: | ---: | ---: |"]
    for row in value["groups"]:
        latency = row["median_success_latency_ms"]
        lines.append(f"| {row['variant']} | {row['attempted']} | {row['completed']} | "
                     f"{row['outcomes'].get('output_limit', 0)} | {round(latency, 2) if latency is not None else '未知'} |")
    lines.extend(["", "## 口径与边界", ""] + [f"- {x}" for x in value["metric_notes"] + value["run"]["plan"]["assumptions"]])
    return "\n".join(lines) + "\n"
