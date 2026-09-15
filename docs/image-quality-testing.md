# 生图质量子项目：测试清单与交接入口

适用规范：根 AGENTS.md 引用的 docs/ai-rules v1.0。测试清单版本 image-quality-1。

## 范围

工作台独有模块 features/image_quality；挂载、导航、独立启动、依赖、合成夹具与测试入口是必要消费者。极限测试引擎源码未更改，仍保留工作台原有全部必需检查。原工作台未提交样式修改不属于本次候选。

开发目录：/Users/lmurder/Desktop/api中转站/模型测试工作台-生图质量测试
分支：feature/image-quality-testing
基线：f1c01de999e46d7800fd95a221b9f51b6c99b97d
正式规范来源：feature/ai-coding-rules 的仓内版本，与本机 AI_RULES manifest 指纹一致。

## 环境准备与入口

新建仓库外隔离环境，不加载业务 .env、渠道库、通知配置或密钥。macOS 的环境、临时文件、结果与依赖全部放在统一测试数据目录下：

    export IMAGE_TEST_ROOT="/Users/lmurder/Desktop/api中转站/中转站极限测试数据/生图质量测试/20260915-development"
    export PYTHONDONTWRITEBYTECODE=1
    export TMPDIR="$IMAGE_TEST_ROOT/tmp"
    export PLATFORM_DATA_DIR="$IMAGE_TEST_ROOT/platform"
    export RELAY_LAB_DATA_DIR="$IMAGE_TEST_ROOT/relay-lab"
    "$IMAGE_TEST_ROOT/venv/bin/python" -m pip install -r requirements.txt

venv 由 python3 -m venv 创建在上述 IMAGE_TEST_ROOT/venv；tmp 需预先创建。Playwright 安装在仓库外目录，通过 PLAYWRIGHT_MODULE 指定模块绝对路径，浏览器安装缓存用 PLAYWRIGHT_BROWSERS_PATH 指向仓库外。可使用本机 Edge 时指定 PLAYWRIGHT_CHANNEL=msedge。新设备/CI 须显式映射一个仓库外隔离根，不回落源码目录。

一键只读配置检查（不触网、不读取 Key）：

    "$IMAGE_TEST_ROOT/venv/bin/python" -m features.image_quality inspect-config --config tests/fixtures/image_quality/config.json

完整验证（output 必须全新；脚本覆盖本轮全部必需组）：

    "$IMAGE_TEST_ROOT/venv/bin/python" scripts/verify_image_quality.py --output "$IMAGE_TEST_ROOT/evidence/full-01"

独立验收使用指定完整 SHA 的干净 detached worktree、单独 venv/浏览器依赖和全新 output。候选存在未提交改动时，用 HEAD 加 source_sha256 绑定实际快照。正式交接在外置证据中记录候选完整 SHA、规则与依赖指纹、逐组状态、发现问题及复核结果。

## 完整测试清单

以下是本次代码、页面、依赖与启动配置交付全部适用必需组。统一执行器中的同名组为实际注册入口；总预算 30 分钟，每组上限由执行器实施。超时、依赖缺失、零用例、缺失框架结果或必需跳过为 incomplete；已确认断言失败为 failed，逐组记录 failure_count。后续预算耗尽的组为 not_run，不覆盖为通过。容器阶段收到预算终止信号时，在外层 5 秒清理宽限内清理登记的专属容器及镜像；每个清理命令最多 1.5 秒，失败保留资源身份与证据。执行器收到 SIGINT/SIGTERM 时也终止所属进程组，记录当前组取消与剩余组 not_run；确定失败数量和失败状态不会被取消或源码指纹变化覆盖。

| suite_id | 验证内容与入口 | 依赖 | 上限 | 结果 |
| --- | --- | --- | --- | --- |
| syntax | verify_image_quality.py --syntax-only：全部 Python AST、JS/CJS/MJS 与 HTML 内联脚本 | Python / Node | 5 分钟 | syntax.log |
| image-inspect | python -m features.image_quality inspect-config --config tests/fixtures/image_quality/config.json | 项目依赖 | 1 分钟 | image-inspect.log |
| workbench-python | python scripts/test_all.py：三个原引擎 + tests 下全部 unittest；含生图领域、API、取消与验证执行器回归 | 项目依赖 | 15 分钟 | workbench-python.log |
| workbench-web | node scripts/test_web.js：既有页面行为契约 | Node | 2 分钟 | workbench-web.log |
| workbench-security | python scripts/repo_security_scan.py .：源码与新增夹具、规范文件的安全和语法扫描 | Git / Python | 2 分钟 | workbench-security.log |
| workbench-e2e | python scripts/e2e.py --output 本轮/e2e：原有五模式真实本地 Mock HTTP 链路 | 项目依赖 | 10 分钟 | workbench-e2e.log 与 e2e/ |
| workbench-browser | node scripts/ui_smoke.cjs：原页面 + 生图本地 HTTP、逐次确认、精确模型、PNG 验证、评分、导出、401、停止等待、清空与窄屏 | 项目依赖 / Playwright / 浏览器 | 10 分钟 | workbench-browser.log 与本轮 tmp/ 下截图 |
| build-container | python scripts/image_quality_container.py --output 本轮/container：构建及 all/image-quality 两种模式的健康、静态资源、检查入口、鉴权访问 | Docker | 15 分钟 | build-container.log 与 container/ |

全部组在开发和独立验收环境均适用。类型/格式工具当前未配置，不宣称已执行类型检查。真实上游、实际模型身份、长期实测和生产部署不属于本次 Mock 验收。CI 当前只在版本标签触发部分组；本次不推送、不发 Tag，也不宣称已完成远端 CI。

## 关键场景与数据边界

合成内容独立编写在 tests/test_image_quality.py、tests/fixtures/image_quality 与已登记的 UI Mock 内。不得使用实际海报、真实输入或客户数据做夹具。

新增用例覆盖固定 gpt-image-2 与精确负载、默认参数、URL/Key/Prompt 输入边界、无确认不触网、无 Key/URL/正文持久化或指标输出、重定向/鉴权/限流/5xx、空或损坏 JSON、URL-only 响应、损坏或错误图片格式、图片元数据移除与 16 位像素/色彩参数保真、16 位彩色及不支持的色彩/动画明确拒绝、PNG 压缩流内外尾随内容与未使用调色板清理、响应大小限制、总超时及断连取消、缺失模型/用量语义、工作台鉴权与跨站保护。

出站保护沿用 shared.network，现有共享集成用例验证 DNS 重绑定与实际固定 IP；生图浏览器 E2E 仅对白名单中的 127.0.0.1 本地 Mock 发请求。脚本不自动配置通知或外部渠道。检查结果只含安全别名、指纹、状态、计数、耗时与合成验证摘要。

## 启动本次候选

先使用上述隔离环境，然后：

    "$IMAGE_TEST_ROOT/venv/bin/python" run.py --app image-quality --port 18096

访问 http://127.0.0.1:18096/image-quality/。以 all 启动可在统一工作台导航进入；使用独立新数据目录，避免启动已有业务库的调度器。

本地候选提交、完整开发验证与独立审查完成后才提出具体合并申请。开发及验收不自动授权写入目标交付分支、push、发布或真实渠道调用。
