# 输入 Token 对照与准入队列

准入、半小时监控和 Monitor 主动复测共用输入用量检测。页面入口为 `/token-usage/`，在完整工作台、定时工具和独立准入中可见；后台定时及主动消费者只在完整工作台或定时工具启动。Eval 只提供观测，不改渠道路由、权重或上游配置。

## 检测与结论

先为模型和协议显式登记可信参照渠道。候选与参照发送相同的固定合成长输入，使用相同协议、模型及生成参数，各一次；重大异常最多追加一组双端复核，总计最多四次，无自动重试。支持 `openai`、`responses`、`anthropic`。Astra Responses 使用 low 和至少 4096 的含推理输出预算。

默认重大阈值为候选输入用量至少参照的 2 倍，且差额至少 1024 Token。首次重大结果立即持久化并发布高风险事件，之后的复核不抹掉首次异常。默认小偏差为至少 1.10 倍且差额至少 128 Token，连续两轮相邻半小时、相同冻结条件的有效异常才确认。阈值随可信参照配置保存，变更后重新冻结，旧证据不能累计到新条件。

缺失用量为 `null`，零保持零但不能作为比例分母；终态失败、输入用量自相矛盾或参照失效时无法判定。Anthropic 缓存创建和读取用量加入总输入；OpenAI 缓存已包含在输入中，不重复相加。流式 usage 保留首尾与有界轨迹，累计更新不相加。固定样本未发现异常只表示本次未复现，Monitor 原日志证据保留。没有匹配实际账单时，所有结果的 `billing_status` 为 `unverified`，不能认定已经多扣款。

启用计划默认每 1800 秒一轮，每目标基础新增 96 请求/天，重大复核另计，MT/Nerfed 原请求另计。保存禁用配置不发送请求；启用需明确确认，下一半小时才采样，不追赶历史窗口。`EVAL_TOKEN_USAGE_EXECUTOR=live` 控制手动与 Monitor 主动任务，已启用定时计划独立运行。紧急任务使用独立消费者，但仍遵守共享出站容量。

普通报告只展示结论与计费核实状态；技术报告保留数字证据。准入总览在六题均正常时仍突出 Token 高风险；缺参照或专项证据不足时保守提示。旧报告无专项字段仍可读取，各类下载保留。

Token 请求在 SSE 解析前限制原始传输和解压后的字节，各不超过 1 MiB；gzip/deflate 逐块有界解压。Messages 的真实停止原因由 `message_delta` 保留，`message_stop` 只表示流结束；截断、缺原因或非法顺序无法判定。Responses 完成后再来正文、重复或冲突终态也不作为有效证据。

Token 计划、结果和完整性任务冻结创建时渠道名称；名称是显示元数据，不参与连接、采样条件或幂等身份。历史缺少名称时显示“未记录渠道名称”，不使用当前名称回填。Token 页面在空闲时每 15 秒刷新，显示最近成功更新时间及刷新失败；主动执行器 off 时禁用立即测试并解释，定时监测独立运行。失效连接仍可暂停现存计划。租约恢复会原子收尾已请求取消的任务与结果事件，保留 unknown 和首次重大告警。

## 服务器准入队列

所有准入提交先进入服务器 FIFO，按创建顺序执行，同时一个准入任务。六题（含糖果测试）及原预热规则保留，专项独立执行。切页、刷新或关浏览器只停止观看，后台继续；回来读取任务快照与已完成报告。旧 `/admission/api/compare` 为入队和事件订阅，断开订阅不会取消任务。

任务队列默认折叠，标题显示任务总数、运行数和等待数。点击或用键盘展开后，队列区域最多显示五个任务，其余向下滚动查看；折叠不取消或暂停任务，刷新进度时保持当前展开状态。

候选 Key 只在服务内存，提交后页面清空。重启保留任务、部分结果和请求账本，未完成任务等待补原 Key。队首等 Key 时阻塞后续，可补 Key 或取消；已经确认的请求复用结果，已发送但未确认的请求标记 `unknown`，不自动重发。报告历史滚动保留最近 30 条，活跃任务不受报告裁剪影响。

提交时可填写候选渠道名称，参照名称从公共渠道冻结。队列、历史、详情及三类导出保留双方名称、模型、协议和任务编号；运行项不标“排队第 1”。页面说明队首阻塞、等待原 Key、执行错误和停止对象，区分六题及 Token 专项阶段、部分结果与最终结论，并展示 confirmed/unknown/not_needed 覆盖。创建前只读检查 Token 可信绑定，缺少配置时提供 `/token-usage/` 操作入口，六题仍可提交。

恢复与每次发送前核验完整请求构造指纹，涵盖题目内容、变体、系统提示、生成参数、协议 headers 和 Token 构造。指纹漂移或旧任务未记录指纹时拒绝继续，请重新创建；旧报告仍可读取。取消先形成 unknown 记录，再统一计算报告摘要，历史列表和详情使用同一已持久结果。

删除或裁剪终态报告会在同一事务清除 job 报告、事件正文、step_results 和请求构造快照，保留无正文任务身份与去重账本；活跃任务不清理。升级初始化同时清除旧删除路径遗留副本。事件历史有裁剪缺口或旧库无法证明完整边界时，订阅返回 `error_category=event_history_gap` 与 `snapshot_url`，不以 `run_finished` 冒充完整成功；调用方应改读任务快照。

| 路径 | 行为 |
| --- | --- |
| `POST /admission/api/jobs` | 原 CompareInput，`Idempotency-Key` 绑定同一提交 |
| `GET /admission/api/jobs` | 活跃及近期任务，状态和位置，不含 Key |
| `GET /admission/api/token-precheck` | `model,candidate_protocol,reference_model,reference_protocol,reference_channel_id` 查询参数；零请求，返回 `available,reason,label,configuration_url` |
| `GET /admission/api/jobs/{id}` | 脱敏进度、部分或完整报告 |
| `POST /admission/api/jobs/{id}/credentials` | 提交 `api_key`，校验原连接身份后继续 |
| `POST /admission/api/jobs/{id}/cancel` | 取消并推进后续队列 |

## Monitor 与配置接口

工作台登录可配置可信参照、启用监控、预览、主动复测及查看结果。普通接口为 `/api/token-usage/{meta,bindings,configs,preview,manualcheck,status,results}`。Monitor 使用既有 HMAC 原始 path/query/body 签名，不接受工作台 Basic 认证；凭据由 `EVAL_MONITOR_KEY_ID` 和 `EVAL_MONITOR_SECRET` 配置，见 [签名合同](monitor-internal-api.md)。

| 签名接口 | 行为 |
| --- | --- |
| `POST /internal/v1/token-usage-jobs` | 按 Registry ID、模型与协议创建异步主动复测，关联异常 `event_id`；不接受 URL 或 Key 覆盖 |
| `GET /internal/v1/token-usage-jobs/{id}` | 进度及发送账本 |
| `GET /internal/v1/token-usage-jobs/{id}/result` | 当前或终态结果 |
| `POST /internal/v1/token-usage-jobs/{id}/cancel` | 取消自身任务 |
| `GET /internal/v1/token-usage-events` | 按持久游标读取自身主动结果及定时结果事件 |

同一事件重复提交复用原任务，内容冲突拒绝；恢复验证需新事件。固定可信绑定及两端连接/模型映射在发送前复验，失效时无法判定。Monitor 可据事件制定策略，策略执行由 Monitor 负责。

## 验证

只读合同检查：`python -B -m features.token_usage inspect-config`。完整注册清单：`python -B scripts/inspect_test_plan.py integrity`。开发入口：`python -B scripts/verify_integrity.py --output <新的仓外证据目录>`；独立验收在干净 detached 候选追加 `--sha <40位SHA>`。

注册清单共开发 29 组、独立 30 组，含 Token 主动 live/off 两组、每日检测与火山单次验收浏览器检查；off 组验证后台定时与空闲刷新。领域用例经完整 `workbench-python` 发现，并核对必需模块 `test_token_usage_remediation`、原 Token、准入队列/报告与完整性回归均非零收集。覆盖三协议、解压与 SSE 限额、严格终态、缓存和 usage 变化、取消租约、失效连接暂停、重大立即发布、最多四次、连续两轮、HMAC/幂等/游标、FIFO、重启合同漂移、删除副本、订阅缺口、冻结名称、操作对象、队列折叠与五项滚动，以及报告和容器入口。Mock 通过不等于真实 Monitor 联调、真实计费核验或生产部署。
