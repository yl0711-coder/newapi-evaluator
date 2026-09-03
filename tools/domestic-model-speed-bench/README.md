# 模型渠道速度快测

一个独立、本地、无数据库的极简工具台。固定用 5 道题（3 易、2 难）让待测渠道与参照端同时发起流式请求，快速比较响应速度。参照端既可以是模型厂商官方接口，也可以是已经上线的渠道。

## 看什么

- **首包延迟**：从发出请求到收到第一段回答或推理内容，体现“等多久才开始说话”。
- **首答延迟**：从发出请求到收到第一段正式回答内容；模型仍在推理时保持为空。
- **总耗时**：完整流结束所用时间，会受到回答长短影响。
- **Token/s**：仅使用上游 usage 返回的真实输出 token 数计算，口径包含推理；未返回 usage 或整包返回时显示为空。
- **字符/秒**：从首段内容到末段内容的可见字符速度，作为上游不返回 usage 时的辅助指标。
- **P50 / P95**：P50 代表典型表现，P95 用来观察慢请求和线路抖动；多轮测试时比单次平均值更有参考性。

每题两端同时发起，5 道题依次执行，减少网络时段差异和并发过高造成的干扰。工具不做正确率评分；推理题的内容可展开人工查看。

两道难题允许最多 4096 个输出 token，避免推理内容被过早截断。若上游仍因输出上限截断、返回空内容，或使用了工具无法识别的流式格式，页面会明确标注原因，并且不会把该次结果计入速度均值；无法识别的响应会在回答区原样显示，便于直接检查上游实际返回内容。

## 启动

需要 Python 3.10 或更高版本。

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pip install -r requirements.txt
.\start.ps1
```

浏览器打开 `http://127.0.0.1:8091`。

## 使用

1. 选择模型预设。
2. 可直接粘贴渠道连接信息并提取 URL、Key，也可以手动填写。
3. 在“参照来源”中选择官方接口或已上线渠道。国产模型可直接使用预置官方接口；Codex、Claude 使用已上线渠道作为参照。
4. 需要维护标杆时，打开“管理已上线渠道”，保存渠道名称、URL 和 Key。之后可直接选作参照端。
5. 点击“开始 5 题快测”。默认只跑 1 轮；需要看稳定性时，在“高级测试设置”中改为 3 轮或 5 轮。
6. 完成后可查看本机历史，或导出 JSON / HTML 报告。

Codex、GLM、Kimi、DeepSeek 使用 OpenAI-compatible Chat Completions 流式协议。Base URL 可填完整的 `/chat/completions`，也可只填服务根域名：DeepSeek 自动使用根 API，智谱 GLM 自动补为 `/api/paas/v4`，其他 OpenAI-compatible 渠道默认补为 `/v1`。如果渠道采用非标准路径，直接填写其完整 API 根路径或 `/chat/completions`。Claude 使用原生 Anthropic Messages 流式协议：Base URL 可填服务根路径、`/v1`，或完整的 `/v1/messages`；请求会使用 `x-api-key` 和 `anthropic-version`，不会套用 OpenAI 请求格式。

当前内置 Codex 家族包含 GPT-5.6 Sol、GPT-5.6 Terra；Claude 家族包含 Fable 5、Opus 5、Sonnet 5；同时保留 GLM、Kimi、DeepSeek 测试模型。模型 ID 会随上游变化，若渠道实际名称不同，直接修改两端的“请求模型”即可。国产模型官方端点参考：

- [智谱 OpenAI 兼容文档](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)
- [Kimi API 概述](https://platform.kimi.com/docs/api/overview)
- [DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)
- [Anthropic Messages 流式协议](https://platform.claude.com/docs/en/build-with-claude/streaming)

## Key 与数据

- 服务只监听 `127.0.0.1`。
- 待测渠道 Key 和粘贴内容只在本次页面中使用，不写文件、不进浏览器存储。
- 官方 Key 仅在你明确点击保存后，按智谱、Kimi、DeepSeek 分别写入当前浏览器的本地站点存储；可以随时点击“清除保存”。
- “已上线渠道库”中的名称、URL 和 Key 会在你点击保存后写入当前浏览器的本地站点存储；删除渠道即可一并清除。
- 每次完整测试会自动保存一份脱敏报告到当前浏览器，最多保留 12 份。报告包含去除账号、查询参数和锚点后的地址、速度指标与末轮回答，不包含任何 Key。
- 为避免浏览器存储过大，历史报告中的单份末轮回答会限制保存长度；页面实时显示不受此限制。
- 工具关闭访问日志；刷新页面后待测渠道配置消失，已保存的官方 Key、已上线渠道和脱敏报告会保留。
- 页面输出使用纯文本渲染，不执行模型返回的 HTML。

## 自检

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" selftest.py
```
