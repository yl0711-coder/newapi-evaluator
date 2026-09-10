# 中转站极限测试实验室

独立的 Python 3.11+ 异步 HTTP 测试工具，统一 CLI 覆盖单号、号池、网关、长任务与故障注入。默认自动启动本地 Mock，通过真实 loopback HTTP/SSE 测量运行行为。

**本阶段只有 Mock 验证，没有真实账号、真实 API Key 或真实 Sub2API 号池。Mock 容量不是生产容量，也不代表 Sub2API 的实际调度效率。**

## 路径与一键运行

开发项目：`/Users/lmurder/Desktop/api中转站/中转站极限测试实验室`。
独立验收副本：`/Users/lmurder/Desktop/api中转站/中转站极限测试-验收副本`。
所有依赖环境、运行结果、日志、检查点和临时数据：`/Users/lmurder/Desktop/api中转站/中转站极限测试数据`。

本机已提供离线缓存，可以用明确的 Python 3.13 解释器创建独立环境；脚本仅从本地 pip 缓存提取 wheel，并用 `--no-index` 安装，不访问包索引：

```sh
cd '/Users/lmurder/Desktop/api中转站/中转站极限测试实验室'
/opt/homebrew/opt/python@3.13/libexec/bin/python3 scripts/offline_setup.py --env '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/dev-env313'
./relay-lab account-test --config configs/account.yaml
```

最后一条就是一键启动单号测试，会自动启动并关闭本地 Mock，同时生成 `results.jsonl`、`summary.json` 和 `report.md`。不传 `--output` 时在专用数据目录生成新的唯一运行目录。已有结果目录不覆盖。

`./relay-lab` 默认使用上述 dev-env313；其他环境用 `RELAY_LAB_PYTHON` 指定解释器。依赖以 requirements.txt 为准。缓存中的 PyYAML wheel 与 Python ABI/平台相关；其他机器需预先准备匹配平台的离线依赖，不自动联网下载。

全部示例一键运行并核对结果：

```sh
PYTHONDONTWRITEBYTECODE=1 '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/dev-env313/bin/python' scripts/e2e.py --output '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/my-mock-e2e'
```

## 五种测试

| 模式 | 测什么 | 不能由该结果推出什么 |
| --- | --- | --- |
| `account-test` | 单账号可用性、连续成功率、TTFT、总耗时、SSE 完整率、速度、并发阶梯和限流恢复 | 通过一个账号不能证明整个号池容量 |
| `pool-test` | 独立账号状态、逐级增号、不同每号并发/延迟/故障率、单号失效、半数退出、扩容效率 | Mock 调度不等于 Sub2API 调度 |
| `gateway-test` | 排除账号瓶颈后网关的稳定在途量、吞吐、排队、连接错误、资源、降速与短暂不可用后的恢复 | 本机 Mock 结果不是实际网关的技术上限 |
| `long-task-test` | 单次持续 SSE 与多个顺序步骤；指定步骤故障、检查点、重试、跨进程续跑及最终完整性 | 不能推断小时级或天级真实任务可靠性 |
| `chaos-test` | 各类可控故障及解除后的恢复 | 不代表真实平台所有故障语义 |

```sh
./relay-lab account-test --config configs/account.yaml
./relay-lab pool-test --config configs/pool.yaml
./relay-lab gateway-test --config configs/gateway.yaml
./relay-lab long-task-test --config configs/long-task.yaml
./relay-lab chaos-test --config configs/chaos.yaml
./relay-lab report --output '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/某次运行目录'
```

账号必须先通过单号质量门槛再考虑入池。`admission_gate_passed` 表示测得的低并发成功率和完整率达标；若同时 `admission_gate_low_confidence=true`，该判断仍需补充样本。默认单号阶梯 1、2、3、5、8、13，网关示例阶梯 10、20、50、100、200、400、800、1200，均可配置。

每阶段请求数为 `max(samples, concurrency × rounds_per_stage)`，默认至少两轮，避免刚触及阈值就结束。所有任务实际经异步 HTTP；队列不会凭空产生成功记录。`samples`、`min_samples`、`connection_limit`、超时和恢复探测间隔均可配置。

