# 范围与恢复入口

目标：创建中转站 Skill，并交付五种模式的独立 Mock 实验室，固定本地功能提交后独立验收。真实环境验证尚未开始。

固定基线：main 初始空提交；开发分支 feature/mock-capacity-lab。无远程、无 PR、无合并。

## 本轮工作

- [x] 完整阅读 skill-creator、既有 evaluator 工作流及内部开发/验收模式。
- [x] 创建 relay-station 与项目 AGENTS.md、空基线和功能分支。
- [x] Skill 格式通过 quick_validate；名称、UI 名称、自动调用与两个内部模式引用已核对。
- [x] 异步 HTTP、OpenAI 兼容适配器、Mock 上游/号池/网关及故障注入。
- [x] 单号、号池、网关、长任务、故障五种模式及统一指标/脱敏报告。
- [x] 36 项自动测试通过，覆盖 Ctrl+C、跨执行器检查点恢复、敏感内容不落盘和网络边界；源码安全扫描通过。
- [x] 五模式首轮 Mock E2E 通过；最终版本与提交后独立 E2E 均需要另行留存证据。

## 提交后验收状态

为避免“更新计划又改变待验 SHA”的循环，提交后的权威状态保存在数据目录 `delivery.json`、`acceptance-<完整SHA>/acceptance.json` 和 `acceptance-report.md`，不通过修改此计划来宣称验收完成。继续时必须核对这些文件的 SHA 与实时 HEAD 相同。

独立验收会重新执行聚焦、完整、安全和五模式 E2E；验收副本必须 clean detached HEAD，使用独立离线环境。示例原始指标、summary 和 report 位于该验收目录的 examples/ 数据子目录。

范围审计同时保存既有项目指纹。如观察到其他工作区在本轮期间被外部活动改动，应记录差异并保留，不将“本任务未写入其他项目”夸大为“所有其他工作区期间完全不变”。本任务写入范围仅 Skill、开发项目、指定验收检出和运行数据目录。

## 继续时

读取实时 Git 状态、当前 HEAD、数据目录对应 SHA 的验收报告。验收失败只在开发目录修复，提交后重验。不要依靠计划复选框宣称完成。

## 验收入口

`python scripts/test_all.py` 完整自动测试；`python scripts/e2e.py --output <数据目录中的新目录>` 五种模式端到端；`python scripts/repo_security_scan.py .` 安全扫描。所有 Python 命令设置 `PYTHONDONTWRITEBYTECODE=1`；依赖环境、TMPDIR 与运行证据都放在指定数据目录。
