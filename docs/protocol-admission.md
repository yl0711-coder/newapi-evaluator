# 协议识别与 NewAPI 配置准入

第一阶段把供应商声明、候选端实测能力、NewAPI 类型建议和分组门禁分开记录。入口为准入页面的“协议识别与 NewAPI 配置准入”，地址 `/admission/protocol/`；公共渠道卡片也可带渠道选择进入。

本规格承接用户的《新渠道协议识别与 NewAPI 配置准入说明.md》，目标 New API 为用户确认的官方 `v1.0.0-rc.26`。Alpha Search 按官方 Codex `rust-v0.154.0` 的请求、响应和兼容测试实现，客户端只作为格式参考，不是运行依赖。原说明中的阶段门禁和类型推断按下述规则统一。

## 操作流程

1. 在公共渠道新增或编辑上游来源、供应商说明、确认依据/日期、凭据方式及拟配置类型。现有渠道默认“未确认”；旧客户端省略新增字段时保留已有协议资料。临时候选可直接在协议准入页面填写，同样不保存临时密钥。
2. 填写待售卖模型及精确请求映射，选择目标场景和实际 NexusAPI 分组名称。每批最多 5 个模型、8 个目标分组。多个分组重叠的必测项在同一模型上只执行一次；不同模型分别测试。
3. 预览清单和请求数量，再执行。默认本地 Mock 演示。真实模式必须主动切换并勾选“确认使用内部测试凭据”；API 对应 `mode=live, confirm_live=true`，确认只授权本批预览清单。该新增页面/API 确认契约为本子功能对 G6 的界面落实；本轮开发验收只使用本地 Mock，不请求供应商。
4. 查看模板结论及逐项证据，下载 JSON 或 HTML。两个导出共享同一后端报告，HTML 的完整脱敏证据包含全部 JSON 字段。保存最近 30 次协议报告。公共渠道的连接、密钥或资料版本变化后，旧报告提示重新验证并保留原快照。之后继续现有质量快测和稳定性流程。

## 四套模板

| 模板 | 每个模型的必测项 |
| --- | --- |
| Codex 标准 | Responses 非流式、流式、强制工具调用格式；检查有效 usage 和正常终态 |
| Codex 搜索 | Codex 标准的全部项目，再加独立 Alpha Search |
| OpenAI 通用 | Chat Completions 非流式、流式；usage、结束原因；可追加 Codex 标准的 Responses 项目 |
| Claude | Messages 非流式、流式、工具格式、约 8 KiB 合成长文本请求；usage 和终态 |

长文本项只验证基础接收能力，不声称验证模型最大上下文。工具项核对实际返回的调用 ID、工具名和参数，不冒充已验证完整工具往返。Compact、最大上下文、图像、Embeddings 等专用模板不在第一阶段，不能借相同模型名复用为通过结果。分组模板与必测项由 `features/protocol_admission/catalog.py` 唯一维护，页面从 API 读取。

## 请求与判定

探测每项仅发一次 HTTP 请求，串行执行；不自动重试，不跟随重定向，不采用环境代理，使用现有出站地址白名单。每次最多读取 1 MiB 正文，分别约束响应头/正文首段、空闲和总超时，整批上限 1800 秒。停止会取消在途请求，未执行项保留；进程重启把未结束运行标为 interrupted，不自动恢复发请求。

SSE 接收支持 LF、CRLF、CR 换行及开头 UTF-8 BOM，包括跨块分片。流式请求验证各自的结束信号及事件顺序：Responses 对应 completed/incomplete/failed，Chat 对应 finish_reason 与 `[DONE]`，Messages 对应 block/message 事件。生成截断与传输缺少终态分开分类。usage 缺失不填零。流式 TTFT 为首个可见文本增量的接收耗时，非流式为 null；响应头和总耗时另存毫秒值。

Alpha Search 为 `POST /v1/alpha/search` 的非流式 JSON。包含搜索会话 id、实际模型和 `commands.search_query`，`response_length` 位于 commands。每次运行使用独立合成 RFC 9110 查询，不携带用户会话。响应要求 output 字符串；results 可以缺省/null/空数组，encrypted_output 可以缺省/null。明确的搜索失败前缀或结构化错误优先记失败，即使同时含预期来源链接。只对已识别的 text_result（来源 URL 和相关标题/摘要），或标题/URL 紧接搜索引用的格式，核对 RFC 9110 标准页来源。只有名称和链接的普通正文可能是查询回显，仍记“格式通过、结果待确认”；明确无结果前缀同样保留待确认。该内容检查不证明实时联网或搜索真实性，不解密 encrypted_output。仅有 200 或 RC26 工具计费不算通过。

