"""Supplier declarations describe the offered interface, not an inferred backend."""
from datetime import date
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

UPSTREAM_TYPES = {
    "unknown": "未确认", "openai": "OpenAI 官方", "newapi": "New API",
    "sub2api": "Sub2API", "codex": "Codex / OAuth 账号池", "anthropic": "Anthropic 官方",
    "gemini": "Gemini 官方", "deepseek": "DeepSeek 官方", "native": "其他原生厂商",
    "custom": "第三方自研中转",
}
# SOURCE: QuantumNous/new-api v1.0.0-rc.26 constant/channel.go
CHANNEL_TYPES = {"unknown": "未确认", "openai": "OpenAI", "newapi": "New API", "sub2api": "Sub2API",
                 "codex": "ChatGPT Subscription (Codex)", "anthropic": "Anthropic", "gemini": "Gemini",
                 "deepseek": "DeepSeek", "native": "其他原生类型（待确认）", "custom": "自研接入（待确认）",
                 "advanced": "Advanced Custom（高级自定义）"}
CHANNEL_TYPE_IDS = {"openai": 1, "anthropic": 14, "gemini": 24, "deepseek": 43,
                    "codex": 57, "advanced": 58, "sub2api": 59, "newapi": 60}
SOURCE_TYPES = {"unknown": "未确认", "documentation": "供应商文档", "supplier": "供应商人工确认",
                "observation": "实际测试", "other": "其他"}
SECRET_PATTERN = re.compile(r"(?i)(?:\bsk-[a-z0-9_-]{16,}|\bBearer\s+\S+|(?:api[_-]?key|access_token|password|authorization)\s*[:=]\s*\S+|https?://\S*[?@]\S*)")


def safe_text(value: str) -> str:
    if SECRET_PATTERN.search(value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("资料不能包含凭据、带查询参数的地址或控制字符")
    return value


class ProtocolProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    upstream_type: Literal["unknown", "openai", "newapi", "sub2api", "codex", "anthropic", "gemini", "deepseek", "native", "custom"] = "unknown"
    proposed_type: Literal["unknown", "openai", "newapi", "sub2api", "codex", "anthropic", "gemini", "deepseek", "native", "custom", "advanced"] = "unknown"
    credential_mode: Literal["api_key", "codex_oauth"] = "api_key"
    supplier: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=2000)
    confirmation_source: Literal["unknown", "documentation", "supplier", "observation", "other"] = "unknown"
    confirmed_on: date | None = None
    note: str = Field(default="", max_length=2000)

    @field_validator("supplier", "description", "note")
    @classmethod
    def no_credentials(cls, value):
        return safe_text(value)

    @field_validator("confirmed_on")
    @classmethod
    def not_future(cls, value):
        if value and value > date.today():
            raise ValueError("确认日期不能晚于今天")
        return value


def clean_profile(value, secrets=()):
    profile = ProtocolProfile.model_validate(value or {}).model_dump(mode="json")
    for text in (profile["supplier"], profile["description"], profile["note"]):
        if any(secret and secret in text for secret in secrets):
            raise ValueError("协议资料不能包含渠道密钥")
    return profile
