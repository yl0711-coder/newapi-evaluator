# 模型与协议检测

从首页“模型与协议检测”卡片或顶部同名导航进入，也可从公共渠道的“获取上游模型与检测协议”、渠道卡片或单个模型的“检测协议”进入。独立地址保持 `/admission/protocol/`，准入页面也提供入口。此功能回答：上游列出哪些模型，选定模型的哪些接口实测通过。无需供应商、NewAPI 类型、确认日期、分组或模型家族配置，不输出接入类型建议或业务放行结论。

首页和全局导航共用 `/api/platform` 的工具入口清单；在 `all` 或 `admission` 模式展示本功能，未加载准入模块时不展示。健康接口的 `features` 仍表示已启动的模块，不随导航子功能增加。

## 操作流程

1. 选择已保存渠道，或填写临时 Base URL 和 API Key。默认 Mock 演示；真实请求需切换模式并明确勾选确认。临时密钥只用于页面和在途请求，不写入数据库或报告；检测开始后页面清空密钥。
2. 点击“获取上游模型”。此操作只读取列表，不自动进行模型调用。已有公共渠道会载入上次成功获取的列表，不发送上游请求；缓存与原常用模型覆盖模块共用，连接变化后失效。
3. 从列表勾选最多5个模型，也可手动输入实际模型 ID。列表支持搜索，每次最多显示200条；未显示的模型可通过搜索选择。列表名称仅是上游声明，不能当作实测支持。
4. 预览本批请求数量，再开始检测。每个渠道的每个模型固定9项；单渠道选5个模型最多45次请求，全部渠道模式按参与渠道数相乘，逐项串行执行。模型列表获取不计入该检测批次。
5. 查看“模型 × 协议”结果表，展开工具、搜索和逐请求明细；支持历史、JSON和HTML下载。

## 获取模型列表

复用 `features/model_coverage/discovery.py`，GET `/v1/models`。先用 Bearer；只有401/403时尝试 x-api-key 与 anthropic-version，不依据模型名称推断协议。每种鉴权最多20页、30秒；单页最多2MB，总模型数最多10000。通常一次请求完成，极端分页和鉴权切换不超过40次请求、60秒。跳转、环境代理和自动重试关闭，遵循现有出站地址保护。

解析、分页、错误归类、密钥及模型名校验由同一实现负责。公共渠道获取结果写入现有发现快照；失败保留历史成功快照及错误状态。临时候选的列表只保留在当前页面，不新建渠道、巡检目标或计划。接口不提供列表时可手动输入模型继续测试。列表获取失败不等于该渠道没有模型。

## 检测项目与结果口径

| 协议 / 能力 | 项目 |
| --- | --- |
| OpenAI Chat Completions | 普通 JSON、SSE 流式 |
| OpenAI Responses | 普通 JSON、SSE 流式、强制工具调用格式 |
| Anthropic / Claude Messages | 普通 JSON、SSE 流式、强制工具调用格式 |
| Alpha Search | 独立搜索 JSON 请求，核对已识别来源记录 |

定义唯一维护于 `features/protocol_admission/catalog.py`。协议“支持”表示本次至少一种普通调用方式（非流式或流式）通过；流式、工具和搜索分别列明，不把工具或搜索失败扩大成所有普通调用失败。工具项核对调用ID、名称和参数，不代表完整工具往返。

- 支持：本次对应探测通过。Mock 结果始终标记为演示。
- 不支持：该协议已执行的普通/流式项目均收到明确不支持的错误，且没有未执行项目。
- 未通过：格式、有效输出或流式结束等明确校验失败。
- 待确认：鉴权、路径、参数、限流、网络、超时、返回模型不一致等未能确定能力的情况；普通404/405不直接视为不支持。
- 未检测 / 检测中：没有完成实测。未选择的模型没有通过结论。

