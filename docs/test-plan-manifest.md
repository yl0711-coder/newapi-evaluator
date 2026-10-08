# 统一测试清单

`scripts/test_manifest.py` 是开发、准入验收、请求诊断、生图集成验收与工作流控制面专项门禁共用的测试组定义入口。每个测试组固定记录：

- `suite_id`：稳定的测试组身份；
- `command`：实际执行命令；
- `timeout_seconds`：单组时间上限；
- `kind`：结果解析所需的分类。
- `expected_modules`：需要逐模块核对收集的 Python 模块；其他组为空列表。

清单只描述测试，不读取业务数据库、不加载凭据、不发真实请求。以下 `python` 指仓库支持的 Python 3.10+ 解释器；实际测试环境按候选版本安装 `requirements.txt`。查看清单使用：

```bash
python -B scripts/inspect_test_plan.py admission
python -B scripts/inspect_test_plan.py diagnosis
python -B scripts/inspect_test_plan.py image-quality
python -B scripts/inspect_test_plan.py workflow-control-plane
```

`admission` 或 `diagnosis` 计划追加候选完整 SHA，会显示额外的 `legacy-acceptance` 组；`image-quality` 与 `workflow-control-plane` 不追加该组：

```bash
python -B scripts/inspect_test_plan.py admission --sha <40位完整SHA>
```

清单检查是只读检查，不替代实际测试。实际验证仍必须使用仓库外的新证据目录，并按 `TESTING.md` 记录 `passed`、`failed`、`incomplete` 和 `not_run` 的真实状态。

## 设备证据根

`test_all.py`、`verify_workflow_control_plane.py` 和 `verify_image_quality.py` 共用证据根校验：先取 `--artifact-root`，其次 `EVAL_TEST_ARTIFACT_ROOT`；macOS 未配置时沿用 `/Users/lmurder/Desktop/api中转站/中转站极限测试数据`，其他平台必须显式配置，不回落到源码目录或业务数据目录。