HTTP 401/403 表示鉴权/访问限制；404/405 默认待核对方法、路径和分组，不直接证明上游缺少端点。上游明确的 unsupported endpoint 错误码或 RC26 本地拒绝可形成确定阻断。400/422 要检查请求及模型。限流、超时、连接错误和 5xx 分别记录，不用于反推供应商程序。

## 类型与分组规则

类型依据供应商文档/人工确认及日期，不能通过一次接口成功认定程序身份。New API、Sub2API 和原生官方来源各自保留声明；Codex 仅在实际接入凭据为 Codex OAuth 时才可给出对应建议。账号池对外提供普通 API Key 而协议未确认时，保持待确认。第一阶段执行器支持供应商 API Key（Bearer 或 Anthropic x-api-key）；OAuth 直连、其他原生协议和高级自定义路由须走后续专项验证，不能声称已经实现相应原生适配。

RC26 Alpha Search 类型限制与候选端能力分别核对。OpenAI 等不在放行名单的拟配置类型，即使候选端返回有效 Alpha Search 结果，也阻止进入搜索分组。来源未确认、拟配置不一致或高级自定义需要人工确认。鉴权、确定不支持、坏响应、流式结束或必测 usage 错误形成对应模板阻断。超时和未知结果不算协议通过。任意检查尚未通过都保留缺口。

本阶段状态为“禁止进入该分组”“仅允许内部测试”“待人工确认”，不会无条件输出“允许灰度”。质量、稳定性门槛和 NexusAPI 实际 channel_id、模型映射、重试链路及计费证据尚未采集，统一标为未评估/未验证。返回模型与请求模型不一致时需复核映射或版本别名，结果保留待确认；精确匹配也不能证明真实模型身份。网关尝试不可观察时为 null；Eval 一次尝试不等于全链路零重试。Mock 结论始终带演示标识。

## 数据与接口

公共渠道增加 `protocol_profile`，SQLite 在原事务中追加 JSON 字段并为历史渠道使用空声明；资料修改沿用原版本冲突检查，不改变密钥留空保留、替换和加密方式。协议资料不接受凭据模式文本、带查询参数的 URL 或本渠道密钥。

新增报告库为 `PLATFORM_DATA_DIR/protocol-admission/reports.db`，独立于现有质量报告。只保存配置声明、模型/分组、渠道稳定 ID 或脱敏主机指纹、请求数量及逐项白名单指标，不保存上游请求/响应正文、临时 Key、完整连接地址或任意上游错误文本。原报告的题目、回答、技术证据及各导出版本保持原有边界。

| API（相对 `/admission/protocol`） | 行为 |
| --- | --- |
| GET `/api/meta` | 模板、错误分类、参考版本 |
| POST `/api/preview` | 模型/分组/资料/超时生成清单与指纹，不发送上游请求 |
| POST `/api/runs` | 预览指纹必须仍匹配；开始一批 Mock 或明确确认的真实探测 |
| GET `/api/runs`、`/api/runs/{id}` | 历史摘要、完整脱敏报告 |
| POST `/api/runs/{id}/stop` | 停止本次运行，不重试 |
| GET `/api/runs/{id}/export/{json,html}` | 后端报告导出 |

公共协议选项为 `/api/registry/protocol-options`；渠道 CRUD 的 `protocol_profile` 为可省略的嵌套字段。协议 API 拒绝未知字段及超过 64 KiB 的请求体，验证失败不回显输入。页面继承工作台鉴权和同源保护。不自动写 NewAPI 数据库、渠道、权重、分组或禁用状态，也不发送外部通知。

## 验证与源码依据

只读配置检查：`python -B scripts/inspect_protocol_admission.py`。可选 `--data-dir` 指向已授权的公共渠道目录，只读 channels.db 中安全字段，不解密、不初始化数据库、不发请求。完整检查清单见 [协议准入测试清单](protocol-admission-testing.md)。

- [Codex 请求/响应定义](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/codex-api/src/search.rs)
- [Codex 序列化及兼容测试](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/codex-api/src/endpoint/search.rs)
- [RC26 Alpha Search 转发和计费](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.26/relay/alpha_search_handler.go)
- [RC26 渠道名称及类型编号](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.26/constant/channel.go)
- [RC26 Codex 适配器](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.26/relay/channel/codex/adaptor.go)

SSE 分行依据：[WHATWG Event Stream 解析规则](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)。
