import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CaseInput(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    total_tokens: int | None = Field(default=None, ge=0, le=20_000_000)
    latency_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    latency_kind: Literal["total", "first_content", "unknown"] = "unknown"
    status_code: int | None = Field(default=None, ge=100, le=599)
    stream: bool

    @model_validator(mode="after")
    def tokens_consistent(self):
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if all(v is None for v in values):
            raise ValueError("需要至少一项 Token 记录")
        if all(v is not None for v in values) and sum(values[:2]) != values[2]:
            raise ValueError("输入输出与总 Token 不一致")
        if self.total_tokens is not None and any(v is not None and v > self.total_tokens for v in values[:2]):
            raise ValueError("分项不能超过总 Token")
        return self


class CaseImport(StrictModel):
    cases: list[CaseInput] = Field(min_length=1, max_length=50)


class Target(StrictModel):
    mode: Literal["mock", "live"] = "mock"
    protocol: Literal["openai", "responses", "anthropic"] = "openai"
    model: str = Field(default="diagnosis-mock", min_length=1, max_length=100,
                       pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    channel_id: int | None = Field(default=None, ge=1)
    mock_scenario: Literal["healthy", "large_input_error", "stream_break", "slow_first", "missing_usage"] = "healthy"

    @model_validator(mode="after")
    def matching_mode(self):
        if self.model.lower().startswith(("sk-", "bearer")):
            raise ValueError("模型名称不能填写凭据")
        if (self.mode == "live") != (self.channel_id is not None):
            raise ValueError("真实目标必须选择公共渠道，Mock 不使用公共渠道")
        if self.mode == "live" and self.mock_scenario != "healthy":
            raise ValueError("模拟故障仅用于本地 Mock")
        return self


class PlanInput(StrictModel):
    case_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target: Target = Field(default_factory=Target)
    input_tokens: int = Field(default=1024, ge=32, le=65_536)
    output_tokens: int = Field(default=256, ge=2, le=8192)
    repetitions: int = Field(default=2, ge=1, le=5)
    variants: list[Literal["smaller_input", "smaller_output", "toggle_stream"]] = Field(
        default_factory=lambda: ["smaller_input", "smaller_output", "toggle_stream"], max_length=3)
    timeout_seconds: float = Field(default=60, ge=1, le=180)
    max_duration_seconds: float = Field(default=600, ge=1, le=1800)
    first_content_timeout_seconds: float = Field(default=30, ge=0.1, le=180)
    idle_timeout_seconds: float = Field(default=15, ge=0.1, le=180)
    max_estimated_tokens: int = Field(default=200_000, ge=1, le=1_000_000)
    seed: int = Field(default=17, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def distinct_variants(self):
        if len(self.variants) != len(set(self.variants)):
            raise ValueError("对照组不能重复")
        if max(self.first_content_timeout_seconds, self.idle_timeout_seconds) > self.timeout_seconds:
            raise ValueError("首段及空闲时限不能超过总时限")
        return self


class StartInput(StrictModel):
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirm_live: bool = False


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_plan(body, case, target_snapshot):
    groups = [{"variant": "baseline", "input_tokens": body.input_tokens,
               "output_tokens": body.output_tokens, "stream": case["stream"]}]
    for variant in body.variants:
        item = {**groups[0], "variant": variant}
        if variant == "smaller_input":
            item["input_tokens"] = max(32, body.input_tokens // 2)
        elif variant == "smaller_output":
            item["output_tokens"] = max(1, body.output_tokens // 2)
        else:
            item["stream"] = not case["stream"]
        if all(item[k] == groups[0][k] for k in ("input_tokens", "output_tokens", "stream")):
            raise ValueError("对照值与原条件相同，请提高目标规模")
        groups.append(item)
    attempts = []
    for repetition in range(body.repetitions):
        for group in groups:
            attempts.append({**group, "ordinal": len(attempts) + 1, "repetition": repetition + 1,
                             "seed": body.seed + repetition})
    estimated = sum(row["input_tokens"] + row["output_tokens"] for row in attempts)
    if estimated > body.max_estimated_tokens:
        raise ValueError("计划超过本次 Token 估算上限，请减少规模、轮次或对照组")
    assumptions = ["使用独立合成文本，无法复现原内容、原网络或历史负载。",
                   "输入 Token 为 ASCII 字符数/4 的估算，真实 usage 单独记录，不保证精确配平。",
                   "输出目标不是最低输出保证；历史耗时和状态不控制新请求结果。"]
    if case["input_tokens"] is None or case["output_tokens"] is None:
        assumptions.append("原输入或输出 Token 未分别记录；计划中的分项由操作者指定，未从总量推断。")
    if case["status_code"] != 200:
        assumptions.append("原失败或状态未知请求的输出量，不代表其原定输出上限。")
    if case["latency_kind"] == "unknown":
        assumptions.append("原耗时口径未知，不计算与新总耗时的比值。")
    value = {"schema_version": 1, "case": case, "config": body.model_dump(), "target": target_snapshot,
             "template_version": "synthetic-records-v1", "token_estimator": "ascii_chars_div_4",
             "attempts": attempts, "request_count": len(attempts), "estimated_tokens": estimated,
             "assumptions": assumptions, "comparison_scope": "request_features_only"}
    value["fingerprint"] = fingerprint(value)
    return value
