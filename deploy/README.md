# 独立生产部署

仅部署 nexusapi-channel-diagnostic，不替换 Eval、Monitor、API 或 NewAPI。
本次默认只运行静态报告容器，16 个导入渠道保持停用，不发送真实请求。

## 位置与隔离

- 代码：/opt/channel-diagnostic/releases/<完整源码 SHA>，不写入既有服务目录。
- 状态：/var/lib/channel-diagnostic；目录 0700，配置和凭据 0600，属主与容器 UID/GID 一致。
- 导入目录：/var/lib/channel-diagnostic-inbox；不存入代码、镜像或 Git。
- Compose 项目：nexusapi-channel-diagnostic；报告监听 127.0.0.1:18097。
- 目标域名：diagnostic.nexusapi.link；独立 Caddy 站点及 Basic Auth，指向回环报告端口。
- 版本：新仓库 v1.0.0 起步，与 Eval 版本无关。默认从 GHCR 拉取 `latest`；生产环境建议将 `DIAGNOSTIC_IMAGE` 固定为版本 tag 或实际 digest。
- 不创建 S3、Redis、数据库服务或新的 AWS 实例。

## 首次部署

1. CI 完整离线验收、构建、安全扫描成功后发布镜像。
2. 固定源码 SHA、镜像版本或 digest；在新目录保存生产环境文件（0600，不提交）。
   环境变量为 DIAGNOSTIC_IMAGE（可省略，默认 GHCR latest）、DIAGNOSTIC_STATE_DIR、DIAGNOSTIC_INBOX_DIR、
   DIAGNOSTIC_UID、DIAGNOSTIC_GID。IMAGE 指向本仓库 ghcr.io 镜像及不可变 digest。
3. 安全复制 config.json、credentials.json 和 diagnostic.sqlite3。
   先核对数据库是否仍为空、是否正在使用；非空且在写入时用 SQLite 在线备份，不直接复制活跃文件。
   不复制 scheduler.lock、diagnostic.lock 或 .DS_Store；旧数据原地保留。
4. 用 inspect 核对渠道数量、启用数量与检测模型。禁止打印凭据或完整配置。
5. 一次性执行 report 生成报告；此步骤会初始化/兼容 SQLite，但不会发真实请求。
6. 只启动 report 服务，不使用 monitor profile：

```bash
docker compose --env-file /opt/channel-diagnostic/production.env -f deploy/compose.prod.yaml --profile tools run --rm --no-deps toolbox inspect --state-dir /state
docker compose --env-file /opt/channel-diagnostic/production.env -f deploy/compose.prod.yaml --profile tools run --rm --no-deps toolbox report --state-dir /state
docker compose --env-file /opt/channel-diagnostic/production.env -f deploy/compose.prod.yaml up -d --no-deps --no-build --pull never report
```

   命令须在固定版本源码根执行，首次使用可由 Compose 自动拉取默认 GHCR 镜像；生产环境仍建议提前按 digest 拉取。检查 report 健康、配置/数据库路径
返回 404、Compose 没有 worker；确认既有 Eval、Monitor、API 容器未被重建。

## 独立域名

创建 diagnostic 的 A 记录指向部署主机；只改新增记录，不改变 eval/monitor。
先备份 Caddy 配置，在新增站点设置 Basic Auth 和 reverse_proxy。
核对 Caddy 网络模式：host 网络可用 127.0.0.1:18097；其他模式须先验证独立报告容器可达，
不能直接把报告端口公开来绕过。validate 成功后仅 reload，不重启其他服务。
验收未认证 401、认证后 200、TLS 有效，以及旧域名状态不变。
DNS 或认证未就绪时维持回环访问，不无鉴权公开报告。

## 后续启用真实检测

本次不执行。得到明确的渠道范围与真实请求授权后，先停 worker、备份状态、通过 toolbox 启用
指定渠道，再 force-recreate worker。配置和凭据只在进程启动时读取；单文件绑定遇到原子替换
也必须重建容器，不能指望修改文件自动生效。manage 命令与运行中的 daemon 有互斥锁。

每渠道每轮 110 请求，16 渠道每轮 1,760 请求，全部串行且不补跑错过的整点；
不能承诺 16 渠道都每小时完成。零启用渠道不应运行 worker，否则会在整点退出并重启。
worker 上限 512MB/0.5 CPU 是隔离保护，不代表真实长期容量已验证。

## 更新与回滚

更新只操作该 Compose 项目下明确的服务，不运行整套宿主机 compose down。
版本化目录保留上一个 SHA/镜像 digest，数据目录独立；迁移前备份 SQLite 和配置到私有目录。
回滚重新使用旧版本配置和镜像，保持 worker 停止。若有数据结构变化，先核对兼容再决定是否恢复备份，
不得覆盖新产生的数据。首次部署撤销只停止本项目 report、撤销新增域名路由，保留状态供检查。
