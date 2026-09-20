# 模型与协议检测测试清单 v2

范围：`features/protocol_admission`、公共渠道入口与模型发现共用逻辑、准入挂载与页面入口。基线为 `ef111ba4ae15e6421ceb6ea0378edd41a15dbaad`。规则 `docs/ai-rules` v1.0。新增后端 Python/FastAPI；无构建、依赖或部署配置变更。

统一入口为 `python -B scripts/verify_admission.py --output <仓库外新证据目录>`；独立验收追加 `--sha <当前完整40位SHA>`，必须干净 detached 检出。输出 `verification.json` 绑定源文件指纹、收集数、逐组退出码、失败/跳过及预算；失败、超时、未跑和零用例不被汇总成功覆盖。需设置隔离环境、Playwright 模块及浏览器，完整实际命令登记在外置交接单。

| suite_id | 入口 | 责任 / 条件 | 上限 |
| --- | --- | --- | --- |
| protocol-inspect | `python -B scripts/inspect_protocol_admission.py` | 本功能每次交付；只读模板与可选渠道摘要，零请求、零解密 | 60 秒 |
| protocol-browser | `node scripts/protocol_admission_ui.cjs` | 每次本功能 UI 交付；公共/临时渠道、列表获取与缓存、预览失效、协议矩阵、真实 HTTP 本地 Mock、停止、历史与导出、窄屏 | 300 秒 |
| workbench-python | `python scripts/test_all.py` | 完整既有三引擎 selftest + unittest discover；包含下述协议契约测试 | 900 秒 |
| workbench-web / security / syntax | 原统一入口中的三个命令 | 页面契约、安全扫描、Python/JS/HTML 语法 | 120 / 120 / 300 秒 |
| workbench-e2e / browser | 原统一入口中的 E2E 与 UI smoke | 工作台挂载、鉴权、现有功能与报告兼容 | 各 600 秒 |
| diagnosis-browser / inspect、image-inspect、model-coverage-browser / inspect | 原统一入口保留的五组 | 工作台既有必需检查 | 原登记预算 |
| legacy-acceptance | `scripts/acceptance.py` | 正式独立验收的既有必需组 | 1200 秒 |

协议领域及 API 用例由 `tests/test_protocol_admission.py` 自动发现，覆盖：固定项目数量/模型标识/精确映射、JSON/Alpha Search 可选字段与内容待确认、流式结束/断流/字段异常、工具调用与 usage、错误分类和一次尝试、实际本地 socket、禁止非白名单出站与重定向、各超时和响应大小、取消/单运行/进程恢复、鉴权/超时待确认、明确不支持、工具/搜索独立于普通调用、旧渠道迁移与资料保留、临时密钥不落报告、验证错误不回显、HTML/JSON 一致。

合成夹具仅为独立编写的标准查询和响应；所有网络回归通过本地 Mock，不读取业务数据库、不使用客户令牌、不发送通知。运行产物、截图及依赖环境位于统一的仓库外数据根。标准库 unittest 无新增测试依赖。未经实际执行，不把源码审查、Mock 或历史版本的通过替代本候选的验证。

本轮不包含NewAPI配置或分组准入。真实供应商能力、质量和稳定性未验证；结果只说明对应请求实测状况。构建/容器按 T2/T9 条件不适用；本改动未修改构建/依赖/启动配置，也未准备发布。

新增对照：列表获取不能触发模型调用；公共渠道复用缓存且连接变更失效；临时密钥不入库；Bearer鉴权失败后有限切换x-api-key和分页；混合成功/鉴权/明确不支持/工具错误结果；历史version=1仍可读；配置表单移除。首次失败及修复后结果保留外置证据。
