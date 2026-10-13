# 输入 Token 双端监测

上游 reported 输入与缓存计数、Monitor `log_evidence` 数值均只支持严格整数 `0..9223372036854775807`（`2**63-1`）。Messages 的缓存创建、读取与普通输入总和也必须在该范围内。上游超界字段或总和标记 invalid，结论为无法判定；不截断、clamp 或估算。API 提交的超界日志数值按字段契约拒绝。

检测版本 `input-token-paired-v1`。准入专项、独立半小时计划及 Monitor 主动事件共用固定合成长输入、参数和领域判据。必须先由工作台使用者保存某个模型及协议的可信 Registry 参照，保存不发送请求。候选与参照采用同一模型和协议；连接、映射、可信绑定、阈值及样本条件冻结，发送前复验，漂移不继续发送。

重大偏差须同时满足候选至少参照 2 倍且差值至少 1024 Token。首轮立即持久发布高风险，再执行最多一轮双端复核；复核正常不覆盖首轮告警。小偏差默认至少 1.10 倍且差值至少 128 Token，可在可信绑定中配置 `threshold_config={"minor_ratio":1.10,"minor_difference":128}`；配置进入冻结条件 hash。只有连续两个相邻半小时时刻、同一冻结条件的有效定时轮次可确认小偏差；单次专项保持待确认。

每任务首轮 2 请求、最多 4 请求，无自动重试、重定向或环境代理。双端并行，各请求参与稳定性全局容量 2。计划从保存后的下一个半小时时刻开始，默认关闭，错过历史只生成当前一个 occurrence；已持久排队的旧定时 occurrence 以 `missed_scheduled_window` 结束，零补发，已有重大告警保留。常规每天每目标 48 轮、96 请求；全部重大复核上限 192 请求。`EVAL_TOKEN_USAGE_EXECUTOR=live` 显式启用工作台手动和 Monitor 主动消费者，默认 off；使用者明确启用的计划独立消费。主动与定时消费者分别领取队列，共用有界请求容量；已发送在途请求可能暂时占用容量。模块仅在工作台 all/stability 生命周期启动，单独 admission 提供配置与准入专项。

OpenAI Chat/Responses 的 input 已含 cached Token，缓存只记录、不会再加。Messages 在缓存字段成对存在且合法时使用 input + creation + read；缺失数值保留 null。usage 逐条保留首尾与变化，不累加流式累计值。同一请求 input 首尾变化保留差额，结论为无法可靠判定并明确提示上报变化。缺失、非法、零 input、参照异常及缺正常终态均无法判定。不使用字符数或本地 tokenizer 填补。`billing_status=unverified` 恒成立，首版未提供真实匹配账单导入器。附带生产日志异常时，固定复测正常只表示 `not_reproduced`，原日志证据另存。

普通工作台 API 受现有登录和跨站请求保护，前端 `/token-usage/` 提供完整操作：

| 方法 / 路径 | 输入与结果 |
| --- | --- |
| GET `/api/token-usage/meta` | 安全渠道选择、模型目录、结论文案、消费者状态 |
| GET/POST `/api/token-usage/bindings` | POST `{model,protocol,reference_channel_id,confirm_trusted:true,threshold_config?}`；GET 不返回密钥 |
| GET/POST `/api/token-usage/configs` | POST `{channel_id,model,protocol,enabled:false,confirm_live:false,config_id?}`；启用须 `confirm_live:true` |
| POST `/api/token-usage/preview` | `{channel_id,model,protocol}`；零请求预算与冻结摘要 |
| POST `/api/token-usage/manualcheck` | 上述 target 加 `confirm_live:true,idempotency_key,log_evidence?`；202 入队 |
| GET `/api/token-usage/status` | scheduled/active 独立状态 |
| GET `/api/token-usage/results`、`/results/{job_id}` | 安全历史和许可轨迹 |
| POST `/api/token-usage/results/{job_id}/cancel` | 202 取消，不证明已发上游停止 |

Monitor 的签名合同完整复用 [现有 HMAC API](monitor-internal-api.md)：签原始 method/path/query/body，时间窗和 nonce 防重放，平台登录不能替代签名。未配置 `EVAL_MONITOR_KEY_ID` / `EVAL_MONITOR_SECRET` 返回 503。principal 使用签名 key ID，主动任务仅对所属 principal 可见，工作台管理员可查看；所有 Monitor principal 可消费定时结果。

