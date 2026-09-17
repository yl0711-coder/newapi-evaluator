# 常用模型覆盖：测试清单 v1

适用 docs/ai-rules v1.0。继承 docs/admission-gpt6.md 和 docs/frontend-style.md 的全部现有必需测试组。环境 Python 3.13、Node 24、独立 venv、Playwright/Edge；产物全部位于仓库外中转站极限测试数据。默认白名单环境、合成输入、本地 Mock，无真实上游或通知。

统一入口：`python -B scripts/verify_admission.py --output <全新外置目录>`。本版本开发共 12 组；独立干净 detached 候选追加 `--sha <40位SHA>` 后为 13 组。总预算分别 1800/3000 秒；单组限时、收集数、失败/跳过/超时、取消清理沿用执行器。独立验收额外运行既有 legacy-acceptance 十组。

| suite_id | 入口与覆盖 | 上限 |
| --- | --- | --- |
| workbench-python | scripts/test_all.py，完整三引擎及 unittest；包含 test_model_coverage.py 的手动获取、分页、失败保留、连接变化、精确映射、稳定样本/过期、接入派生状态、事务回滚、预览变化、去重、只读与鉴权；test_stability_responses.py 的请求/非流式/SSE/拒答/截断/断流/超时/取消/用量 | 900s |
| model-coverage-inspect | scripts/inspect_model_coverage.py；无凭据、无出站，公开配置及模型映射参与指纹，旧库缺表保持只读 | 60s |
| model-coverage-browser | node scripts/model_coverage_ui.cjs；真实本地 HTTP + Edge：人工确认获取、12项、单次 GPT-6 六请求、结果回写、预览请求量、加入/重复跳过、新模型、映射、过滤、获取失败保留、刷新无出站、390/900/1440px | 300s |
| workbench-browser | node scripts/ui_smoke.cjs；既有八页面交互与宽窄屏回归 | 600s |
| 其余继承组 | admission-inspect、workbench-web、workbench-security、syntax、workbench-e2e、diagnosis-browser、diagnosis-inspect、image-inspect，及独立 legacy-acceptance | 见原清单 |

无依赖、构建、容器配置变更，无本次发布，容器构建不触发。workbench.py 仅注册应用内路由与可用性，不改变进程启动命令、容器入口或调度器选择。极限实验室子集未修改；工作台 Python 与 E2E 套件保留该子集既有回归。

检查报告必须关联最终源码快照/提交，独立审查覆盖生产代码、测试、执行器新增注册与源文件扫描入口。测试通过不授权合并、push 或发布。
