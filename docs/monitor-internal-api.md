# Monitor 内部接口使用说明

实现《Monitor—Eval 内部接口契约 v1.0》的 Eval 侧。只有 Monitor 主动调用 Eval；Eval 不回调 Monitor，不修改生产路由、权重或渠道状态，只输出证据和“建议生产对比”，不输出禁用决定。

新增分层异常复核使用独立 [完整性任务 v2](integrity-monitor-api.md)：同一 HMAC 边界，HLwY/KBF 异步主动复核，is-gpt-nerfed 改为官方账号白名单证据离线分析。旧主动nerfed请求明确返回422 strategy_contract_changed；本页其余v1 probe-jobs合同保留。新执行开关为 EVAL_INTEGRITY_EXECUTOR（默认off），新费用仅记录、没有每日金额上限，与旧v1执行器分开。

## 启用与权限

| 环境变量 | 作用 |
| --- | --- |
| `EVAL_MONITOR_KEY_ID` | Monitor 专用凭据 ID，字母数字及 `._:/@+-`，最长 160 |
| `EVAL_MONITOR_SECRET` | HMAC 密钥，至少 32 字符；只通过环境变量注入，不写入仓库、日志或数据库 |
| `EVAL_MONITOR_EXECUTOR` | `live` 才启动复测执行器并真实请求上游；默认关闭，任务保持排队直到过期 |

- 两个凭据变量都不设置时，`/internal/v1/*` 全部返回 `503 monitor_access_disabled`；只设置一个或密钥过短返回 `503 monitor_access_misconfigured`。
- 权限隔离：Monitor 凭据只能调用 `/internal/v1/*`，不能打开工作台页面或 `/api/*`；工作台登录账号（`PLATFORM_USERNAME`）不能调用 `/internal/v1/*`。
- Monitor 能做的事：同步生产清单、创建/查询/取消复测任务、拉取结果和事件。它不能读取渠道密钥或 Base URL，不能增删改公共渠道、计划或身份绑定，不能指定任意提示词或任意 URL。
- 旧 v1 渠道身份由工作台管理 API `/api/model-coverage/monitor/identities/{registry_channel_id}` 维护，公共渠道页已移除绑定控件。旧 v1 未绑定的 `channel_identity` 一律拒绝；新的完整性 v2 使用 Registry ID，详见独立 v2 契约。
- 执行器只在完整工作台或定时测试模式运行，与定时测试共享单调度器锁和全局请求并发上限（2）。

## 签名

请求体上限 4 MiB：`Content-Length` 超限或流式读取超限都直接返回 413，不整包读入内存。`/internal/v1/*` 路径末尾带 `/` 返回 404，不做重定向。

每个请求带以下请求头：

```http
X-Nexus-Client: monitor
X-Nexus-Key-Id: <EVAL_MONITOR_KEY_ID>
X-Nexus-Timestamp: 2026-09-29T03:10:00Z
X-Nexus-Nonce: <16-128 位字母数字、- 或 _，每个请求唯一>
X-Nexus-Signature: v1=<hex>
Idempotency-Key: <所有写请求必填>
Content-Type: application/json
```

签名串按换行连接：`METHOD`、路径加查询串（按线上实际发送的字节，不做 percent 解码，查询参数顺序也不重排；例如发送 `probe%2D1` 就对 `probe%2D1` 签名）、时间戳、nonce、请求体字节的 SHA-256 十六进制（无请求体时对空字节计算）。`X-Nexus-Signature = "v1=" + HMAC-SHA256(secret, 签名串).hex()`。

```python
import hashlib, hmac
def sign(secret: bytes, method: str, path_qs: str, ts: str, nonce: str, body: bytes) -> str:
    text = "\n".join([method.upper(), path_qs, ts, nonce, hashlib.sha256(body).hexdigest()])
    return "v1=" + hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()
```

时间戳允许 ±300 秒；nonce 在签名校验通过后记录，重复使用返回 `401 replayed_request`。重试同一业务请求时必须重新生成时间戳、nonce 和签名，并保持 `Idempotency-Key` 不变。

## 错误与重试

错误体统一为 `{"schema_version":"1.0","error":{"code","message","retryable","request_id"}}`，同时返回 `X-Request-Id`。