| 方法 / 路径 | 契约 |
| --- | --- |
| POST `/internal/v1/token-usage-jobs` | `Idempotency-Key` 必需，body 见下；202 仅说明已入队 |
| GET `/internal/v1/token-usage-jobs/{job_id}` | job 状态及安全 checkpoint |
| GET `/internal/v1/token-usage-jobs/{job_id}/result` | 执行状态及已发布 result，运行中亦可有首次重大告警 |
| POST `/internal/v1/token-usage-jobs/{job_id}/cancel` | 幂等键必需，空 body/`{}`，202 |
| GET `/internal/v1/token-usage-events?after=0&limit=100` | 持久 Outbox，最多 100 条，`next_cursor` 续读；结果与终态事务提交 |

```json
{"schema_version":"1.0","target":{"registry_channel_id":1,"model":"gpt-6.1-sol","protocol":"responses"},"reference_channel_id":2,"event_id":"synthetic-incident-1","confirm_live":true,"log_evidence":{"event_id":"synthetic-log-1","observed_at":"2026-10-12T00:00:00Z","first_input_tokens_reported":9000,"final_input_tokens_reported":126000,"source":"monitor_log"}}
```

`reference_channel_id` 可省略，存在时必须等于已保存可信绑定。仅接受严格字段；拒绝额外 URL、key、正文或任意对象。protocol 接受 `openai`/`chat`/`chat_completions`、`responses`、`anthropic`/`messages`，归一后冻结。event_id 与幂等键均按 principal 绑定提交时的 public payload hash；同内容重复事件直接复用原任务，不依赖后续渠道或可信绑定状态，新事件重新冻结复验。同身份不同内容 409；查外部 principal 任务 404。结果包含检测版本、冻结目标/参照、observed_at、status/severity/conclusion/label、比例与差值、双端 normalized Token、首尾 usage/trace、条件 hash、独立日志证据，绝无凭据及请求/回答正文。

任务状态 `queued/running/completed/cancelled` 与检测结论分别评价；completed 可以得出 unable_to_determine。检测结论为 `high_risk/minor_anomaly/pending_confirmation/normal/not_reproduced/unable_to_determine`。逐请求 immutable request_id、permit、confirmed/unknown checkpoint 持久保存；租约过期 sending 保守转 unknown，unknown/confirmed 永不重放，只续 definitely-not-sent 部分。`requests_sent` 包含已恢复的历史许可请求，`new_requests_sent` 仅本次执行新增；unknown 数量可从 attempts 独立核对。

任务及 result 的可选 `display_identity` 分别保存 `candidate`、`reference` 的 `name,registry_channel_id,model,protocol`，名称在创建时脱敏冻结；旧记录可能缺失，应显示“未记录渠道名称”。显示元数据不进入请求/条件/幂等身份，渠道改名不改历史或阻止相同请求复用。结果事件可包含该字段，策略仍以冻结 Registry ID、模型、协议及条件 hash 关联。

取消请求遇到过期租约时，恢复事务直接生成 cancelled 终态和结果事件，不留无法领取的 queued；已发布高风险仍保留。工作台对现存配置执行暂停时不要求连接或可信绑定仍有效。流式证据仅在各协议合法结束后有效；压缩响应在 SSE/JSON 解析前分别限制原始与解压字节，超限、截断、终态后内容或冲突字段只形成无法判定证据。

无副作用配置检查：`python -B -m features.token_usage inspect-config`，不初始化数据库、不读取密钥、不联网。开发领域/SQLite/回环HTTP验证：`python -B -m unittest tests.test_token_usage tests.test_token_usage_service tests.test_token_usage_remediation`，须设置仓外 `EVAL_TEST_ARTIFACT_ROOT`、TMPDIR 和 PLATFORM_DATA_DIR。页面验证 `node scripts/token_usage_ui.cjs` 及 `node scripts/token_usage_ui.cjs --active-off`，均纳入 [完整清单](integrity-testing.md)；off 组验证独立定时与空闲刷新。本地 Mock 通过不代表真实 Monitor、上游 Token 真实性或实际计费已核验。
