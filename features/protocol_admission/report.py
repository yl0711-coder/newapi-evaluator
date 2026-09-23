import html
import json
from .catalog import PROTOCOLS

ERRORS = {
    "search_no_results": "搜索工具报告未找到结果，本次能力待确认",
    "search_failed": "搜索工具明确返回失败",
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
    "channel_changed": "渠道在检测期间变化，本项未执行",
}

STATUS_LABELS = {"supported": "支持", "unsupported": "不支持", "failed": "未通过",
                 "unconfirmed": "待确认", "not_tested": "未检测", "running": "检测中"}
UNCERTAIN_ERRORS = {"authentication_failed", "endpoint_unconfirmed", "request_invalid", "rate_limited",
                    "request_timeout", "upstream_timeout", "upstream_error", "http_error", "connection_error",
                    "headers_timeout", "first_byte_timeout", "idle_timeout", "total_timeout",
                    "cancelled", "interrupted", "internal_error", "model_mapping_unconfirmed", "channel_changed"}
UNSUPPORTED_ERRORS = {"upstream_protocol_unsupported", "local_protocol_unsupported"}


def capability(rows):
    if any(r.get("status") == "passed" for r in rows):
        state = "supported"
    elif any(r.get("status") == "running" for r in rows):
        state = "running"
    elif not rows or all(r.get("status") == "not_run" for r in rows):
        state = "not_tested"
    elif all(r.get("error_class") in UNSUPPORTED_ERRORS for r in rows):
        state = "unsupported"
    elif any(r.get("status") in {"not_run", "unconfirmed"} or r.get("error_class") in UNCERTAIN_ERRORS for r in rows):
        state = "unconfirmed"
    else:
        state = "failed"
    return {"status": state, "label": STATUS_LABELS[state]}


def evaluate(report):
    for row in report["probes"]:
        if report["config"].get("all_channels") and row.get("channel_id") is None:
            # Reports written before channel_id was persisted encoded it in c<id>-... IDs.
            probe_id = row.get("probe_id") or row.get("id") or ""
            prefix = probe_id.split("-", 1)[0]
            if prefix.startswith("c") and prefix[1:].isdigit():
                row["channel_id"] = int(prefix[1:])
        row["capability"] = capability([row])
    models = []
    groups = [(None, model) for model in report["config"]["models"]]
    if report["config"].get("all_channels"):
        groups = [(channel_id, model) for channel_id in report["config"].get("channel_ids", [])
                  for model in report["config"]["models"]]
    for channel_id, model in groups:
        rows = {r["check"]: r for r in report["probes"]
                if r["model"] == model["model"] and r.get("channel_id") == channel_id}
        protocols = {}
        for protocol, spec in PROTOCOLS.items():
            modes = {name: capability([rows[check]] if check in rows else [])
                     for name, check in spec.items() if name != "name"}
            protocols[protocol] = {"name": spec["name"],
                **capability([rows.get(spec[name], {"status": "not_run"}) for name in ("basic", "stream")]),
                "details": modes}
        models.append({"model": model["model"], "channel_id": channel_id,
                       "channel_name": report["config"].get("channel_names", {}).get(str(channel_id), "") if channel_id else "",
                       "upstream_model": model.get("upstream_model") or model["model"],
                       "protocols": protocols, "search": capability([rows["alpha_search"]] if "alpha_search" in rows else []),
                       "supported_protocols": [key for key, value in protocols.items() if value["status"] == "supported"]})
    report["capabilities"] = models
    report["warnings"] = (["本地 Mock 演示结果，不能证明真实上游支持这些模型或协议"] if report["config"]["mode"] == "mock" else [])
    return report


def html_report(report):
    e = lambda value: html.escape(str(value))
    rows = "".join("<tr><td>" + e(m["upstream_model"]) + "</td>" +
                   "".join("<td>" + e(m["protocols"][key]["label"]) + "</td>" for key in PROTOCOLS) + "</tr>"
                   for m in report["capabilities"])
    details = "".join(f"<tr><td>{e(p['model'])}</td><td>{e(p['label'])}</td><td>{e(p['capability']['label'])}</td><td>{e(p.get('http_status'))}</td><td>{e(ERRORS.get(p.get('error_class'), p.get('error_class', '')))}</td></tr>" for p in report["probes"])
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>模型与协议检测报告</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:16px}table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>模型与协议检测报告</h1>'
            + f"<p>运行 {e(report['id'])} · {e(report['state'])} · {e(report['config']['mode'])}</p>"
            + "".join(f"<p>{e(w)}</p>" for w in report["warnings"])
            + '<p>支持表示本次至少一种普通调用方式通过；流式、工具和搜索分别查看明细。仅覆盖已检测模型。</p><table><tr><th>模型</th>'
            + "".join(f"<th>{e(v['name'])}</th>" for v in PROTOCOLS.values()) + '</tr>' + rows
            + '</table><h2>检测明细</h2><table><tr><th>模型</th><th>检测项目</th><th>状态</th><th>HTTP</th><th>说明</th></tr>' + details
            + '</table><details><summary>完整脱敏证据（与 JSON 下载一致）</summary><pre>'
            + e(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></details></html>')
