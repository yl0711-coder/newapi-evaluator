"""请求体模型。"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImportPayload(BaseModel):
    """从中转站复制过来的配置文本，可能是 JSON、curl 或 KEY=VALUE。"""
    text: str = Field(min_length=1)


class MetricSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=1000)
    token: str = Field(default="", max_length=2000)
    protocol_version: str = Field(default="1", pattern=r"^\d+$")
    enabled: bool = True
    poll_interval_seconds: int = Field(default=60, ge=30, le=3600)


class MetricSourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    endpoint: str | None = Field(default=None, min_length=1, max_length=1000)
    token: str | None = Field(default=None, max_length=2000)
    protocol_version: str | None = Field(default=None, pattern=r"^\d+$")
    enabled: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)


class MetricPayloadIn(BaseModel):
    version: str = Field(pattern=r"^\d+$")
    next_cursor: str = Field(default="", max_length=1000)
    has_more: bool = False
    buckets: list[dict[str, Any]] = Field(max_length=1000)


class RecommendationReviewIn(BaseModel):
    channel_id: int = Field(gt=0)
    production_channel: str = Field(min_length=1, max_length=160)
    activated_at: float = Field(gt=0)


class UsageProfileBindingIn(BaseModel):
    subject_type: Literal["user", "token", "group"]
    subject_id: str = Field(min_length=1, max_length=500)
    subject_label: str = Field(default="", max_length=120)
    primary_profile: Literal["general", "agent", "coding", "customer_service"] = "general"
    secondary_profiles: list[Literal["general", "agent", "coding", "customer_service"]] = \
        Field(default_factory=list, max_length=3)
    note: str = Field(default="", max_length=500)


class MonitorSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    secret: str = Field(min_length=16, max_length=500)
    enabled: bool = True


class MonitorAlertIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    event_time: float = Field(gt=0)
    channel: str = Field(min_length=1, max_length=160)
    model: str = Field(default="", max_length=160)
    platform_group: str = Field(default="", max_length=160)
    symptom: str = Field(min_length=1, max_length=160)
    severity: Literal["info", "warning", "critical"]
    affected_users: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentProbeLocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    location_type: Literal["production", "independent"]
    endpoint: str = Field(min_length=1, max_length=1000)
    token: str = Field(default="", max_length=2000)
    enabled: bool = True
    max_requests_per_hour: int = Field(default=12, ge=1, le=120)


class OnlineVerificationSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=1000)
    token: str = Field(default="", max_length=2000)
    protocol_version: str = Field(default="1", pattern=r"^\d+$")
    enabled: bool = True


class ChannelLaunchConfirmIn(BaseModel):
    owner_note: str = Field(default="", max_length=500)


class FeishuBitableSettingsIn(BaseModel):
    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(default="", max_length=1000)
    base_token: str = Field(min_length=1, max_length=200)
    channel_table_id: str = Field(min_length=1, max_length=200)
    model_table_id: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class ChannelImportIn(ImportPayload):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    protocol: Literal["openai", "anthropic"] | None = None


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    protocol: Literal["openai", "anthropic"] = "openai"
    group_name: str = ""
    env: Literal["prod", "test"] = "prod"
    price_in: float | None = None
    price_out: float | None = None
    source: Literal["import", "manual"] = "manual"
    edited_fields: list[str] = []


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    protocol: Literal["openai", "anthropic"] = "openai"
    group_name: str = ""
    env: Literal["prod", "test"] = "prod"
    source: Literal["import", "manual"] = "manual"
    edited_fields: list[str] = []


class ChannelModelIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1)
    group_id: int | None = None
    price_in: float | None = None
    price_out: float | None = None


class BatchAdmissionIn(BaseModel):
    platform_group_id: int = Field(gt=0)
    upstream_multiplier: float = Field(gt=0, le=100)
    price_in: float | None = None
    price_out: float | None = None


class ModelFamilyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ModelFamilyPatch(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class FamilyModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    sort_order: int = Field(default=0, ge=0, le=10000)


class FamilyModelPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10000)


class ModelAliasIn(BaseModel):
    model_id: int = Field(gt=0)
    alias: str = Field(min_length=1, max_length=120)


class ModelAliasPatch(BaseModel):
    model_id: int | None = Field(default=None, gt=0)
    alias: str | None = Field(default=None, min_length=1, max_length=120)


class LifecycleTransitionIn(BaseModel):
    to_status: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=2, max_length=500)
    replacement_model_id: int | None = Field(default=None, gt=0)


class PlatformGroupIn(BaseModel):
    family_id: int = Field(gt=0)
    online_multiplier: float = Field(gt=0, le=100)


class PlatformGroupPatch(BaseModel):
    online_multiplier: float = Field(gt=0, le=100)


class PlatformGroupTargetIn(BaseModel):
    platform_group_id: int | None = Field(default=None, gt=0)


class PlatformBenchmarkIn(BaseModel):
    source_task_id: int = Field(gt=0)
    tolerance: float = Field(default=0.10, ge=0.0, le=0.5)


class TaskIn(BaseModel):
    """提交任务：只需目标、范围（测试包）和费用上限。

    include_hard 只对能力复测包有意义，别的包忽略它。
    """
    kind: Literal[
        "admission", "inspect", "degrade", "capability", "load",
        "agent_stability", "development_speed", "long_context",
    ]
    target_id: int
    cost_limit: float | None = None
    benchmark_id: int | None = None
    include_hard: bool | None = None
    load_levels: list[int] = Field(default_factory=lambda: [10, 20, 30, 40, 50])
    load_requests_per_level: int = Field(default=20, ge=10, le=200)
    load_cooldown_seconds: int = Field(default=10, ge=0, le=300)
    load_stream: bool = True
    load_mode: Literal["closed", "open"] = "closed"
    load_prompt_profile: Literal["simple", "reasoning", "coding"] = "simple"
    load_max_tokens: int | None = Field(default=None, ge=16, le=8192)
    load_interval_seconds: float = Field(default=0.0, ge=0.0, le=10.0)
    load_burst_period_seconds: float = Field(default=1.0, ge=1.0, le=10.0)
    load_max_in_flight: int = Field(default=250, ge=1, le=1000)
    local_runner_id: int | None = Field(default=None, gt=0)


class TaskMetadataIn(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)
    owner: str = Field(default="", max_length=80)
    review_status: Literal["unreviewed", "in_review", "approved", "rejected"] = "unreviewed"
    note: str = Field(default="", max_length=1000)


IdempotencyKey = Annotated[
    str, Field(min_length=8, max_length=120, pattern=r"^\S+$")
]


class PairedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("reason", check_fields=False)
    @classmethod
    def require_reason_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason 不能为空")
        return normalized


class PairedTaskIn(PairedRequest):
    candidate_target_id: int = Field(gt=0)
    idempotency_key: IdempotencyKey


class PairedStartIn(PairedRequest):
    benchmark_target_id: int = Field(gt=0)
    scale_confirmed: bool = False
    expected_state_version: int = Field(ge=1)
    idempotency_key: IdempotencyKey
    request_limit: int | None = Field(default=None, ge=1, le=10000)
    token_limit: int | None = Field(default=None, ge=1, le=100_000_000)
    money_limit: float | None = Field(default=None, gt=0, le=1_000_000)


class PairedCancelIn(PairedRequest):
    expected_state_version: int = Field(ge=1)
    idempotency_key: IdempotencyKey


class PairedRerunIn(PairedRequest):
    idempotency_key: IdempotencyKey


class FidelityDecisionIn(PairedRequest):
    value: Literal["true", "false"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    expected_state_version: int = Field(ge=1)
    idempotency_key: IdempotencyKey


class FidelityTruthRevokeIn(PairedRequest):
    reason: str = Field(min_length=1, max_length=2000)


class PairedConclusionIn(PairedRequest):
    verdict: Literal["admit", "do_not_admit"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)
    report_version: int = Field(ge=1)
    expected_conclusion_version: int = Field(ge=0)
    idempotency_key: IdempotencyKey


class ConfigurationModelMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_model: str = Field(min_length=1, max_length=240)
    request_model: str = Field(min_length=1, max_length=240)


class ChannelConfigurationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_configuration_id: int | None = Field(default=None, gt=0)
    family_id: int = Field(gt=0)
    channel_name: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    protocol: Literal["openai", "anthropic"]
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=2000)
    upstream_multiplier: float = Field(gt=0, le=100)
    route: str = Field(default="", max_length=240)
    group_name: str = Field(default="", max_length=160)
    model_mappings: list[ConfigurationModelMappingIn] = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)


class ChannelConfigurationPresentationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=1000)


class ConfigurationBusinessStatusIn(PairedRequest):
    to_status: Literal["pending_test", "offline", "online", "disabled"]
    reason_code: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class AdmissionBenchmarkSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_model: str = Field(min_length=1, max_length=240)
    benchmark_configuration_id: int = Field(gt=0)


class AdmissionBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: int = Field(gt=0)
    benchmark_selections: list[AdmissionBenchmarkSelectionIn] = Field(min_length=1, max_length=100)
    scale_confirmed: bool = False


class AdmissionBatchConclusionIn(PairedRequest):
    verdict: Literal["admit", "do_not_admit"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)
    report_versions: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    expected_conclusion_version: int = Field(ge=0)


class AdmissionBatchContinueIn(PairedRequest):
    models: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)


class PairedReportRevisionIn(PairedRequest):
    reason: str = Field(min_length=1, max_length=2000)
    expected_report_version: int = Field(ge=1)
    idempotency_key: IdempotencyKey


class PairedRetentionExtensionIn(PairedRequest):
    raw_expires_at: float | None = Field(default=None, gt=0)
    structured_expires_at: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: IdempotencyKey


class RoleGrantIn(PairedRequest):
    user_id: int = Field(gt=0)
    role: Literal[
        "viewer", "operator", "fidelity_reviewer", "admission_reviewer", "admin",
        "raw_export",
    ]
    scope_type: Literal["global", "channel", "task"] = "global"
    scope_id: str = Field(default="*", min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


class RoleRevokeIn(PairedRequest):
    reason: str = Field(min_length=1, max_length=1000)


class RunnerPairIn(BaseModel):
    pairing_code: str = Field(min_length=8, max_length=40)
    encryption_public_key: str = Field(min_length=40, max_length=200)
    signing_public_key: str = Field(min_length=40, max_length=200)
    version: str = Field(min_length=1, max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RunnerPairingCodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RunnerHeartbeatIn(BaseModel):
    version: str = Field(min_length=1, max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RunnerResultIn(BaseModel):
    job_id: int = Field(gt=0)
    status: Literal["success", "failed", "cancelled"]
    report: dict[str, Any] = Field(default_factory=dict)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    signature: str = Field(min_length=40, max_length=500)


class BenchmarkIn(BaseModel):
    """为一个模型-倍率组定义标杆，可从任务生成或直接填分。"""
    name: str = Field(min_length=1, max_length=80)
    group_id: int | None = None
    model_hint: str = ""
    note: str = ""
    tolerance: float = Field(default=0.10, ge=0.0, le=0.5)
    # 二选一：给任务号从结果生成，或直接给维度分
    source_task_id: int | None = None
    dims: dict[str, float] | None = None


class BenchmarkPatch(BaseModel):
    """改标杆：名字、容差、备注、维度分都可以手工调。"""
    name: str | None = Field(default=None, min_length=1, max_length=80)
    note: str | None = None
    tolerance: float | None = Field(default=None, ge=0.0, le=0.5)
    dims: dict[str, float] | None = None


class BindBenchmarkIn(BaseModel):
    benchmark_id: int | None = None


class BenchmarkCompareIn(BaseModel):
    benchmark_id: int = Field(gt=0)


class TargetGroupIn(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    multiplier: float = Field(gt=0, le=100)


class UpstreamMultiplierIn(BaseModel):
    upstream_multiplier: float = Field(gt=0, le=100)


class GroupIn(BaseModel):
    """模型分组（步骤 2）：同组模型共用倍率和黄金标杆。"""
    name: str = Field(min_length=1, max_length=60)
    multiplier: float = Field(gt=0, le=100)
    note: str = ""
    benchmark_id: int | None = None


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    multiplier: float | None = Field(default=None, gt=0, le=100)
    note: str | None = None
    benchmark_id: int | None = None


class RateGroupIn(BaseModel):
    multiplier: float = Field(gt=0, le=100)


class SetGoldenIn(BaseModel):
    """步骤 3：从该组已有模型的历史任务里指定一条作为黄金标杆。"""
    task_id: int
    name: str | None = Field(default=None, min_length=1, max_length=80)


class DecideIn(BaseModel):
    """步骤 6：人工定夺。group_id 为空表示放入试玩池。"""
    accept: bool = True
    group_id: int | None = None
    note: str = ""


class ScheduledTestIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    report_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    feishu_webhook_ids: list[int] = Field(default_factory=list)
    email_recipient_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class ScheduledConfigurationTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    report_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    configuration_ids: list[int] = Field(min_length=1, max_length=100)
    feishu_webhook_ids: list[int] = Field(default_factory=list, max_length=100)
    email_recipient_ids: list[int] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class ScheduledPrimaryModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=1000)


class ScheduledBaselineRebuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class ScheduledAttributionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribution: Literal["upstream_error", "platform_error"]
    reason: str = Field(min_length=1, max_length=2000)


class ScheduledTargetIn(BaseModel):
    enabled: bool


class ScheduledReportGroupIn(BaseModel):
    model_family: str = Field(min_length=1, max_length=120)
    online_multiplier: float = Field(gt=0, le=100)


class ScheduledReportGroupTargetsIn(BaseModel):
    target_ids: list[int] = Field(default_factory=list)


class FeishuWebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    webhook: str = Field(min_length=1)
    secret: str = ""


class EmailRecipientIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    address: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SmtpSettingsIn(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    tls_mode: Literal["ssl", "starttls"] = "ssl"
    username: str = Field(min_length=1)
    password: str = ""
    from_name: str = ""
    from_address: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
