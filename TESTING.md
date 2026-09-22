# 离线验收清单

适用正式规则：本仓库 AGENTS.md 引用的 AI_RULES v1.0 及独立工具适用范围。产品为独立脚本，不属于工作台极限子集，本次无需执行实验室或工作台套件。

## 环境与入口

运行时需要 Python 3.11+ 标准库；测试额外需要 Node.js、Playwright 与已安装的 Chrome、Edge 或 Chromium。依赖位置由 `PLAYWRIGHT_MODULE` 和 `PLAYWRIGHT_CHANNEL` 显式提供。缺少浏览器能力必须记为 incomplete，不能跳过后宣称全量通过。

本机所有测试环境、临时文件和结果位于 `/Users/lmurder/Desktop/api中转站/中转站极限测试数据`。其他机器应指定源码目录外的隔离根。统一入口：

```bash
python3 -B scripts/test_all.py --output /绝对路径/全新验收目录
```

该命令须从源码根执行并事先设置浏览器依赖环境变量。它会逐组输出状态，保存命令、退出码、数量、超时、stdout/stderr、Python 版本和源码 SHA256。结果目录必须全新。各组在独立子进程运行，执行器以进程组终止超时子进程；执行总预算 420 秒（逐组预算合计 360 秒，另留启动余量），耗尽后尚未执行组记 not_run。任何失败、零用例、跳过、丢失结果或非零退出都不能转为 passed。`--suite` 仅用于定位问题，不代表完整验收。

| suite_id | 实际入口 | 验证范围 | 每次代码交付 | 单组上限 |
| --- | --- | --- | --- | --- |
| syntax | `scripts/test_all.py --suite syntax` | 全部 Python AST、浏览器脚本 JS 语法 | 必跑 | 30 秒 |
| security | `scripts/test_all.py --suite security` | 仓库凭据模式、禁止的本地配置与数据库文件 | 必跑；模式扫描不替代人工审查 | 30 秒 |
| domain | `tests/test_domain.py` | 数值/空值、分母、Juice 阈值、异常响应、后续渠道、锁、中断、旧库、HTML 转义、时区、验收失败聚合 | 必跑 | 60 秒 |
| http | `tests/test_http.py` | 实际回环 HTTP → CLI → 解析 → SQLite → HTML；双模型矩阵、顺序、代理、重定向、超时、闸门和受控时钟调度 | 必跑 | 90 秒 |
| catalog | `tests/test_catalog.py` | 合成文档导入、凭据隔离、渠道管理、无请求报告和模型配置 | 必跑 | 60 秒 |
| browser | `tests/browser_check.cjs` | 完整报告 22 行双模型细分结果、折叠交互、单点图、桌面与 390px 窄屏、横向表格与截图 | 必跑 | 90 秒 |

测试文件注册在统一入口中；新增 `test_*.py` 必须更新清单与发现规则。夹具只允许独立编写于 `tests/`，不读取真实配置、密钥、业务库或渠道响应。HTTP 服务只绑定 `127.0.0.1`，子进程凭据为合成值；浏览器外部 HTTP(S) 请求被阻断。

产品没有配置格式化器或静态类型工具，不宣称这些检查通过。准备发布还必须执行 Docker 构建、CLI、生产 Compose 校验、非 root 私有报告健康检查和发布包检查；CI verify 总时限 25 分钟，publish 10 分钟。OS 漏洞扫描发现 HIGH/CRITICAL 阻止镜像发布。本机容器验收单独记录证据，不把 Python 汇总当成容器通过。真实渠道与长期运行需另行授权并保留对应证据。本产品无第三方 Python 运行时包；这不代表 OS 扫描等于全部安全审查。

## 版本绑定与独立复核

发布镜像使用固定摘要的 Python 3.13 Alpine，另在实际镜像内执行全部 Python unittest（domain/http/catalog），
验证 SQLite、文件锁、信号超时、时区、配置凭据与报告；检查 CA store 非空。此检查不能由 Ubuntu/macOS
宿主机测试替代。报告镜像同样固定摘要并扫描；不通过降低等级或忽略未修复项绕过门禁。

开发测试以 HEAD 和源码指纹绑定；正式独立验收从完整候选 SHA 检出到外置只读副本，在新 venv 和新数据目录运行相同入口。独立审查应覆盖原始三文件版本到候选提交的完整差异、调用链、测试实现和报告。最终结论绑定实际候选 SHA；修复后按影响重跑，保留首次失败证据。
