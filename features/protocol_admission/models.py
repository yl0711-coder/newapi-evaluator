from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from shared.channel_protocol import ProtocolProfile, safe_text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelTarget(StrictModel):
    model: str = Field(min_length=1, max_length=160, pattern=r"^[\w.\-/:]+$")
    upstream_model: str = Field(default="", max_length=160, pattern=r"^[\w.\-/:]*$")


class GroupTarget(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w.\-]+$")
    template: Literal["codex_standard", "codex_search", "openai_common", "claude"]
    include_responses: bool = False

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        return safe_text(value)


class PlanInput(StrictModel):
    channel_id: int | None = Field(default=None, ge=1)
    base_url: str = Field(default="", max_length=1000)
    profile: ProtocolProfile = Field(default_factory=ProtocolProfile)
    models: list[ModelTarget] = Field(min_length=1, max_length=5)
    groups: list[GroupTarget] = Field(min_length=1, max_length=8)
    total_timeout: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    first_byte_timeout: float = Field(default=10, ge=0.1, le=60, allow_inf_nan=False)
    idle_timeout: float = Field(default=10, ge=0.1, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({m.model for m in self.models}) != len(self.models):
            raise ValueError("模型不能重复")
        if len({g.name for g in self.groups}) != len(self.groups):
            raise ValueError("分组名称不能重复")
        for target in self.models:
            safe_text(target.model)
            safe_text(target.upstream_model)
        return self


class StartInput(PlanInput):
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    mode: Literal["mock", "live"] = "mock"
    confirm_live: bool = False
    preview_fingerprint: str = Field(min_length=64, max_length=64)
