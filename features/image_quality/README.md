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

向上游只发送 model、prompt、size、quality、output_format=png、n=1。Authorization 仅用于用户指定端点，不跟随重定向，不读环境代理、不自动重试。响应体最多 48 MiB；PNG 验证后无损保留压缩像素、位深、调色板、透明度以及结构化色彩参数，移除文本、EXIF 等附加元数据。支持静态 PNG（含 16 位灰度）；含 ICC 配置、HDR 参数或动画控制块的 PNG 返回 unsupported_png_features，避免静默改变画面。仅返回 URL 的响应标记 image_url_only，不自动下载未知地址。

结果 HTTP 200 表示工作台已完成本次处理，不表示上游生图成功；以 status=success/failed、http_status 和 error_code 判断。输入无效为 422，未确认请求为 400；断开连接取消本地任务。上游错误正文不返回；error_code 为固定分类。total_seconds 为服务端总耗时，从开始请求上游至完成图片验证，不含浏览器与工作台之间的传输和页面渲染；upstream_seconds 为收到完整上游响应的耗时，两者单位均为秒。

成功样本包含 Base64 PNG、实际尺寸、下载图片及上游原图各自 SHA256、尺寸匹配标记。请求指纹不含 Key、连接指纹不含 Prompt，两者均不展示原文。返回模型、请求编号和 token 用量由上游自报，缺失或不安全值保留 null；不以字符数代替 token。

## 判断范围

固定提示词、尺寸、质量和网络环境，多次交替采样，按提示词遵循、构图和细节人工评分（1 分低、5 分高）。样本差异可以帮助判断可用性与画面表现；图片、自报模型字段或请求编号均无法单独证明是否调用原厂模型，不生成“真假认证”或自动质量分。

图片只在本轮内存中处理并展示，下载 PNG 去除了文本等非渲染元数据，因此文件哈希可能与上游不同。提示词、Key、Base URL 和原始响应不进入指标导出；评分属于用户主观评价，不与 token 用量或模型身份混淆。

## 只读检查

在本项目解释器和仓库外隔离环境中执行：

    python -m features.image_quality inspect-config --config tests/fixtures/image_quality/config.json

输出协议、固定模型、脱敏主机别名、配置指纹、参数与时间，不读取凭据、不发请求、不创建计划。用户配置文件也只接受 Settings 字段；不要在该配置中保存 Key 或 Prompt。

开发与独立验收入口及全部测试清单见 ../../docs/image-quality-testing.md。
