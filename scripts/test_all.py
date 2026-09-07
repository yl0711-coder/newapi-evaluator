import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = {**os.environ, "PYTHONPATH": str(ROOT)}
commands = [
    [sys.executable, "features/admission/selftest.py"],
    [sys.executable, "features/stability/selftest.py"],
    [sys.executable, "features/reasoning/selftest.py"],
    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
]
for command in commands:
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)
print("All engine and integration checks passed.")
