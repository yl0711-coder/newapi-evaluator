from __future__ import annotations

from features.admission.main import PRESETS


MODEL_GROUPS = {
    str(item["id"]).casefold(): str(item["provider"])
    for item in PRESETS
}

FAMILY_MARKERS = (
    (("claude",), "Claude"),
    (("codex", "gpt-"), "Codex"),
    (("glm-",), "智谱"),
    (("kimi-",), "Kimi"),
    (("deepseek-",), "DeepSeek"),
)


def group_for_model(model: str) -> str:
    normalized = model.strip().casefold()
    if not normalized:
        raise ValueError("模型不能为空")
    if normalized in MODEL_GROUPS:
        return MODEL_GROUPS[normalized]
    for markers, group in FAMILY_MARKERS:
        if any(marker in normalized for marker in markers):
            return group
    raise ValueError("无法确定该模型所属分组，请先在准入模型预设中登记")


def model_options() -> list[dict[str, str]]:
    return [
        {
            "id": str(item["id"]),
            "label": str(item["label"]),
            "group": str(item["provider"]),
        }
        for item in PRESETS
    ]
