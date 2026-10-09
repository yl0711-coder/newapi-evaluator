# 分层完整性测试清单 v1

适用规则 docs/ai-rules v1.0；本功能属工作台定时/控制面，不改 relay_lab 共享引擎。实验室五模式仍纳入既有 E2E 和独立 legacy acceptance。研发/验收日志、环境、数据和截图均在仓外新目录，不加载业务 .env、真实库、凭据、通知设置或客户报告。原上游复现是 source 证据，不能替代产品测试。

完整入口：`python -B scripts/verify_integrity.py --output <新外置目录>`；独立验收在干净 detached 完整 SHA 的候选加 `--sha <40位SHA>`。PLAYWRIGHT_MODULE、PLAYWRIGHT_BROWSERS_PATH、EVAL_TEST_ARTIFACT_ROOT 显式指向外置环境，PYTHON_EXECUTABLE由执行器设置。执行器采用白名单环境、单组/总时限、收集数、退出码和失败优先汇总；开发18组，总3000秒；独立19组含legacy，总4200秒。失败首次日志保留，新目录记录修复重跑原因，不以旧快照替代当前版本。

| suite_id | 入口 | 验证与时限 |
| --- | --- | --- |
| protocol-inspect / admission-inspect / diagnosis-inspect / image-inspect / model-coverage-inspect | 清单对应 inspect CLI | 只读、零发送、脱敏配置；每组60秒 |
| integrity-inspect | scripts/inspect_integrity.py | 全bundle/运行hash、17/16类区别、零数据库/凭据/网络；60秒 |
| workbench-python | scripts/test_all.py --output | 全既有selftests+unittest发现含新六模块；900秒 |
| workbench-web | scripts/test_web.js | 既有页面契约；120秒 |
| workbench-security | scripts/repo_security_scan.py . | 全注册源码及合成夹具安全；120秒 |
| syntax | scripts/diagnosis_syntax.py | Python/JS/CJS/HTML脚本；300秒 |
| workbench-e2e | scripts/e2e.py --output | 原实验室五模式真实本地Mock链；600秒 |
| workbench-browser | scripts/ui_smoke.cjs | 原工作台导航/报告/主题/窄屏；600秒 |
| protocol-browser / diagnosis-browser / model-coverage-browser / monitor-control-browser | 清单对应 *.cjs | 实际HTTP/浏览器、旧HMAC/控制面与覆盖兼容；每组300秒 |
| integrity-browser | scripts/integrity_ui.cjs | 已记录渠道零身份/生产快照/target三项首测，用户显式上线后零target选计划、完整192题当前成绩、>3美元继续、显式baseline、参考导入、KBF取消/恢复unknown不重发、HLwY、账号离线、导出、390/900/1440屏；600秒 |
| build-container | scripts/image_quality_container.py --output | 当前镜像all/image-quality健康、integrity静态页/API/默认off/零任务与离线状态，自有容器精确清理；900秒 |
| legacy-acceptance | scripts/acceptance.py --sha --output | 仅独立干净提交，既有实验室全量/持续/混合负载；1200秒 |

领域映射：test_schedule_candidates 覆盖用户定义online、recorded拒排期、停用、缺/不可解密凭据、零身份/生产快照、非法协议、原子创建/回滚；test_scheduled_integrity 覆盖230/35、五工作日持久轮转、健康依赖/过期、重复slot、提交后崩溃恢复、Registry 状态/连接/映射漂移零发送、连续异常缺测/漂移/冷却、旧报告/export/baseline保留。test_integrity_execution 覆盖三协议真实解析、终态、usage/reasoning、请求/token/time/租约原子边界、同日>3/未知价格、unknown/取消/漂移/正文不落盘。test_kbf_review 覆盖参考hash/授权/自测/覆盖、真实消费者、冷却/幂等、unknown、数值JSON浏览器往返。test_integrity_scoring 覆盖17/16类、UNLISTED、两答门槛、10对McNemar/四family Holm、invalid/not_run/条件拒比较、白名单元数据。test_integrity_monitor 覆盖异步HMAC、nonce/body、principal隔离、旧nerfed错误、取消/恢复与主动consumer。

测试替换上游和不可控时钟，真实执行解析、许可、持久化和报告。浏览器完整日常为单渠道202次，五渠道/五日轮转由领域测试证明，不能宣称五日真实运行。usage/费用为合成上报，不是真账单。未开展真实Monitor联调、真实上游、API校准或实测功效，不发送通知、不部署。

本地修改记录：候选基线088337c包含已有Monitor执行/验收修复；本次新增integrity纯计算/资产与有限执行、分层调度/Registry候选、前端和v2HMAC。取消旧美元硬门禁并同步消费者/页面/测试；离线冲突错误码、Chat DONE、terminal-job发布恢复、JSON数字往返reference hash。逐资产来源和修改许可见 features/integrity/NOTICE.md；实际首次失败、快照指纹、逐组结果和独立复核在外置任务证据目录，由最终交接绑定完整SHA。

统一主链回归 test_unified_integrity：零历史渠道可用、八个独立请求及三个结果、同键复用/冲突、健康失败七项skipped、单方法invalid不中断其他方法、usage缺失继续、取消unknown恢复不重发、Sol unsupported、连接/映射漂移零发送；test_integrity_monitor另覆盖新nerfed-api类型4尝试实际消费者与原离线契约保留。

integrity-browser已扩充实际浏览器三项主链：新上线零target/零历史→一次选择/启动→八次独立HTTP→三份有效结果→导出→另轮取消/恢复unknown不重发→刷新历史；并保留原192canary/HLwY/KBF/离线和窄屏检查。当前完整发现含新七个域模块（test_unified_integrity为第七），必需组仍18/19。

本次独立产品审查修复回归：噪声grid、5000rows/numbers/digits、深层JSON为有界投影，子项invalid仍执行后续方法；同caller重claim的旧session reserve/complete/finish/heartbeat全部fenced；已提交192题canary在健康/时间窗过期、目标漂移及slot缺绑定后纯重建，真实分母和原观测时间保留，零重发。容器清理Mock以实际blocked Docker操作就绪为超时起点，启动另有5秒界限，避免将解释器启动负载误当cleanup超时；原失败保留外置记录。
