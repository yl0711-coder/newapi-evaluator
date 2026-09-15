# 项目事实与执行清单

规则 v1.0；此表记录实际代码入口及审查基线，不宣称历史实现已经满足所有规则。每次任务核对实时 Git/README，不能把本表的路径或命令视为远端/真实调用授权。

## 职责与维护来源

| 项目类型 | 维护范围 | 运行技术与身份 |
| --- | --- | --- |
| 中转站极限测试实验室 | `relay_lab/` 五模式引擎、CLI、指标、配置、安全及独立控制台；`tests/`、`configs/`、`scripts/` 的对应测试 | Python 3.11+；httpx、PyYAML；独立 Git common-dir。CLI/控制台既有实现保留，新增 Web API 按 G5 使用 FastAPI |
| 模型测试工作台及其功能 worktree | `workbench.py`、`shared/`、`features/admission`、`features/stability`、`features/reasoning`、工作台静态资源及 `features/capacity` 适配 | Python 3.10+，FastAPI，HTML/JS/CSS，httpx、SQLite；实际依赖见 requirements.txt |
| newapi-evaluator-upload 系列与 api-envantor | 独立版本的渠道质量评估平台；按所在版本的 README、run_selftests.py 或 selftest.py 核对 | 不把工作台 scripts/test_all.py 当成这里必然存在的入口 |
| 国产模型速度快测、定时稳定性测试、nexus-shell | 既有单工具与门户；已整合功能的历史源码仍按本次任务明确范围维护 | Python 工具及 Vue 前端保留既有栈，不因 G5 自动迁移 |
| 验收、存档、提取副本 | 只读审查或指定提交验证，不编辑产品代码 | 同 SHA 且文件相同可复用静态审查；独立验收仍需独立环境和新数据 |

`newapi-evaluator-workflow-v2` 是工作台仓库的一个 worktree，目录名不能证明它属于另一个产品。其他目录身份依据 Git common-dir、SHA 和实际源码核对；名称含“副本”不能替代证据，也不授权删除。

## 实验室与工作台映射

实验室 `relay_lab/*.py` 与 `relay_lab/web/` 是极限子集主维护来源；工作台同名 `relay_lab/` 保存已集成版本。实验室 `configs/` 的适用配置对应工作台 `configs/relay-lab/`。工作台的 `features/capacity/`、统一鉴权、模块挂载、`PLATFORM_DATA_DIR` 映射、`relay_lab/web/integration.css` 等属于工作台适配；按实际文件差异验证，不整目录覆盖。正在开发的长任务/渠道恢复分支不因目录存在就视为已合入或已验收，先核对其来源与实验室维护方向。

## 安全环境准备

本机在 `/Users/lmurder/Desktop/api中转站/中转站极限测试数据` 下为本次检查新建隔离目录；在其中建立 venv、TMPDIR、PLATFORM_DATA_DIR、RELAY_LAB_DATA_DIR 和证据目录，设置 PYTHONDONTWRITEBYTECODE=1。不得加载业务 .env、主密钥、渠道库或通知配置。其他设备显式选择仓库外隔离根，写入任务环境配置。依赖安装入口为所审版本 requirements.txt；环境准备不得调用真实业务服务。完整命令在交接单中填写实际工作目录、解释器和产物路径，不把变量未定义的示例当成功证据。

## 已有测试组与缺口

以下命令在对应源码根和已准备的隔离环境运行。预算是本规范首轮执行上限，超过后记 incomplete 并保留结果；不得当作经过基准测试的性能阈值。实际调度器需实施上限，当前历史脚本内部缺少总超时/汇总的事实仍须登记问题。

| suite_id | 命令或入口 | 触发与上限 | 结果及已知缺口 |
| --- | --- | --- | --- |
| workbench-python | `python scripts/test_all.py` | 工作台代码交付，15 分钟 | 三引擎 selftest + unittest discover；须核对收集数、跳过和子进程结果，入口未统一汇总其他组 |
| workbench-web | `node scripts/test_web.js` | 工作台代码交付，2 分钟 | 页面契约检查，不替代浏览器交互 |
| workbench-security | `python scripts/repo_security_scan.py .` | 工作台代码交付，2 分钟 | 只输出文件与规则；扫描未覆盖的秘密类型另行审查 |
| workbench-e2e | `python scripts/e2e.py --output` 后接本次新证据目录 | 核心交付，10 分钟 | 实际完整命令必须写 H2；不得把本行当可直接复制的一键命令 |
| workbench-browser | `node scripts/ui_smoke.cjs` | UI 交互变更及原有项目规定，10 分钟 | 先核对脚本依赖、BASE_URL、登录和本地 Mock 服务；不得连接现有业务实例 |
| lab-python | `python scripts/test_all.py` | 实验室代码交付，15 分钟 | unittest 发现；不等于五模式 E2E 已执行 |
| lab-security | `python scripts/repo_security_scan.py .` | 实验室代码交付，2 分钟 | 核对源码允许范围与输出脱敏 |
| lab-e2e | `python scripts/e2e.py --output` 后接本次新证据目录 | 五模式核心交付，10 分钟 | 先检查版本的参数和输出目录契约 |
| lab-inspect | `python -m relay_lab inspect-config --config configs/mock.yaml` | 渠道/配置变更，1 分钟 | 实验室使用 configs/mock.yaml；工作台使用 configs/relay-lab/example.yaml，具体版本先核对 |
| syntax | 全部所审 Python AST 与 JS/CJS/MJS 语法，含 HTML 内联脚本 | 代码交付，5 分钟 | 格式/类型仅在项目已有工具配置时适用；未配置如实写未配置 |
| legacy-suites | 所在版本的 run_selftests.py / selftest.py / tests | 对应项目代码交付，逐组 5 分钟、整组 30 分钟 | 先检查清理路径与失败聚合，不能直接运行会访问业务库或外部接口的旧入口 |
| build-container | 所在版本 Dockerfile、Compose 与 CI 构建健康验证 | T2/T9 触发，15 分钟 | 没有 Docker/依赖不是不适用；需登记环境缺口 |

合成夹具登记位置：工作台/实验室 `tests/` 及各引擎已有 `selftest.py`，旧平台 `selftest_*.py`；新增独立夹具放 `tests/fixtures/` 并同步安全扫描范围。禁止从真实运行数据复制样本。

现有脚本仍存在统一结果汇总、超时实施、部分项目注册清单和 CI 对应关系的落地缺口；本文件不通过文档宣称这些机制已实现。按正式规则审查时，它们属于历史问题。后续代码交付必须完成具体清单及实际检查；无法满足则保持 failed/incomplete，按 T6 处理。
