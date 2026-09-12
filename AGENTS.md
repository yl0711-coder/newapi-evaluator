# 项目约束

使用 `/Users/lmurder/.codex/skills/relay-station/SKILL.md`，按其链接读取只读 evaluator 工作流并应用本项目本地 Git 覆盖规则。

产品代码仅写本目录。运行数据、日志、依赖环境、临时文件全部写 `/Users/lmurder/Desktop/api中转站/中转站极限测试数据`。验收目录 `/Users/lmurder/Desktop/api中转站/中转站极限测试-验收副本` 仅检出测试指定提交，不编辑产品代码。

不修改其他项目。保护已有改动。仅功能分支开发与提交。用户于 2026-09-11 指定并授权推送到 `https://github.com/yl0711-coder/newapi-evaluator.git` 的 `feature/mock-capacity-lab`；允许沿用该 origin 和分支。不得推送、修改或合并 main，不创建 PR。首次本地 main 仅为空基线。

本阶段所有网络测试只用本地 Mock，无真实账号验证。真实模式须有用户授权与 --confirm-live；破坏性测试仅限本地 Mock 或明确授权的隔离目标。

不得保存凭据、请求/响应正文、提示词、原始异常或完整敏感 URL。JSONL 仅含白名单请求指标。

继续先看 Git 状态和 PLAN.md。运行聚焦、完整测试、Mock E2E、安全扫描，然后固定完整 SHA 独立复测。报告在数据目录，绑定实际 HEAD。
