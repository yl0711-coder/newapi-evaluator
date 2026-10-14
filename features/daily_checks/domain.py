from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import re
from statistics import median
from zoneinfo import ZoneInfo

from features.integrity.strategies import canonical_hash
from shared.channel_protocol import SECRET_PATTERN
from shared.redaction import scrub

VERSION = "online-channel-daily-v1"
TIMEZONE = "Asia/Shanghai"
REPORT_TIMES = ("10:30", "14:00", "15:30", "18:00")
LEAD_SECONDS = 3600
MT_ALIASES = {"claude-opus-4.7": "claude-opus-4-7", "claude-opus-4.8": "claude-opus-4-8",
              "claude-sonnet-4.6": "claude-sonnet-4-6"}
REASONS = {
    "family_unrecognized": "模型家族未识别，请在渠道分类填写 GPT、codex 或 Claude",
    "family_not_configured": "该模型家族尚未配置测试模型",
    "channel_disabled": "已上线资料中的渠道已停用",
    "credential_unavailable": "API Key 缺失或无法解密",
    "target_unavailable": "渠道连接或模型协议映射无效",
    "trusted_reference_missing": "尚未配置同模型、同协议的可信参照",
    "reference_changed": "可信参照或用量条件已变化，请重新保存配置",
    "reference_is_candidate": "本渠道是可信参照，不进行自身用量对照",
    "paired_conditions_mismatch": "候选和参照的实际模型或协议不一致",
    "report_deadline": "报告时间已到，余下请求未发送",
    "start_window_missed": "服务错过本轮测试窗口，未补发请求",
    "plan_changed": "计划已暂停或配置变化，后续请求停止",
    "batch_cancelled": "本轮已取消，未确认的请求不重发",
    "strategy_changed": "检测资产或请求条件已变化",
    "execution_failed": "执行异常，保留已确认和未确认的请求记录",
    "pending": "等待本轮检测",
    "within_threshold": "固定输入双端上报用量在阈值内",
    "fixed_input_did_not_reproduce": "固定输入未复现日志异常，日志证据保留",
    "major_input_token_anomaly": "候选上报输入用量至少为参照2倍且差值至少1024 Token",
    "minor_requires_successive_scheduled_round": "等待相邻下一轮同条件报告确认小偏差",
    "successive_scheduled_minor": "相邻两轮同条件输入用量均偏高",
    "cancelled_unknown": "已停止等待，许可请求结果未确认",
    "target_or_reference_changed_or_execution_stopped": "连接、映射或可信参照变化，后续用量请求停止",
    "candidate_input_usage_missing": "候选未返回输入用量",
    "reference_input_usage_missing": "参照未返回输入用量",
    "deadline_exceeded": "原报告截止时间已到，余下请求未发送",
    "executor_stopped": "进程中断，保留未确认请求及未测项",
}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def families(scope):
    text = str(scope).casefold()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    result = []
    if tokens & {"gpt", "codex", "openai"} or any(t.startswith("gpt") for t in tokens):
        result.append("GPT")
    if tokens & {"claude", "anthropic"} or any(t.startswith("claude") for t in tokens):
        result.append("Claude")
    return result


def display(value, secrets=()):
    text = scrub(str(value or ""), list(secrets))
    text = re.sub(r"https?://\S+", "[地址已隐藏]", text, flags=re.I)
    text = SECRET_PATTERN.sub("[凭据已隐藏]", text)
    return " ".join(text.split())[:160]


def slots(now):
    local = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    for stamp in REPORT_TIMES:
        hour, minute = map(int, stamp.split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        yield {"report_at": due.timestamp(), "start_at": (due - timedelta(seconds=LEAD_SECONDS)).timestamp(),
               "report_time": stamp, "date": due.date().isoformat()}


def contract():
    return {"version": VERSION, "timezone": TIMEZONE, "report_times": list(REPORT_TIMES),
            "start_times": [datetime.fromtimestamp(s["start_at"], ZoneInfo(TIMEZONE)).strftime("%H:%M") for s in slots(0)],
            "lead_seconds": LEAD_SECONDS, "modeltrace_requests": 3, "usage_initial_requests": 2,
            "usage_max_requests": 4, "max_retries": 0, "max_concurrent_modeltrace_requests": 1,
            "max_concurrent_shared_probes": 2, "inventory_source": "last_manual_registry_sync",
            "billing_status": "unverified", "identity_authenticated": False}


def previous_start(start_at):
    today = list(slots(start_at))
    earlier = [s["start_at"] for s in today if s["start_at"] < start_at]
    return max(earlier) if earlier else list(slots(start_at - 86400))[-1]["start_at"]


def identity_verdict(score, job_status, unknown, planned, completed):
    if (job_status != "completed" or unknown or completed != planned or score.get("status") != "scored"
            or score.get("source_verdict") == "UNLISTED"):
        return "无法判定"
    rows = score.get("results", [])
    if (len(rows) < 2 or score.get("valid_answers", 0) < 2 or rows[0].get("model") != score.get("prediction")
            or rows[0].get("probability", 0) < .8 or rows[0].get("score", 0) - rows[1].get("score", 0) < .5):
        return "无法判定"
    return "行为更接近 " + display(rows[0]["model"]) + "（未校准）"


def performance(observations, planned):
    sent = [o for o in observations if o.get("status") != "not_sent"]
    good = [o for o in sent if o.get("status") == "completed"]
    def measured(field):
        return [o[field] for o in good if type(o.get(field)) in {int, float} and math.isfinite(o[field]) and o[field] >= 0]
    values = measured("latency_ms")
    ttft = measured("ttft_ms")
    speed = measured("tokens_per_second")
    errors = {}
    for row in sent:
        if row.get("status") != "completed":
            category = row.get("transport_status") or row.get("status") or "unknown"
            errors[category] = errors.get(category, 0) + 1
    return {"planned": planned, "attempted": len(sent), "completed": len(good),
            "not_run": max(0, planned - len(sent)), "unknown": sum(o.get("status") == "unknown" for o in sent),
            "completion_rate": len(good) / len(sent) if sent else None,
            "latency_median_ms": median(values) if values else None,
            "ttft_median_ms": median(ttft) if ttft else None,
            "tokens_per_second_median": median(speed) if speed else None,
            "latency_samples": len(values), "ttft_samples": len(ttft), "speed_samples": len(speed),
            "p95_latency_ms": None, "errors": errors, "sample_scope": "this_batch_only",
            "stability": "无法判定" if not sent or len(sent) < planned else "本轮请求均完成" if len(good) == len(sent) else "本轮存在失败或未确认请求"}


def fingerprint(value):
    return canonical_hash(value)
