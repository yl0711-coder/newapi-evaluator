# 分层巡检与异常复核

定时页选择「分层完整性 v1」，从使用者标记为已上线、启用且地址与凭据有效的公共 Registry 渠道首次建计划。最多五条渠道；保存计划幂等创建两个模型的本地巡检目标，不要求历史准入或历史观测。Registry online、Eval 配置覆盖和检测结果分别展示。保存计划冻结 Registry 连接和模型/协议映射，发送许可在同一事务中再次核验；用户改为已记录、停用、连接或映射变化后拒绝发送。旧分层配置可读取，执行前需要显式重新保存以冻结 Registry 条件。

## 日常计划

按计划 IANA 时区、周一至周五为工作日。默认 Asia/Shanghai，可配置时区。

| 方法 | 时间 | 五渠道尝试上限 | 条件 |
| --- | --- | ---: | --- |
| 短探活 | 09:30/12:30/15:30/18:00 | 20/天 | 默认 Astra/Responses；只证明该路径 |
| TraceOne / Astra | 10:00/15:35 | 10/天 | 依赖对应探活，low |
| TraceOne / 6.1 Sol | 15:50 | 5/天 | 保守依赖 Astra 探活；Sol 健康并未被探活验证 |
| ModelTrace / Astra | 工作日 16:10 | 3/天 | 一条健康渠道轮转，17 类 bank |
| 固定能力 canary / Astra | 工作日 18:15 | 192/天 | 同一轮转渠道，18:00 探活后，独立能力轴 |

工作日最多 230 次，非工作日 35 次；少于五条时轻量请求按实际渠道数减少。ModelTrace 正常三答、最低两份有效答，三次硬上限，零补采。TraceOne 只注册一个方法、两模型目标；普通 API `calibration_status=unvalidated`，结果不能认证真实权重。ModelTrace 未列 6.1 Sol。TraceOne、ModelTrace、HLwY 不算独立多票。

新分层传输全局在途 1，与旧稳定性/压力测试的并发规则分开。健康证据默认有效 60 分钟，过期跳过而不额外探活。白天 17:55 截止；canary 次日 08:55 截止，仍受策略总时间 36000 秒约束。调度去重绑定日期、slot、目标、方法和配置 hash；持久 least-served 轮转，五条持续健康五个工作日各选一次。默认 `EVAL_INTEGRITY_EXECUTOR=off`，新分层日常与主动复核均不出站；显式 live 才消费。这个配置开关不代替真实请求授权。

## 费用与恢复

不设每日金额上限。累计估算超过 3 美元、未知价格或缺 usage 都继续既定有限采样。请求、重试、token、时间、租约、去重和健康门控仍有效。配置价格仅为估算；input/output reported 缺失为 null，显式 0 保留。reasoning token 单列，包含在 output 中，不重复计费；unknown 不能当零。费用、已许可尝试、有效/无效、unknown、not_run 分开显示。

发送许可与 attempt 事务提交；结果与 attempt 完成原子提交。取消/进程中断中的未确认发送保留 unknown，不自动重发或释放请求/token 限额。显式恢复仅在原 deadline 内续未发送项，最终可能 partially_completed。复用同键找回 durable job，发布前崩溃不重复入队或延长期限。传输要求各协议真实结束、有效文本；HTTP 200 不是有效答案。

## 能力 baseline 与结论

使用完整 MIT 192 题、四 family 各 48；每题一次请求、512 output 上限、零重试。无 baseline 只有 current_only 当前成绩；not_run 为 incomplete，invalid 计错且保留分母。完整运行可在报告中显式「锁定可信 baseline」，再在计划配置选择该 baseline；不会自动把当前成绩升级为可信参照。baseline 独立保存，不随旧报告五天清理删除。

比较必须匹配题集、scorer/manifest、provider/连接、模型、协议、effort、wrapper、输出上限、采样和预算。漂移拒比较。单侧 exact McNemar、overall alpha .05、最低净损失 .05，四 family Holm 次级检验；无显著差异只称未检出降级。10 对全正确到全错误 p=1/1024，无不一致对 p=1。功效文件的 192 题约 .898 是给定回退/改善概率的计算，不是普通 API 实测功效；未包含 Sol 能力覆盖。

## 主动异常复核

