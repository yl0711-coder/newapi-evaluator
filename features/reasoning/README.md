# 非流式回答测试

工作台版本：从项目根目录执行 `python run.py --app reasoning`，打开 http://127.0.0.1:8093/reasoning/ 。使用根目录的虚拟环境与 requirements.txt。

工作台实际入口为 `api.py`：选择公共渠道，填写本次模型和协议，密钥由后端读取；不再每次粘贴密钥。本轮完整报告可下载 JSON。`main.py` 保留上游响应检查引擎，修改后在根目录执行 `python scripts/test_all.py`。

以下为引擎来源工具的原始说明，用于对照判定规则；其中的原始启动和临时密钥输入方法不适用于工作台入口。

本地单渠道非流式测试工具。粘贴或填写渠道信息，运行 4 道推理题与 1 道短答基线，直接查看上游回答、独立推理字段、usage 和完整原始响应。

## 检查范围

- 每次请求均发送 `stream: false`；检查 HTTP/JSON 是否有效、答案是否非空、结束原因及显式未完成标记。
- 读取 OpenAI 兼容 Chat Completions 的 `message.reasoning_content` 或 Anthropic Messages 的 `thinking` 内容块；单独标记 `redacted_thinking`。
- 逐题默认展开上游返回的答案正文，并提供独立推理文本、完整原始响应和 token 使用量；可下载本轮 JSON 报告。
- 单独展示 `completion_tokens_details.reasoning_tokens` 或 `output_tokens_details.reasoning_tokens`，不把它误认为已返回的推理文本。
- 记录响应头、响应体首字节及完整接收耗时。首字节是非流式 JSON 响应体的首字节，不是首 token。

接口返回的推理可能只是摘要；单渠道响应无法证明模型未公开的内部推理是否完整，也不能证明渠道没有修改内容。正常结束不代表答案正确。工具把答案正文、独立推理字段和 usage 分开呈现，由使用者查看原文并判断正文有没有推导过程。

## 启动与自测

需要 Python 3.10 或更高版本。推荐在项目目录建立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.ps1
```

浏览器打开 http://127.0.0.1:8092 。启动脚本支持从其他工作目录调用。

运行离线自测：

```powershell
.\.venv\Scripts\python.exe selftest.py
```

也可双击 `run_selftest.bat`。自测使用内存模拟上游和应用请求，不占用固定端口、不访问真实渠道、不消耗 API 额度。

## 配置

1. 将 JSON、curl、环境变量或普通连接信息粘贴到导入框，点击“一键提取渠道信息”；也可以手动填写 Base URL、API Key、模型及协议。提取内容不会持久保存，成功后立即清空导入框。
2. 配置最大输出 token 数（默认 8192）和每题总时限（默认 120 秒）。
3. Anthropic 选择模型支持的思考模式：手动 `enabled`、自适应 `adaptive` 或关闭 `disabled`。手动预算至少 1024，且小于最大输出 token 数。所选配置用于所有题目。
4. 点击开始，5 道题按顺序各请求一次，全量结束后展示结果。单次错误会记录并继续后续题目。

OpenAI 兼容协议不自动附加厂商专属思考开关，使用所填模型及渠道的默认行为。当前适配 Chat Completions 与 Anthropic Messages；其他响应协议需另外适配。模型必须支持所选协议和请求参数。

| 输入地址 | OpenAI 兼容请求地址 |
| --- | --- |
| `https://api.example.com` | `https://api.example.com/v1/chat/completions` |
| `https://api.example.com/v1` | `https://api.example.com/v1/chat/completions` |
| `https://api.example.com/api/paas/v4` | `https://api.example.com/api/paas/v4/chat/completions` |
| 完整 `/chat/completions` 地址 | 原样使用 |

Anthropic 按相同规则追加 `/messages`，根地址使用 `/v1/messages`。协议与完整路径必须一致。地址不接受账号密码、查询参数或片段；API Key 单独填写。

## 判定与统计

| 响应状态 | 含义 |
| --- | --- |
| 正常结束 | 非空答案、正常结束原因，无显式未完成标记 |
| 截断／未完成 | `length`、`max_tokens`、上下文限制或显式未完成标记 |
| 空答案 | 报告正常结束，但没有非空最终答案 |
| 待复核 | 缺少正常结束证据，或出现拒答、工具调用等情况 |
| 请求／解析失败 | HTTP 错误、超时、连接中断、无效 JSON 或协议结构错误 |

推理状态只描述独立字段：有独立推理文本、无独立推理文本、含不可见推理、基线不要求或无法检查。空行数量、文本长短、thinking block 数量不参与完整性判定。

`reasoning_tokens > 0` 表示上游报告模型消耗了推理 token；如果同时没有 `reasoning_content` 或 `thinking`，代表这些推理 token 没有作为独立文本返回。答案正文仍可能包含模型写出的解题说明，应直接查看“上游原始回答（content）”进行人工判断。

- 响应正常结束率 = 正常结束题数 / 全部 5 题。
- 独立推理字段返回率 = 返回非空独立推理文本且没有不可见块的推理题数 / 全部 4 道推理题。
- 推理返回且正常结束率 = 同时符合上述推理条件和正常结束条件的题数 / 全部 4 道推理题。
- 失败样本保留在比例分母中；短答基线不参与推理相关比例。
- 最小／中位／最大耗时只统计有效解析的响应。每题仅测一次，这些值只描述本轮样本。

## 数据与运行边界

服务只监听 `127.0.0.1:8092`，不建立数据库，不持久保存 Key 或测试历史。Key 在请求时发送给本地服务，再用于用户指定的上游。浏览器刷新会清空本轮结果；用户主动下载的报告保存在浏览器下载位置。

报告不包含配置中的 API Key。有效 JSON 中回显的同一 Key 在解析后脱敏，必要时重新序列化；无效 JSON 或中断片段仅做文本替换，下载前请复核。上游内容按纯文本展示。响应体最多保留 8 MiB（解压后），JSON 嵌套最多 32 层；超过本地限制的样本标记错误。超时或中断时保留已收到的片段。每题时限覆盖连接、接收响应头和读取响应体的全过程。不自动跟随重定向、不重试失败请求，不读取系统代理配置。

## 协议依据

- [DeepSeek 可见推理字段与思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Anthropic 思考模式、预算规则与摘要说明](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

不同模型支持的思考配置不同；应按模型文档选择手动或自适应模式。
