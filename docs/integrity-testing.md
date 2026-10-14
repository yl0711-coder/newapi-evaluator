# 完整性、半小时默认采样与主动 Canary 测试清单

适用规则 `docs/ai-rules` v1.0。本功能属于工作台定时、准入及完整性模块，不改 `relay_lab` 共享引擎；实验室五模式继续纳入既有 E2E 和独立 legacy acceptance。环境、数据、日志、截图和证据使用仓外新目录，不加载业务 `.env`、渠道库、凭据或通知设置。上游源码核对不替代产品测试。

统一入口：`python -B scripts/verify_integrity.py --output <新外置目录>`。独立验收在干净 detached 候选提交增加 `--sha <40位SHA>`，使用独立环境与新数据。`PLAYWRIGHT_MODULE`、`PLAYWRIGHT_BROWSERS_PATH`、`EVAL_TEST_ARTIFACT_ROOT` 和 `TMPDIR` 显式指向仓外；执行器设置 `PYTHON_EXECUTABLE`。执行器保留收集数、退出码、前后源码指纹及首次失败，实施逐组时限和总预算，失败优先汇总。开发25组、3000秒；独立26组、4200秒。输入 Token 与服务器准入队列的合同见 [功能和接口](token-usage-queue.md)。

| suite_id | 入口 | 验证与时限 |
| --- | --- | --- |
| feishu-channels-inspect / feishu-channels-browser | `scripts/inspect_feishu_channels.py` / `scripts/feishu_channels_ui.cjs` | 只读合同检查及合成飞书的设置、筛选、同步、换 Key、下线与失败保留；60/180 秒 |
| protocol-inspect / admission-inspect / diagnosis-inspect / image-inspect / model-coverage-inspect | 清单对应 inspect CLI | 零发送、脱敏配置；各60秒 |
| token-usage-inspect | `python -m features.token_usage inspect-config` | 固定样本、预算、阈值、协议与半小时间隔，零发送；60秒 |
| token-usage-browser / admission-queue-browser | `scripts/token_usage_ui.cjs` / `scripts/admission_queue_ui.cjs` | 可信绑定、主动结果、切页后台继续、严格FIFO、重启补Key与unknown不重发；各300秒 |
| token-usage-browser-off | `scripts/token_usage_ui.cjs --active-off` | 主动按钮禁用与解释、独立定时真实调度、空闲15秒刷新发现结果；300秒 |
| integrity-inspect | `scripts/inspect_integrity.py` | 全资产及运行hash、17/16类区别、v3图及重试上限；零数据库、凭据与网络；60秒 |
| workbench-python | `scripts/test_all.py --output` | 三引擎 selftests 与全部 unittest discover，含下列领域回归；900秒 |
| workbench-web | `scripts/test_web.js` | 既有页面契约；120秒 |
| workbench-security | `scripts/repo_security_scan.py .` | 全注册源码与合成夹具；120秒 |
| syntax | `scripts/diagnosis_syntax.py` | Python、JS/CJS/MJS及HTML内联脚本；300秒 |
| workbench-e2e | `scripts/e2e.py --output` | 原实验室五模式真实回环Mock；600秒 |
| workbench-browser | `scripts/ui_smoke.cjs` | 原导航、主题、六题准入、双端报告、各导出与窄屏；600秒 |
| protocol-browser / diagnosis-browser / model-coverage-browser / monitor-control-browser | 清单对应 `.cjs` | 实际HTTP、旧HMAC、控制面与覆盖兼容；各300秒 |
| integrity-browser | `scripts/integrity_ui.cjs` | 三项首测、主动Canary完整193请求、显式参照、取消恢复、导出、重载、HLwY/KBF及账号离线；600秒 |
| integrity-timetable-browser | `scripts/integrity_timetable_ui.cjs` | v3同步网格、双空、默认整点/半点48轮及288首次请求、恢复默认、六轮36独立请求、零Canary、历史名称倍率、结论及筛选导出、合成演示与窄屏；600秒 |
| build-container | `scripts/image_quality_container.py --output` | 当前镜像all/image-quality健康、完整性页面/API、主动默认off、定时状态、离线零出站，自有容器精确清理；900秒 |
| legacy-acceptance | `scripts/acceptance.py --sha --output` | 仅独立干净SHA，既有实验室全量、持续与混合负载；1200秒 |

## 定时采样与结论

`test_integrity_timetable` 验证 `layered-integrity-v3` 每个选定时刻各执行MT三题和Nerfed三题，互不复用答案，无health或Canary。默认全天48轮（整点/半点），单渠道首次288请求；已确认连接故障、超时或断流每题再重试三次，最多1152物理请求。十分钟刻度仍可显式自定义，已有配置与双空保留。成功立即停止重试；HTTP错误、格式无效及unknown不重发。固定逻辑分母与物理尝试、最终错误分别计数；早期断联后成功只用最终结果评分。

