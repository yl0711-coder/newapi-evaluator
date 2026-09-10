# 验收覆盖索引

实现与测试均以本地 Mock 为范围。本文件用于定位证据，不代替指定提交上的实际测试结果。

| 要求 | 实现 | 行为验证/产物 |
| --- | --- | --- |
| 中转站 Skill、固定目录、内部模式路由、继续恢复、本地 Git 边界 | 安装目录 relay-station/SKILL.md、agents/openai.yaml、AGENTS.md、PLAN.md | skill-creator quick_validate；只读检查引用路径、名称、策略与路由 |
| 单号可用性、连续成功率、TTFT、耗时、完整率、输出速度、并发和恢复 | adapter.py、runner.py account、metrics.py | test_protocol、test_account_limits_and_raw_artifacts；account/summary.json |
| 独立账号状态、不同并发/延迟/限流/故障率 | mock.py Account、MockServer | test_per_account_capacity_and_recovery、test_independent_account_quality |
| 逐级增号、理论与实际容量、一个及一半账号退出、扩容效率 | runner.py pool | test_pool_scaling_and_capacity_drop；9 个 pool_scenarios |
| 网关可配阶梯 10 至 1200、稳定在途、吞吐、队列、连接错误、资源、降速、崩溃、恢复 | runner.py gateway、resources.py | test_gateway_slow_collapse_and_recovery、test_pool_queue_is_measured；gateway/summary.json |
| 单次持续流、多个顺序步骤 | runner.py long_task | E2E 约 15 秒长流及 8 个步骤；test_long_task_auto_recovery_and_completeness |
| 唯一步骤 ID、逐步检查点、安全恢复、不重跑已确认步骤 | checkpoint.py SQLite WAL、文件锁、持久化尝试次数 | test_resume_in_new_executor_skips_confirmed_steps、test_checkpoint_exclusion_and_mismatch |
| 指定步骤 429、500、读超时、连接超时、断网和断流；首次失败、恢复次数/耗时、最终完整性 | runner.py long_task、adapter.py | test_long_task_failure_types_at_selected_step；Mock 输出散列匹配 |
| 401、429、500、普通响应、正常与慢速 SSE、缺少结束事件、SSE 损坏、中途断流、超时 | mock.py、adapter.py | test_normal_response、test_normal_and_slow_sse、test_status_errors、test_timeout_faults、test_stream_faults |
| 抖动、账号禁用、部分账号失效、整个上游短暂不可用、定时恢复 | mock.py、runner.py chaos | test_temporary_account_and_upstream_outage、test_chaos_injection_and_automatic_restoration；14 个 fault_scenarios |
| 七个统一极限字段、号池额外字段、low_confidence、分位数 | metrics.py | test_limits_from_request_results、test_percentiles_interpolate、test_insufficient_samples_and_censoring、test_one_failure_is_not_collapse |
| 统一 CLI、OpenAI 兼容、Mock server、各模式与报告命令 | cli.py、relay-lab | test_mock_server_command、test_config_inspection_safe；scripts/e2e.py 执行全部公共模式 |
| Ctrl+C 部分报告、突停后恢复已落盘指标 | cli.py signal handler、report.py | test_ctrl_c_saves_partial_report、test_rebuild_after_abrupt_stop |
| 凭据、敏感内容、URL 不进入结果、日志或代码 | 白名单 Result 与 summary、固定错误枚举、security.py | test_redaction_nested_values、test_sensitive_response_and_config_never_written、test_bad_yaml_never_echoes_sensitive_input、repo_security_scan.py |
| 默认只允许本地 Mock、真实模式需 --confirm-live、禁用代理/重定向 | security.py、network.py、adapter.py | test_url_policy、test_socket_guard_blocks_before_connect、test_external_target_rejected_before_request、test_environment_proxy_disabled、test_redirect_not_followed、test_local_nonmock_is_rejected |
| README、AGENTS、.env.example、统一及六类配置、源代码、自动测试、一键命令 | 根目录、configs/、relay_lab/、tests/、scripts/ | 仓库清单和独立验收源代码检查 |
| JSONL 原始指标、JSON 汇总、Markdown 报告、独立验收报告 | report.py、scripts/acceptance.py | 数据目录 acceptance-<SHA>/examples/{account,pool,gateway,long-task,chaos}/ 与 acceptance-report.md |
| 功能提交、detached 验收、准确 40 位 SHA、无远程/合并/push/PR | 空 main 基线、本地 feature 分支、独立 worktree | acceptance.json、Git HEAD/branch/status/remote/worktree 检查 |
| 不改其他项目、不发真实请求 | 固定写入范围、只读范围审计、socket 审计 | protected-before/final 指纹、scope-review.json、各模式 network_policy.socket_audit |

所有极限点来自测量样本，不从 Mock 配置阈值直接拷贝。测试中的确定性模型阈值用于验证实际响应行为，不能替代实测统计。独立验收需对新提交重新运行，不接受旧提交报告。
