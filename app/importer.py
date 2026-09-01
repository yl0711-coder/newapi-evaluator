"""从 JSON、curl、KEY=VALUE 或普通文本中尽量提取中转站配置。"""
import json
import re
from typing import Any

from .security import mask

# 各字段的常见别名，中转站不同版本导出的 key 名不完全一样
ALIASES = {
    "base_url": ["base_url", "baseUrl", "api_base", "apiBase", "url", "endpoint", "host"],
    "model": ["model", "model_name", "modelName", "models"],
    "api_key": ["api_key", "apiKey", "key", "token", "secret", "authorization"],
    "name": ["name", "channel", "channel_name", "provider", "title"],
    "protocol": ["protocol", "type", "endpoint_type", "supported_endpoint_types"],
    "group_name": ["group", "group_name", "groups"],
}

def _pick(data: dict[str, Any], field: str) -> str:
    """按别名在字典里找值，大小写不敏感。"""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for alias in ALIASES[field]:
        val = lowered.get(alias.lower())
        if val in (None, "", [], {}):
            continue
        if isinstance(val, list):
            val = val[0] if val else ""
        return str(val).strip()
    return ""


def _from_curl(text: str) -> dict[str, Any]:
    """从 curl 命令里抽地址、Key 和模型。"""
    out: dict[str, Any] = {}
    url = re.search(r"curl\s+(?:-[a-zA-Z-]+\s+)*['\"]?(https?://[^\s'\"]+)", text)
    if url:
        raw = url.group(1)
        # 去掉 /v1/chat/completions 之类的路径尾巴，只留 base
        out["base_url"] = re.sub(r"/v1/(chat/completions|messages|models).*$", "", raw)
    key = re.search(r"(?:Authorization:\s*Bearer|x-api-key:)\s*([^\s'\"\\]+)", text, re.I)
    if key:
        out["api_key"] = key.group(1)
    body = re.search(r"-d\s+'(\{.*?\})'", text, re.S) or re.search(r'-d\s+"(\{.*?\})"', text, re.S)
    if body:
        try:
            out.update(json.loads(body.group(1)))
        except ValueError:
            m = re.search(r'"model"\s*:\s*"([^"]+)"', body.group(1))
            if m:
                out["model"] = m.group(1)
    if "anthropic" in text.lower() or "x-api-key" in text.lower():
        out.setdefault("protocol", "anthropic")
    return out


def _from_kv(text: str) -> dict[str, Any]:
    """解析 KEY=VALUE 或 KEY: VALUE 逐行格式。"""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([\w.\-]+)\s*[=:]\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"',")
    return out


def _from_text(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    url = re.search(r"https?://[^\s'\"<>{}\[\]，。；、]+", text, re.I)
    if url:
        out["base_url"] = url.group(0).rstrip(".,;:!?)")
    key = re.search(r"(?<![\w.-])(sk-[A-Za-z0-9][A-Za-z0-9._-]*)", text)
    if key:
        out["api_key"] = key.group(1)
    return out


def _normalize_protocol(raw: str) -> str:
    low = (raw or "").lower()
    return "anthropic" if "anthropic" in low or "claude" in low else "openai"


def _normalize_url(raw: str) -> str:
    """补协议头、去掉多余的接口路径尾巴。"""
    url = (raw or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return re.sub(r"/v1(/(chat/completions|messages|models).*)?$", "", url)


def parse_config(text: str) -> dict[str, Any]:
    """返回所有能识别的字段；未识别字段为空，由用户确认和补充。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("粘贴内容为空，请复制中转站的测试配置后重试")

    data: dict[str, Any] = {}
    if raw.startswith("{") or raw.startswith("["):
        try:
            parsed = json.loads(raw)
            data = parsed[0] if isinstance(parsed, list) and parsed else parsed
            if not isinstance(data, dict):
                data = {}
        except json.JSONDecodeError:
            data = _from_kv(raw)
    elif "curl" in raw[:200].lower():
        data = _from_curl(raw)
    else:
        data = _from_kv(raw)

    # 嵌套一层的情况，例如 {"channel": {...}} 或 {"data": {...}}
    for wrapper in ("channel", "config", "data", "target"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            merged = {**inner, **{k: v for k, v in data.items() if k != wrapper}}
            data = merged
            break

    extracted = _from_text(raw)
    for field, value in extracted.items():
        if not _pick(data, field):
            data[field] = value

    result = {
        "name": _pick(data, "name"),
        "base_url": _normalize_url(_pick(data, "base_url")),
        "model": _pick(data, "model"),
        "api_key": _pick(data, "api_key"),
        "protocol": _normalize_protocol(_pick(data, "protocol")),
        "group_name": _pick(data, "group_name"),
    }
    if result["api_key"].lower().startswith("bearer "):
        result["api_key"] = result["api_key"][7:].strip()

    if not result["name"] and result["base_url"]:
        host = re.sub(r"^https?://", "", result["base_url"]).split("/")[0]
        result["name"] = f"{host} · {result['model']}" if result["model"] else host

    return {
        **result,
        "key_masked": mask(result["api_key"]),
        "has_key": bool(result["api_key"]),
        "source": "import",
    }
