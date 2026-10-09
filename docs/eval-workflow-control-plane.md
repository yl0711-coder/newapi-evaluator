# Eval 自主开发控制面 v0.1

## 当前边界

该控制面只处理本地脱敏数据和合成事件。它不读取生产数据库，不保存凭据，不自动连接上游，不自动发布。

## 状态分离

生产覆盖、Eval 排期和 Eval 实测是三个独立维度：

- `production_status`：生产快照声明的 `online / offline / unknown`。
- `coverage`：根据显式 `eval_channel_id` 与模型协议匹配得到 `covered / missing / stale / conflict`。
- 任务状态：队列自身的 `queued / running / succeeded / failed / cancelled`。

生产快照的来源和版本必须稳定；同一来源同一版本的内容哈希改变会返回冲突，重复内容只返回已有批次。失败导入不会改变最近有效快照。总览按每个来源的 `generated_at` 最大值选择最近快照；同一来源时间相等时，后接收记录（更大的 `id`）优先。乱序接收的旧版本保留为历史，不使总览回退；完全重放不改变接收顺序。同版本生成时间变化也属于内容冲突。游标只保存哈希值。

## 任务契约

创建任务必须提供 `task_type`、JSON `payload`、`idempotency_key` 和秒级预算。幂等键绑定任务类型和负载哈希。任务由 `claim_next` 领取，执行器必须通过 `finish` 写入终态；取消请求在领取前直接终止，运行中由处理器通过取消回调检查。`finish` 与取消操作通过写事务串行化，已提交的取消请求优先，终态不可覆盖。

`finish` 在持久化前先校验原始结果中的全部字段和值，再校验实际 JSON 结果（对象或省略，允许普通 JSON 值及可序列化的 tuple），避免数字与字符串键归一化碰撞漏检；复用任务负载的敏感字段、字符串、深度、节点数限制，并限制结果大小为 64 KiB。错误全文先检查凭据赋值、Bearer、API Key 与完整 URL，再截取前 300 字符；普通长错误保留该截断行为。拒绝时使用固定脱敏错误，任务整行不变，仍可安全完成。取消优先时丢弃传入的结果和错误，只保存固定取消原因。现有 `run_one` 的敏感处理器异常会被拒绝，任务保持 `running`；不安全结果则由其现有异常路径记录为固定安全失败，不保存原结果。

Monitor 风格事件使用 `event:<event_id>` 作为幂等键，并映射为 `monitor:<event_type>` 任务。当前事件入口是本地契约，不代表真实 Monitor 已接入。

## I1 Monitor 内部接口

按《Monitor—Eval 内部接口契约 v1.0》实现，调用说明、签名算法和权限见 [monitor-internal-api.md](monitor-internal-api.md)。旧的未签名入口 `/api/model-coverage/internal/v1/*` 已删除；本地队列 `eval_workflow_tasks` 不再对 Monitor 暴露。

Monitor 复测使用独立的 `monitor_probe_jobs` / `monitor_probe_attempts` 账本。每个请求在取得共享请求名额、完成 URL 预检后，通过发送许可事务核验有效租约、取消/期限、原连接和身份、关联生产清单及预算，再保存唯一 attempt 与 token 预留。结果、attempt 完成状态和 reported 用量同事务提交；心跳只续有效租约。未知 attempt 保留预算且不自动重发，旧任务缺原始连接快照时拒绝执行。结果和复现事件绑定发送许可时的脱敏目标快照；缺失用量、显式 0 和字符估算分别表示，详见内部接口文档。

生产清单明确停用的渠道仍可发起恢复复测，前提是 Eval 测试连接启用且清单状态未在排队后漂移。发送许可前的 DNS/连接失败和超时在任务终态分别保留 `transport_connect` / `transport_timeout`，未发送步骤不产生 attempt、预算预留、HTTP 请求或伪造结果。旧运行任务恢复只保留可证明的旧请求数与预留，缺失预留和无法追溯的 reported 用量保持 `null`。

本地生产覆盖导入和 Monitor 签名清单是两个入口：前者允许保留乱序旧快照，按来源选择最新；后者拒绝生成时间不晚于当前清单的新版本。不要互换它们的版本规则。

## 前端入口

公共渠道页 `/channels/` 提供生产覆盖导入、任务创建、刷新和取消，以及 Monitor 旧 v1 复测任务状态。渠道身份绑定 UI 已移除；v1 数据与 API 合同保留，新的完整性 v2 使用 Registry 精确 ID。所有新建后端用户能力都必须同步提供可见操作和状态反馈。

## 放权与门禁

当前只允许本地合成任务。进入真实生产或自动发布前，必须新增明确的授权、凭据注入、目标白名单、停止条件、版本 SHA、独立验收证据和人工接受记录。

统一本地门禁：

```bash
python -B scripts/inspect_test_plan.py workflow-control-plane
python -B scripts/verify_workflow_control_plane.py --output <仓库外新证据目录>
```

清单登记六组：`workflow-python`、`workflow-syntax`、`workflow-security`、`workflow-web`、`workflow-browser` 和 `monitor-control-browser`。后两组分别运行模型覆盖页面与控制面/签名本地调用方的合成浏览器检查；它们不证明真实 Monitor 联调或生产上游命中。逐组入口、依赖、预算和设备证据根配置见 [统一测试清单](test-plan-manifest.md)。

门禁默认整体预算 1800 秒，可显式传入正有限值 `--overall-budget`；每组实际时限取单组上限与剩余整体预算的较小值。它预先注册全部组，并持续保存 `summary.json` 和逐组日志。零收集、注册模块漏项、必需跳过、缺依赖、超时、取消或结果证据缺失均不能得到 `passed`；取消/预算耗尽后未启动的组为 `not_run`。已有确定失败时该组和总体保持 `failed`，同时保留超时、取消或未知退出原因及 `timed_out`，后续成功或执行中断都不覆盖已知失败；只有执行缺口而无已知失败时为 `incomplete`。`complete=true` 仅表示本轮汇总已收尾，质量结论以 `status` 为准。只有总体 `passed` 返回 0，取消返回 130（即使已记录质量失败），其余返回 1。

专项门禁不替代工作台全部适用测试组、干净候选提交的独立审查/验收、容器和发布验收；最终证据仍需绑定实际候选版本。真实 Monitor 联调、真实上游、`end_to_end` 隔离身份及自动发布仍未由本地门禁验证。
