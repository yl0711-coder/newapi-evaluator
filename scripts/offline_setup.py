"""Build a separate environment from local cached wheels; never access a network."""
import argparse
import email
import shutil
import os
import tempfile
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

DATA = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据').resolve()


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ required; select an explicit interpreter path')
    p = argparse.ArgumentParser()
    p.add_argument('--env', required=True, type=Path)
    p.add_argument('--cache', type=Path, default=Path('/Users/lmurder/Library/Caches/pip/http-v2'))
    args = p.parse_args()
    target = args.env.resolve()
    if not target.is_relative_to(DATA) or target == DATA:
        p.error('Environment must be a child of the dedicated data directory')
    temp = target.parent / 'tmp'
    temp.mkdir(parents=True, exist_ok=True)
    os.environ['TMPDIR'] = str(temp)
    tempfile.tempdir = str(temp)
    wheels = target.parent / (target.name + '-wheels')
    wheels.mkdir(parents=True, exist_ok=True)
    required = dict(line.strip().lower().replace('-', '_').split('==') for line in
                    (Path(__file__).resolve().parents[1] / 'requirements.txt').read_text().splitlines())
    found = set()
    for path in args.cache.rglob('*.body'):
        if not zipfile.is_zipfile(path):
            continue
        with zipfile.ZipFile(path) as z:
            meta = next((n for n in z.namelist() if n.endswith('.dist-info/METADATA')), None)
            if meta is None:
                continue
            msg = email.message_from_bytes(z.read(meta))
            name = msg.get('Name', '').lower().replace('-', '_')
            if required.get(name) != msg.get('Version'):
                continue
            wheel_meta = email.message_from_bytes(z.read(meta.replace('METADATA', 'WHEEL')))
            tag = wheel_meta.get_all('Tag')[0]
            filename = f'{name}-{msg["Version"]}-{tag}.whl'
            shutil.copyfile(path, wheels / filename)
            found.add(name)
    if missing := set(required) - found:
        raise SystemExit('Missing offline wheels: ' + ', '.join(sorted(missing)))
    if not (target / 'bin/python').exists():
        venv.EnvBuilder(with_pip=True).create(target)
    subprocess.run([str(target / 'bin/python'), '-m', 'pip', '--disable-pip-version-check',
                    'install', '--no-index', '--no-cache-dir', '--find-links', str(wheels),
                    '-r', str(Path(__file__).resolve().parents[1] / 'requirements.txt')], check=True)
    print('Offline environment ready:', target)


if __name__ == '__main__':
    main()
