# 输入 Token 对照与准入队列

准入、半小时监控和 Monitor 主动复测共用输入用量检测。页面入口为 `/token-usage/`，在完整工作台、定时工具和独立准入中可见；后台定时及主动消费者只在完整工作台或定时工具启动。Eval 只提供观测，不改渠道路由、权重或上游配置。

## 检测与结论

先为模型和协议显式登记可信参照渠道。候选与参照发送相同的固定合成长输入，使用相同协议、模型及生成参数，各一次；重大异常最多追加一组双端复核，总计最多四次，无自动重试。支持 `openai`、`responses`、`anthropic`。Astra Responses 使用 low 和至少 4096 的含推理输出预算。

默认重大阈值为候选输入用量至少参照的 2 倍，且差额至少 1024 Token。首次重大结果立即持久化并发布高风险事件，之后的复核不抹掉首次异常。默认小偏差为至少 1.10 倍且差额至少 128 Token，连续两轮相邻半小时、相同冻结条件的有效异常才确认。阈值随可信参照配置保存，变更后重新冻结，旧证据不能累计到新条件。

缺失用量为 `null`，零保持零但不能作为比例分母；终态失败、输入用量自相矛盾或参照失效时无法判定。Anthropic 缓存创建和读取用量加入总输入；OpenAI 缓存已包含在输入中，不重复相加。流式 usage 保留首尾与有界轨迹，累计更新不相加。固定样本未发现异常只表示本次未复现，Monitor 原日志证据保留。没有匹配实际账单时，所有结果的 `billing_status` 为 `unverified`，不能认定已经多扣款。

启用计划默认每 1800 秒一轮，每目标基础新增 96 请求/天，重大复核另计，MT/Nerfed 原请求另计。保存禁用配置不发送请求；启用需明确确认，下一半小时才采样，不追赶历史窗口。`EVAL_TOKEN_USAGE_EXECUTOR=live` 控制手动与 Monitor 主动任务，已启用定时计划独立运行。紧急任务使用独立消费者，但仍遵守共享出站容量。

普通报告只展示结论与计费核实状态；技术报告保留数字证据。准入总览在六题均正常时仍突出 Token 高风险；缺参照或专项证据不足时保守提示。旧报告无专项字段仍可读取，各类下载保留。

## 服务器准入队列

所有准入提交先进入服务器 FIFO，按创建顺序执行，同时一个准入任务。六题（含糖果测试）及原预热规则保留，专项独立执行。切页、刷新或关浏览器只停止观看，后台继续；回来读取任务快照与已完成报告。旧 `/admission/api/compare` 为入队和事件订阅，断开订阅不会取消任务。

候选 Key 只在服务内存，提交后页面清空。重启保留任务、部分结果和请求账本，未完成任务等待补原 Key。队首等 Key 时阻塞后续，可补 Key 或取消；已经确认的请求复用结果，已发送但未确认的请求标记 `unknown`，不自动重发。报告历史滚动保留最近 30 条，活跃任务不受报告裁剪影响。

| 路径 | 行为 |
| --- | --- |
| `POST /admission/api/jobs` | 原 CompareInput，`Idempotency-Key` 绑定同一提交 |
| `GET /admission/api/jobs` | 活跃及近期任务，状态和位置，不含 Key |
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

本次清单在原完整性 19/20 组上增加 Token 只读检查、Token 浏览器和准入队列浏览器，共开发 22 组、独立 23 组；领域用例经完整 `workbench-python` 发现。覆盖三协议、缓存与 usage 变化、缺失/零、重大立即发布、最多四次、连续两轮、HMAC/幂等/游标、严格 FIFO、取消/切页/关页/重启补 Key 与 unknown 不重发，以及报告和容器入口。Mock 通过不等于真实 Monitor 联调、真实计费核验或生产部署。
