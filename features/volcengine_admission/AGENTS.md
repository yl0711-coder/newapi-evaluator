# 火山新 Key 一次性验收专项规则

继承根 AGENTS.md 与 docs/ai-rules v1.0。本模块为工作台独有功能，维护 Python/FastAPI 后端与现有 HTML/JS/CSS 前端，不扩展固定 gpt-image-2 的 image_quality 专项。

- 仅允许目录中精确的 11 个型号；默认 6 款、补充 5 款存量兼容型号。单次每型号只发送一次单图/单视频生成，不创建持续监控、计划、通知或公共渠道。
- 默认 Mock。实际 Key 仅内存，主站固定 `https://ark.cn-beijing.volces.com/api/v3`，不提供任意 Base URL。界面单次“确认只读查询”对应 `confirm_read=true`；界面单次核对预览后“确认按该预算发送一次生成请求”对应 `confirm_live=true` 且绑定有效 `preview_id`/`fingerprint`。提交即复位确认并清空 Key。这是本模块的具体界面确认入口，不授权开发、自动化验收或本轮代理真实调用。
- 逐项 POST 许可先持久提交 `sending`，结果不明记 `unknown`，禁止重发生成。重启后未发送项停止，保留已提交 task ID；补原 Key 仅继续 GET 已知视频，不能补发任何 POST。停止只停后续本地发送/轮询，不声称上游已取消，不执行 DELETE。
- Key、Prompt、签名媒体 URL、上游自由文本及原响应不进入数据库、报告、日志或开发证据。只持久化本地编号、官方精确型号/规格、合同摘要、固定状态/错误类别、HTTP、白名单 usage 和指标。历史任务列表响应体直接丢弃。媒体签名 URL 仅在下载期间存在内存，通过受父工作台鉴权的本地媒体路由展示/下载。
- 幂等标识只存摘要；Prompt 摘要/长度为私有字段，仅用于恢复时拦截回显。上游 task ID/媒体 URL 含 Key/Prompt（包括常见转义）必须拒绝。MP4 只验证自包含非分片单视频轨道的 H.264/H.265 配置、采样表、偏移与 NAL 边界，清理非播放元数据/SEI 文本；清理后整个文件检查本次 Key、Prompt、当前下载签名 URL 的 UTF-8 文本及常见百分号/HTML/Unicode 转义回显，残留拒收、扫描超限无法判定，不识别画面文本或任意隐写。不把结构验证称为解码、播放或人工看完。
- 禁止系统代理、重定向和自动重试；复用 shared.network 的 DNS/实际建连防护。媒体只接受 HTTPS 公网目的地及合法媒体类型，禁止带 Ark Authorization/Referer 到媒体源，限制时间、字节、像素、总容量和文件数；不猜测不受官方保证的通配 CDN 域。媒体请求绕开会记录签名 URL 的高层 HTTP 日志。
- 仅任务列表 HTTP 成功叫“该端点鉴权通过”，不证明官 Key 来源或模型开通。视频须终态成功且样片本地保存才能本项通过；task ID 只表示提交。usage 刊例价估算与真实账单分开；默认 `billing_status=unverified`，实际扣款为 null。单次样本不声称稳定性或原厂模型身份。
- 新运行数据在 `PLATFORM_DATA_DIR/volcengine-admission/`；目录 0700、SQLite/本地媒体文件 0600，SQLite WAL/FULL。保留最近 30 条终态报告，媒体上限 512 MiB/200 文件，每视频 128 MiB、每图片 24 MiB。开发/验收必须映射到本次外置隔离目录，不读取线上业务库或真实 Key。
- 运行目录独占进程锁必须在数据库初始化/recover 前取得；第二实例固定脱敏失败关闭。每项付费 POST 前原子预留最大媒体容量/文件位，不足则 not_sent，绝不先付费后才处理已知满容量。停止/重启释放虚拟预留，GET 恢复重新预留。页面清理通过 `confirm_local_delete=true` 逐次确认，只清当前终态报告的本地样片，保留报告/任务 ID，不调用任何上游 DELETE。
- GET `/api/meta` 保持静态零服务/零数据库初始化；本地容量通过独立 GET `/api/capacity` 读取，服务初始化与独占锁边界明确分开。

测试由 `tests/test_volcengine_admission.py` 提供，完整注册/独立验收由 Main 统一维护。