| 状态码 | 典型 code | Monitor 处理 |
| --- | --- | --- |
| 400 | `invalid_field` `invalid_budget` `invalid_time_window` `invalid_cursor` `user_content_forbidden` `idempotency_key_required` | 修正请求，不重试 |
| 401/403 | `invalid_signature` `signature_expired` `replayed_request` `invalid_credentials` `client_not_allowed` | 立即凭据告警，不自动重试 |
| 404 | `job_not_found` `channel_not_found` | 目标不存在或已删除/停用 |
| 409 | `idempotency_conflict` `inventory_version_conflict` `unknown_inventory_version` `target_identity_changed` `connection_changed` | 核对幂等输入、清单与渠道连接；放弃已失效的旧任务 |
| 422 | `unsupported_schema_version` `target_channel_not_verified` `budget_exceeded` `end_to_end_isolation_unavailable` `egress_denied` `model_not_in_inventory` `protocol_not_supported` | 当前能力不适用，不重试 |
| 503 | `monitor_access_disabled` `monitor_access_misconfigured` | `retryable=false`；先修正部署凭据配置 |
| 503 | `egress_check_timeout` `eval_unavailable` | `retryable=true`；有 `Retry-After` 时按它退避，否则由调用方控制退避；显示“主动证据不可用” |

生产清单和任务创建请求体必须带 `schema_version`；主版本不是 `1` 时返回 422。取消接口不读取业务请求体，但仍校验原始体字节的签名及写请求幂等头。枚举、场景、清单列表和预算在集合操作前校验类型；例如 `protocol=[]`、场景中含对象、`models=null`、`groups=true` 均返回 400 `invalid_field`、`retryable=false`，不会提交相关清单或任务。类型正确但不支持的协议返回 422 `protocol_not_supported`。

## 接口

### `PUT /internal/v1/production-inventory/{inventory_version}`

请求体按契约第 4 节：`schema_version`、`generated_at`（RFC3339 UTC）、`newapi_version`、`channels[]`（`channel_identity`、`models`、`config_fingerprint` 等，不得含密钥、URL）。同版本同内容重复提交返回相同结果；同版本不同内容返回 409 `inventory_version_conflict`；`generated_at` 早于或等于当前清单的新版本返回 409 `invalid_inventory_version`（同一秒的两份清单无法排序，一律拒绝后到者），当前版本始终按 `generated_at` 取最新。

响应里的 `coverage_gaps[].reason`：

- `target_channel_not_verified`：该 `channel_identity` 尚未在 Eval 绑定。
- `model_not_in_eval_catalog`：已绑定，但该模型不在 Eval 常用模型或映射中。
- `no_fresh_probe`：Eval 有该模型，但未测试、结果超过 48 小时或连接已变更。

Eval 不会因为清单自动创建任务。

### `POST /internal/v1/probe-jobs`

请求体按契约第 5 节。`Idempotency-Key` 请求头必须等于请求体 `idempotency_key`。同键同内容返回 200 和原任务；同键不同内容返回 409；新建返回 201：

```json
{"schema_version":"1.0","job_id":"probe-…","status":"queued","created_at":"…Z","estimated_start_at":"…Z"}
```

当前支持：

- `job_type`：`admission` `patrol` `incident` `recovery` `release_validation`；`priority`：`p0`–`p3`，数字越小越先执行。
- `protocol`：`openai` `anthropic` `responses`；`probe_path` 只支持 `direct`。`end_to_end` 返回 422，因为 Eval 还没有隔离网关身份。
- `scenarios`：固定合成场景 `short_stream` `long_stream` `non_stream` `instruction`，不接受任何用户内容字段（`messages`、`prompt`、`input` 等）。
- `rounds` 1–5；`expires_at` 必填，且距开始时间不超过 3600 秒。
- `budget`：`max_requests`、`max_input_tokens`、`max_output_tokens` 必填，`max_cost_usd` 可选。按“场景数 × 轮数”计算请求数；token 按每个场景的固定上限预留（Responses 最少 4096 输出 token）。任一项超预算时拒绝，不截断执行。Eval 没有价格表，不估算也不报告费用。
- 带 `expected_inventory_version` 时：版本未同步返回 409；目标不在该版本或模型不在该渠道返回 404/422；更新的清单里该目标的模型或 `config_fingerprint` 已变化返回 409。
- 绑定的 Eval 渠道已删除或停用返回 404；目标地址违反出网策略返回 422。

生产清单的 `enabled_status=disabled` 本身不阻止 `direct` 复测，允许 Monitor 为已停用的生产渠道创建 `recovery` 任务；绑定的 Eval 测试渠道仍必须启用。关联清单的 `enabled_status` 在排队后变化会以 `inventory_version_conflict` 拒绝发送，不把生产停用状态等同于 Eval 测试连接停用，也不因复测成功自动恢复生产渠道。

新任务记录创建时的渠道连接指纹。DNS/出站预检在写事务之外执行；入队事务再次核对身份绑定、连接指纹和关联生产清单。预检期间连接变化会拒绝入队。同键同内容的已存在任务直接重放，不再做 DNS 预检。

