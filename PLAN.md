# 独立渠道诊断部署

目标：将已有独立诊断工具迁入 nexusapi-channel-diagnostic，独立版本、镜像、容器和域名；不替换 Eval。

工作目录：脚本/nexusapi-channel-diagnostic；分支 feature/standalone-release。
基线：原独立工具 0d273a26981c7d6afb7eae81bd74005bc44d4e43（原仓库 v1.6.4），不是线上工作台 v1.6.3 的后续版本。
规范：根 AGENTS.md 和仓库内 docs/ai-rules，按 AGENTS 的独立工具适用范围执行。

范围：代码库身份、构建发布、独立生产 Compose、资源限制、日志轮转、部署文档及离线验收。
不改诊断矩阵/协议/业务计算，不动现有 Monitor/Eval/API/NewAPI，不启动真实检测。
16 个渠道保持停用，配置和凭据只在私有目录；报告服务默认回环监听。

验收入口：TESTING.md；生产流程：deploy/README.md。
本机证据：/private/tmp/channel-diagnostic-release.ECXbpt；首次 Python3.14 HTTP 夹具失败记录保留，
根因是解析器对深层数组的处理不同和测试归并同类错误时覆盖计数；仅修测试断言与计数，不改业务。

当前待办：最终独立复核、候选提交绑定、确认仓库公开/私有后推送、CI 安全门禁、按固定摘要部署、独立域名 DNS/TLS/鉴权验收。
未验证：真实渠道质量、收费请求和长期检测容量。本次只做报告部署，不能把 Mock 结论当成渠道可用。
