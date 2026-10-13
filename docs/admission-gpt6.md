# GPT-6 Astra 准入测试

模型 `gpt-6-astra` 在预设、手动输入和后端请求中固定使用 Responses；旧客户端提交其他协议时也如此，并在 run_started 返回实际协议。其他模型保留原行为；准入独立扩展协议，不扩大非流式模块的协议声明。

## 请求与报告

- POST `/v1/responses`，使用 input、instructions、stream: true、store: false。接受 Base URL、完整 Responses/Chat Completions/Messages 地址及自定义路径前缀。
- 快测默认 reasoning.effort: low，每题 max_output_tokens: 4096，双端相同。4096 是测试预算，不是模型最大能力；额度包含推理 tokens，耗尽时记录截断。Responses 不发送 max_tokens、max_completion_tokens、temperature、top_p 或 logprobs。
- 技术页面、技术 HTML 和 JSON 保留请求协议、额度、推理强度、输入/输出/推理 tokens、响应状态及原有回答。缺失 usage 为 null；包含推理或缺少明细时不声称正文 Token/s，保留首答、总耗时及字符速度。普通报告保持原有摘要边界，历史版本继续可读。
- 按输出块的 done 与最终 output 补齐正文而不重复；不一致或终态之后到达正文均记协议异常。仅靠结束事件补齐的正文不计算流式 Token/s。成功要求完整 response.completed、无错误/拒答/截断，并有答案正文。半途失败、缺结束、格式错误和仅推理内容不会通过，不依赖 DONE。总时限 240 秒，包含建立连接与读取；取消关闭本次流，原有 HTTP/read timeout 继续生效。

官方依据：[模型迁移](https://developers.openai.com/api/docs/guides/latest-model#migration-quickstart)、[Responses 参数](https://developers.openai.com/api/reference/python/resources/responses/methods/create)。工具调用、WebSocket、中途转向和多轮状态不在本次范围。

## 注册测试清单 v2

依据正式规范 v1.0。Python 3.13 独立 venv 按 requirements.txt 安装，Node、Playwright 与 Chromium/Edge。venv、TMPDIR、数据与报告位于仓库外的中转站极限测试数据目录，环境白名单不继承业务配置。

统一入口：`python -B scripts/verify_admission.py --output <仓库外全新证据目录>`。独立验收使用干净 detached HEAD 和独立 venv，追加 `--sha <40位SHA>`。支持 PLAYWRIGHT_MODULE、PLAYWRIGHT_CHANNEL、UI_TEST_PORT。动态发现用例数并核对既有引擎收集契约，失败/跳过/缺组/超时不报通过。

| suite_id | 入口 | 验证与单组上限 |
| --- | --- | --- |
| admission-inspect | scripts/inspect_admission.py --protocol openai | 不带凭据检查实际协议、参数、脱敏主机、指纹和时间；60s |
| workbench-python | scripts/test_all.py | 三引擎及完整 unittest；Responses 请求/SSE/异常/总时限、双端预热、报告、协议隔离；900s |
| workbench-web | node scripts/test_web.js | 既有页面契约；120s |
| workbench-security | scripts/repo_security_scan.py . | 全部源码与合成夹具安全；120s |
| syntax | scripts/diagnosis_syntax.py | Python、JS/CJS/MJS 及内联脚本；300s |
| workbench-e2e | scripts/e2e.py --output 分配目录 | 五模式本地 HTTP/SSE 及报告；600s |
| workbench-browser | node scripts/ui_smoke.cjs | 现有流程，加 GPT-6 预设/手填锁协议、双端 HTTP、三类导出、停止、宽窄屏；600s |
| diagnosis-browser | node scripts/diagnosis_ui.cjs | 继承诊断页面浏览器回归；300s |
| diagnosis-inspect | python -m features.diagnosis.inspect --data-dir 分配目录 | 继承诊断只读检查；60s |
| image-inspect | python -m features.image_quality inspect-config --config tests/fixtures/image_quality/config.json | 继承生图只读检查；60s |
| legacy-acceptance | scripts/acceptance.py --sha 完整SHA --output 分配目录 | 独立验收额外运行既有十组验收，含持续/突发 Mock；1200s |

当前开发十八组总体预算 1800 秒，独立验收十九组总体预算 3000 秒；注册清单以 `scripts/inspect_test_plan.py admission` 为准。Token专项和后台队列新增组见 [功能和接口](token-usage-queue.md)。复用既有失败聚合与进程组清理，超时/取消终止本次子进程，未执行组保留 not_run。共享聚合回归位于 tests/test_diagnosis_verification.py；当前执行器失败聚合和收集/执行中取消回归位于 tests/test_admission_verification.py；Responses 与只读入口回归位于 tests/test_admission_responses.py。只读检查不发请求，不读取渠道库或飞书配置。

准入参数的基础清单不含容器组。涉及工作台生命周期或容器配置时，采用 [完整性清单](integrity-testing.md) 的开发23组、独立24组，并执行 `build-container`；Token 专项与服务器队列变更采用该完整清单，包含主动 off 的定时与空闲刷新。实验室引擎无源码修改，五模式回归仍保留。Mock 不证明真实模型可用性或真实推理表现。

## 六题准入与糖果测试

题库保留原三道简单题和两道推理题，增加 `hard-3`“糖果测试”，合计六题。题意源于 [haowang02/codex-candy-eval](https://github.com/haowang02/codex-candy-eval/blob/main/codex_candy_eval.py)：圆形苹果/桃子/西瓜数量为7/9/8，五角星为7/6/4；形状可触摸辨认，目标是苹果与桃子且形状不同。使用本地中文转述，通过现有候选/参照双端API运行，不执行上游CLI。参考答案21；9颗圆形与12颗五角星保证目标，少于21不能保证。

上游以回答出现独立21判分。Eval沿用准入人工阅读答案及理由的契约，不以数字出现或正常返回认定能力通过、模型身份或降级。Astra仍固定Responses/low/4096；不套用上游CLI的effort条件。六题参与每轮及原预热规则，最多五轮共60个双端测量、30个题对；报告历史、普通与技术导出保留，旧五题历史仍可读取。
