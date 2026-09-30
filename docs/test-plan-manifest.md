# 统一测试清单

`scripts/test_manifest.py` 是开发、准入验收、请求诊断和生图集成验收共用的测试组定义入口。每个测试组固定记录：

- `suite_id`：稳定的测试组身份；
- `command`：实际执行命令；
- `timeout_seconds`：单组时间上限；
- `kind`：结果解析所需的分类。

清单只描述测试，不读取业务数据库、不加载凭据、不发真实请求。查看清单使用：

```bash
python -B scripts/inspect_test_plan.py admission
python -B scripts/inspect_test_plan.py diagnosis
python -B scripts/inspect_test_plan.py image-quality
```

独立验收候选版本追加完整 SHA，会显示额外的 `legacy-acceptance` 组：

```bash
python -B scripts/inspect_test_plan.py admission --sha <40位完整SHA>
```

清单检查是只读检查，不替代实际测试。实际验证仍必须使用仓库外的新证据目录，并按 `TESTING.md` 记录 `passed`、`failed`、`incomplete` 和 `not_run` 的真实状态。

## 工作台 Python 执行器

清单中的 `workbench-python` 通过 `scripts/test_all.py --output <新目录>` 执行。执行器内部固定登记四组：`admission-selftest`、`stability-selftest`、`reasoning-selftest` 和 `unittest-discover`。每组在独立进程组中运行，并实施单组超时和 840 秒整体预算；超时只终止该组创建的进程。子进程只继承必要的系统和测试工具环境变量，并使用证据目录下隔离的 `HOME`、临时目录和数据目录，不继承渠道凭据、代理、通知或 Docker 上下文。

`<新目录>/summary.json` 保存总体状态、逐组状态、退出码、执行数量、跳过数量、超时/取消原因和日志路径；每组标准输出保存为 `<suite_id>.log`。失败组不会覆盖后续组的结果，取消或预算耗尽会把当前组记为 `incomplete`，未启动组记为 `not_run`。不带 `--output` 仍可运行历史命令，但证据目录为临时目录，适合本地快速检查。
