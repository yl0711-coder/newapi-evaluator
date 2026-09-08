# 准入渠道与测试分组飞书记录

这是一个尚未接入主工作台的独立小项目，只验证一件事：准入测试开始时，把参与渠道和模型家族写入飞书多维表格。

## 数据契约

多维表格共有三列，但程序只负责前两列：

| 列 | 写入方 | 本程序行为 |
| --- | --- | --- |
| 渠道 | 本程序 | 写入 |
| 测试分组 | 本程序 | 根据所选模型自动识别并写入 |
| 人工评判结果 | 表格操作人 | 不读取、不写入、不覆盖 |

模型与分组沿用正式准入测试的预设，当前包括 Codex、Claude、智谱、Kimi 和 DeepSeek。页面不再提供人工评判功能。

## 本机运行

在仓库根目录执行：

```bash
python -m prototypes.admission_review
```

打开 <http://127.0.0.1:8095>。

数据默认写入被 Git 忽略的 `prototypes/admission_review/data/framework.db`。旧框架表不会被读取，新数据使用独立表名。

## 配置飞书多维表格

凭据只通过本机安全环境注入，不写入仓库：

```bash
export ADMISSION_FEISHU_APP_ID='<app-id>'
export ADMISSION_FEISHU_APP_SECRET='<app-secret>'
export ADMISSION_FEISHU_APP_TOKEN='<app-token>'
export ADMISSION_FEISHU_TABLE_ID='<table-id>'
python -m prototypes.admission_review
```

默认字段名是 `渠道` 和 `测试分组`。如果表格标题不同，可设置：

```bash
export ADMISSION_FEISHU_CHANNEL_FIELD='<渠道列标题>'
export ADMISSION_FEISHU_GROUP_FIELD='<分组列标题>'
```

未配置飞书时，记录会安全保留在本地并显示“飞书尚未配置”；配置完成后可重试写入。真实写入使用飞书开放平台的 tenant access token 和多维表格新增记录接口。

## 自测

```bash
python -m prototypes.admission_review.selftest
node --check prototypes/admission_review/web/app.js
```

自测覆盖：五个模型家族、严格两字段写入、第三列不触碰、真实 HTTP 请求形状、失败重试、幂等重试和校验错误脱敏。

## 一键提取脱敏渠道信息

```bash
python -m prototypes.admission_review.cli --latest
```

输出仅包含脱敏渠道别名、测试分组、同步状态、写入字段数量、配置指纹和提取时间，不包含渠道原名、飞书凭据或飞书记录 ID。

## 接入主程序前

1. 提供目标多维表格的 App Token、Table ID 和应用凭据。
2. 确认表中前两列的准确标题和字段类型。
3. 在独立项目中完成真实写入验收。
4. 验收通过后，再在正式准入测试开始事件中调用同一写入服务。
