# 小时渠道诊断独立版

这个目录可以脱离主项目运行。它把两个附件脚本合并成一轮完整诊断：每个启用渠道每小时执行 `reasoning`（`/responses` 和 `/chat/completions`，3 档位）以及 Juice（5 档位），默认 `5` 轮，共 55 次请求/渠道/小时。只从环境变量读取 API Key，不写入 SQLite、JSON 或 HTML。

每轮的 reasoning 请求按附件脚本交错为“responses 的 low/medium/high，再 chat 的 low/medium/high”；Juice 每轮会轮换起始档位，降低顺序造成的时间偏差。真实运行前必须显式添加 `--confirm-live`；不带该参数的运行不会发出真实请求。

默认请求量按渠道计算为每小时 55 次；接口返回 `usage` 时，报告表会同时显示这批请求的累计 Tokens。真实费用和隐藏推理 Tokens 由渠道返回值决定，脚本不会用估算值代替实际值。

先复制配置并填入真实渠道：

```bash
cd "/Users/lmurder/Desktop/api中转站/小时渠道诊断独立版"
cp config.example.json config.json
export CHANNEL_A_API_KEY='...'
python3 hourly_channel_diagnostic.py run-once --config config.json \
  --confirm-live \
  --data-dir "/Users/lmurder/Desktop/api中转站/中转站极限测试数据/小时渠道诊断独立版"
open "/Users/lmurder/Desktop/api中转站/中转站极限测试数据/小时渠道诊断独立版/report.html"
```

发给验收方的只读配置检查不会发请求，也不会打印密钥：

```bash
python3 hourly_channel_diagnostic.py inspect --config config.json
```

上线前先跑完全离线验收：

```bash
python3 hourly_channel_diagnostic.py run-once --config config.example.json --mock --data-dir /tmp/hourly-diagnostic-mock
open /tmp/hourly-diagnostic-mock/report.html
```

持续每小时整点运行：

```bash
python3 hourly_channel_diagnostic.py daemon --config config.json \
  --confirm-live \
  --data-dir "/Users/lmurder/Desktop/api中转站/中转站极限测试数据/小时渠道诊断独立版"
```

`report.html` 是静态文件，含成功率、正确率、匹配率、中位延迟折线图、按配置时区小时的渠道/测试包热力图、异常小时表和聚合表。数据目录默认指向 `/Users/lmurder/Desktop/api中转站/中转站极限测试数据/小时渠道诊断独立版`，也可每次用 `--data-dir` 改为新的隔离目录；数据库默认保留 14 天，清理周期在每轮结束时执行。任何渠道若不支持某个接口或字段，会显示为失败/无匹配数据，不会被伪造为 0；需要在结论里单独标为不支持。

单轮请求数和 Tokens：每个渠道默认 `5 × 2 × 3 + 5 × 5 = 55` 次；实际 Tokens 以接口返回的 `usage` 为准，报告只累计返回值，不用估算值替代。若接口不返回 usage，报告保留为 `-`。

生产运行前要确认 `config.json` 中列出的就是要测的全部渠道。脚本不能从主项目私自读取渠道密钥或业务数据库，也不会自动发现渠道。
