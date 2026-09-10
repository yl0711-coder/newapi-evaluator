"""One-command persistent local console launcher; all runtime files stay in DATA_ROOT."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据/console')
URL = 'http://127.0.0.1:8878'


def probe():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL + '/api/state', timeout=2) as response:
            value = json.load(response)
        return value.get('service') == 'relay-lab-console'
    except (OSError, ValueError):
        return False


if __name__ == '__main__':
    if probe():
        print(URL)
        raise SystemExit(0)
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-B', '-m', 'relay_lab', 'ui', '--port', '8878'], cwd=ROOT,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if probe():
            (DATA / 'server.pid').write_text(str(process.pid) + '\n')
            print(URL)
            break
        if process.poll() is not None:
            raise SystemExit('Console did not start. Inspect the dedicated console/server.log.')
        time.sleep(.1)
    else:
        raise SystemExit('Console startup is still pending; inspect console/server.log before retrying.')
