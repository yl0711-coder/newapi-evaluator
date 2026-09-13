"""Build the current commit and start the loopback-only OrbStack console."""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据/orbstack')
COMPOSE = ROOT / 'compose.orbstack.yaml'
URL = 'http://127.0.0.1:8878'


def output(*command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def state():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(URL + '/api/state', timeout=2) as response:
        return json.load(response)


def main():
    if output('docker', 'context', 'show') != 'orbstack':
        raise SystemExit('Docker context must be orbstack before starting the lab.')
    sha = output('git', 'rev-parse', 'HEAD')
    if len(sha) != 40 or output('git', 'status', '--porcelain'):
        raise SystemExit('Commit the intended source first; OrbStack images are bound to a clean 40-character SHA.')
    DATA.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'RELAY_LAB_IMAGE_REVISION': sha}
    subprocess.run(['docker', 'compose', '-f', str(COMPOSE), 'up', '--build', '--detach', '--remove-orphans'],
                   cwd=ROOT, env=env, check=True)
    for _ in range(60):
        try:
            value = state()
            if value.get('service') == 'relay-lab-console' and value.get('revision', {}).get('commit_sha') == sha:
                print(json.dumps({'url': URL, 'commit_sha': sha, 'container': 'relay-station-lab'}, ensure_ascii=False))
                return 0
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise SystemExit('OrbStack container started but did not become healthy with the expected commit.')


if __name__ == '__main__':
    raise SystemExit(main())
