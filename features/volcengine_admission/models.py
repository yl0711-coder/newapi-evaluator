"""Strict, credential-safe HTTP inputs for one-time admission."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Spec(Input):
    resolution: Literal["480p", "720p", "1080p", "4k"] | None = None
    duration: int | None = Field(default=None, strict=True, ge=2, le=30)
    frames: int | None = Field(default=None, strict=True, ge=29, le=289)
    size: Literal["1K", "1.5K", "2K", "3K", "4K"] | None = None


class Selection(Input):
    model_id: str = Field(min_length=1, max_length=100)
    spec: Spec = Field(default_factory=Spec)


class PreviewInput(Input):
    mode: Literal["mock", "live"] = "mock"
    items: list[Selection] = Field(min_length=1, max_length=11)
    prompt: str = Field(default="A red wooden toy boat floating on still blue water, daylight, no text.",
                        min_length=1, max_length=4000, repr=False)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        if not value.strip():
            raise ValueError("提示词不能为空")
        if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("提示词含不支持的控制字符")
        return value.strip()


class CredentialInput(Input):
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096)

    @field_validator("api_key")
    @classmethod
    def key_characters(cls, value):
        key = value.get_secret_value()
        if key and any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("Key 不允许空格、控制字符或非 ASCII 字符")
        return value


class AuthInput(CredentialInput):
    mode: Literal["mock", "live"] = "mock"
    confirm_read: bool = Field(default=False, strict=True)


class RunInput(CredentialInput):
    preview_id: str = Field(min_length=32, max_length=64, pattern=r"^[a-f0-9]+$")
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    idempotency_key: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    confirm_live: bool = Field(default=False, strict=True)


class ResumeInput(CredentialInput):
    confirm_read: bool = Field(default=False, strict=True)


class CleanupInput(Input):
    confirm_local_delete: bool = Field(default=False, strict=True)
