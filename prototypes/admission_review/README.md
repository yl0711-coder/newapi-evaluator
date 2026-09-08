# 准入人工确认与飞书记录框架

这是一个尚未接入主工作台的独立小项目，用于先验证以下业务闭环：

```text
填写渠道信息 -> 执行准入测试 -> 等待人工确认 -> 合格/不合格 -> 飞书 Outbox
```

当前只实现框架，不访问飞书：

- 开始时收集渠道名称、地址、临时凭据、模型和协议。
- 临时凭据不落库；渠道 URL 的查询参数和片段不落库。
- 正式准入测试通过 `/api/runs/{id}/test-result` 接入，目前页面提供模拟按钮验证状态流转。
- 未完成测试时不能人工判定；判定后不可静默反转。
- 合格和不合格都会生成一条幂等 Outbox 记录。
- 飞书字段映射集中在 `mapping.py`，当前有意留空；任务状态为 `awaiting_field_mapping`。
- 没有真实飞书客户端、凭据配置或发送接口，因此框架不会向外部传输数据。

## 本机运行

在仓库根目录执行：

```bash
python -m prototypes.admission_review
```

打开 <http://127.0.0.1:8095>。

数据默认写入被 Git 忽略的 `prototypes/admission_review/data/framework.db`。也可以通过
`ADMISSION_FEISHU_FRAMEWORK_DATA_DIR` 指定独立数据目录。

## 自测

```bash
python -m prototypes.admission_review.selftest
node --check prototypes/admission_review/web/app.js
```

## 一键提取脱敏渠道信息

测试机完成一次框架流程后，在仓库根目录执行：

```bash
python -m prototypes.admission_review.cli --latest
```

输出只包含准入记录 ID、脱敏别名、协议、主机指纹、模型数量、配置指纹和提取时间；
不输出渠道原名、完整主机、查询参数、Key、鉴权头、Prompt 或响应正文。

## 后续扩展点

1. 根据项目所有者提供的字段清单补充 `mapping.py`。
2. 确认飞书多维表格的 App Token、Table ID、唯一业务键和权限方式。
3. 实现 `pending -> sending -> success/retry/conflict` 的 Outbox 投递器。
4. 小项目闭环和独立验收完成后，再把适配器接入正式准入引擎与主工作台。