证据根须预先建立、为绝对路径，解析后与仓库不互相包含；`--output` 必须是该根内尚不存在的新子目录。现有目录、符号链接目标落到根外或源码内均拒绝。其他设备/CI 的目录映射仍须符合 [数据隔离规则](ai-rules/CODING.md#g6-数据与执行边界)。配置范式（占位符需替换，证据根需先准备）：

```bash
python -B scripts/verify_workflow_control_plane.py \
  --artifact-root /absolute/external/test-artifacts \
  --output /absolute/external/test-artifacts/workflow-run-001
```

Python 环境来自当前候选的 `requirements.txt`；专项门禁还需要 Node、Playwright 包和可用浏览器。可通过 `PLAYWRIGHT_MODULE` 指定已有包、`PLAYWRIGHT_CHANNEL` 指定已安装浏览器；未准备依赖是 `incomplete`，不能记为“不适用”。这些设备/工具配置不是业务凭据或真实请求授权。

`verify_admission.py` 与 `verify_diagnosis.py` 的外层 CLI 没有新增 `--artifact-root`；它们透传已配置的 `EVAL_TEST_ARTIFACT_ROOT`，让内部 `workbench-python` 使用同一映射。因此在非 macOS 上从这两个入口运行时，先准备仓库外证据根并设置该变量，`--output` 选择根内的新子目录。

已有 Linux 调用方也显式准备隔离根：`.github/workflows/release.yml` 的 `Run automated tests` 步骤要求 `RUNNER_TEMP`，在其中用 `mktemp` 建立 `eval-tests.*`；`Makefile` 的 `test` 目标在 Compose 临时容器的 `/tmp` 建立 `eval-tests.*`。两者都设置 `EVAL_TEST_ARTIFACT_ROOT`、`PYTHONDONTWRITEBYTECODE=1`，并将 `root/evidence` 传给 `test_all.py --output`，测试非零退出会传回调用方。临时容器使用 `--rm`，其 `/tmp` 证据不保证在宿主持久保留；正式验收仍需外置可保留的产物目录。这些配置不等于远程 CI 已运行或覆盖了全部必需组。

## 工作台 Python 执行器

清单中的 `workbench-python` 通过 `scripts/test_all.py --output <新目录>` 执行。执行器内部固定登记四组：`admission-selftest`、`stability-selftest`、`reasoning-selftest` 和 `unittest-discover`。每组在独立进程组中运行，并实施单组超时和 840 秒整体预算；超时只终止该组创建的进程。子进程只继承必要的系统和测试工具环境变量，并使用证据目录下隔离的 `HOME`、临时目录和数据目录，不继承渠道凭据、代理、通知或 Docker 上下文。

`<新目录>/summary.json` 保存总体状态、逐组状态、退出码、执行数量、跳过数量、超时/取消原因和日志路径；每组 stdout/stderr 合并保存为 `<group_id>.log`。`failed` 计数取框架失败/错误记录，不重复累加详细 `FAIL/ERROR` 与同一汇总；`failed_test_count` 另记可识别的唯一失败用例数，无法确认精确计数时由 `failure_count_exact=false` 标明。已知失败优先：即使随后超时、取消、整体预算耗尽或退出状态未知，仍保留 `failed`，同时记录 `reason` 和 `timed_out`。只有执行缺口而无已知失败时为 `incomplete`，未启动组为 `not_run`；其他组成功不能覆盖失败。零用例、必需跳过、非零退出或框架证据不完整不能输出全量成功。不带 `--output` 时仍在有效证据根内运行临时目录，结束后清理，适合本地快速检查。

## 工作流控制面专项门禁

`workflow-control-plane` 固定登记以下六组，使用 `scripts/verify_workflow_control_plane.py --output <新目录>` 执行：

| suite_id | 实际入口 | 单组上限（秒） |
| --- | --- | --- |
| `workflow-python` | `python scripts/verify_workflow_control_plane.py --python-tests` | 900 |
| `workflow-syntax` | `python scripts/diagnosis_syntax.py` | 300 |
| `workflow-security` | `python scripts/repo_security_scan.py .` | 120 |
| `workflow-web` | `node scripts/test_web.js` | 120 |
| `workflow-browser` | `node scripts/model_coverage_ui.cjs` | 300 |
| `monitor-control-browser` | `node scripts/monitor_control_plane_ui.cjs` | 300 |

`workflow-python.expected_modules` 为 `tests.test_model_coverage`、`tests.test_monitor_internal`、`tests.test_monitor_execution`、`tests.test_monitor_transport`、`tests.test_workflow_control_plane`、`tests.test_test_all` 和 `tests.test_test_manifest`。Python 子入口输出 `WORKFLOW_COLLECTION` 与 `WORKFLOW_RESULT`，必须逐模块收集到正数用例、无导入错误、执行数等于注册数且无必需跳过；不能用单一 `OK` 或退出码代替收集核对。语法、安全和浏览器组还核对各自结构化结果及正数检查量；浏览器要求实际本地 `mockRequests>0`。

整体默认预算 1800 秒，可用 `--overall-budget` 配置正有限秒数。执行器预登记六组，用各自隔离的 `HOME`、临时目录和数据目录运行，按有效时限终止自己创建的进程组。每组 stdout/stderr 和退出码写入 `<suite_id>.log` 与 `summary.json`。超时、取消、信号/未知退出及依赖缺口在无已知失败时为 `incomplete`，未运行组为 `not_run`；已有确定失败时该组和总体保持 `failed`，同时保留中断 `reason` 与 `timed_out`，不因其他组成功改为通过。汇总的 `complete` 表示记录完成，不表示测试通过；总体 `passed` 才返回 0，取消返回 130（质量状态仍可为 `failed`），其余返回 1。

两个浏览器组只使用隔离工作台、合成签名调用方和回环 Mock；清单登记与本地通过都不证明真实 Monitor 联调、生产命中或容量。专项门禁不能代替工作台全量清单、最终 SHA 的独立验收与适用容器检查。