`/integrity/` 导入授权 immutable reference、选择 HLwY/KBF、确认有限请求/token/time，再提交异步任务；可查看、取消、安全恢复与导出。没有 reference 不发请求。参考绑定 provider、模型、协议、system/wrapper/thinking、输出上限、重试、实际采样、probe/scorer hash 和预算；OpenRouter 必须固定一个 provider 且禁 fallback。首期 reference 不自动采集。HLwY 每目标最多 50 次，KBF 自有选题最多 256、每题一请求、零重试，是单独版本的 Eval harness。

同条件连续两个有效 slot、48 小时内同方向偏离生成待人工复核 incident；invalid/skipped/unknown 或条件漂移打断连续计数。事件七天冷却。只建议复核，不自动创建收费任务、通知、停用或修改路由。已有告警与新参考门控要求显式决策，因此主动复核由页面或 Monitor 明确创建。任务同键冲突拒绝；同目标/参考/条件七天冷却复用原任务。

KBF 缺同模型同条件 self-test 为 UNKNOWN，双方 coverage 不足 .5 为 UNDETERMINED。失效/未执行仍在完整题数分母。99% Clopper-Pearson 自测错误上界作为 p0，单侧 binomial p<.05 才 DIFF；SAME 只表示未检出显著差异。HLwY 只输出分布行为比较，不输出身份或纯度认证。

## 账号证据与旁路

官方账号 nerfed 保留独立的显式白名单证据异步离线分析，与新 API 任务分开。只接受最小 events/catalog 和 body-free 数字 projection，不读取路径、私人会话或 Codex 文件，不导入 nerfed 可执行模块，不伪造 app-server。单独 16 类 bank、至少两有效答；top>=.8、expected<=.2、margin>=.5sigma 才上游 MISMATCH，其他 SUSPICIOUS；未列 UNLISTED。缺元数据 `metadata_status=unavailable`。HMAC 证明调用认证，不证明输入真伪或完备；账号线索不归因到 API 渠道。

Monitor [v2 契约与示例](../../docs/integrity-monitor-api.md) 复用 `/internal/v1/*` HMAC，工作台登录不能替代，Monitor 不获得普通渠道管理权限。旧 v1 probe-jobs/报告保留；旧主动 nerfed 请求明确 `strategy_contract_changed`，不静默转义。

fpverify 仅在合格官方同条件 reference 后的后续按需审计，本轮不建库/运行。Modivue 仅文档化独立客户端旁路和显式安全摘要，不安装、不改客户端/BaseURL、不扫描日志，不引入 Meow。

版本与许可见 [NOTICE](NOTICE.md) 和 assets/provenance.json；验收见 [测试清单](../../docs/integrity-testing.md)。本轮只验证合成 Mock，source 上游证据不替代 Eval 产品验收或真实校准。

## 三项统一 API 测试

从 `/integrity/` 选择一次合格渠道，默认 `gpt-6-astra` / Responses / low，点击「开始三项测试」。可选范围不限制五条渠道；不要求历史target或通过记录，允许已记录和已上线渠道，但须启用且地址、凭据及模型/协议映射有效；保存渠道不自动测试。一次持久任务分别执行健康1次、TraceOne1次、ModelTrace最多3次、nerfed-api最多3次，总计最多8次尝试、零重试，每种方法独立发送与评分。健康失败跳过长探针；子项invalid不阻止其他子项。

POST `/api/integrity/tests` 请求 `{registry_channel_id,model,protocol,idempotency_key,confirm_live:true}` 返回202统一任务。GET `/api/integrity/tests`、GET `/api/integrity/tests/{id}` 和 `/export` 提供历史/状态/三份报告；POST同路径 `/cancel`、`/resume` 仅续原窗内未发送项。任务 `three-method-api-v1`、600秒总窗；长探针各60秒，固定输出上限和token总预留。重复幂等键保留原任务和期限，改变目标/版本409。「新一轮测试」明确重新采样。

nerfed API版本 `nerfed16-api-v1-ff0d7c0c` 使用独立16类bank、至少2有效答；ModelTrace固定17类。普通API calibration_status=unvalidated、metadata_status=unavailable；上游MISMATCH阈值是行为线索，不是认证。高级Sol时ModelTrace为unsupported；nerfed的Sol声明为UNLISTED。报告分别显示结论、有效/无效/unknown/未运行分母、条件、耗时和usage；这些状态不能当通过。消费者与主动复核共用有限执行器，默认off，不调用Codex app-server或上游可执行入口。官方账号白名单证据原HMAC2.0契约独立保留。
