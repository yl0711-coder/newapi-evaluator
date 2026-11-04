# 准入测试缓存命中率

准入测试只展示上游响应 `usage` 中明确报告的缓存字段。它不使用本地 tokenizer、字符数或价格推算补齐数据；缺失值保持 `null`，不解释为 0%。缓存信息是技术证据，不参与自动准入结论。

## 三种协议的字段口径

| 协议 | 输入 Token | 缓存命中 | 缓存写入 | 命中率分母 |
| --- | --- | --- | --- | --- |
| OpenAI Chat Completions | `prompt_tokens` | `prompt_tokens_details.cached_tokens` | 未使用 | `prompt_tokens`（已包含缓存命中 Token） |
| OpenAI Responses | `input_tokens` | `input_tokens_details.cached_tokens` | 未使用 | `input_tokens`（已包含缓存命中 Token） |
| Anthropic Messages | `input_tokens`（未缓存输入） | `cache_read_input_tokens` | `cache_creation_input_tokens` | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` |

公式为：

- OpenAI Chat/Responses：`cached_tokens / input_tokens`。
- Anthropic Messages：`cache_read_input_tokens / (input_tokens + cache_read_input_tokens + cache_creation_input_tokens)`。

Anthropic 的 `cache_creation_input_tokens` 是缓存写入，不计入命中 Token；它只进入分母。OpenAI 的 `input_tokens` 已包含缓存 Token，不再把 `cached_tokens` 加回分母。

## 报告展示

- 每个有效评测请求的技术证据展示缓存命中 Token、缓存写入 Token、命中率分母、命中率、字段状态和口径。
- 概览和普通版报告按候选端、参照端分别展示；多题汇总使用输入 Token 加权，而不是简单平均每题百分比。首轮预热不计入汇总。
- `difference` 是候选端命中率减去参照端命中率；任一端缺失时保持 `null`。
- 报告同时提供请求级缓存复用率：`cache_request_hit=true` 的有效请求数 / 已报告缓存字段的请求数；`cache_read=0` 明确表示该请求未命中，不是缺失。
- 报告同时提供缓存写入占比：`cache_write_input_tokens / cache_total_input_tokens`。OpenAI 当前协议写入字段未报告时为 0（该协议的输入总量已经包含缓存 Token）；Anthropic 按 `cache_creation_input_tokens / 完整分母` 计算。
- `cache_status=not_reported` 表示没有可读的输入 usage；`unavailable` 表示有输入 Token 但上游没有缓存字段；`invalid` 表示字段非法、超界、缓存字段不成对或分母为零；这些状态都不能转成 0%。

缓存命中率只说明上游本次响应报告了什么，不能证明缓存实际生效、计费金额或供应商内部路由。真实供应商验证仍须单独授权并保留原始上游证据边界。
