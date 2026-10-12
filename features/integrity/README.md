# 定时模型行为采样与主动复核

## MT / Nerfed 半小时默认采样

新计划默认 `layered-integrity-v3`，最多五条已上线、启用且凭据与精确模型映射有效的 Registry 渠道，固定 `gpt-6-astra` / Responses / low。MT 与 Nerfed API 在同一时刻各自发送三题，全部选中渠道每天独立采样，不含 Canary 或共享探活。默认全天 48 个整点和半点，可按十分钟刻度同步自定义或清空为零请求计划。新建和“恢复默认”均从后端配置读取默认时刻；已有 v3 计划的显式时刻和双空配置保留。

每渠道默认首次 288 请求/天；仅已确认的断联、超时或断流给予最多三次重试，成功即停止，首次加重试最多四次，每渠道最多 1152 物理尝试/天。429、HTTP 错误、无效答案、未确认的许可或 unknown 不触发连接重试。逻辑评分分母保持每方法三题，物理尝试单独记账；最终成功不被先前已确认的连接错误污染。

每轮截止为计划时刻后 600 秒，重试、取消和恢复都沿用原窗。共享传输全局在途 1，每题提交后让出并公平选择。开始一轮不保证能在原窗完成；窗口结束未完成时给出无法判定。DST 不存在时刻跳过，重复时刻仅第一次。过去新增时刻不追赶；未来零尝试的取消时刻可恢复原实例。发送许可再次核对启用状态、冻结连接、模型映射、时刻和控制 revision。

用户报告的时间总览和明细表显示“更接近 Astra / 更接近某模型 / 无法判定”，技术数据保留在导出。两方法必须完成、每方法至少两份有效答案、无 unknown/缺测/最终请求异常、相同目标与采样条件，并且预测模型一致；闭集第一候选概率至少 .8、分数领先第二候选至少 .5 才给出更接近结论。这是保守行为比较，普通 API 校准仍为 unvalidated，不认证真实模型身份，也不将相关数字指纹当独立多票。

启用计划由定时调度器自动运行，不受 `EVAL_INTEGRITY_EXECUTOR=off` 影响；该变量只控制主动任务消费。暂停、删除或移除时刻阻止后续许可。旧 v1/v2 配置启动时升级为 v3，保留启用/暂停状态、冻结目标和原配置；仅旧 v2 的 MT 与 Canary 两集合均空时保持空，其余升级全天半小时。升级不补采保存之前的时刻，旧未完成任务退休，未确认许可记 unknown，已提交结果纯重建。旧定时 Canary 不再发送；旧历史、导出和可信参照仍可读取。ins-v2 普通稳定性计划保留。

时间报告按日期、渠道及异常筛选，同一时间多计划与不同时区均保留。“查看演示”展示三渠道合成结论，零 API、零计划、零出站；退出失败不能沿用合成数据。名称与倍率在首次许可冻结，技术导出保留标记，倍率未经实际扣费验证。到期报告先归档再裁剪，保留 occurrence、attempt、消费与 unknown，归档日期不重建。

`POST /stability/api/timetable/preview` 返回首次与重试最大额度；`GET /stability/api/timetable/report?date=YYYY-MM-DD&channel_id=ID&anomalies_only=true` 返回结论和导出证据。只读 `scripts/inspect_integrity.py` 不开数据库、凭据或网络。

## 主动 Canary

在 `/integrity/` 选择渠道与可选可信参照，确认后点击“开始 Canary 测试”。固定 Astra / Responses / low：一次短探活加完整 192 题，最多 193 请求，零重试，总窗 36000 秒。探活失败保留 192 项未运行分母。任务支持查询、历史、取消、原窗安全恢复和 JSON 导出，unknown 不重发。仅工作台显式创建，定时采样和 Monitor 不创建主动 Canary。

完整且无请求异常、unknown、缺测的结果可显式锁定为可信参照，不自动提升。下一轮选择同目标/连接、参数、题集及版本的参照；不兼容参照在入队前拒绝。旧定时参照保留读取，不能直接用于新主动窗口。接口为 `POST /api/integrity/canary`、`GET /api/integrity/canary/baselines` 和 `POST /api/integrity/tests/{id}/baseline`，任务管理复用 `/tests/{id}`。

## 费用与恢复

不设每日金额或额外输入/输出/每日 token 预算截停。累计估算超过 3 美元、未知价格、缺 usage 或上游 reported usage 超过本地预估，都继续既定固定探针。固定请求清单、每题输出参数、各任务重试策略、原时窗、租约、去重和健康门控仍有效。旧 `limits` / `daily_limits` 字段保留任务身份和账本兼容，只记录预估；`budget_policy` 明确声明 token/day 预算不控制发送，`reservation_exceeded` 仅标记 reported 超过预估。配置价格仅为估算；input/output reported 缺失为 null，显式 0 保留。reasoning token 单列，包含在 output 中，不重复计费；unknown 不能当零。费用、已许可尝试、有效/无效、unknown、not_run 分开显示。

发送许可与 attempt 事务提交；结果与 attempt 完成原子提交。取消/进程中断中的未确认发送保留 unknown 及其记账预估，不自动重发。显式恢复仅在原 deadline 内续未发送项，最终可能 partially_completed。复用同键找回 durable job，发布前崩溃不重复入队或延长期限。传输要求各协议真实结束、有效文本；HTTP 200 不是有效答案。

## 能力 baseline 与结论

使用完整 MIT 192 题、四 family 各 48；每题一次请求、512 output 上限、零重试。无 baseline 只有 current_only 当前成绩；not_run 为 incomplete，invalid 计错且保留分母。完整运行可在报告中显式「锁定可信 baseline」，再在下一轮主动 Canary 选择该 baseline；不会自动把当前成绩升级为可信参照。baseline 独立保存，不随旧报告五天清理删除。

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
