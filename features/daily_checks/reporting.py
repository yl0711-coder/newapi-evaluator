from datetime import datetime
from collections import Counter
from zoneinfo import ZoneInfo

from .domain import TIMEZONE, REASONS, display


def reason_text(reason):
    return REASONS.get(reason, "未完成，详见本轮技术证据" if reason else "")


def notification_parts(report):
    due = datetime.fromtimestamp(report["report_at"], ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    title = f"【Eval 已上线渠道检测】{due} 北京时间"
    lines = [title, f"本轮覆盖 {report['coverage']['fully_measured_rows']}/{report['coverage']['rows']} 个渠道模型组合；已上线渠道 {report['coverage']['online_channels']} 家",
             "ModelTrace 为未校准行为比较；Token 结论只涉及上报用量，实际计费待核实。"]
    for row in report["channels"]:
        label = f"{display(row['channel_name'])}｜{row['family']}｜{row['model'] or '未配置模型'}｜{row['multiplier']}x｜#{row['channel_id']}"
        mt, usage = row["modeltrace"], row["usage"]
        reasons = list(dict.fromkeys(reason_text(r) for r in (mt.get("reason"), usage.get("reason")) if r))
        perf = row["performance"]
        def metric(value):
            return "未测" if value is None else f"{value:.0f} ms"
        lines += [label, f"  MT：{mt['label']}；用量：{usage['label']}",
                  f"  MT 完成 {perf['modeltrace']['completed']}/{perf['modeltrace']['planned']}，响应中位 {metric(perf['modeltrace']['latency_median_ms'])}；"
                  f"usage 候选完成 {perf['usage_candidate']['completed']}/{perf['usage_candidate']['planned']}，响应中位 {metric(perf['usage_candidate']['latency_median_ms'])}，首文本中位 {metric(perf['usage_candidate']['ttft_median_ms'])}"]
        speed = perf['usage_candidate']['tokens_per_second_median']
        lines.append("  输出速度：" + (f"{speed:.1f} Token/s" if speed is not None else "无法测量"))
        if reasons:
            lines.append("  原因：" + "；".join(reasons))
        failures = Counter(perf["modeltrace"]["errors"]) + Counter(perf["usage_candidate"]["errors"])
        if failures:
            lines.append("  请求异常：" + "、".join(f"{k} {v}" for k, v in sorted(failures.items())))
    lines.append("范围：本轮请求；依据最近一次手动同步的渠道资料。缺测不计为正常。")
    chunks, current = [], []
    for line in lines:
        # Reserve room for the JSON envelope and part numbering under Feishu's 20 KB cap.
        if current and len("\n".join([*current, line]).encode()) > 15000:
            chunks.append("\n".join(current))
            current = [title, line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return [f"{body}\n报告 {report['batch_id']} · {i}/{len(chunks)}" for i, body in enumerate(chunks, 1)]


def markdown(report):
    lines = ["# Eval 已上线渠道检测", f"批次：{report['batch_id']}",
             f"时区：{TIMEZONE}；范围：本轮请求；计费未核实；模型身份未认证。", ""]
    for row in report["channels"]:
        name = display(row["channel_name"]).replace("|", "\\|")
        lines += [f"## {name} / {row['family']} / #{row['channel_id']}",
                  f"模型：{row['model'] or '未配置'}；采购倍率：{row['multiplier']}x。",
                  f"ModelTrace：{row['modeltrace']['label']}。", f"Token：{row['usage']['label']}。"]
        for method in ("modeltrace", "usage_candidate", "usage_reference"):
            p = row["performance"][method]
            lines.append(f"{method}：完成 {p['completed']}/{p['planned']}，未确认 {p['unknown']}，未测 {p['not_run']}；响应中位 {p['latency_median_ms']} ms，首文本中位 {p['ttft_median_ms']} ms，输出速度 {p['tokens_per_second_median']} Token/s。")
        for item in (row["modeltrace"], row["usage"]):
            if item.get("reason"):
                lines.append(reason_text(item["reason"]))
        lines.append("")
    return "\n".join(lines)
