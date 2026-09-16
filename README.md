# 模型测试工作台

一份公共渠道库，六个可分别运行的测试工具：准入快测、定时稳定性测试、非流式回答测试、中转站极限测试、请求特征诊断和生图模型质量测试。

## 启动

需要 Python 3.10+。首次安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

打开 http://127.0.0.1:8090 。macOS 安装依赖后也可以双击 `start.command`。Windows 使用 `.venv\Scripts\python.exe` 执行相同命令。

可分别启动一个工具，默认读取同一个公共渠道库：

| 命令 | 地址 | 功能 |
| --- | --- | --- |
| `python run.py` | 8090 | 工作台与六个工具 |
| `python run.py --app admission` | 8091 | 公共渠道与准入测试 |
| `python run.py --app stability` | 8092 | 公共渠道与定时测试 |
| `python run.py --app reasoning` | 8093 | 公共渠道与非流式测试 |
| `python run.py --app channels` | 8094 | 仅管理公共渠道 |
| `python run.py --app capacity` | 8095 | 中转站极限测试 |
| `python run.py --app diagnosis` | 8096 | 请求特征诊断；需设置仓库外 PLATFORM_DATA_DIR |
| `python run.py --app image-quality` | 8097 | 生图模型质量测试 |

单独启动准入或非流式工具时，不启动定时调度器。相同数据目录只允许一个调度器实例；同时启动工作台和独立定时工具会明确拒绝第二个实例。

请求特征诊断的推荐独立入口为 `python -B scripts/start_diagnosis.py --data-dir /absolute/external/diagnosis-data`，由启动器校验数据位于仓库外。同一诊断数据目录也只允许一个实例。

## 使用流程

1. **准入测试**：粘贴或填写候选端 URL、Key，临时使用且不入库；选择数据库中的参照端；选择模型预设或手动填模型与协议，默认执行 2 轮 5 题双端流式测试，首轮预热、第二轮进入报告。报告先展示普通人员可读的运行结论，再展示逐题摘要和默认折叠的双端原始证据；能力由人员直接阅读回答后判断，系统不增加评审录入或准入标记。后端接受有效提交后，会把归一化候选渠道作为“渠道”、把候选模型家族作为“测试分组”异步写入已配置的飞书多维表格；“人工评判结果”列完全由表格操作人填写，程序不读也不写。可以停止、打开历史报告，并分别下载普通版 HTML、技术版 HTML 和技术版 JSON。报告由后端保存且不含 API Key，滚动保留最近 30 条。
2. **公共渠道**：前端支持新增已上线渠道、编辑地址/倍率/分类/状态、替换密钥、停用渠道。保存后密钥不回显；编辑时留空保留旧密钥。导入的资料默认只标记为“已记录”。候选端通过准入后，由使用者自行决定是否新增为已上线渠道。
3. **定时测试**：从公共库选择渠道，填写模型和协议，保存为巡检目标。再创建计划，明确选择目标与开始时刻；整批巡检的题目请求全局最多同时执行 2 个，“渠道并发”只控制并行推进的渠道数量，不会突破该请求上限。“报告延迟”以每次计划开始时刻为基准，测试提前完成时会等到该时刻再通知，测试超时则在完成后尽快通知。速度可使用自适应阈值：每个“渠道 + 模型”先收集至少 5 个全部请求成功的定时批次，以各批成功请求 P95 的历史中位数为基线，当前 P95 超过基线的 1.5 倍时判为速度缓慢。采样期只判定成功率、超时和断流，不会因速度将渠道误报为异常。保存公共渠道、标记上线均不会自动创建目标或计划。飞书报告分组独立引用公共渠道 ID，同一批实测结果可以复用于多个上线倍率分组；免测分组可以固定显示正常。报告会单独列出速度缓慢渠道和不稳定渠道，包含 P95、历史中位数、慢速线、TTFT、输出速度、成功/超时/断流的比率与计数。每次运行会保存当时的配置快照，后续调整不会改变历史运行。已完成且通知处理完毕的报告和探测明细滚动保留 5 天；运行中或待通知记录不会被清理。
4. **非流式测试**：选择公共渠道、模型及协议，每次执行 4 道推理题和 1 道短答基线，展示答案、可见推理、usage、截断信号和原始响应，可下载 JSON 报告。
5. **中转站极限测试**：作为工作台的 `/capacity/` 模块运行，覆盖单号、号池、网关、长任务恢复和故障注入。默认只使用本地 Mock；真实接口每次都必须填写临时 Key 并勾选确认，Key 不写入报告或磁盘。支持固定混合批次、持续并发和短／中／长三路实时负载推子。Mock 结果不能解释为真实账号或真实网关容量。

