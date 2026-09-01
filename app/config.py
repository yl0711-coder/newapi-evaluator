"""集中配置：路径、默认值、可调参数。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TEST_DATA_DIR", str(BASE_DIR / "data"))).resolve()
WEB_DIR = BASE_DIR / "web"

# 库文件名可用环境变量换掉，自测专用。
#
# 为什么需要这个：自测套件是「从空状态开始断言」的，所以 run_selftests.py 每次跑前会清库。
# 如果自测和正式运行共用 data/platform.db，那么在你已经跑过真实接入检测之后
# **跑一次自测就会把真实渠道、任务、标杆全部删掉**。
# 自测改用 data/selftest.db，两边彻底隔开，正式数据再也不会被自测碰到。
DB_NAME = os.getenv("TEST_DB_NAME", "platform.db")
DB_PATH = DATA_DIR / DB_NAME
METRIC_DB_NAME = os.getenv(
    "TEST_METRIC_DB_NAME",
    "metrics-selftest.db" if DB_NAME == "selftest.db" else "metrics.db",
)
METRIC_DB_PATH = DATA_DIR / METRIC_DB_NAME
KEY_PATH = DATA_DIR / "secret.key"
BACKUP_DIR = Path(os.getenv("TEST_BACKUP_DIR", str(DATA_DIR / "backups"))).resolve()
BACKUP_KEY_PATH = Path(
    os.getenv("TEST_BACKUP_KEY_PATH", str(DATA_DIR / "backup.key"))
).resolve()
BACKUP_HOUR_UTC = int(os.getenv("TEST_BACKUP_HOUR_UTC", "19"))  # 北京时间 03:00
SQLITE_MIGRATION_BYTES = int(
    os.getenv("TEST_SQLITE_MIGRATION_BYTES", str(1024 * 1024 * 1024))
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 执行参数
WORKER_COUNT = int(os.getenv("TEST_WORKERS", "2"))
HTTP_TIMEOUT = float(os.getenv("TEST_HTTP_TIMEOUT", "180"))

# 双端准入的正式时间口径固定在规格版本中。开发自检可以缩短真实等待，
# 但相应任务会被标记为非正式，不能形成可复用保真或正式准入结论。
PAIRED_SELFTEST_MODE = os.getenv("TEST_PAIRED_SELFTEST_MODE", "0") in ("1", "true")
PAIRED_COOLDOWN_SECONDS = 0.01 if PAIRED_SELFTEST_MODE else 5.0
PAIRED_FIDELITY_TIMEOUT_SECONDS = 60.0
PAIRED_FORMAL_TIMEOUT_SECONDS = 180.0
PAIRED_TASK_ACTIVE_LIMIT_SECONDS = 60 * 60.0
PAIRED_FIDELITY_WAIT_SECONDS = 24 * 60 * 60.0
PAIRED_TRUTH_TTL_SECONDS = 30 * 24 * 60 * 60.0
PAIRED_RAW_RETENTION_SECONDS = 180 * 24 * 60 * 60.0
PAIRED_STRUCTURED_RETENTION_SECONDS = 2 * 365 * 24 * 60 * 60.0

# 管理端认证。Cookie 默认跟随当前请求协议，本机 HTTP 可直接使用，HTTPS
# 自动附加 Secure；部署者也可以用 TEST_COOKIE_SECURE 强制指定。
SESSION_TTL_SECONDS = int(os.getenv("TEST_SESSION_TTL_SECONDS", str(12 * 60 * 60)))
_cookie_secure_setting = os.getenv("TEST_COOKIE_SECURE", "auto").lower()
COOKIE_SECURE = None if _cookie_secure_setting == "auto" else \
    _cookie_secure_setting not in ("0", "false", "")
BOOTSTRAP_USERNAME = os.getenv("TEST_BOOTSTRAP_USERNAME", "").strip()
BOOTSTRAP_PASSWORD = os.getenv("TEST_BOOTSTRAP_PASSWORD", "")

# 出站访问默认只允许公网地址。确需检测内网上游时，由部署者逐项加入主机名、
# IP 或 CIDR；不接受通配符，避免一个宽泛配置重新打开整个内网。
EGRESS_ALLOWLIST = tuple(
    item.strip() for item in os.getenv("TEST_EGRESS_ALLOWLIST", "").split(",")
    if item.strip()
)
EGRESS_MAX_REDIRECTS = int(os.getenv("TEST_EGRESS_MAX_REDIRECTS", "3"))
EGRESS_MAX_RESPONSE_BYTES = int(
    os.getenv("TEST_EGRESS_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024))
)

# 硬题单独的超时。预算抬到 3.2 万后，推理模型跑满可能要十分钟量级，
# 300s 会把「慢但答对」误判成超时。
HARD_HTTP_TIMEOUT = float(os.getenv("TEST_HARD_HTTP_TIMEOUT", "600"))

# 硬题的 token 预算下限，按题库分开给。
#
# **思考 token 算在 max_tokens 里**（Anthropic extended thinking 的 thinking 计入
# max_tokens；OpenAI o 系列的 reasoning tokens 计入 max_completion_tokens；
# DeepSeek-R1 的 reasoning_content 计入 completion tokens）。题库原本给的
# 8192 / 4096 是按「普通模型简短推理几句」定的，推理模型光思考就能烧穿，
# 结果是正文被截断成空的 —— 那会被记成答错，而且在报告里跟真答错长得一模一样。
#
# 所以这里给一个下限，盖在题库自带的 maxTokens 上（取两者较大值）。
# 编程硬核要先想清楚算法再写程序再算结果，给得比 HLE 多。
# 注意：HARD_VERSION 只按题面与答案算哈希，不含预算，所以调这两个数
# **不会让已有的硬题标杆失效**。
HARD_TOKEN_FLOOR = {
    "hardcore_unsolvable": int(os.getenv("TEST_HARD_TOKENS_LOGIC", "16384")),
}

# 能力复测默认是否加入独立硬题。
HARD_IN_CAPABILITY = os.getenv("TEST_HARD_IN_CAPABILITY", "1") not in ("0", "false", "")

# 默认价格（¥ / 1M token），目标未配置时使用
DEFAULT_PRICE_IN = 2.0
DEFAULT_PRICE_OUT = 8.0

# 步骤 1 硬门槛：超时率上限。超过则终止流程，不进入分组推荐。
TIMEOUT_RATE_LIMIT = 0.05
STABILITY_SCORE_LIMIT = 0.95
GROUP_RECYCLE_DAYS = 30
