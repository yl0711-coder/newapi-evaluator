from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("PLATFORM_DATA_DIR", ROOT / "data")).expanduser().resolve()
WEB_DIR = ROOT / "web"


def prepare_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA_DIR.chmod(0o700)
