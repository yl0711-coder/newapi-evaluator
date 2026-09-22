# 资源包使用与 AI 交接手册

这份手册给第一次接手本资源包的 AI 或部署人员使用。先读本文件，再读 `README.md`；不要从源码中的默认值推测部署状态，部署状态以外置状态目录里的 `config.json` 为准。

## 交接结论

- 实际检测模型固定为 `gpt-5.6-sol` 和 `gpt-6-astra`。渠道文档里的原始模型只作来源记录，不改变检测模型。
- 每个模型执行 55 次请求，所以每个启用渠道每轮执行 110 次：Reasoning 30 次、Juice 25 次，两个模型各一套。
- 只支持 OpenAI 兼容的非流式 Responses 和 Chat Completions 接口。
- 代码、配置状态、凭据、SQLite 数据和 HTML 报告必须分开保存；状态目录必须位于源码目录之外。
- `inspect`、`init`、`import`、`enable`、`disable`、`report` 不发送渠道请求。只有 `run-once` 和 `daemon` 在显式带 `--confirm-live` 时才允许真实请求。

## 资源包和状态包

代码资源包应包含这些文件：

```text
hourly-channel-diagnostic/
├── hourly_channel_diagnostic.py  # 诊断引擎和 CLI
├── channel_catalog.py             # 渠道文档导入器
├── manage.py                      # 初始化、导入、启停和报告命令
├── config.example.json            # 不含密钥的配置模板
├── README.md
├── DEPLOYMENT.md
├── TESTING.md
├── Dockerfile
├── compose.yaml
├── nginx.conf
├── scripts/test_all.py
└── tests/
```

上线还需要一个独立的私有状态目录。当前机器已经生成的示例状态目录是：

```text
/Users/lmurder/Desktop/api中转站/中转站极限测试数据/hourly-channel-release-o75jmg4j/private-channels/
├── config.json       # 渠道元数据和 test_models
├── credentials.json  # 凭据；必须是 0600 或 0400
├── data/             # diagnostic.sqlite3 和锁文件
└── reports/          # report.html
```

代码资源包不应包含 `credentials.json`、真实 `config.json`、SQLite、报告或原始渠道文档。原始渠道文档可能含真实密钥，只能作为受保护的导入输入。

## 首次部署（非 Docker）

下面的变量只是假设路径，部署 AI 必须替换成实际的绝对路径：

```bash
CODE=/opt/hourly-channel-diagnostic
STATE=/var/lib/hourly-channel-diagnostic
SOURCE=/secure/inbox/onetoken-0.4x
PYTHON=python3
```

如果已经有转换好的状态目录，跳过 `init` 和 `import`，直接从“检查”开始。首次从渠道文档导入时：

```bash
mkdir -p "$STATE"
"$PYTHON" "$CODE/manage.py" init --state-dir "$STATE"
"$PYTHON" "$CODE/manage.py" import --state-dir "$STATE" --source "$SOURCE"
```

导入会生成渠道 ID、服务商、倍率、Base URL 和独立凭据索引；不会发送渠道请求。导入完成后，原始文档仍按部署环境的密钥管理规则处理，不得复制到代码包或 Git。

## 检查模型和渠道状态

先执行只读检查：

```bash
"$PYTHON" "$CODE/manage.py" inspect --state-dir "$STATE"
```

输出必须包含：

```json
"test_models": ["gpt-5.6-sol", "gpt-6-astra"]
```

如果模型列表不同，先修正 `config.json`，再继续。不要把渠道条目里的 `model` 字段当成实际检测模型。

首次部署时所有渠道默认停用。确认要检测的渠道后启用：

```bash
"$PYTHON" "$CODE/manage.py" enable --state-dir "$STATE" --all
```

也可以只启用指定渠道：

```bash
"$PYTHON" "$CODE/manage.py" enable --state-dir "$STATE" \
  --id ch_XXXXXXXXXXXXXXXX
```

启用命令只修改配置并刷新报告，不发请求。当前实现的实际检测模型已经由顶层 `test_models` 明确指定，因此原渠道文档是否写明模型不会阻止启用。

## 启动小时检测

真实单轮检查必须显式确认：

```bash
"$PYTHON" "$CODE/hourly_channel_diagnostic.py" run-once \
  --config "$STATE/config.json" \
  --credentials "$STATE/credentials.json" \
  --data-dir "$STATE/data" \
  --output "$STATE/reports/report.html" \
  --confirm-live
```

上线后持续整点运行：

```bash
"$PYTHON" "$CODE/hourly_channel_diagnostic.py" daemon \
  --config "$STATE/config.json" \
  --credentials "$STATE/credentials.json" \
  --data-dir "$STATE/data" \
  --output "$STATE/reports/report.html" \
  --confirm-live
```

调度器首次等待配置时区的下一个整点；错过的小时不补跑。每个渠道每个模型的 110 次请求串行执行、不重试。请求失败、空响应、畸形 JSON 和截断响应会保留为失败观测，并继续后续请求；`completed` 只表示矩阵执行结束，不表示渠道通过。

## Docker Compose 部署

Compose 将报告服务、管理工具和检测 worker 分开：

```bash
export DIAGNOSTIC_STATE_DIR=/var/lib/hourly-channel-diagnostic
docker compose build
docker compose --profile monitor up -d
docker compose ps
```

- `worker` 使用 `/state/config.json`、私有 Compose secret 中的凭据、`/state/data` 和 `/state/reports`。
- `report` 默认只绑定 `127.0.0.1:8097`，访问 `/` 或 `/report.html` 查看报告，`/healthz` 做健康检查。
- 管理操作通过 tools profile 执行，例如：

```bash
docker compose --profile tools run --rm toolbox inspect --state-dir /state
docker compose --profile tools run --rm toolbox enable --state-dir /state --all
```

凭据文件由 Compose secret 挂载为 `/run/secrets/channel_credentials`，不要把它写入镜像层、环境变量、日志或报告。

## 离线验证和报告

本地验证必须使用独立的新数据目录和 Mock：

```bash
"$PYTHON" "$CODE/hourly_channel_diagnostic.py" run-once \
  --config "$CODE/config.example.json" \
  --data-dir /tmp/hourly-diagnostic-mock \
  --mock
```

完整验收入口：

```bash
PLAYWRIGHT_MODULE=/path/to/playwright \
PLAYWRIGHT_CHANNEL=msedge \
"$PYTHON" "$CODE/scripts/test_all.py" \
  --output /外置/全新验收目录
```

验收结果必须逐组检查 `syntax`、`security`、`domain`、`http`、`catalog`、`browser`，不能只看最后一行。报告可随时重建：

```bash
"$PYTHON" "$CODE/manage.py" report --state-dir "$STATE"
```

## 给另一个 AI 的执行边界

1. 开始前读取 `README.md`、`DEPLOYMENT.md`、`TESTING.md` 和当前 `config.json`；记录源码路径、状态路径、候选版本和 `test_models`。
2. 永远把真实凭据留在外置权限目录；不要打印、复制、提交或放入 ZIP 的密钥和原始响应。
3. 不要把渠道条目的来源模型改成检测模型；实际检测模型只能由顶层 `test_models` 控制，并保持为两个指定值。
4. 修改代码后使用全新的外置验收目录跑完整测试；不要复用旧 SQLite 或把 Mock 结果当真实渠道质量。
5. 没有部署负责人明确授权时，不执行 `run-once`/`daemon --confirm-live`，不推送、不发布、不修改远端。

交付记录至少包含：候选源码 SHA、`test_models`、启用渠道数量、状态目录位置、完整测试逐组结果、报告位置和未验证边界。
