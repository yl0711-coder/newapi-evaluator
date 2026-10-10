# 分层巡检与异常复核

定时页选择「分层完整性 v1」，从使用者标记为已上线、启用且地址与凭据有效的公共 Registry 渠道首次建计划。最多五条渠道；保存计划幂等创建两个模型的本地巡检目标，不要求历史准入或历史观测。Registry online、Eval 配置覆盖和检测结果分别展示。保存计划冻结 Registry 连接和模型/协议映射，发送许可在同一事务中再次核验；用户改为已记录、停用、连接或映射变化后拒绝发送。旧分层配置可读取，执行前需要显式重新保存以冻结 Registry 条件。

## 十分钟调度表 v2

新建计划默认 `layered-integrity-v2`，最多五条已上线、启用且凭据与精确模型映射有效的 Registry 渠道，固定 `gpt-6-astra` / Responses / low。全部选中渠道每天分别执行，不按工作日轮转。旧 v1 与 ins-v2 不自动迁移，原计划及历史导出仍可用。

保存并启用计划后，工作台的单调度器自动按时采样，无需设置执行器变量；暂停/删除计划或取消选中时刻可停止后续请求。已有部署的 `EVAL_INTEGRITY_EXECUTOR=off` 仅关闭手动/Monitor 主动消费者，不阻止定时 MT/Canary。root 与定时页健康接口的 `scheduled_integrity_executor` 分别显示定时配置启用和实际运行状态。

24 小时 × 6 格调度表让 Canary 与 ModelTrace 独立勾选任意十分钟时刻，可全选、清空、恢复默认、仅整点或加入时间范围。默认 Canary 为 24 个整点，每个时刻独立完整 192 题；MT 为 144 个十分钟点，每次独立 3 题。例如 Canary `09:20,11:00,16:40` 与 MT `09:10,09:50,14:30` 均合法。两个集合都可为空，保存和重开保留零时段、零请求，不能隐式恢复默认。

每时刻每渠道一次共享 health 依赖。固定计划的日请求数为 `N × (192 × |Canary| + 3 × |MT| + |union|)`：默认每渠道 4608 + 432 + 144 = **5184**，五渠道 **25920**。输入/输出 token 预估由实际去重图求和，仅供预览和记账，不能作为发送上限。保留已许可和 unknown 账本，不设置额外日请求或 token 预算，也不设每日金额上限。

MT 截止为计划时刻后 600 秒，Canary 为 3600 秒，health 使用最早依赖截止。取得真实共享发送容量后在题边界重新选择：MT/相关 health 优先、同级较早截止先；Canary 渠道轮流。已发送请求不抢占，HTTP 全局在途 1。慢请求导致未完成/过期，开始批次不表示 192 题完成，也不为完成分母延期。跨日结果归原计划日；DST 不存在时刻跳过，重复时刻仅第一次。

稳定 occurrence 由计划 ID、UTC 计划时刻、Registry ID、方法组成，不含配置 revision/hash。重复保存、取消再选、重启均复用原实例/账本，已发送、确认和 unknown 不重采。新保存的过去时刻不追赶，未来零尝试的已取消时刻可恢复原实例。保存/暂停/删除/改时区在许可边界再次校验，单独取消 Canary 保留有效 MT 的共享 health。两库保存/建日中断后从已提交源配置恢复控制镜像和原 run，不换冻结目标，不清理消费。

主报告默认「时间总览」：一行一个带时区的计划时刻，一列一个渠道，格内分别显示状态、倍率、Canary 成绩/基线变化与 MT 结论；可切换同源「明细表」。日期、渠道、仅异常或缺测筛选对两视图一致。同一时刻同一渠道的多计划记录均保留，不混合不同时区；实际许可至结果/停止等待的时段与技术指标可展开，明细中直接显示。unknown 的上游结束未确认。渠道名称与倍率在该方法首次许可时持久化，改名或改倍率不改历史；同一格两方法间倍率变化分别标示。旧记录缺倍率显示“未记录”，不拿当前值回填。倍率是用户配置标记，未验证真实扣费且不进入连接指纹。

「查看演示」切换到三条合成渠道、多个时刻的固定展示，明确标注不代表真实检测，可看稳定、疑似下降、指纹偏离、缺测、参照不足和后续恢复观测及倍率变化。演示只读，不请求 API、不写数据库、不创建计划或提供真实运行下载；返回真实报告恢复原筛选。切换查询失败只显示错误，不把演示数据标成真实记录。

已完成/失败且通知已处理的到期 v2 报告按原保留期清理：先提交 day 归档标记，再删除 run，默认报告不展示已归档行。跨库中断后幂等收敛，归档日不会重建或重采；occurrence、attempt、消费/unknown 预留和独立可信 baseline 保留。运行中与未完成记录不会被此清理删除。