只覆盖上述接口与已选择模型，不推断所有原生协议、真实模型身份、回答质量、长期稳定性或生产可用性。未提供usage不会填零；可见有效响应与调用结束信号分别校验。返回模型不同需核对供应商的模型别名。

## 协议与运行边界

每项最多一次HTTP尝试，不跟随重定向、不采用环境代理。响应上限1MiB，默认响应头/正文首段及空闲超时各10秒，每项总超时30秒；整批最长1800秒。停止取消在途请求并保留未执行状态；进程重启将未完成报告标为 interrupted，不自动补发。

全部渠道模式以启动时确认的渠道和探测清单为准；启动后新增渠道不进入本轮。每项探测开始前核对渠道是否仍启用且版本未变；发现变化后，该渠道余项记为 `channel_changed`（待确认），整轮标为 interrupted，其他未变化渠道继续。已开始的请求不强制中断。

SSE支持LF、CRLF、CR和开头UTF-8 BOM，包括跨块分片。分别校验Responses completed/incomplete/failed、Chat finish_reason与DONE、Messages事件序列及终态。合法生成截断与传输未完成分别归类。流式TTFT以首个可见文本增量计，非流式为null；耗时单位毫秒。

Alpha Search 的请求和可选响应字段依据官方 Codex `rust-v0.154.0`，不依赖安装Codex客户端。固定使用独立合成RFC9110查询。明确搜索错误优先；只有已识别的 text_result 来源记录（URL及相关标题/摘要），或标题/URL紧接搜索引用的格式，才判定结果通过。results缺省/null/空数组合法；仅回显链接、未知正文或无结果保持待确认，不解密encrypted_output，也不证明实时联网真实性。

## 数据和兼容

报告仍位于 `PLATFORM_DATA_DIR/protocol-admission/reports.db`，保留最近30条。新报告version=2，仅保存脱敏连接标识、模型、逐请求指标和协议能力结果，不含供应商/分组配置、URL、临时Key或上游正文。HTML和JSON使用同一后端结果。历史version=1报告保留原快照和已有字段，读取时由原探测证据生成协议表；不删除历史报告或改写原存储快照。

公共渠道旧版 `protocol_profile` 字段及校验仅保留已有数据/API兼容，页面不再编辑它，省略字段继续保留原值。原准入质量、稳定性、非流式和极限测试的报告边界不变。协议探测结果不自动改写常用模型的长期稳定状态，不创建计划、不写NewAPI配置、不发送通知。

| API（相对 `/admission/protocol`） | 行为 |
| --- | --- |
| GET `/api/meta` | 协议、每模型项目数量和错误分类 |
| GET `/api/models?channel_id=…` | 读取已保存渠道的模型列表缓存，不请求上游 |
| POST `/api/models` | 获取临时或公共渠道模型列表；真实模式需confirm_live |
| POST `/api/preview` | 选择模型后生成请求清单和指纹，无上游请求 |
| POST `/api/runs` | 校验预览指纹及本次真实请求确认，执行检测 |
| GET `/api/runs`、`/api/runs/{id}` | 历史摘要及模型协议结果 |
| POST `/api/runs/{id}/stop` | 停止本次检测 |
| GET `/api/runs/{id}/export/{json,html}` | 下载同一脱敏报告 |

新增模型列表请求与探测请求均拒绝未知字段及超过64KiB的请求体，错误不回显输入；继承工作台鉴权及同源保护。`groups`与`profile`不再属于新探测输入。

## 验证和格式依据

只读入口：`python -B scripts/inspect_protocol_admission.py`。测试清单见 [协议检测测试清单](protocol-admission-testing.md)。本轮开发和独立验收只用合成Mock及localhost，不访问实际供应商。

- [Codex搜索请求及响应](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/codex-api/src/search.rs)
- [Codex兼容测试](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/codex-api/src/endpoint/search.rs)
- [Alpha Search转发格式参考](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.26/relay/alpha_search_handler.go)
- [SSE事件流解析](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)
