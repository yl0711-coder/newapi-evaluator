# 生图模型质量测试

工作台路径 /image-quality/；独立启动使用 python run.py --app image-quality，默认端口 8096。运行数据目录按根正式规则设置为仓库外隔离目录；部署时沿用平台配置。

## 使用

填写 Base URL、临时 Key、提示词，选择尺寸、质量和等待上限，逐次确认后生成 1 张图。模型固定 gpt-image-2，格式固定 PNG。本轮最多保留 6 个样本，可并排查看、人工评分、下载 PNG 或导出指标；刷新后清空。

支持根地址（自动附加 /v1/images/generations）、以 /v1 等前缀结尾的 Base URL（附加 /images/generations）、完整 /images/generations 地址。禁止 URL 中携带用户信息、查询参数或片段；内网/回环测试地址必须由部署者配置 PLATFORM_EGRESS_ALLOWLIST。开发验收只对白名单中的本地 Mock 开放。

## 接口契约

POST /image-quality/api/generate 的 JSON 输入：

| 字段 | 规则 |
| --- | --- |
| base_url / api_key / prompt | 本轮输入，不持久化；Key 使用 SecretStr 校验 |
| model | 仅接受 gpt-image-2，默认同名 |
| size | 1024x1536（默认）、1536x1024、1024x1024 |
| quality | high（默认）、medium、low |
| timeout_seconds | 1–600 秒，默认 600 |
| confirm_live | 必须为 true；每次请求独立确认 |

向上游只发送 model、prompt、size、quality、output_format=png、n=1。Authorization 仅用于用户指定端点，不跟随重定向，不读环境代理、不自动重试。本次接收数据最多 48 MiB。UTF-8 JSON 对象/数组的完整边界到达后即用标准 JSON 解析器校验，并关闭本次上游读取，不等待未发出的 HTTP 分块结束标志；同一已收数据中的额外非空白内容仍判无效。JSON 完成不表示已验证 HTTP 尾部，也不读取完成后才发送的额外文档。未请求的 text/event-stream 响应立即返回 unexpected_stream_response；非成功 HTTP 状态在收到响应头后分类，不等待错误正文。PNG 解码后按支持的像素模式重新编码，保留像素值、透明度以及安全的 gAMA/cHRM/sRGB 数值色彩参数，移除文本、EXIF、压缩数据尾随载荷和未使用的调色板内容。支持静态 1/2/4/8 位 PNG 和 16 位灰度；调色板按显示像素转为 RGBA。16 位彩色、ICC/HDR、动画、sBIT/bKGD 以及超出支持范围的色彩参数返回 unsupported_png_features，避免静默降低位深或改变画面。仅返回 URL 的响应标记 image_url_only，不自动下载未知地址。

结果 HTTP 200 表示工作台已完成本次处理，不表示上游生图成功；以 status=success/failed、http_status 和 error_code 判断。输入无效为 422，未确认请求为 400；断开连接取消本地任务。上游错误正文不返回；error_code 为固定分类。total_seconds 为服务端总耗时，从开始请求上游至完成图片验证，不含浏览器与工作台之间的传输和页面渲染；upstream_seconds 为取得待解析 JSON 文档的耗时（未取得时为 null），两者单位均为秒。diagnostics 包含当前/停止阶段、实际接收字节数、响应头与首字节耗时、归类后的内容类型、接收结束依据。response_completion=json_complete 表示 JSON 文档边界完整，不证明上游 HTTP 分块或后续内容完整；transport_eof 表示读到传输结束；http_status 表示仅凭非成功状态码结束。

成功样本包含 Base64 PNG、实际尺寸、下载图片及上游原图各自 SHA256、尺寸匹配标记。请求指纹不含 Key、连接指纹不含 Prompt，两者均不展示原文。返回模型、请求编号和 token 用量由上游自报，缺失或不安全值保留 null；不以字符数代替 token。

## 运行诊断

image_quality.progress 日志仅输出服务端生成的 sample_id、UTC 开始时间、固定事件与阶段、HTTP 状态、字节数、耗时和固定错误分类。正常完成、超时和客户端取消均记录最后阶段；不输出输入、原始响应、图片、返回模型名或上游请求编号。默认写标准错误，部署者可将该命名 logger 接到仓库外有界滚动日志。日志用于定位接收/解析/图片处理卡点，不提供图片恢复或后端样本历史。此前未记录的请求不能事后还原。

## 判断范围

固定提示词、尺寸、质量和网络环境，多次交替采样，按提示词遵循、构图和细节人工评分（1 分低、5 分高）。样本差异可以帮助判断可用性与画面表现；图片、自报模型字段或请求编号均无法单独证明是否调用原厂模型，不生成“真假认证”或自动质量分。

图片只在本轮内存中处理并展示，下载 PNG 去除了文本等非渲染元数据，因此文件哈希可能与上游不同。提示词、Key、Base URL 和原始响应不进入指标导出；评分属于用户主观评价，不与 token 用量或模型身份混淆。

## 只读检查

在本项目解释器和仓库外隔离环境中执行：

    python -m features.image_quality inspect-config --config tests/fixtures/image_quality/config.json

输出协议、固定模型、脱敏主机别名、配置指纹、参数与时间，不读取凭据、不发请求、不创建计划。用户配置文件也只接受 Settings 字段；不要在该配置中保存 Key 或 Prompt。

开发与独立验收入口及全部测试清单见 ../../docs/image-quality-testing.md。