准入测试延续原工具的速度测量和回答展示；它不自动给出完整的业务准入结论。非流式工具检查接口返回的可见文本及结束信号，不能证明模型未公开的内部推理完整性。准入报告在服务器滚动保留最近 30 条，也可主动下载；非流式报告仍只保留在本轮页面中，刷新清空。

准入预设新增 **GPT-6 Astra**（`gpt-6-astra`），固定使用 Responses；推理强度 low，每题 `max_output_tokens=4096`（含推理）。技术报告展示推理用量与完成状态。[参数与验证说明](docs/admission-gpt6.md)。

## 生图模型质量测试

从工作台导航进入 `/image-quality/`，手动填写 Base URL、临时 Key 和提示词。每次确认后请求 `gpt-image-2`，可并排查看本轮图片、人工评分、下载去元数据 PNG 和脱敏指标。此模块不读取公共渠道凭据、不创建计划，也不保存后端历史；刷新清空本轮样本。返回模型字段只是上游自报，不能据此认证模型身份。详见 [子项目说明](features/image_quality/README.md) 和 [开发验收清单](docs/image-quality-testing.md)。

## 数据

```text
data/
  channels.db             公共连接资料，无模型和定时计划
  channels.key            渠道密钥的加密主密钥
  admission/
    reports.db            最近 30 条准入报告及飞书待写队列，不含 API Key
  stability/
    stability.db          巡检目标、计划、结果、通知设置
    secret.key            飞书 Webhook 的加密主密钥（配置后生成）
  relay-lab/
    console/runs/         极限测试脱敏请求指标、汇总和报告
  diagnosis/
    diagnosis.db          请求特征案例、计划快照和测量指标，不含请求/响应正文
```

`PLATFORM_DATA_DIR` 可指定其他数据目录；准入、定时和非流式工具使用相同值即可共享公共渠道。极限测试共用工作台的登录和进程，但不会自动读取公共渠道密钥。定时目标只引用公共渠道 ID，执行时读取最新地址和密钥。停用公共渠道会阻止后续使用；已发出的请求不被强制中断。模型名、协议、频率、轮次归各测试任务配置，模板模型名不会写入公共渠道资料。

极限测试默认使用 `PLATFORM_DATA_DIR/relay-lab`；原生启动时可通过 `RELAY_LAB_DATA_DIR` 指向专用磁盘。Docker 部署如需独立磁盘，应在 Compose 中同时增加容器挂载与容器内路径；默认仍使用同一 `./data` 挂载下的隔离子目录。输出目录由服务端生成，浏览器不能指定任意文件路径。命令行的一键脱敏配置检查为：

```bash
python -m relay_lab inspect-config --config configs/relay-lab/example.yaml
```

数据库和主密钥的权限分别为 0600，目录为 0700。SQLite 自身不提供密钥隔离，Fernet 加密和文件权限负责保护；具有服务器文件读取权限的人仍可访问密钥。因此数据目录和备份都不能提交到 Git。工作台没有下载数据库密钥或回显已保存 API Key 的接口。

准入飞书写入通过 `ADMISSION_FEISHU_APP_ID`、`ADMISSION_FEISHU_APP_SECRET`、`ADMISSION_FEISHU_APP_TOKEN` 和 `ADMISSION_FEISHU_TABLE_ID` 配置；四项齐全时启用。默认只写入标题为“渠道”和“测试分组”的两列，标题不同时使用 `ADMISSION_FEISHU_CHANNEL_FIELD`、`ADMISSION_FEISHU_GROUP_FIELD` 调整。外部写入失败不会中断准入测试，记录会留在本地队列并在服务重启后重试。所有飞书凭据只允许写入未纳入 Git 的 `.env` 或部署平台密钥配置。

同一渠道的 `/chat/completions`、`/responses`、`/messages` 等兼容接口地址在写入前会归一为同一个渠道值。飞书多维表格的既有视图按“渠道”字段分组并保存折叠状态后，后续记录会自动进入对应渠道分组；程序不需要额外权限，也不会覆盖历史记录。

旧资料可重复迁入，重复项跳过；源数据库仅以只读方式打开：

```bash
.venv/bin/python scripts/migrate_inventory.py --source-db ../定时稳定性测试/data/stability.db --source-key ../定时稳定性测试/data/secret.key
```

迁移只处理 `channel_inventory`，旧项目运行历史和计划不迁移。批量导入入口支持原先的混合配置文本，也支持 `base_url/api_key/multiplier` 的 JSON 数组。缺少必需字段的记录需补全；不会把模板模型名当成实际测试模型。