Mock 号池采用轮转并跳过不可用/满载账号，每个账号各有 active、capacity、延迟、抖动、随机故障率和禁用/冷却截止时间。随机种子可固定，但操作系统调度、延迟和极限点不会写死。

## 极限定义和证据边界

| 字段 | 计算方式 |
| --- | --- |
| `baseline` | 同一场景最低阶梯成功请求的 P95 延迟、并发和样本数 |
| `slow_point` | 首个成功请求 P95 超过 baseline 两倍的阶梯 |
| `unstable_point` | 首个成功率或完整率低于 99% 的阶梯 |
| `rate_limit_point` | 首个 429 比例达到 5% 的阶梯 |
| `collapse_point` | 连续至少 collapse_streak（默认 3）个不可用响应、连接错误或连接池耗尽的阶梯 |
| `max_stable_concurrency` | 成功率和完整率均至少 99%、P95 不超过 baseline 两倍且未崩溃的最高已测阶梯 |
| `recovery_time` | 停止压力或解除故障后，到连续 recovery_successes 个完整成功探测的实测秒数；阶梯测试另要求延迟回到 baseline 两倍以内 |

`null` 表示未观察到或未测得，不能读作零。`range_censored=true` 表示最高已测阶梯仍稳定，工具尚未找到上限。P50/P95/P99 使用排序后的线性插值。成功请求延迟和全部请求 P95 分开记录，快速返回的 429 不会拉低成功请求的 P95。

请求成功需要 HTTP 200、有效响应格式、非空输出和完整结束语义。SSE 同时要求 finish_reason=stop 与 `[DONE]`；中途断开、格式损坏或缺少结束事件均失败。TTFT 以首个非空内容片段为准；流输出速度以首片段之后的字符/秒计算，**不把字符冒充 token**。

每组不足 `min_samples` 时标 `low_confidence`。默认 100 是最低样本提示，不是“99% 置信度”证明；样本有关联、负载持续时长不足或平台未接入时，不能夸大结果。长任务只有并发 1，顺序步骤与长流采用各自的基线，不能把输出更长造成的耗时误当成并发降速。

号池额外输出 configured_capacity（全部配置并发之和）、available_configured_capacity（当前健康账号配置容量）、observed_capacity（最高稳定阶梯内服务端实测在途峰值）、capacity_utilization（observed/configured）、scaling_efficiency（对最小账号组线性扩容的效率）及健康/失败账号数。max_stable_concurrency 是客户端并发阶梯，observed_capacity 是服务端实测并发，客户端发不满时两者可能不同。每个账号数量都分别测试 healthy、one_failed、half_failed。利用率不是实际调度器内部利用率。

网关资源按 20ms 采样。CPU 为本进程消耗折算单核百分比，内存为进程生命周期峰值 RSS，FD 为采样得到的打开描述符数，客户端连接数和连接池上限同时输出。嵌入 Mock 与客户端在同一进程，`resources.scope` 会明确此范围。外部目标仅测客户端资源，远端 CPU/内存/FD 标记为不可用，需未来配合部署监控，不能从客户端推断。

Mock 达到 slow_at 后增加延迟，达到 crash_at 后短暂 503 不可用，crash_duration 到期恢复。并不真的杀死进程。collapse_point 必须有连续不可用证据，可能高于配置触发阈值；资源采样峰值也可能错过极短峰值。真实进程退出只能在未来隔离部署中结合进程监控验证。

## 长任务恢复

长任务示例含约 15 秒持续 SSE；可通过 stream_chunks、stream_chunk_delay 和 timeout 延长。每个步骤有稳定哈希 ID，SQLite WAL + FULL 同步保存已确认成功的检查点；同一数据库使用文件锁阻止两个执行器并行重复执行。

```sh
./relay-lab long-task-test --config configs/long-task.yaml --task-id demo-task --checkpoint '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/demo/checkpoint.sqlite3'
```

重复使用相同 task-id、checkpoint 和任务配置会跳过已确认成功的步骤。每次输出目录必须新建。`auto_resume: false` 可让第一次失败后结束，下一次命令恢复；`max_retries` 限定每次运行的重试预算。首次失败步骤、每步尝试次数、成功恢复次数和总恢复耗时持久化。任务步骤数、输出规格、目标或模型改变时拒绝复用旧检查点。

