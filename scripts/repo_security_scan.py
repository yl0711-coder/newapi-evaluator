"""Local source scanner. Only safe relative paths and rule names are emitted."""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path


def scan(root):
    output = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            capture_output=True, check=True).stdout.decode()
    findings = []
    count = 0
    patterns = [re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'),
                re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                re.compile(r'https?://[^\s/]+:[^\s/@]+@'),
                re.compile(r'(?i)https?://[^\s]+[?&](?:api_key|token|secret)=[A-Za-z0-9_-]{12,}')]
    for name in sorted(set(output.split('\0')) - {''}):
        path = root / name
        if not path.is_file():
            continue
        count += 1
        if path.name == '.env' or path.suffix in ('.db', '.sqlite3', '.jsonl', '.log', '.pem', '.key'):
            findings.append({'path': name, 'rule': 'runtime_or_secret_file_in_source'})
        try:
            content = path.read_text()
        except UnicodeError:
            findings.append({'path': name, 'rule': 'unexpected_binary'})
            continue
        for i, pattern in enumerate(patterns):
            if pattern.search(content):
                findings.append({'path': name, 'rule': 'sensitive_pattern_' + str(i)})
        if path.suffix == '.py':
            try:
                ast.parse(content, filename=name)
            except SyntaxError:
                findings.append({'path': name, 'rule': 'python_syntax'})
    return {'files_checked': count, 'findings': findings, 'passed': not findings}


if __name__ == '__main__':
    result = scan(Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve())
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(not result['passed'])
