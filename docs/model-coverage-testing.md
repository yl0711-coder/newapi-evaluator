# 常用模型覆盖：测试清单 v1

适用 docs/ai-rules v1.0。继承 docs/admission-gpt6.md 和 docs/frontend-style.md 的全部现有必需测试组。环境 Python 3.13、Node 24、独立 venv、Playwright/Edge；产物全部位于仓库外中转站极限测试数据。默认白名单环境、合成输入、本地 Mock，无真实上游或通知。

统一入口：`python -B scripts/verify_admission.py --output <全新外置目录>`。本版本开发共 12 组；独立干净 detached 候选追加 `--sha <40位SHA>` 后为 13 组。总预算分别 1800/3000 秒；单组限时、收集数、失败/跳过/超时、取消清理沿用执行器。独立验收额外运行既有 legacy-acceptance 十组。

| suite_id | 入口与覆盖 | 上限 |
| --- | --- | --- |
| workbench-python | scripts/test_all.py，完整三引擎及 unittest；包含 test_model_coverage.py 的手动获取、分页、失败保留、连接变化、精确映射、稳定样本/过期、接入派生状态、事务回滚、预览变化、去重、只读与鉴权；test_stability_responses.py 的请求/非流式/SSE/拒答/截断/断流/超时/取消/用量 | 900s |
| model-coverage-inspect | scripts/inspect_model_coverage.py；无凭据、无出站，公开配置及模型映射参与指纹，旧库缺表保持只读 | 60s |
| model-coverage-browser | node scripts/model_coverage_ui.cjs；真实本地 HTTP + Edge：人工确认获取、13项、单次 GPT-6 六请求、结果回写、预览请求量、加入/重复跳过、新模型、映射、过滤、获取失败保留、刷新无出站、390/900/1440px | 300s |
| workbench-browser | node scripts/ui_smoke.cjs；既有八页面交互与宽窄屏回归 | 600s |
| 其余继承组 | admission-inspect、workbench-web、workbench-security、syntax、workbench-e2e、diagnosis-browser、diagnosis-inspect、image-inspect，及独立 legacy-acceptance | 见原清单 |

共用模型更新采用[完整性测试清单](integrity-testing.md)的开发27组及独立28组，包含daily-checks-browser与两项容器验证。新增回归接入已有test_model_coverage.py、test_daily_checks.py及daily_checks_ui.cjs：单来源确认读取、自动鉴权回退、公共目录读取与添加、重复不覆盖、跨家族来源及减少/空列表/失败均只增不删、跨渠道凭据拒绝、失败保留、连接变化失效；已打开的每日页面刷新后保留当前选择，九类测试入口共用选项，并使用新ID实际发送每日Mock请求。protocol-browser验证目录与渠道声明合并后刷新仍保留，且请求量不增加。无依赖或启动配置变更，不发布镜像。极限测试仅由工作台common.js提供模型建议，不修改relay_lab引擎；工作台Python与E2E保留该子集回归。

检查报告必须关联最终源码快照/提交，独立审查覆盖生产代码、测试、执行器新增注册与源文件扫描入口。测试通过不授权合并、push 或发布。