检查点不保存响应正文，只保存内容散列、字符数和确认状态；最终完整性验证覆盖步骤齐全、各步成功与摘要。Mock 另用预期合成内容散列核验。`Idempotency-Key` 传给上游，但不假设真实上游支持去重：已确认成功步骤不重跑；响应丢失、成功响应后本地提交前进程被强杀等未确认状态仍可能重试。真实业务若有副作用，未来适配需上游幂等契约。本阶段只有合成推理请求。

Ctrl+C 或 SIGTERM 会停止派发新请求，等待当前请求结束或超时，然后保存部分 JSONL、summary 和 report；最长等待与配置请求超时有关。若被 SIGKILL，可从已落盘的完整 JSONL 行重建部分报告，撕裂尾行不计为样本，原始 wall-clock 吞吐不可恢复时明确标记。

## 故障配置

`configs/chaos.yaml` 包括 401、429、500、连接超时、读超时、断网、SSE 格式损坏、半途断流、缺少结束事件、慢 SSE、抖动、账号临时禁用、部分账号失效和整个上游暂时不可用。账号与上游故障按 fault_duration 自动恢复。HTTP/SSE 注入作用于当前故障阶段，后续恢复探测不再携带故障指令。

连接超时在 Mock 客户端传输层经过指定等待后注入，模拟建连失败且不发送该请求；读超时由本地 Mock 真正延迟响应触发。故障种类在报告中保留，不能称作真实网络链路故障验证。

## 独立 Mock 服务和未来 Sub2API 接入

```sh
./relay-lab mock-server --config configs/mock.yaml --port 8877
./relay-lab account-test --base-url http://127.0.0.1:8877/v1
```

默认连接前验证 loopback Mock 身份。localhost 被固定为数值回环地址，禁止 URL 凭据、查询串、重定向和环境代理，并用 socket audit 再次阻止外部连接。号池拓扑实验和 chaos 控制当前需要嵌入 Mock，不向外部服务开放注入管理接口。

未来经过用户授权后，可用 OpenAI 兼容 `/v1/chat/completions` 接入 Sub2API 测试部署：在仓库外配置 base_url、model、timeout，通过安全环境注入 RELAY_LAB_API_KEY，并显式添加 `--confirm-live`。代码不会自动加载 `.env`。示意命令（本阶段未执行）：

```sh
./relay-lab account-test --config /安全的仓库外路径/live.yaml --confirm-live
./relay-lab gateway-test --config /安全的仓库外路径/live.yaml --confirm-live
```

真实长任务需将 fail_attempts 设为 0。现阶段未实现对真实 Sub2API 账号管理/调度的控制接口，未实现远端资源采集或真实进程崩溃注入；这些是未来独立授权的适配工作。不得对生产部署发送破坏性请求。

## 文件与验证

源代码在 relay_lab/；配置在 configs/；自动测试在 tests/；scripts/ 提供完整测试、五模式 E2E、安全扫描、范围指纹与独立验收。JSONL 的“原始”指请求级指标，不指原始请求/响应。

```sh
PYTHONDONTWRITEBYTECODE=1 '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/dev-env313/bin/python' scripts/test_all.py
PYTHONDONTWRITEBYTECODE=1 '/Users/lmurder/Desktop/api中转站/中转站极限测试数据/dev-env313/bin/python' scripts/repo_security_scan.py .
./relay-lab inspect-config --config config.example.yaml
```

独立验收必须 clean detached HEAD、准确 40 位 SHA、无远程，使用独立离线环境；执行 `python scripts/acceptance.py --sha <完整SHA> --output <全新数据子目录>`。该命令独立运行聚焦、完整、安全和五模式 E2E 检查，输出 acceptance-report.md、acceptance.json 和示例原始结果/汇总/报告。发布报告前再次校验 HEAD 不变及工作树干净。

项目只在 feature/mock-capacity-lab 本地开发；main 保留初始空基线，不添加远程、不 push、不创建 PR、不合并。参考工作流所在旧项目保持只读。范围审计记录既有兄弟项目源码指纹和 Git 状态指纹。

中转站 Skill：`/Users/lmurder/.codex/skills/relay-station/SKILL.md`。以后调用：

```text
$relay-station 继续开发中转站极限测试
```