备份：`python scripts/backup.py`。备份包含公共渠道、准入报告、稳定性记录及配套主密钥，保存在忽略 Git 的 `backups/`。恢复时先停止服务，将同一份备份内的数据库和密钥成套放回数据目录。跨库备份不是同一事务，在线备份适用于连接资料和运行记录分别恢复；迁移或整库恢复建议停机操作。

## 部署

设置 `PLATFORM_USERNAME` 和至少 12 位的 `PLATFORM_PASSWORD` 后，才可使用 `--host 0.0.0.0`。所有页面、接口统一鉴权，修改接口检查跨站请求。公网接入使用 HTTPS 反向代理；代理应保留 Host，关闭准入流响应缓冲，并允许非流式长请求（最多 5 × 600 秒）。

Docker 默认以稳定模式运行，容器端口只发布到本机 8090，`./data` 挂载到容器中，因此重建镜像不会丢失渠道、密钥、计划和历史。稳定性报告默认保留 5 天，可通过 `STABILITY_RETENTION_DAYS` 调整；容器 JSON 日志按 10 MiB、最多 5 个文件轮转。先从 `.env.example` 创建 `.env` 并设置至少 12 位的密码，然后使用：

```bash
make up       # 构建/重建并后台启动
make ps       # 查看健康状态
make logs     # 跟踪日志
make restart  # 只重启服务
make down     # 停止容器
```

服务器也可以直接使用 GitHub Container Registry 中已经测试通过的镜像，省去本机编译时间：

```bash
cp .env.example .env
# 编辑 .env，至少更换 PLATFORM_PASSWORD；需要固定版本时把 WORKBENCH_IMAGE 改为对应的 v* 标签。
make prod-up
make prod-ps
```

`compose.prod.yml` 会拉取 `WORKBENCH_IMAGE`，默认是 `ghcr.io/yl0711-coder/newapi-evaluator:latest`。如果容器包保持私有，服务器需要先执行 `docker login ghcr.io`；设为公开后可匿名拉取。更新时再次执行 `make prod-up`，数据仍保存在宿主机的 `./data`。

仓库的版本标签触发 `.github/workflows/release.yml`。形如 `v1.1.0` 的标签会先运行全部自动测试，再构建并启动一次临时容器验证健康状态；全部通过后才发布 `linux/amd64`、`linux/arm64` 镜像，并生成版本号、提交 SHA 和 `latest` 三类镜像标签。GitHub Actions 使用仓库自带的 `GITHUB_TOKEN` 发布，不需要在仓库中保存个人访问令牌。

需要边改代码边看效果时运行 `make dev`。它会把 `features/`、`relay_lab/`、`configs/`、`shared/`、`web/` 和入口文件挂载到容器；Python 代码保存后自动重启，HTML/CSS/JS 保存后刷新浏览器即可。依赖或 Dockerfile 变更后重新执行 `make dev`。稳定模式下代码变更后执行 `make up` 重建。Linux 宿主机需保证容器 UID 10001 可读写 `./data`。

上游连接默认禁止内网、回环和保留地址，并在实际建连时校验和固定解析出的 IP，保留原始 TLS SNI。确有内部测试服务时，由部署者设置 `PLATFORM_EGRESS_ALLOWLIST` 为逗号分隔的主机名或 CIDR。测试请求不读取系统代理、不跟随重定向、不自动重试。

## 验证和来源

八个工作台页面共用公共主题，保留各测试工具的专用布局与报告能力。前端样式约定、宽窄屏回归和发布验证入口见 [前端统一清单](docs/frontend-style.md)。

运行 `python scripts/test_all.py` 执行原有引擎自测和共享渠道集成测试；模拟上游不会消耗真实渠道额度。新集成测试覆盖临时候选端不入库、密钥不下发、共用凭据更新、独立启动、定时选择、导入幂等、鉴权和跨站限制。

准入测量引擎来自 `国产模型速度快测/main.py`；定时引擎来自 `定时稳定性测试/app`；第三项来自 `newapi-evaluator` 的 `feature/single-channel-reasoning-integrity` 分支提交 `be5cc982bd435d3aa00a2496311cd2b3695483bd` 下的 `tools/thinking-integrity-test`。

`features/` 中保留引擎代码与离线自测，通过 `run.py --app ...` 独立使用；实际工作台只暴露引用公共渠道的适配接口，准入候选端保留临时输入接口。没有合入大仓库中的其他业务模块。

## 请求特征诊断子项目

根据历史 Token、耗时、状态码和流式标记，在本地 Mock 或经确认的公共渠道做合成请求对照。支持独立启动与工作台入口。详见 [使用说明](docs/请求特征诊断.md) 和 [测试清单](docs/请求特征诊断测试清单.md)。
