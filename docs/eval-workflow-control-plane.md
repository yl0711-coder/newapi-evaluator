# Eval 自主开发控制面 v0.1

## 当前边界

该控制面只处理本地脱敏数据和合成事件。它不读取生产数据库，不保存凭据，不自动连接上游，不自动发布。

## 状态分离

生产覆盖、Eval 排期和 Eval 实测是三个独立维度：

- `production_status`：生产快照声明的 `online / offline / unknown`。
- `coverage`：根据显式 `eval_channel_id` 与模型协议匹配得到 `covered / missing / stale / conflict`。
- 任务状态：队列自身的 `queued / running / succeeded / failed / cancelled`。

生产快照的来源和版本必须稳定；同一来源同一版本的内容哈希改变会返回冲突，重复内容只返回已有批次。失败导入不会改变最近有效快照。游标只保存哈希值。

## 任务契约

创建任务必须提供 `task_type`、JSON `payload`、`idempotency_key` 和秒级预算。幂等键绑定任务类型和负载哈希。任务由 `claim_next` 领取，执行器必须通过 `finish` 写入终态；取消请求在领取前直接终止，运行中由处理器通过取消回调检查。

Monitor 风格事件使用 `event:<event_id>` 作为幂等键，并映射为 `monitor:<event_type>` 任务。当前事件入口是本地契约，不代表真实 Monitor 已接入。

## I1 Monitor 内部接口

按《Monitor—Eval 内部接口契约 v1.0》实现，调用说明、签名算法和权限见 [monitor-internal-api.md](monitor-internal-api.md)。旧的未签名入口 `/api/model-coverage/internal/v1/*` 已删除；本地队列 `eval_workflow_tasks` 不再对 Monitor 暴露。

## 前端入口

公共渠道页 `/channels/` 提供生产覆盖导入、任务创建、刷新和取消，以及 NewAPI 渠道身份绑定和 Monitor 复测任务状态。所有新建后端用户能力都必须同步提供可见操作和状态反馈。

## 放权与门禁

当前只允许本地合成任务。进入真实生产或自动发布前，必须新增明确的授权、凭据注入、目标白名单、停止条件、版本 SHA、独立验收证据和人工接受记录。

统一本地门禁：

```bash
python scripts/verify_workflow_control_plane.py --output <仓库外新证据目录>
```

它分别记录模型覆盖测试、语法诊断、安全扫描和 Web 契约测试；该门禁不替代独立浏览器、容器和发布验收。
