"""Run the registered layered integrity and full workbench Mock checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.verify_admission import main

if __name__ == "__main__":
    raise SystemExit(main(default_plan="integrity"))
