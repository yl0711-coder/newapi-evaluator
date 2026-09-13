"""One-command persistent local console launcher; all runtime files stay in DATA_ROOT."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据/console')
URL = 'http://127.0.0.1:8878'


def current_revision():
    sha = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], capture_output=True, text=True).stdout.strip()
    return {'commit_sha': sha if len(sha) == 40 else None, 'dirty': bool(dirty)}


def probe():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL + '/api/state', timeout=2) as response:
            value = json.load(response)
        return value if value.get('service') == 'relay-lab-console' else None
    except (OSError, ValueError):
        return None


def current(value):
    return bool(value and value.get('ui_schema_version') == 4 and value.get('revision') == current_revision())


def stop_tracked_stale(value):
    pid_file = DATA / 'server.pid'
    if not value or not pid_file.is_file():
        return False
    try:
        tracked = int(pid_file.read_text().strip())
        reported = int(value['pid'])
    except (KeyError, TypeError, ValueError, OSError):
        return False
    if tracked != reported or tracked <= 1:
        return False
    os.kill(tracked, signal.SIGTERM)
    for _ in range(50):
        time.sleep(.1)
        if probe() is None:
            return True
    return False


if __name__ == '__main__':
    existing = probe()
    if current(existing):
        print(URL)
        raise SystemExit(0)
    if existing and not stop_tracked_stale(existing):
        raise SystemExit('Port 8878 already serves an untracked or stale console; stop it explicitly before retrying.')
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-B', '-m', 'relay_lab', 'ui', '--port', '8878'], cwd=ROOT,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if current(probe()):
            (DATA / 'server.pid').write_text(str(process.pid) + '\n')
            print(URL)
            break
        if process.poll() is not None:
            raise SystemExit('Console did not start. Inspect the dedicated console/server.log.')
        time.sleep(.1)
    else:
        raise SystemExit('Console startup is still pending; inspect console/server.log before retrying.')