任务创建的预检使用最多 2 个专用线程：等待线程名额和等待解析结果分别最多 5 秒；任一等待超时返回 503 `egress_check_timeout`，不会迟到入队。运行中的系统 DNS 解析无法被强制终止，超时后仍占用名额，直到线程返回；这限制线程数和 API 等待，不保证系统解析本身在 5 秒结束。此限制只描述任务创建预检，执行时仍会进行协议 URL 预检和实际建连时的 IP 校验。

### `GET /internal/v1/probe-jobs/{job_id}`

返回状态（`queued` `running` `completed` `partially_completed` `cancelled` `expired` `rejected` `failed`）、`status_reason`、进度（`completed_requests`/`planned_requests`）、预算与账本 `budget_consumed`、`skipped[]`（场景、轮次、原因）、`result_ids` 和时间字段。不返回渠道密钥、Base URL 或提示词。尚无账本时 `budget_consumed` 为 `null`。

### 发送许可、账本与恢复

执行器等待全局请求名额时持续核验、续租；三协议的异步 `before_send` 回调在 URL/DNS 预检之后、调用 HTTP 发送之前执行一次。发送许可写事务同时检查有效租约、取消、期限、身份绑定、连接指纹、关联生产清单和剩余请求/token 预算，通过后才提交唯一的 `(job_id, scenario, round)` attempt 及预留。未取得许可不会调用 HTTP 发送；许可控制信号不会被归成上游错误。

每个 attempt 保存脱敏 `target_snapshot`：`channel_identity`、`registry_channel_id`、`connection_fingerprint`、`inventory_version`、`model` 和 `protocol`。执行器使用原连接；地址、密钥、停用状态或身份绑定变化会停止后续许可。关联清单的模型、`config_fingerprint` 或启用状态变化也会拒绝后续许可。在途请求允许结束，结果仍绑定发送许可时的快照，不随随后重新绑定的身份漂移。

结果发布、attempt 转为 `completed`、已报告用量和任务账本在同一事务提交；同一 attempt 重复完成返回原 `result_id`。`progress.completed_requests` 是已提交结果数，不是成功请求数。heartbeat 只续尚未过期且仍属于本执行器的租约，不覆盖账本，也不复活失效租约。

| `budget_consumed` 字段 | 语义 |
| --- | --- |
| `requests` | 已提交发送许可的 attempt 数；兼容旧记录时不小于已有结果数。许可不证明 HTTP 已送达上游 |
| `input_tokens_reserved` / `output_tokens_reserved` | 已许可 attempt 的固定预算预留；包含未确认 attempt，不是实际 token 用量 |
| `input_tokens_reported` / `output_tokens_reported` | 各自对有合法上游报告值的 attempt 求和；没有报告值时为 `null`，显式报告 0 时为 0 |
| `input_tokens_reported_requests` / `output_tokens_reported_requests` | 各自具有合法报告值的 attempt 数，包含显式 0 |
| `input_tokens_missing_requests` / `output_tokens_missing_requests` | `requests` 减去对应报告值计数，不能解释为零用量 |
| `unknown_requests` | 已许可但未提交结果、在终止或租约回收时标为 `unknown` 的 attempt 数 |
| `reservation_source` / `reported_usage_source` | 无原始连接指纹且无 attempt 的旧账本路径分别为 `legacy_persisted` / `unavailable_legacy`；新 attempt 账本不附这两个旧数据来源标记 |

有效租约默认 120 秒。租约丢失后，原执行器停发并丢弃未提交的在途结果；下次领取或启动会回收过期租约，将任务标为 `failed`，已有结果则为 `partially_completed`，原因为 `executor_lease_lost`。尚未完成的许可 attempt 转为 `unknown`，保留请求与 token 预留，不释放后补发。进程取消/停止使用 `executor_stopped`，意外执行异常使用 `executor_error`；结束状态同样按已保存结果区分 `failed` / `partially_completed`，保留未执行步骤和账本，其他排队任务可继续执行。此恢复策略不承诺上游恰好执行一次，也不自动重试未知结果。

### `POST /internal/v1/probe-jobs/{job_id}/cancel`

需要 `Idempotency-Key`，返回 202 和任务详情。排队中的任务立即变为 `cancelled`；执行中的任务在下一个请求前停止；已经结束的任务保持原状态。重复取消结果相同。每次取消请求（包括对已结束任务的）都记录审计：时间、`Idempotency-Key`、取消前后状态。同一个 `Idempotency-Key` 重试也各记一条，以便看到重试；不存在的任务返回 404，不写审计。审计目前只存数据库（`monitor_job_audit`），没有查询接口。