每渠道在表单中显式选择该渠道已锁定的完整、同条件可信 Canary baseline；不同渠道不共用参照。无 baseline 展示成绩与参照不足。完整 192 题、没有传输异常且同条件配对才支持疑似能力下降；Holm 局部下降线索保留，统计未确认变化显示需复核。超时、429、断流与答错区分。ModelTrace MISMATCH 仅需复核，scored 且 prediction 为 Astra 可显示“与 Astra 参考相近（未校准）”；SUSPICIOUS 本身不是降质。

`POST /stability/api/timetable/preview` 接受 v2 计划输入，返回实际时区当日去重图额度；`GET /stability/api/timetable/report?date=YYYY-MM-DD&channel_id=ID&anomalies_only=true` 返回简单行、完整分方法证据、筛选计数和查询截断标记。只读 `scripts/inspect_integrity.py` 输出默认图与版本，不开数据库/凭据，不发请求。

## 旧分层 v1 日常计划

按计划 IANA 时区、周一至周五为工作日。默认 Asia/Shanghai，可配置时区。

| 方法 | 时间 | 五渠道尝试上限 | 条件 |
| --- | --- | ---: | --- |
| 短探活 | 09:30/12:30/15:30/18:00 | 20/天 | 默认 Astra/Responses；只证明该路径 |
| TraceOne / Astra | 10:00/15:35 | 10/天 | 依赖对应探活，low |
| TraceOne / 6.1 Sol | 15:50 | 5/天 | 保守依赖 Astra 探活；Sol 健康并未被探活验证 |
| ModelTrace / Astra | 工作日 16:10 | 3/天 | 一条健康渠道轮转，17 类 bank |
| 固定能力 canary / Astra | 工作日 18:15 | 192/天 | 同一轮转渠道，18:00 探活后，独立能力轴 |

工作日最多 230 次，非工作日 35 次；少于五条时轻量请求按实际渠道数减少。ModelTrace 正常三答、最低两份有效答，三次硬上限，零补采。TraceOne 只注册一个方法、两模型目标；普通 API `calibration_status=unvalidated`，结果不能认证真实权重。ModelTrace 未列 6.1 Sol。TraceOne、ModelTrace、HLwY 不算独立多票。

新分层传输全局在途 1，与旧稳定性/压力测试的并发规则分开。健康证据默认有效 60 分钟，过期跳过而不额外探活。白天 17:55 截止；canary 次日 08:55 截止，仍受策略总时间 36000 秒约束。调度去重绑定日期、slot、目标、方法和配置 hash；持久 least-served 轮转，五条持续健康五个工作日各选一次。启用的分层计划随定时调度器自动执行，暂停计划停止后续采样。`EVAL_INTEGRITY_EXECUTOR=off` 默认仅关闭手动三项与 Monitor v2 主动消费者，显式 `live` 才消费主动任务；不影响已启用定时计划。这个主动配置开关不代替真实请求授权。

## 费用与恢复

不设每日金额或额外输入/输出/每日 token 预算截停。累计估算超过 3 美元、未知价格、缺 usage 或上游 reported usage 超过本地预估，都继续既定固定探针。固定请求清单、每题输出参数、零重试、原时窗、租约、去重和健康门控仍有效。旧 `limits` / `daily_limits` 字段保留任务身份和账本兼容，只记录预估；`budget_policy` 明确声明 token/day 预算不控制发送，`reservation_exceeded` 仅标记 reported 超过预估。配置价格仅为估算；input/output reported 缺失为 null，显式 0 保留。reasoning token 单列，包含在 output 中，不重复计费；unknown 不能当零。费用、已许可尝试、有效/无效、unknown、not_run 分开显示。

发送许可与 attempt 事务提交；结果与 attempt 完成原子提交。取消/进程中断中的未确认发送保留 unknown 及其记账预估，不自动重发。显式恢复仅在原 deadline 内续未发送项，最终可能 partially_completed。复用同键找回 durable job，发布前崩溃不重复入队或延长期限。传输要求各协议真实结束、有效文本；HTTP 200 不是有效答案。

## 能力 baseline 与结论

使用完整 MIT 192 题、四 family 各 48；每题一次请求、512 output 上限、零重试。无 baseline 只有 current_only 当前成绩；not_run 为 incomplete，invalid 计错且保留分母。完整运行可在报告中显式「锁定可信 baseline」，再在计划配置选择该 baseline；不会自动把当前成绩升级为可信参照。baseline 独立保存，不随旧报告五天清理删除。

比较必须匹配题集、scorer/manifest、provider/连接、模型、协议、effort、wrapper、输出上限、采样和预算。漂移拒比较。单侧 exact McNemar、overall alpha .05、最低净损失 .05，四 family Holm 次级检验；无显著差异只称未检出降级。10 对全正确到全错误 p=1/1024，无不一致对 p=1。功效文件的 192 题约 .898 是给定回退/改善概率的计算，不是普通 API 实测功效；未包含 Sol 能力覆盖。

