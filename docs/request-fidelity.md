# 请求保真专项

入口：准入 → 模型与协议检测 → 检测专项 → 请求保真专项。复用既有预览、真实请求确认、停止、历史30条、JSON和HTML导出。默认 Mock；本功能不自动部署、变更渠道配置、重试或降级。

## 契约

`PlanInput.suite` 默认 `protocol`，可选 `fidelity`；`thinking_mode` 为 `adaptive`（默认）或 `enabled`（预算1024）。`reference_channel_id` 仅在全渠道 fidelity 模式有效，必须是本次启用渠道之一。参照由用户指定，不自动认证为官方渠道。已有请求和历史报告兼容。

每模型最多13次串行 Messages 非流式请求：System精确输出、短长文逐字复述、64输出上限、tools none、指定合成路径、thinking首轮/续轮、thinking工具首轮/工具结果续轮、固定缓存前缀首次/重复/改变前缀。普通协议与SSE检测沿用原专项，保真专项不声称覆盖流式thinking重组。

工具仅生成合成结果，不执行模型返回的工具或访问文件。thinking工具选择为auto，不强制调用；没有合法思考块或工具块时相关续轮记待确认、attempts=0。两类续轮分别独立，工具输入不符合指定路径不能继续。单项尝试最多一次，沿用响应1MiB、首段/空闲/总超时和整批1800秒预算；取消销毁内存中的原始历史，不恢复或补发。

完整assistant content（包括空thinking文本、signature、redacted_thinking、工具块顺序）在内存原样回传，system/tools/thinking及此前messages保持不变。不删除思考块来隐藏失败。不启用drop_block降级。哈希只证明Eval构造请求一致，不能证明转发后没有改变；账号关联、切号、上游重试均不可观察。

## 判定

- thinking 400分为块修改、签名无效、会话前缀绑定和配置不支持；不回显上游错误原文。不能仅凭400归责上游。无思考块可能为模型正常省略，记待确认。
- 计量只比较双方成功且逐字相同的合成正文；不trim空白。端点上报output减端点上报thinking，仅当两个字段均为有效非负整数且thinking不超过output时可用。任一缺失、正文不同、返回模型不匹配或参照未执行均待确认。相对参照偏差超过8%标计量口径异常，不称虚报或恶意；这是版本化筛查阈值，不是官方容差。一个长度一个样本，不做统计显著性、同货源或模型身份证明。
- 指令偏离、禁工具仍调用、路径偏离、超出输出上限分别报告观察事实，不推断隐藏system原文或获利机制。模型自述不作为证据；速度不作为物理上限。
- 缓存usage缺失保留null。token命中率=读/总输入，写比=写/总输入，总输入=未缓存输入+读+写；请求复用率=读>0的有效样本/有效样本。汇总仅纳入成功且三个输入字段完整的样本，显示完整样本数和计划数。逐条显示首次、重复和改变前缀，不保证缓存命中，不判断5分钟/1小时TTL。固定前缀每运行唯一；改变前缀不要求读量必为零。
- 续轮HTTP400率统计所有续轮400，分母为实际发送的两类续轮，不包含依赖跳过；逐项另列thinking错误分类，汇总另列计划数、通过数。完成不等于全部通过。

报告保留白名单指标、请求/正文/回传块哈希，不新增原始正文、签名或错误文本持久化。已有质量准入技术报告的原始证据能力不变。

## 验证

`python -B scripts/inspect_protocol_admission.py`：零请求、零解密检查，含专项定义与指纹。

`python -B -m unittest tests.test_request_fidelity -v`：合成夹具验证块原样、工具往返、400分类、无自动重试、依赖跳过、精确正文过滤、缺失usage、缓存前缀、Mock历史及导出。由既有unittest发现收集，纳入 `scripts/verify_admission.py` 的 workbench-python。浏览器新增专项流程由 protocol-browser 覆盖，沿用全量清单及独立验收要求。

依据（2026-11-05核对）：[thinking往返](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)、[400分类](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)。本地Mock不是实际供应商能力证明。
