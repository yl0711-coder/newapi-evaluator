# Eval 自主开发基线与下一步任务

状态：E1-R0、E2 本地控制面与 I1 Monitor 内部接口已实现（2026-09-30），未提交、未合并，等待用户验收

## 目标

让 AI 可以根据项目发展目标，设计、执行并交付 Eval 工作流；用户只验收最终结果。当前阶段只建立可回放、可审计、可从前端操作的最小闭环，不自动触达生产渠道。

## 基线身份

- 仓库：`/Users/lmurder/Desktop/eval`
- 远端：`yl0711-coder/newapi-evaluator`
- 分支：`feature/eval-workflow-control-plane`
- 基线 SHA：`f06999aa511dd3a5b42973ccdfa38de8c537e32f`
- 证据目录：`/Users/lmurder/Desktop/api中转站/中转站极限测试数据/eval-prep-20260928-baseline`
- 当前工作区已有用户改动：`.gitignore`、`AGENTS.md`、`README.md`、删除的 `PLAN.md`、测试清单/测试脚本、AOCI 文件及发布流程文档；本任务不覆盖这些改动。

## 已验证能力

| 能力 | 当前实现 | 结论 |
| --- | --- | --- |
| 统一渠道与凭据 | `shared/registry.py`，Fernet 加密、版本冲突、连接指纹、脱敏列表 | 可作为 Eval 渠道身份基础 |
| 模型目录与映射 | `features/model_coverage/catalog.py` | 有标准模型、渠道实际模型、协议精确映射 |
| 上游模型发现 | `features/model_coverage/discovery.py` | 有分页、失败保留、连接变化失效和人工确认入口 |
| Eval 覆盖状态 | `features/model_coverage/service.py` | 有上游提供、排期、实测三类状态；支持 stale/connection_changed |
| 定时执行 | `features/stability/app/scheduler.py`、`storage.py` | 有计划、租约、恢复、限并发、运行快照和历史结果 |
| 前端操作面 | `/channels/`、`/stability/`、模型覆盖按钮 | 现有后端能力均有可见入口；新能力需继续遵守此约束 |
| 本轮基线验证 | `test_all.py`、`test_web.js`、安全扫描、语法诊断 | 300 Python 测试、Web、220 文件安全扫描、143 项语法诊断均通过；证据见外置目录 |

## 能力缺口

| 缺口 | 现状 | 风险 | 优先级 |
| --- | --- | --- | --- |
| 生产覆盖快照 | 当前只有公共渠道/模型发现和 Eval 实测覆盖，没有生产系统只读覆盖导入模型 | 不能区分“生产已覆盖”与“Eval 已测试” | E1 |
| 快照版本与陈旧语义 | 有连接指纹与实测过期，但没有生产导入批次、游标、版本冲突和明确 stale 原因 | 导入延迟或重复时可能误判 | E1 |
| Monitor→Eval 入口 | 已实现，见下方 I1；尚未与真实 Monitor 联调 | 真实上游执行需另行授权 | I1 |
| 统一队列与 Outbox | 准入模块有飞书写入 Outbox；尚未抽象成 Eval 任务队列、幂等键、取消和重试 Outbox | 自动执行难以恢复和审计 | E2 |
| 治理与发布门禁 | 现有发布流程和测试清单存在，但缺少面向自主开发任务的目标、验收、证据和放权等级记录 | AI 可能越过人工验收边界 | G1/R1 |

## 已实现的最小增量：E1-R0

本轮实现了 **E1-R0：生产覆盖只读导入与状态预览**：

1. `features/model_coverage/production.py` 定义脱敏快照契约；游标只保存 SHA-256，不保存原值。
2. 只读导入合成 JSON 快照，保存批次与内容哈希；重复批次幂等，旧批次保留。
3. 将生产状态与现有 Eval 状态并列展示，计算 `covered / missing / stale / conflict`，不自动创建测试目标、不发起上游请求。
4. 在 `/channels/` 增加“导入生产覆盖”按钮、摘要状态和错误反馈。
5. `tests/test_model_coverage.py` 覆盖契约、幂等、冲突、陈旧、敏感字段拒绝。

本增量不包含 Monitor 联调、自动放权、真实生产连接或自动发布。

## I1 Monitor 内部接口（2026-09-30）

按知识库《Monitor—Eval 内部接口契约 v1.0》实现，使用说明见 [monitor-internal-api.md](monitor-internal-api.md)。

- `features/model_coverage/monitor.py`：签名、错误体、清单、复测任务、执行器、结果/事件映射，数据在 `monitor_*` 表。
- `features/model_coverage/internal_api.py`：`/internal/v1` 路由与后台执行器；`shared/access.py` 让该路径只认 Monitor 签名。
- 公共渠道页查看 Monitor 旧 v1 复测任务；身份绑定 UI 已移除，旧 v1 管理 API 和数据保留，新完整性 v2 使用 Registry ID。
- 执行器默认关闭（`EVAL_MONITOR_EXECUTOR=live` 才请求上游），只用 Mock 验证过。
- 契约测试：`tests/test_monitor_internal.py`；控制面门禁已包含该文件。
- 未做：真实 Monitor/上游联调、`end_to_end` 隔离身份、JSON 金样、密钥轮换、审计查询接口。
- 历史待办：`production.py` 的工作台快照导入按到达顺序取最新，迟到旧快照会覆盖新快照。

## E2 本地控制面

- `features/model_coverage/workflow.py` 提供持久化任务队列、幂等键、预算、取消、领取和终态记录。
- `/api/model-coverage/workflow/tasks` 提供任务列表、创建和取消；`/workflow/events` 将 Monitor 风格事件映射为幂等任务。
- `/channels/` 提供创建本地任务、刷新和取消按钮。
- 当前没有后台执行器自动消费队列；后续接入 Eval 执行器时必须复用 `claim_next`/`finish`，不能绕过任务状态。
- 统一本地门禁：`scripts/verify_workflow_control_plane.py --output <仓库外证据目录>`，会分别记录 Python、语法、安全和 Web 结果。

## 验收口径

- 所有输入均为本地合成数据；真实生产导入需要另行授权。
- 不保存 API Key、Token、Cookie、完整 URL、完整 Prompt/响应或客户身份。
- 测试、独立审查、交付接受和 Git/发布授权分开记录。
- 新后端能力必须有前端按钮、状态和错误反馈。
- 以新提交 SHA 和全新外置证据目录绑定结果；当前基线通过不代表 E1-R0 已完成。

## 未决问题

- 生产覆盖快照的实际来源和字段名称尚未接入；先用本地合成契约占位。
- Monitor 的事件来源、鉴权和游标协议尚未确定；不在 E1-R0 猜测。
- E1-R0 的数据库放在公共 `channels.db` 还是独立 `eval.db`，实现时按数据边界和迁移风险决定并记录。
