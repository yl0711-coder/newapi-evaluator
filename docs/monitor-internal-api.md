# Monitor 内部接口使用说明

实现《Monitor—Eval 内部接口契约 v1.0》的 Eval 侧。只有 Monitor 主动调用 Eval；Eval 不回调 Monitor，不修改生产路由、权重或渠道状态，只输出证据和“建议生产对比”，不输出禁用决定。

## 启用与权限

| 环境变量 | 作用 |
| --- | --- |
| `EVAL_MONITOR_KEY_ID` | Monitor 专用凭据 ID，字母数字及 `._:/@+-`，最长 160 |
| `EVAL_MONITOR_SECRET` | HMAC 密钥，至少 32 字符；只通过环境变量注入，不写入仓库、日志或数据库 |
| `EVAL_MONITOR_EXECUTOR` | `live` 才启动复测执行器并真实请求上游；默认关闭，任务保持排队直到过期 |

- 两个凭据变量都不设置时，`/internal/v1/*` 全部返回 `503 monitor_access_disabled`；只设置一个或密钥过短返回 `503 monitor_access_misconfigured`。
- 权限隔离：Monitor 凭据只能调用 `/internal/v1/*`，不能打开工作台页面或 `/api/*`；工作台登录账号（`PLATFORM_USERNAME`）不能调用 `/internal/v1/*`。
- Monitor 能做的事：同步生产清单、创建/查询/取消复测任务、拉取结果和事件。它不能读取渠道密钥或 Base URL，不能增删改公共渠道、计划或身份绑定，不能指定任意提示词或任意 URL。
- 渠道身份绑定只能由工作台用户在公共渠道页完成（每个渠道卡片的「绑定 NewAPI 渠道身份」）。未绑定的 `channel_identity` 一律拒绝。
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
| 409 | `idempotency_conflict` `inventory_version_conflict` `unknown_inventory_version` | 重新同步清单或放弃旧任务 |
| 422 | `unsupported_schema_version` `target_channel_not_verified` `budget_exceeded` `end_to_end_isolation_unavailable` `egress_denied` `model_not_in_inventory` `protocol_not_supported` | 当前能力不适用，不重试 |
| 503 | `monitor_access_disabled` `eval_unavailable` | 按 `Retry-After` 退避；显示“主动证据不可用”，不解释为渠道正常 |

所有请求体必须带 `schema_version`；主版本不是 `1` 时返回 422。

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

### `GET /internal/v1/probe-jobs/{job_id}`

返回状态（`queued` `running` `completed` `partially_completed` `cancelled` `expired` `rejected` `failed`）、`status_reason`、进度（`completed_requests`/`planned_requests`）、预算与已消耗量（请求数、预留 token、上游报告的输出 token）、`skipped[]`（场景、轮次、原因）、`result_ids` 和时间字段。不返回渠道密钥、Base URL 或提示词。

执行规则：每发一个请求之前都会重新检查取消、过期和剩余预算，停止后不再发新请求；已经发出的请求会等它结束。每个执行进程有独立的租约身份，只有持有租约的进程能写结果和终态。租约过期（进程崩溃或停止超过 120 秒）的任务在下次领取或启动时标记为 `failed`，已有结果则为 `partially_completed`，原因为 `executor_lease_lost`；原执行器发现租约丢失后立即停止，不再发请求，丢弃在途结果。执行器自身异常时任务立即结束，原因为 `executor_error`：`budget_consumed` 保留已发出的全部请求，未执行步骤写入 `skipped`，同一轮后面的任务照常执行。租约被回收时，`progress.completed_requests` 按已保存的结果数回填；`budget_consumed` 不含丢失租约时正在进行的请求。

### `POST /internal/v1/probe-jobs/{job_id}/cancel`

需要 `Idempotency-Key`，返回 202 和任务详情。排队中的任务立即变为 `cancelled`；执行中的任务在下一个请求前停止；已经结束的任务保持原状态。重复取消结果相同。每次取消请求（包括对已结束任务的）都记录审计：时间、`Idempotency-Key`、取消前后状态。同一个 `Idempotency-Key` 重试也各记一条，以便看到重试；不存在的任务返回 404，不写审计。审计目前只存数据库（`monitor_job_audit`），没有查询接口。

### `GET /internal/v1/probe-results?cursor=&limit=`

- 首次调用时 `cursor` 为空或 `0`；之后原样回传 `next_cursor`（格式 `c1.<n>`，是一个不透明值，不要自己构造）。`limit` 为 1–200。
- 结果按发布顺序只追加，发布后不会修改。重复拉取同一游标得到完全相同的条目，Monitor 应以 `result_id` 去重。
- 字段遵循契约第 8 节，另外增加：`round`、`channel_result`、`model_mismatch`、`exclude_from_business_metrics: true`、`supersedes_result_id`（当前恒为 `null`）。
- `error_category` 取自分类真值表的 `fault_class`。Eval 自身导致的问题使用 `eval_egress_denied`、`eval_internal_error`，不归责渠道。
- `http_status` 只在上游返回错误状态时记录，成功请求为 `null`。`usage_status` 为 `complete`、`missing` 或 `unknown`（请求没有收到上游响应）。

### `GET /internal/v1/probe-events?cursor=&limit=`

游标规则与结果接口相同。同一场景的所有轮次都以同一种上游类错误失败时，产生一个 `probe_failure_reproduced` 事件（`confidence`：只有 1 轮为 `low`，≥2 轮为 `medium`），`recommended_monitor_action` 固定为 `compare_with_production_traffic`。混合错误、内容不符和 Eval 自身错误都不产生事件。

## 数据保留

任务、结果和事件存放在公共渠道库（`channels.db`）的 `monitor_*` 表中，目前没有自动清理，满足契约“至少 30 天”的要求。以后加入清理时，必须保证未被消费的结果不会被删除。

## 尚未实现

- 真实 Monitor 联调，以及真实上游执行的授权记录。
- `end_to_end` 隔离身份和统计排除。
- 定时巡检主动产生的 `time_window_degradation` 等事件（当前只有复测任务的复现事件）。
- 固定 JSON 金样和跨仓库契约测试。
- 密钥轮换：目前只支持单个 key id。
