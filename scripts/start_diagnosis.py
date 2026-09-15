"""Launch the diagnosis subproject with an explicit external data directory."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args()
    data = args.data_dir.expanduser().resolve()
    if data == ROOT or ROOT in data.parents:
        parser.error("数据目录必须位于源码仓库之外")
    if not 1 <= args.port <= 65535:
        parser.error("端口超出范围")
    os.environ.update(PLATFORM_DATA_DIR=str(data), PYTHONDONTWRITEBYTECODE="1")
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-B", "run.py", "--app", "diagnosis", "--host", "127.0.0.1", "--port", str(args.port)])


if __name__ == "__main__":
    main()
