# 中转站极限测试实验室

独立的 Python 3.11+ 异步 HTTP 测试工具，统一 CLI 覆盖单号、号池、网关、长任务与故障注入。默认自动启动本地 Mock，通过真实 loopback HTTP/SSE 测量运行行为。

**代码交付采用独立 Mock 验收。控制台可在用户显式确认后测试真实接口；历史真实请求不代表新版本已完成真实验证。Mock 容量不是生产容量，也不代表 Sub2API 的实际调度效率。**

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

独立验收必须 clean detached HEAD、准确 40 位 SHA，使用独立离线环境；仅允许已授权的 origin，验收不修改远程。执行 `python scripts/acceptance.py --sha <完整SHA> --output <全新数据子目录>`，独立运行聚焦、完整、安全和五模式 E2E 检查，输出 acceptance-report.md、acceptance.json 和示例原始结果/汇总/报告。发布报告前再次校验 HEAD 不变及工作树干净。

项目在 feature/mock-capacity-lab 开发；用户已授权同名分支推送至 yl0711-coder/newapi-evaluator。本地 main 保留初始空基线，不创建 PR、不合并，参考工作流所在旧项目保持只读。

中转站 Skill：`/Users/lmurder/.codex/skills/relay-station/SKILL.md`。以后调用：

```text
$relay-station 继续开发中转站极限测试
```

## 持续并发与输出占用

本地页面选择“持续并发”，填写并发阶梯、每阶持续时长及请求上限。每个执行槽结束一条请求后立即补发，直到时长或请求上限到达，再等待在途请求收尾。恢复探测单独串行执行，不计入负载占用。点击停止会取消正在等待的请求并保留部分结果；客户端取消不保证远端立即终止生成。

长输出模式使用固定合成负载，可选择接口支持的 `max_tokens` 或 `max_completion_tokens`。上限不是最低输出保证；模型可以提前结束。报告提供实际平均输出字符数和首段内容后的持续时间。长输出达到上限且收到正常结束帧视为完整传输，`finish_reason=length` 仍记录；短输出模式维持原有完整性标准。Mock 长输出按字符块模拟，不等同于真实 token。

CLI 示例（默认本地 Mock；输出须使用新的数据子目录）：

```sh
python -B -m relay_lab account-test --config configs/sustained.yaml --output /Users/lmurder/Desktop/api中转站/中转站极限测试数据/my-sustained-run
```

也可使用 `--duration 60 --max-requests 1000 --long-output --output-tokens 1024 --output-limit-field max_tokens` 覆盖配置。持续补发按时长结束，最后一条请求可能额外运行至其完成或超时。请求上限提前到达时不判定该持续阶梯通过。

报告 `occupancy` 中的在途包括连接建立、网关等待及上游排队；接收中仅表示从首段内容至请求结束。均值和满并发时间占比通过请求状态变化精确积分，排除收尾及恢复；`series` 是最多 1200 点的采样曲线。真实单账号的生成占用必须结合服务端账号标识、调度及生成起止记录核对，不能用客户端并发代替。当前未接入此类服务端证据。

新增请求时间字段为 UTC Unix 秒；旧 JSONL 仍可重建报告，旧记录不会补造占用曲线。`peak_active_connections` 为兼容保留的客户端活动请求计数，并非已建立 socket 或上游生成数。

## 固定混合批次：观察排队与拒绝

控制台默认方案为“固定 10 条 · 2 短 / 2 中 / 6 长”。同一批请求通过共同启动信号发出，每条只执行一次，结束后不补发，也不追加恢复探测。数量可分别调整，总请求数就是本次客户端并发目标；连接池至少容纳整批请求。报告记录实际发起时间跨度，不假设网络或上游的接收顺序。

默认输出上限为短 64、中 512、长 4096 token，使用各档合成指令和所选输出上限参数。输出上限不是长度保证，也不能保证上游转发或执行该参数；应检查实收字符和接收时长。Mock 分别使用 8、32、96 个字符块演示长短差异，不模拟真实 token 数量。

参考上游限制默认为 5，只用于真实结果对照；不把结果写死为 5，不控制真实账号。Mock 可选择“排队等待空位”或“立即返回 429”，参考限制用于虚拟账号容量。

新模式独立设置首段等待上限（600 秒）、有正文后无新内容的空闲上限（60 秒）、单请求总时限（900 秒），连接／写入上限默认 10 秒。心跳不会重置正文空闲计时。总时限到达、首段等待超时、流空闲超时、HTTP 错误、流内错误和不完整结束分别记录；主动停止保留部分输出指标。旧模式保留原有超时行为。

进行中的页面与最终报告按 S1、S2、M1、M2、L1…逐条展示发起、HTTP 头、首段、末段、结束、输出量及终止原因。短／中请求完整结束时，记录原始批次中仍在等待首段的请求，追踪其后来输出或失败；“释放后开始”列使用默认 10 秒窗口，明细保留实际间隔。时间相关性不是服务端排队证明，接收中也包含流停顿，不能代替真实账号生成占用。这个固定批次不输出未经测定的稳定并发上限。

```sh
./relay-lab account-test --config configs/mixed-burst.yaml
```

默认只发往本地 Mock。`--mixed-burst` 可对单号或网关命令启用该模式，分档数量、输出与超时参数通过 `mixed_burst` 配置设置。JSONL 新字段均为脱敏指标；旧版本两种请求记录格式仍可读取。平均输出现在包含失败请求的部分正文；首段后平均时间按所有收到正文的请求计算。成功请求吞吐仍单独按完整成功统计。

## 三路实时负载推子

启动控制台：`python -B scripts/start_ui.py`，打开 `http://127.0.0.1:8878`。默认选择本地 Mock 与三路实时推子。点“启动推子测试”后，短／中／长目标起始均为 0；拖动推子或输入数值实时调整。

- 推子值是该类请求的目标在途数，包含等待和接收中。上调后补足目标，下调让原请求自然结束；“暂停补发”可观察已有等待请求获得输出，“停止测试”取消在途并生成报告。
- 可先推高长请求，看到其开始接收正文后，再推短／中请求，观察新请求的等待时长、HTTP 状态和输出。默认 Mock 容量为 5，三类流约持续 2／6／18 秒；配置中的排队／拒绝选项只作用于 Mock。
- 推子量程可以调整，也可输入准确数值；三路目标之和须在本次客户端并发上限内，支持上限 1200。连接池提前按该上限配置。负载降低期间旧请求仍占位时，新请求按客户端总上限派发。
- 输出上限、最长运行时长、累计请求上限和补发间隔在启动前设置。到时或达到累计上限后停止补发并收尾。暂停期间运行时限继续计算。持续失败也按补发间隔派发，不进行无间隔重试。
- 真实接口仍需输入本次凭据并确认真实请求。界面的“等待响应头”“等待首段”包含网络及上游调度，无法单独证明服务端队列；“接收中”不等于账号生成占用。没有服务端证据时不显示确定的队列长度或账号并发上限。
- 页面优先展示在途和最新请求，完整指标见 results.jsonl，推子调节历史见 fader-events.json。报告保存动态目标和实际请求时间，恢复部分报告时保留调节记录并标记为部分结果。

自动检查：`python -B -m unittest tests.test_faders tests.test_fader_http -v`。控制台同样支持原固定混合批次、持续阶梯以及其他既有模式。
