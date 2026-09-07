---
name: evaluator-development
description: 在 newapi-evaluator 开发机上为功能分支创建独立 worktree，实施、测试、提交和推送代码，或根据 PR 测试反馈修复。不用于独立验收、测试机操作或合并。
---

# Evaluator Development

只处理以下开发机工作：从最新 `origin/main` 创建功能分支与独立 worktree、开发、测试、提交、推送，以及根据 PR 中与明确 commit 关联的测试反馈修复。

执行前必须完整阅读 [共同流程](../../evaluator-workflow.md)，并将其视为跨设备协作、安全、报告与合并权限的唯一真源。若无法读取该文件，不得开始任何写操作。

## 执行流程

1. 确认位于正确的已有仓库，检查当前 worktree 状态、远端 URL 和用户指定的分支范围。保留任何现有未提交更改。
2. 执行 `git fetch origin main`，确认新功能分支基于当时的 `origin/main`。在仓库目录之外创建命名明确的独立 worktree；不复用其他功能的 worktree，不为此重新 clone 仓库。
3. 只实施已授权的业务变更。为该功能沙盒提供一条受版本控制、可直接执行的渠道信息提取命令，并使其默认产生符合共同流程的脱敏输出。
4. 运行聚焦测试和共同流程要求的仓库级检查。检查完整 diff、未追踪文件和敏感信息；不将运行数据、本地配置或凭据加入提交。
5. 创建范围聚焦的 commit 并推送功能分支。创建或更新 Draft PR，写明待测的完整 commit SHA、一键提取命令和已运行测试。
6. 处理测试反馈时，先核对报告中的 PR 和 commit SHA，再在同一功能 worktree 复现、修复、重测、提交并推送。在 PR 中清楚标记需要测试机重验的新 commit SHA。

不执行 PR 的独立验收，不代替测试机发布通过结论，不将 PR 转为 Ready for review，也不合并。