## 主动异常复核

`/integrity/` 导入授权 immutable reference、选择 HLwY/KBF、确认固定样本及时间条件，再提交异步任务；可查看、取消、安全恢复与导出。没有 reference 不发请求。参考绑定 provider、模型、协议、system/wrapper/thinking、输出上限、重试、实际采样、probe/scorer hash 和预算元数据，样本数仍由调用方选定且必须匹配参考，不自动扩样；额外 token/day 预估不截停发送。OpenRouter 必须固定一个 provider 且禁 fallback。首期 reference 不自动采集。HLwY 每目标最多 50 次，KBF 自有选题最多 256、每题一请求、零重试，是单独版本的 Eval harness。

同条件连续两个有效 slot、48 小时内同方向偏离生成待人工复核 incident；invalid/skipped/unknown 或条件漂移打断连续计数。事件七天冷却。只建议复核，不自动创建收费任务、通知、停用或修改路由。已有告警与新参考门控要求显式决策，因此主动复核由页面或 Monitor 明确创建。任务同键冲突拒绝；同目标/参考/条件七天冷却复用原任务。

KBF 缺同模型同条件 self-test 为 UNKNOWN，双方 coverage 不足 .5 为 UNDETERMINED。失效/未执行仍在完整题数分母。99% Clopper-Pearson 自测错误上界作为 p0，单侧 binomial p<.05 才 DIFF；SAME 只表示未检出显著差异。HLwY 只输出分布行为比较，不输出身份或纯度认证。

## 账号证据与旁路

官方账号 nerfed 保留独立的显式白名单证据异步离线分析，与新 API 任务分开。只接受最小 events/catalog 和 body-free 数字 projection，不读取路径、私人会话或 Codex 文件，不导入 nerfed 可执行模块，不伪造 app-server。单独 16 类 bank、至少两有效答；top>=.8、expected<=.2、margin>=.5sigma 才上游 MISMATCH，其他 SUSPICIOUS；未列 UNLISTED。缺元数据 `metadata_status=unavailable`。HMAC 证明调用认证，不证明输入真伪或完备；账号线索不归因到 API 渠道。

Monitor [v2 契约与示例](../../docs/integrity-monitor-api.md) 复用 `/internal/v1/*` HMAC，工作台登录不能替代，Monitor 不获得普通渠道管理权限。旧 v1 probe-jobs/报告保留；旧主动 nerfed 请求明确 `strategy_contract_changed`，不静默转义。

fpverify 仅在合格官方同条件 reference 后的后续按需审计，本轮不建库/运行。Modivue 仅文档化独立客户端旁路和显式安全摘要，不安装、不改客户端/BaseURL、不扫描日志，不引入 Meow。

版本与许可见 [NOTICE](NOTICE.md) 和 assets/provenance.json；验收见 [测试清单](../../docs/integrity-testing.md)。本轮只验证合成 Mock，source 上游证据不替代 Eval 产品验收或真实校准。

## 三项统一 API 测试

从 `/integrity/` 选择一次合格渠道，默认 `gpt-6-astra` / Responses / low，点击「开始三项测试」。可选范围不限制五条渠道；不要求历史target或通过记录，允许已记录和已上线渠道，但须启用且地址、凭据及模型/协议映射有效；保存渠道不自动测试。一次持久任务分别执行健康1次、TraceOne1次、ModelTrace最多3次、nerfed-api最多3次，总计最多8次尝试、零重试，每种方法独立发送与评分。健康失败跳过长探针；子项invalid不阻止其他子项。

POST `/api/integrity/tests` 请求 `{registry_channel_id,model,protocol,idempotency_key,confirm_live:true}` 返回202统一任务。GET `/api/integrity/tests`、GET `/api/integrity/tests/{id}` 和 `/export` 提供历史/状态/三份报告；POST同路径 `/cancel`、`/resume` 仅续原窗内未发送项。任务 `three-method-api-v1`、600秒总窗；长探针各60秒，固定每题输出参数，token 预留仅记账。重复幂等键保留原任务和期限，改变目标/版本409。「新一轮测试」明确重新采样。

nerfed API版本 `nerfed16-api-v1-ff0d7c0c` 使用独立16类bank、至少2有效答；ModelTrace固定17类。普通API calibration_status=unvalidated、metadata_status=unavailable；上游MISMATCH阈值是行为线索，不是认证。高级Sol时ModelTrace为unsupported；nerfed的Sol声明为UNLISTED。报告分别显示结论、有效/无效/unknown/未运行分母、条件、耗时和usage；这些状态不能当通过。手动三项消费者与主动复核共用执行器，默认 `EVAL_INTEGRITY_EXECUTOR=off`，不调用Codex app-server或上游可执行入口。官方账号白名单证据原HMAC2.0契约独立保留。