同模块覆盖同步集合与显式空、非法刻度、DST gap/fold、原due起600秒窗口、两把共享发送容量、题边界公平轮转、保存/暂停/删除许可竞态、未来零尝试重开、过去不追赶、跨库重关联、日期归档保留attempt/消费、终态纯重建及取消恢复unknown不重发。`test_scheduled_executor_enabled` 通过正常workbench lifespan与真实受保护回环HTTP确认：主动消费者未设置或off时，已启用计划仍自动执行MT3+Nerfed3；暂停/删除后零发送，退出停止调度。

结论回归验证两方法完成、每法至少两有效答、无缺测/unknown/最终请求异常、同采样条件、排名与预测一致、top概率至少0.8且分数领先至少0.5。两法一致才显示“更接近 Astra / 更接近某模型”；不足、冲突、异常均为“无法判定”。这些是保守闭集行为指纹规则，普通API校准仍为unvalidated，不认证模型身份。定时页面仅展示结论；技术分母保留在受鉴权报告和导出，旧报告继续可读。

`test_scheduled_integrity` 与迁移回归覆盖旧v1/v2计划启动升级，保留enabled、冻结绑定和legacy原配置；仅旧v2两集合都空时保留零采样，其余升级全天半小时。退休旧待运行槽，已提交终态纯重建、未确认许可转unknown并清租约，旧执行器后续提交拒绝。旧Canary成绩、可信参照、名称、倍率、时区与旧详情/export冻结；重复初始化幂等；同日v3身份独立，升级前时刻不补采。

## 主动 Canary 与原完整性功能

`test_active_canary` 验证工作台显式确认后使用Astra/Responses/low、health1加固定完整192题，零重试、原36000秒总窗。无参照仅当前成绩；完整无异常结果可由用户显式锁定可信参照。目标、条件、题集、asset/scorer/sampling/manifest hash漂移在入队前拒绝。取消保留unknown，恢复原任务不延窗、不重发；health失败保留192题分母；请求异常与目标漂移不能锁参照。

`test_unified_integrity` 保留recorded渠道无生产身份绑定的三项首测、最多8次独立请求、幂等冲突、health失败分母、单项invalid不影响后项、Sol unsupported、取消恢复和连接映射漂移。`test_integrity_execution` 保留三协议真实解析、终态与usage、租约fencing、许可原子边界、费用/token仅记账、unknown、正文不落盘与噪声深层投影。

`test_token_usage_remediation` 注册在 workbench 的必需模块清单，发现阶段核对非零用例和收集错误；覆盖 SSE/压缩字节上限、严格协议终态、取消租约与失效暂停、名称冻结与产物 realpath。`test_admission_queue`/`test_admission_reporting` 覆盖报告副本清理、取消摘要一致、请求构造漂移与旧缺指纹拒绝、事件 gap 及旧缺 watermark。浏览器补冻结双端名称、命名导出与转义、具体停止对象、队首阻塞及窄屏；`test_unified_integrity` 验证名称变化不改请求身份，旧名称不回填、凭据不进入显示字段。

`test_integrity_scoring` 验证17/16类不可混用、UNLISTED、两答门槛、配对McNemar、四family Holm、invalid计错/not_run缺测及条件拒比较。`test_kbf_review` 验证授权参考、hash、self-test、覆盖、冷却幂等、unknown及浏览器JSON数字往返。`test_integrity_monitor` 保留HMAC原始body/nonce、principal隔离、旧nerfed错误、nerfed-api主动四尝试、取消恢复和离线契约。

## 糖果准入与验证边界

准入题库为3 easy+3 hard，新增`hard-3`来自`codex-candy-eval`，中文转述保留题意与数值。双端最多五轮且预热包含在内，最多60 measurements/responses和30 pairs；旧五题报告仍可读写。测试核验题数、Astra Responses/low/4096、计数、失败分母、普通与技术导出边界。答案21及9圆+12星由独立最坏情况枚举核实，产品保持人工阅读答案，未引入“出现21即通过”。

所有运行只替换上游和不可控时钟，保留发送许可、协议解析、真实评分、持久化与报告链路。浏览器演示固定18行合成数据，零API、零上游请求、零计划创建，退出加载失败不得沿用合成结果。Mock usage和费用不是账单；未开展真实上游、真实Monitor联调、API校准、实测检出功效或连续全天容量验证，测试不发送通知、不部署。
