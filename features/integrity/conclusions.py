"""Conservative closed-set behavioral conclusions, never identity authentication."""
import math


def conclusion(methods):
    def unknown(reason, state="review"):
        return {"label": "无法判定：" + reason, "state": state, "model": None,
                "identity_authenticated": False, "calibration_status": "unvalidated"}
    predictions, conditions = [], []
    for method in ("modeltrace", "nerfed"):
        item = methods.get(method, {})
        if item.get("state") in {"waiting", "sampling"}:
            return unknown("采样尚未完成", item["state"])
        if (item.get("status") != "completed" or item.get("unknown") or item.get("not_run")
                or item.get("request_errors") or item.get("valid", 0) < 2):
            return unknown("有效样本不足或连接异常", "incomplete")
        score = item.get("score", {})
        rows = score.get("results", [])
        if score.get("status") != "scored" or len(rows) < 2:
            return unknown("指纹证据不足")
        top, second = rows[:2]
        values = [top.get("probability"), top.get("score"), second.get("score")]
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in values):
            return unknown("指纹证据无效")
        if top["probability"] < .8 or top["score"] - second["score"] < .5:
            return unknown("候选模型区分不充分")
        if not isinstance(top.get("model"), str) or score.get("prediction") != top["model"]:
            return unknown("候选模型证据无效")
        current = score.get("conditions", {})
        parameters = current.get("parameters", {})
        if (parameters.get("reasoning_effort") != "low" or parameters.get("stream") is not False
                or parameters.get("retry_policy") != "connection-retries-v1"):
            return unknown("采样条件无法核实")
        conditions.append(tuple(current.get(k) for k in ("provider", "model", "protocol")))
        predictions.append(top["model"])
    if conditions[0] != conditions[1] or any(v is None for v in conditions[0]):
        return unknown("MT 与 Nerfed 采样条件发生变化")
    if predictions[0] != predictions[1]:
        return unknown("MT 与 Nerfed 结果不一致")
    model = predictions[0]
    return {"label": "更接近 " + ("Astra" if model == "gpt-6-astra" else model),
            "state": "observed", "model": model, "identity_authenticated": False,
            "calibration_status": "unvalidated"}
