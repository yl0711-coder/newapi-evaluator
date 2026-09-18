import html
import json
from shared.channel_protocol import CHANNEL_TYPES, UPSTREAM_TYPES, CHANNEL_TYPE_IDS
from .catalog import group_checks
from .models import GroupTarget

LABELS = {"blocked": "禁止进入该分组", "internal_only": "仅允许内部测试", "manual_review": "待人工确认"}
ERRORS = {
    "model_mapping_unconfirmed": "返回模型与请求模型不同，请核对映射或版本别名",
    "upstream_protocol_unsupported": "上游明确表示不支持此接口",
    "authentication_failed": "鉴权或访问限制", "local_protocol_unsupported": "当前渠道类型在网关本地拒绝接口",
    "endpoint_unconfirmed": "方法、路径或分组尚需确认", "request_invalid": "请求参数或模型需要核对",
    "rate_limited": "上游限流", "request_timeout": "服务端等待请求超时", "upstream_timeout": "上游等待超时",
    "upstream_error": "上游返回错误", "http_error": "HTTP 请求未成功", "invalid_schema": "响应字段不符合接口格式",
    "generation_incomplete": "生成未完成或被截断", "invalid_tool_call": "工具调用名称、ID 或参数不符合要求",
    "empty_output": "输出为空", "usage_missing": "缺少模板要求的有效 usage",
    "stream_incomplete": "未收到完整流式终态", "stream_protocol_error": "流式事件顺序或终态错误",
    "invalid_json_or_event": "JSON 或流式事件无法解析", "body_too_large": "响应超过 1 MiB 探测上限",
    "connection_error": "连接失败", "headers_timeout": "等待响应头超时", "first_byte_timeout": "等待正文首段超时",
    "idle_timeout": "正文读取空闲超时", "total_timeout": "请求总超时", "search_result_unconfirmed": "搜索正文格式正确，尚未识别到预期来源",
    "cancelled": "已停止", "not_run": "未执行", "interrupted": "服务中断，本次结果未完成", "internal_error": "探测执行异常",
}
BLOCKING_ERRORS = {"upstream_protocol_unsupported", "authentication_failed", "local_protocol_unsupported", "request_invalid", "invalid_schema", "invalid_tool_call",
                   "stream_incomplete", "stream_protocol_error", "invalid_json_or_event", "generation_incomplete", "usage_missing"}


def recommendation(profile):
    source = profile["upstream_type"]
    confirmed = profile["confirmation_source"] in {"documentation", "supplier"} and bool(profile["confirmed_on"])
    if source in {"unknown", "custom", "native"} or not confirmed:
        return "unknown", "需要供应商文档或人工确认，以及确认日期；接口成功不能证明程序类型"
    if source == "codex" and profile["credential_mode"] != "codex_oauth":
        return "unknown", "账号池来源不能确定对外接入类型，请确认供应商提供的 API 协议"
    return source, "依据供应商确认的接入协议；不是通过 HTTP 响应识别出的程序身份"


def evaluate(report):
    config = report["config"]
    profile = config["profile"]
    recommended, basis = recommendation(profile)
    groups, warnings = [], []
    proposed = profile["proposed_type"]
    if proposed != recommended:
        warnings.append("拟配置类型与当前推荐不一致，需先复核配置")
    if config["mode"] == "mock":
        warnings.append("本报告来自本地 Mock，只用于演示和验证工作台，不能证明渠道能力")
    for group in config["groups"]:
        required = group_checks(GroupTarget.model_validate(group))
        rows = [r for r in report["probes"] if r["check"] in required]
        reasons = []
        state = "internal_only"
        if recommended == "unknown" or proposed in {"unknown", "advanced", "custom", "native"} or proposed != recommended:
            state = "manual_review"
            reasons.append("来源或拟配置类型尚待确认；高级自定义需要专项路由评审")
        if "alpha_search" in required and proposed not in {"newapi", "sub2api", "codex", "advanced", "unknown"}:
            state = "blocked"
            reasons.append("RC26 的此渠道类型不允许转发 Alpha Search，降低权重不能解决")
        bad = [r for r in rows if r.get("error_class") in BLOCKING_ERRORS]
        if bad:
            state = "blocked"
            reasons += list(dict.fromkeys(ERRORS[r["error_class"]] for r in bad))
        passed = len(rows) == len(required) * len(config["models"]) and all(r.get("status") == "passed" for r in rows)
        if not passed:
            reasons.append("必测能力尚未全部确认，不具备生产放行证据")
        elif report["state"] == "completed":
            reasons.append("本次直测满足模板；仍需质量、稳定性样本及 NexusAPI 端到端确认")
        if profile["credential_mode"] == "codex_oauth":
            state = "manual_review" if state != "blocked" else state
            reasons.append("第一阶段探测使用供应商 API Key；OAuth 直连需后续网关验证")
        if config["mode"] == "mock":
            reasons.append("Mock 结果不用于实际渠道放行")
        groups.append({"name": group["name"], "template": group["template"], "state": state, "label": LABELS[state],
                       "required_checks": required, "direct_checks_passed": passed, "reasons": reasons})
    report["conclusion"] = {"declared_type": profile["upstream_type"], "proposed_type": proposed,
        "recommended_type": recommended, "recommended_type_id": CHANNEL_TYPE_IDS.get(recommended),
        "proposed_type_id": CHANNEL_TYPE_IDS.get(proposed), "recommendation_basis": basis, "warnings": warnings, "groups": groups,
        "production_ready": False, "newapi_version": "v1.0.0-rc.26",
        "quality_status": "not_evaluated", "stability_status": "not_evaluated", "gateway_status": "not_verified",
        "checklist": ["核对供应商协议资料、凭据方式和建议渠道类型", "在 NexusAPI 后台人工配置模型映射及 internal_test 分组",
                      "执行现有质量快测和稳定性测试，核对样本与分组基线", "在测试分组核对实际 channel_id、请求 ID、重试链路和计费",
                      "证据齐全并完成业务审核后，再人工决定正式分组及灰度权重"]}
    return report


def html_report(report):
    e = lambda value: html.escape(str(value))
    c = report["conclusion"]
    sections = "".join(f"<section><h2>{e(g['name'])}：{e(g['label'])}</h2><ul>" + "".join(f"<li>{e(r)}</li>" for r in g["reasons"]) + "</ul></section>" for g in c["groups"])
    rows = "".join(f"<tr><td>{e(p['model'])}</td><td>{e(p['label'])}</td><td>{e(p['status'])}</td><td>{e(p.get('http_status'))}</td><td>{e(ERRORS.get(p.get('error_class'), p.get('error_class', '')))}</td></tr>" for p in report["probes"])
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NewAPI 配置准入报告</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:16px}table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>NewAPI 配置准入报告</h1>' + f"<p>运行 {e(report['id'])} · {e(report['state'])} · {e(report['config']['mode'])}</p><p>声明：{e(UPSTREAM_TYPES[c['declared_type']])}；拟配置：{e(CHANNEL_TYPES[c['proposed_type']])}；推荐：{e(CHANNEL_TYPES[c['recommended_type']])}</p>" + "".join(f"<p>{e(w)}</p>" for w in c["warnings"]) + sections + '<table><tr><th>模型</th><th>探测</th><th>状态</th><th>HTTP</th><th>说明</th></tr>' + rows + '</table><h2>后台检查清单</h2><ol>' + "".join(f"<li>{e(item)}</li>" for item in c["checklist"]) + '</ol><details><summary>完整脱敏证据（与 JSON 下载一致）</summary><pre>' + e(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></details></html>'
