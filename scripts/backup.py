import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.config import DATA_DIR, ROOT


def backup(output: Path):
    if not (DATA_DIR / "channels.db").exists() or not (DATA_DIR / "channels.key").exists():
        raise RuntimeError("公共渠道库及配套密钥尚未创建")
    target = output / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    try:
        for relative in ("channels.db", "channels.key", "stability/stability.db", "stability/secret.key"):
            source = DATA_DIR / relative
            if not source.exists():
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if source.suffix == ".db":
                src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                dst = sqlite3.connect(destination)
                try:
                    destination.chmod(0o600)
                    src.backup(dst)
                    if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份数据库校验失败")
                finally:
                    src.close()
                    dst.close()
            else:
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
        return target
    except Exception:
        (target / "INCOMPLETE").touch(mode=0o600)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="备份公共渠道与定时结果，包括配套密钥")
    parser.add_argument("--output", type=Path, default=ROOT / "backups")
    print(backup(parser.parse_args().output.resolve()))
