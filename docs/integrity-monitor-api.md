# Monitor 完整性任务 v2

复用 [v1 HMAC](monitor-internal-api.md) 的签名、nonce、4 MiB 读体上限、时间窗与权限。URL 仍 `/internal/v1`，完整性任务 body/response `schema_version="2.0"`；v1 probe-jobs、历史结果和原执行器保留。

| 接口 | 返回 |
| --- | --- |
| POST /internal/v1/integrity-jobs | 202，`{schema_version,job}`，仅入队 |
| GET /internal/v1/integrity-jobs | 200，`{schema_version,jobs}`，仅 Monitor 任务 |
| GET /internal/v1/integrity-jobs/{job_id} | 200，状态/分母/账本/报告 |
| GET /internal/v1/integrity-jobs/{job_id}/result | 同查询；未完成也返回实际进度 |
| POST /internal/v1/integrity-jobs/{job_id}/cancel | 202，原状态或请求取消 |
| POST /internal/v1/integrity-jobs/{job_id}/resume | 202，原窗内续未发送项 |

写请求必须 Idempotency-Key，重试重新签名/nonce。提交同键同内容复用原任务；同键不同内容409。取消幂等；恢复不能修改目标/reference/预算/期限，unknown 与已完成 attempt 保留。queued/running/completed/partially_completed/cancelled/expired/rejected/failed 均可观察，HTTP202/200不代表检测完成。

## 官方账号 nerfed 离线分析

```json
{"schema_version":"2.0","job_type":"nerfed-evidence-analysis","confirm_authorized":true,
 "evidence":{"schema":"integrity-official-account-evidence/v1","account_alias":"owned-account-1",
 "expected_model":"gpt-6-astra","authorization":{"authorized":true,"basis":"owned","source":"explicit_export"},
 "provenance":{"source":"official_account","trusted":true,"complete":true},
 "events_coverage_complete":false,"events":[],"catalog":[]}}
```

provenance 是调用方声明，结果 `evidence_authenticity=not_verified`。空证据正常完成分析但 source_verdict UNKNOWN、metadata_status unavailable；不能理解为无异常。白名单事件最多1000条：type 为 turn_context/thread_settings_applied/token_count，字段仅 type/turn_id/timestamp/model/effort/context_window，时间戳必须带时区。catalog 最多300条，仅 slug/visibility/context_window/priority/upgrade/supported_reasoning_levels。可选 observations 最多3条，是绑定 nerfed manifest、probe_id、完整 expected_count 与 validity 的数字投影；不接受响应正文、路径或任意键。传输兼容不等于校准有效。

消费者只做本地纯计算，`execution_mode=offline`、outbound_requests=0，独立于 live 开关。缺账号范围/非法字段/不完整授权拒绝；不会自行读取文件补证。16类bank不得换成定时17类ModelTrace。算法线索、元数据变化与身份认证分开；identity_authenticated 恒 false。

## API 主动 HLwY/KBF

先通过工作台参考导入或受授权服务端流程给 Monitor principal 导入 reference。普通工作台导入不自动授予 Monitor reference 权限；本轮未增加 Monitor 任意参考上传接口。

```json
{"schema_version":"2.0","job_type":"active-review",
 "target":{"channel_identity":"newapi-owned-channel","inventory_version":"inventory-v1","model":"gpt-6-astra","protocol":"responses"},
 "review":{"strategy_id":"kbf","reference_hash":"<64位已导入哈希>","source_ref":"incident:1","incident_id":"incident:1",
 "limits":{"max_requests":4,"max_input_tokens":16384,"max_output_tokens":1024},"budget_seconds":120,
 "conditions":{"provider":"<参考绑定provider>","protocol":"responses","model":"gpt-6-astra",
 "parameters":"<完整参考parameters对象>","budget":"<完整参考budget对象>"},"confirm_live":true}}
```

上例占位字段应替换成已授权参考元数据中的实际对象；实际 budgets 必须与参考绑定一致。review 不接受目标、凭据、幂等键覆盖。生产 identity 必须由工作台明确绑定，inventory 必须有效且模型/协议可用，生产 enabled；发送前再核验连接/identity/inventory。只请求选定候选，reference 不自动在线采集。`EVAL_INTEGRITY_EXECUTOR=live`、持有定时锁的 all/stability 服务才消费主动任务；默认off。费用只记录，无每日金额准入或停止；有限请求/token/time约束仍执行。

## 调用代码

以下只定义函数，不运行或读取任何私人文件。调用方传入自己的授权 body/secret；使用与v1相同原始路径字节签名。

```python
import hashlib, hmac, json, secrets
from datetime import datetime, timezone
import httpx

async def submit(base_url, key_id, secret, payload, idempotency_key):
    path = "/internal/v1/integrity-jobs"
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    nonce = secrets.token_hex(16)
    canonical = "\n".join(["POST", path, ts, nonce, hashlib.sha256(raw).hexdigest()])
    headers = {"X-Nexus-Client":"monitor", "X-Nexus-Key-Id":key_id,
        "X-Nexus-Timestamp":ts, "X-Nexus-Nonce":nonce,
        "X-Nexus-Signature":"v1="+hmac.new(secret.encode(),canonical.encode(),hashlib.sha256).hexdigest(),
        "Idempotency-Key":idempotency_key,"Content-Type":"application/json"}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        return await client.post(base_url+path, content=raw, headers=headers)
```

错误 envelope 为 `{schema_version:"2.0",error:{code,message,retryable,request_id}}`。400 invalid_field/schema_version_unsupported 是输入错误；401/403沿用签名/nonce/principal错误；404 job_not_found 隔离其他principal任务；409 idempotency_conflict/target_identity_changed 等是身份或输入冲突；422 integrity_contract_rejected 是参考/条件/证据/预算不成立，job_type_unsupported为未实现方法。旧主动nerfed在两路入口明确422 strategy_contract_changed，指向v2离线契约。503 monitor_access_disabled/misconfigured 不自动重试；eval_unavailable按retryable/Retry-After处理。无任务内长时同步探测，不回调、不通知、不改生产路由。

## 新增 API 数字指纹 nerfed-api-v1

同一路由、同一HMAC认证，显式新类型，保留官方账号离线证据契约：

```json
{"schema_version":"2.0","job_type":"nerfed-api","confirm_live":true,
 "target":{"channel_identity":"newapi-owned-channel","inventory_version":"inventory-v1",
 "model":"gpt-6-astra","protocol":"responses"}}
```

202仅创建nerfed-api-v1任务；真实消费者异步执行指定channel/model的1次健康和最多3次独立16类bank探针，重试0、总窗600秒、长探针各60秒。沿用查询/result/cancel/resume。返回reports[0]含method/version、planned/attempted/valid/invalid/unknown/not_run、conditions、duration_ms、usage和score。至少2有效答才能评分；top>=.8、expected<=.2、fused margin>=.5sigma才上游MISMATCH，其他SUSPICIOUS，未列UNLISTED。普通API始终unvalidated/unavailable，不读取Codex文件、运行上游入口或伪造会话环境。

工作台另有 `/api/integrity/tests` 的three-method-api-v1八尝试统一任务；普通登录仅工作台范围，HMAC不授予渠道管理权限。新API类型仅接受明确target和confirm_live，不接受路径、证据、reference或预算覆盖。错误语义沿用2.0 envelope；无健康证据长探针skipped，identity/inventory/连接变化拒绝续发，unknown不重发。原旧主动nerfed别名仍报strategy_contract_changed，不静默转为新类型。
