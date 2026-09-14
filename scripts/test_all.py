import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
test_data = tempfile.TemporaryDirectory(prefix="workbench-all-")
env = {
    **os.environ,
    "PYTHONPATH": str(ROOT),
    "PLATFORM_DATA_DIR": test_data.name,
    "RELAY_LAB_DATA_DIR": str(Path(test_data.name) / "relay-lab"),
}
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
