# AI 编码与测试正式规则

## 独立工具适用范围（2026-09-22）

本仓库从 newapi-evaluator 的独立工具 v1.6.4（0d273a26981c7d6afb7eae81bd74005bc44d4e43）拆分。不是线上 Eval 工作台的升级，不向工作台或 Monitor 合并；原仓库不再作为本工具的维护入口。下列共用规范保留来源语义，其中工作台/实验室特有模块不适用于本产品。

本机外置验收目录映射为 /private/tmp/channel-diagnostic-*；CI 为 RUNNER_TEMP 下的独立目录。运行数据、渠道配置及凭据不得进入 Git 或镜像。项目实际清单以根 TESTING.md 为准；运行时 Python 3.11+，浏览器只展示静态报告。报告兼容基线见 README 的“报告与统计口径”，不是工作台四模块报告。

当前授权：提交到 nexusapi-channel-diagnostic 并独立部署；不得覆盖 eval.nexusapi.link，不改 Monitor/API/NewAPI，不自动启用渠道或真实检测。发布采用本仓库独立版本 v1.0.0 起步、固定摘要镜像；实际合并 main 仍须明确授权。

本仓库 `yl0711-coder/nexusapi-channel-diagnostic` 的所有开发、修改、测试、审查和 Git 操作必须遵守 v1.0 规则，不因目录、分支、设备或 worktree 改名而改变。当前任务开始先核对身份、HEAD、已有改动和规则版本。

先完整读取 [编码规范](docs/ai-rules/CODING.md)、[全量测试](docs/ai-rules/TESTING.md)、[独立审查](docs/ai-rules/REVIEW.md)、[报告兼容](docs/ai-rules/REPORTING.md)、[交接规范](docs/ai-rules/HANDOFF.md) 与 [项目事实和执行清单](docs/ai-rules/PROJECTS.md)，再读任务相关 README 和子目录专项规则。

新增后端 Python / FastAPI；功能修改先在对应任务分支完成。实验室子集先在实验室验证，再准备工作台候选。实际合并须用户明确允许该次合并；不得自动 push、发布或改上游配置。保留已有报告展示和导出、用户改动及业务数据。

本正式版本落实用户已确认的范围、技术栈、报告与两阶段流程；历史工作流或 Skill 的相反默认建议不覆盖这些已确认指令。仍保留只读验收、Mock 默认、凭据隔离及具体远端授权限制。旧版本和验收副本从外部加载指定正式规则，不为补规则改变待验收 SHA。

## Code Review Rules

审查全部变更和调用链，存量审查按模块覆盖并标明范围；关注接口、事务、取消/恢复、协议结束语义、指标、报告兼容、出站与凭据、测试注册及失败聚合。独立 AI 审查须实际完成。问题绑定规则、源码位置、版本、触发证据与影响；历史问题登记待办，不自动进行全仓整改。测试失败、未跑或缺证据不得报全量通过。
