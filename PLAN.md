# 独立诊断部署与可控报告

目标：将独立诊断工具维护在 `nexusapi-channel-diagnostic`，提供独立版本、镜像、容器和报告页控制；不替换 Eval、Monitor、API 或 NewAPI。

工作目录：`/Users/lmurder/Desktop/api中转站/小时渠道诊断独立版-v1.1.0`；本地发布分支 `release/v1.1.0` 从目标仓库 `feature/standalone-release` 创建。目标仓库的 `v1.0.1` 基线为 `ca0e475b34eb4042a7ef66dc3bdd808a4188611b`，本候选移植报告控制改动 `32afadcb0bec6754f04a319c15042da1affa03c7` 与 `b408af635d585c03506be40066a6bd5c0384df57`。

规范：仓库 `AGENTS.md`、仓库内 `docs/ai-rules` 及父目录 `/Users/lmurder/Desktop/api中转站/AGENTS.md` 引用的正式规则。

范围：代码库身份、构建发布、独立生产 Compose、报告前端控制区、FastAPI 控制服务、资源限制、部署文档、回归夹具和完整离线验收。保留原始诊断矩阵与默认请求量；控制服务只接受部署令牌，不保存或返回凭据、请求正文。

不改 Eval/Monitor/API/NewAPI，不启动真实检测；16 个渠道保持停用，配置和凭据只在私有目录，报告服务默认回环监听。

验收入口：TESTING.md；生产流程：deploy/README.md。
本机证据：/private/tmp/channel-diagnostic-release.ECXbpt；首次 Python3.14 HTTP 夹具失败记录保留，
根因是解析器对深层数组的处理不同和测试归并同类错误时覆盖计数；仅修测试断言与计数，不改业务。

证据根：`/Users/lmurder/Desktop/api中转站/中转站极限测试数据/hourly-diagnostic-fix-zu5uem8m`。`check-1` 为首次沙箱受限记录；`check-2` 为中间版本通过记录，不能替代最终候选。

验收入口：从该候选提交检出，按 TESTING.md 执行完整离线清单、容器检查和源代码审查；交付以外置 HANDOFF.md 中对应实际 SHA 的记录为准。

未验证：真实渠道质量、令牌配置、容器权限、长期整点运行、独立域名 DNS/TLS/鉴权和收费请求；本次离线 Mock 结论不代表渠道可用。
