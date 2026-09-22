# 小时渠道诊断独立版

一个可脱离工作台运行的 Python 脚本，合并 `reasoning_effort_diagnostic.py` 与 `juice_effort_probe.py` 的诊断矩阵。运行环境为 macOS 或 Linux、Python 3.10+，运行时仅使用标准库。HTML 报告不依赖网络资源。

给部署脚本或另一个 AI 的完整使用、导入、启停、Docker 和安全交接步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 测试范围

每个启用渠道对每个配置模型执行 55 次请求；当前默认模型为 `gpt-5.6-sol` 和 `gpt-6-astra`，因此每渠道每轮共 110 次请求：

| 测试包 | 接口 | 档位 | 每个模型默认请求数 |
| --- | --- | --- | --- |
| Reasoning | Responses、Chat Completions | low / medium / high，各 5 轮 | 30 |
| Juice | Chat Completions | low / medium / high / xhigh / max，各 5 轮 | 25 |

Reasoning 每个模型每轮执行 Responses 三档，再执行 Chat 三档；Juice 每个模型每轮轮换起始档位，并轮换原脚本的三种题目。请求串行执行、不重试。`test_models` 控制实际请求的模型列表，`rounds`、`juice_runs` 控制每个模型的轮数，档位固定。渠道文档中记录的原始模型仅作为来源元数据，不会改变检测模型列表。Reasoning 使用严格数字答案判断，避免把包含正确数字的错误答案算成正确。

Juice 的预设值沿用来源脚本，为 `8 / 16 / 40 / 128 / 960`，只用于与模型自报值比较，不是通用模型标准，也不能证明服务端实际推理预算。

## 配置与运行

在本目录复制 `config.example.json` 为 `config.json`，填写 `test_models`、渠道别名、Base URL、来源模型和密钥环境变量名。`test_models` 是唯一实际请求的模型列表；上线配置保持为 `gpt-5.6-sol`、`gpt-6-astra`。配置不接受直接填入密钥；真实密钥应在启动前由部署端注入相应环境变量。不要把实际配置、密钥或数据加入 Git。

```bash
python3 -B hourly_channel_diagnostic.py inspect --config config.json
```

`inspect` 不读取密钥、不发送请求，输出渠道别名、检测模型列表、协议、来源模型、主机指纹、配置指纹、时间与请求量。主机指纹不暴露原始 URL。仅支持 OpenAI 兼容的非流式接口；每个检测模型的 Responses 和 Chat 会分别测试。

完全离线试运行（使用全新的外置数据目录）：

```bash
python3 -B hourly_channel_diagnostic.py run-once \
  --config config.example.json --mock \
  --data-dir ../中转站极限测试数据/小时渠道诊断独立版-mock
```

真实单轮运行需事先设置渠道对应的密钥环境变量，并明确允许真实请求：

```bash
python3 -B hourly_channel_diagnostic.py run-once \
  --config config.json --confirm-live \
  --data-dir ../中转站极限测试数据/小时渠道诊断独立版-live
```

持续整点运行：

```bash
python3 -B hourly_channel_diagnostic.py daemon \
  --config config.json --confirm-live \
  --data-dir ../中转站极限测试数据/小时渠道诊断独立版-live
```

首次运行等待配置时区的下一个整点。每轮完成后等待下一个整点；若执行跨过一个或多个整点，不补跑已错过的小时，图表保留空档。请求完成时刻决定其小时桶，所以跨小时运行会拆入不同的小时。每个数据目录只允许一个 daemon，单轮执行也有互斥锁。服务管理器和开机自启配置属于部署步骤，当前脚本不会自动安装服务。

`report` 可重新生成历史报告，`--output` 只能指向独立 HTML 文件：

```bash
python3 -B hourly_channel_diagnostic.py report \
  --config config.example.json \
  --data-dir ../中转站极限测试数据/小时渠道诊断独立版-mock
```

打开相应数据目录的 `report.html` 即可查看。默认数据目录为源码目录同级的 `中转站极限测试数据/小时渠道诊断独立版`；配置中的相对路径以配置文件所在目录为基准，命令行相对路径以当前目录为基准。源码目录内禁止保存运行数据与报告。Mock 和真实数据必须使用不同目录。

## 报告与统计口径

报告包含执行记录、小时概览、24 小时成功率热力图、异常与待确认小时、逐接口逐档位明细、各档位趋势。窄屏表格可横向滚动；趋势缺失小时不连线，单个样本点也可见。

| 指标 | 分母或语义 |
| --- | --- |
| 成功率 | 有效非空且未标记截断/失败的响应 / 全部请求；HTTP 200 本身不足以算成功 |
| 答题正确率 | 正确回答 / 有效回答样本；请求失败单列，不混入该分母 |
| 回显匹配率 | Responses 档位匹配 / 返回回显的有效样本；缺回显为未知，Chat 为不适用 |
| Juice 匹配率 | 精确匹配预设整数 / 有效响应样本；无法解析的有效文本计不匹配 |
| Juice 验证率与结论 | 每小时每渠道每档位的精确匹配数 / 全部请求，失败及无法解析均计入分母；至少 `ceil(请求数 × 0.6)` 次匹配为 `verified`，否则 `inconclusive` |
| 推理 Tokens | 显示数值样本数、中位数、字段返回数与空值数；缺字段、null、0 分开处理 |
| 已报告 Tokens | 仅累计实际返回的 `usage.total_tokens`，同时显示覆盖数；缺失为 `—`、真实零为 `0`；不会拿输出 Tokens 充当总 Tokens |
| 延迟中位数 | 有效响应的客户端请求总耗时，单位 ms；不是首 Token 延迟 |

Tokens 覆盖不全时只代表已报告样本的小计，不能据此计算完整费用。异常表逐接口、档位定位失败、不匹配及缺少回显；`verified` 与“存在少量不匹配”可以同时成立。`completed` 仅表示请求矩阵执行结束，不代表渠道全部通过。

SQLite 只保存白名单指标和错误类别，不保存题目、回答、响应正文或凭据。原版 SQLite 自动添加必要元数据列并保留旧行；旧行没有可靠总 Tokens 和回显元数据时，不用于这些新统计，并标记为旧版样本。默认保留 14 天，每轮结束清理到期样本及无剩余样本的到期运行。

## 失败与恢复

畸形 JSON、错误字段类型、HTTP 错误、空响应及截断响应均记录为单次失败，继续后续请求和渠道。完整矩阵即使有请求失败也会生成报告；调用方应读取报告指标判断渠道质量。无效配置或缺少密钥会在真实发请求前失败。

每条样本立即提交；Ctrl-C 保留已完成样本并标记 `interrupted`，退出码 130。进程被强制结束时，下次运行会把遗留 `running` 标为中断；不重放旧请求。未知内部错误只记录异常类别，单轮命令非零退出，daemon 在下一个整点尝试新一轮。环境代理与 HTTP 重定向均禁用；每次请求超时分别由 `reasoning_timeout`、`juice_timeout` 控制，响应体最多读取 2 MiB。

## 验收

完整清单与环境准备见 [TESTING.md](TESTING.md)。开发与独立验收只用合成夹具和回环 HTTP，测试结果不代表真实渠道质量或长期运行已验收。当前任务与版本交接见 [PLAN.md](PLAN.md)。
