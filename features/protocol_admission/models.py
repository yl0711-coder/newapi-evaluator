from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from features.model_coverage.catalog import model_name


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelTarget(StrictModel):
    model: str = Field(min_length=1, max_length=160)
    upstream_model: str = Field(default="", max_length=160)

    @field_validator("model", "upstream_model")
    @classmethod
    def valid_model(cls, value):
        return model_name(value) if value else value


class ConnectionInput(StrictModel):
    channel_id: int | None = Field(default=None, ge=1)
    base_url: str = Field(default="", max_length=1000)


class DiscoveryInput(ConnectionInput):
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    mode: Literal["mock", "live"] = "mock"
    confirm_live: bool = False


class PlanInput(ConnectionInput):
    models: list[ModelTarget] = Field(min_length=1, max_length=5)
    total_timeout: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    first_byte_timeout: float = Field(default=10, ge=0.1, le=60, allow_inf_nan=False)
    idle_timeout: float = Field(default=10, ge=0.1, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({m.model for m in self.models}) != len(self.models):
            raise ValueError("模型不能重复")
        return self


class StartInput(PlanInput):
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    mode: Literal["mock", "live"] = "mock"
    confirm_live: bool = False
    preview_fingerprint: str = Field(min_length=64, max_length=64)