### `GET /internal/v1/probe-results?cursor=&limit=`

- 首次调用时 `cursor` 为空或 `0`；之后原样回传 `next_cursor`（格式 `c1.<n>`，是一个不透明值，不要自己构造）。`limit` 为 1–200。
- 结果按发布顺序只追加，发布后不会修改。重复拉取同一游标得到完全相同的条目，Monitor 应以 `result_id` 去重。
- 字段遵循契约第 8 节，另外增加：`round`、`channel_result`、`model_mismatch`、`exclude_from_business_metrics: true`、`supersedes_result_id`（当前恒为 `null`）。
- 新执行结果还含 `attempt_id` 和上述 `target_snapshot`，复现事件引用同一快照；旧结果可能没有这些字段。
- `error_category` 取自分类真值表的 `fault_class`。Eval 自身导致的问题使用 `eval_egress_denied`、`eval_internal_error`，不归责渠道。
- 实际建连的策略拒绝使用类型化 `SocketEgressDenied`（继承 `httpcore.ConnectError`），沿异常 cause/context 链识别 HTTP 客户端包装，归为 `eval_egress_denied`；不匹配错误文本。传输层普通 DNS 失败、连接失败和超时保留网络/超时分类。
- 执行时，发送许可前的 DNS/连接预检失败以任务 `failed / transport_connect` 收尾，预检超时为 `failed / transport_timeout`，策略拒绝为 `rejected / eval_egress_denied`；当前及余下未执行步骤在 `skipped[].reason` 保留同一分类。未取得许可的步骤不增加 attempt、请求/token 预留或 HTTP 发送，不构造请求结果或复现事件；已有结果与预算仍保留。
- `http_status` 只在上游返回错误状态时记录，成功请求为 `null`。合法响应路径的 `usage_status` 为 `complete`（输入/输出均报告）、`partial`（仅一项报告）或 `missing`（均缺失）；其他错误路径当前为 `unknown`，不能用它断言 HTTP 未响应。
- `input_tokens_reported`、`output_tokens_reported` 只接受非负整数，bool、负数和非整数无效，缺失保留 `null`，显式 0 保留。`output_tokens_estimated` 单列字符估算，只在现有 Chat/Messages 完成流且缺输出用量时提供；Responses 不用字符估算补齐报告用量。估算不进入 reported 账本。
- 模型元数据在传输和发布时清洗；凭据、完整 URL、控制字符或不合规则的模型值不会原样发布，使用 `unrecognized` 或脱敏标记。耗时字段只保留有限非负数值。异常日志使用固定分类，不记录异常全文、签名头、渠道连接或请求/响应正文。

### `GET /internal/v1/probe-events?cursor=&limit=`

游标规则与结果接口相同。同一场景的所有轮次都以同一种上游类错误失败时，产生一个 `probe_failure_reproduced` 事件（`confidence`：只有 1 轮为 `low`，≥2 轮为 `medium`），`recommended_monitor_action` 固定为 `compare_with_production_traffic`。混合错误、内容不符和 Eval 自身错误都不产生事件。

## 数据保留

任务、结果和事件存放在公共渠道库（`channels.db`）的 `monitor_*` 表中，目前没有自动清理，满足契约“至少 30 天”的要求。以后加入清理时，必须保证未被消费的结果不会被删除。

本次结构变更新增 `monitor_probe_jobs.connection_fingerprint`、`monitor_probe_attempts` 及对应索引，初始化事务保留已有任务、结果和事件。旧排队任务没有原始连接指纹时，在发送许可前以 `rejected / connection_snapshot_missing` 结束，不用当前连接代替旧快照。

回收没有原始连接指纹、也没有 attempt 的旧运行任务时，保留旧 `consumed_json` 中可证明的非负整数请求数和 input/output 预留；请求数不低于已有结果数。缺失或无效预留保留 `null`，显式预留 0 保留 0。旧报告用量因没有 attempt 来源均为 `null`，并标记 `reservation_source=legacy_persisted`、`reported_usage_source=unavailable_legacy`；不能把旧估算或未经证明的用量当成上游报告值。`unknown_requests` 只统计新 attempt 的未知状态，不追溯猜测旧请求是否在途。旧结果保持可读，重复回收不再改写终态或账本。

## 尚未实现

- 真实 Monitor 联调，以及真实上游执行的授权记录。
- `end_to_end` 隔离身份和统计排除。
- 定时巡检主动产生的 `time_window_degradation` 等事件（当前只有复测任务的复现事件）。
- 固定 JSON 金样和跨仓库契约测试。
- 密钥轮换：目前只支持单个 key id。
