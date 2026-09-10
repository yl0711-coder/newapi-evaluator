"""Read-only fingerprint of existing sibling projects; no file contents in evidence."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path('/Users/lmurder/Desktop/api中转站')
EXCLUDED = {'中转站极限测试实验室', '中转站极限测试-验收副本', '中转站极限测试数据'}


def snapshot():
    result = {}
    for project in sorted(ROOT.iterdir()):
        if not project.is_dir() or project.name in EXCLUDED:
            continue
        git = (project / '.git').exists()
        if git:
            cp = subprocess.run(['git', '-C', str(project), 'ls-files', '-z', '--cached',
                                 '--others', '--exclude-standard'], capture_output=True, check=True)
            paths = sorted(set(cp.stdout.decode().split('\0')) - {''})
        else:
            paths = [str(f.relative_to(project)) for f in project.rglob('*') if f.is_file()
                     and not any(x in f.parts for x in ('.venv', 'node_modules', '__pycache__'))]
        digest = hashlib.sha256()
        count = 0
        for relative in sorted(paths):
            path = project / relative
            if not path.is_file() or path.is_symlink():
                continue
            digest.update(relative.encode())
            with path.open('rb') as f:
                for chunk in iter(lambda: f.read(1048576), b''):
                    digest.update(chunk)
            count += 1
        state = {'source_digest': digest.hexdigest(), 'file_count': count}
        if git:
            for key, cmd in [('head', ['rev-parse', 'HEAD']),
                             ('status_digest', ['status', '--porcelain=v1', '--untracked-files=all'])]:
                value = subprocess.run(['git', '-C', str(project), *cmd], capture_output=True, check=True).stdout
                state[key] = value.decode().strip() if key == 'head' else hashlib.sha256(value).hexdigest()
        result[project.name] = state
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    data = (ROOT / '中转站极限测试数据').resolve()
    if not args.output.resolve().is_relative_to(data):
        parser.error('Output must be in dedicated data directory')
    current = snapshot()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
    if args.compare:
        old = json.loads(args.compare.read_text())
        changed = [key for key in old.keys() | current.keys() if old.get(key) != current.get(key)]
        print(json.dumps({'protected_projects': len(current), 'changed_projects': changed}, ensure_ascii=False))
        raise SystemExit(bool(changed))
    print(f'Fingerprinted {len(current)} protected projects')
