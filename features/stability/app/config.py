from __future__ import annotations

import os
from pathlib import Path
from shared.config import DATA_DIR as PLATFORM_DATA_DIR


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("STABILITY_DATA_DIR", str(PLATFORM_DATA_DIR / "stability"))).expanduser().resolve()
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "stability.db"
SECRET_PATH = DATA_DIR / "secret.key"
TIMEZONE = os.getenv("STABILITY_TIMEZONE", "Asia/Shanghai")
CATCHUP_HOURS = max(1, int(os.getenv("STABILITY_CATCHUP_HOURS", "6")))
MAX_ACTIVE_RUNS = max(1, int(os.getenv("STABILITY_MAX_ACTIVE_RUNS", "2")))
RETENTION_DAYS = max(1, int(os.getenv("STABILITY_RETENTION_DAYS", "5")))
EGRESS_ALLOWLIST = tuple(
    item.strip() for item in os.getenv("PLATFORM_EGRESS_ALLOWLIST", os.getenv("STABILITY_EGRESS_ALLOWLIST", "")).split(",") if item.strip()
)
USERNAME = os.getenv("STABILITY_USERNAME", "").strip()
PASSWORD = os.getenv("STABILITY_PASSWORD", "")

DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass
