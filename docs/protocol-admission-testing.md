# 协议准入测试清单 v1

范围：`features/protocol_admission`、公共渠道新增协议资料、准入挂载与页面入口。基线为 `56d25d8b3db4d1524295cbdbd9f5f94188bd58ec`。规则 `docs/ai-rules` v1.0。新增后端 Python/FastAPI；无构建、依赖或部署配置变更。

统一入口为 `python -B scripts/verify_admission.py --output <仓库外新证据目录>`；独立验收追加 `--sha <当前完整40位SHA>`，必须干净 detached 检出。输出 `verification.json` 绑定源文件指纹、收集数、逐组退出码、失败/跳过及预算；失败、超时、未跑和零用例不被汇总成功覆盖。需设置隔离环境、Playwright 模块及浏览器，完整实际命令登记在外置交接单。

| suite_id | 入口 | 责任 / 条件 | 上限 |
| --- | --- | --- | --- |
| protocol-inspect | `python -B scripts/inspect_protocol_admission.py` | 本功能每次交付；只读模板与可选渠道摘要，零请求、零解密 | 60 秒 |
| protocol-browser | `node scripts/protocol_admission_ui.cjs` | 每次本功能 UI 交付；公共渠道资料、预览失效、四模板、真实 HTTP 本地 Mock、停止、历史与导出、窄屏 | 300 秒 |
| workbench-python | `python scripts/test_all.py` | 完整既有三引擎 selftest + unittest discover；包含下述协议契约测试 | 900 秒 |
| workbench-web / security / syntax | 原统一入口中的三个命令 | 页面契约、安全扫描、Python/JS/HTML 语法 | 120 / 120 / 300 秒 |
| workbench-e2e / browser | 原统一入口中的 E2E 与 UI smoke | 工作台挂载、鉴权、现有功能与报告兼容 | 各 600 秒 |
| diagnosis-browser / inspect、image-inspect、model-coverage-browser / inspect | 原统一入口保留的五组 | 工作台既有必需检查 | 原登记预算 |
| legacy-acceptance | `scripts/acceptance.py` | 正式独立验收的既有必需组 | 1200 秒 |

协议领域及 API 用例由 `tests/test_protocol_admission.py` 自动发现，覆盖：模板去重/模型映射、JSON/Alpha Search 可选字段与内容待确认、流式结束/断流/字段异常、工具调用与 usage、错误分类和一次尝试、实际本地 socket、禁止非白名单出站与重定向、各超时和响应大小、取消/单运行/进程恢复、来源未知/RC26 类型阻断/不无条件灰度、旧渠道迁移与资料保留、临时密钥不落报告、验证错误不回显、HTML/JSON 一致。

合成夹具仅为独立编写的标准查询和响应；所有网络回归通过本地 Mock，不读取业务数据库、不使用客户令牌、不发送通知。运行产物、截图及依赖环境位于统一的仓库外数据根。标准库 unittest 无新增测试依赖。未经实际执行，不把源码审查、Mock 或历史版本的通过替代本候选的验证。

第二阶段的 NexusAPI 实际 channel_id、重试链路、计费、真实供应商能力和灰度门槛未验证；质量与稳定性使用原工具，并没有从协议探测自动推导。构建/容器按 T2/T9 条件不适用；本改动未修改构建/依赖/启动配置，也未准备发布。
