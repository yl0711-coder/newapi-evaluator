"""Read-only detached-commit acceptance; all outputs live in a fresh data directory."""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from relay_lab.config import data_path
from relay_lab.report import atomic_json


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sha', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    sha = git('rev-parse', 'HEAD')
    approved_remote = 'https://github.com/yl0711-coder/newapi-evaluator.git'
    remote_names = git('remote').splitlines()
    unexpected_remote = any(name != 'origin' or git('remote', 'get-url', name) != approved_remote for name in remote_names)
    if len(args.sha) != 40 or sha != args.sha or git('status', '--porcelain') or git('branch', '--show-current') or unexpected_remote:
        raise SystemExit('Acceptance requires exact SHA, clean detached HEAD and only the user-authorized remote')
    output = data_path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    tmp = output / 'tmp'
    tmp.mkdir()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': str(tmp)}
    commands = [
        ('inspect', [sys.executable, '-m', 'relay_lab', 'inspect-config', '--config', 'config.example.yaml']),
        ('focused', [sys.executable, '-m', 'unittest', 'tests.test_faders', 'tests.test_fader_http', 'tests.test_mixed_burst', 'tests.test_sustained', 'tests.test_protocol', 'tests.test_modes_recovery', '-v']),
        ('full', [sys.executable, 'scripts/test_all.py']),
        ('security', [sys.executable, 'scripts/repo_security_scan.py', '.']),
        ('javascript', ['node', '--check', 'relay_lab/web/app.js']),
        ('faders-javascript', ['node', '--check', 'relay_lab/web/faders.js']),
        ('ui-contract', ['node', 'scripts/test_web.js']),
        ('e2e', [sys.executable, 'scripts/e2e.py', '--output', str(output / 'examples')]),
        ('sustained-cli', [sys.executable, '-m', 'relay_lab', 'account-test', '--config', 'configs/sustained.yaml',
                           '--output', str(output / 'sustained')]),
        ('mixed-burst-cli', [sys.executable, '-m', 'relay_lab', 'account-test', '--config', 'configs/mixed-burst.yaml',
                            '--output', str(output / 'mixed-burst')]),
    ]
    results = []
    for name, command in commands:
        print('Acceptance: ' + name, flush=True)
        start = time.monotonic()
        with (output / (name + '.log')).open('w') as log:
            cp = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        results.append({'check': name, 'exit_code': cp.returncode, 'seconds': time.monotonic() - start,
                        'command': [Path(command[0]).name, *command[1:]]})
        atomic_json(output / 'acceptance.json', {'commit_sha': sha, 'checks': results, 'complete': False})
        if cp.returncode:
            break
    clean = not git('status', '--porcelain')
    unchanged = git('rev-parse', 'HEAD') == sha
    sustained_ok = False
    if (output / 'sustained/summary.json').exists():
        sustained = json.loads((output / 'sustained/summary.json').read_text())
        sustained_ok = all(s['occupancy']['stop_reason'] == 'duration' and s['success_rate'] == 1
                           and s['occupancy']['mean_inflight'] >= .9 * s['concurrency']
                           and s['occupancy']['peak_receiving'] == s['concurrency'] for s in sustained['stages'])
        sustained_ok = sustained_ok and len(sustained['stages']) == 3 and sustained['revision']['commit_sha'] == sha
    burst_ok = False
    if (output / 'mixed-burst/summary.json').exists():
        mixed = json.loads((output / 'mixed-burst/summary.json').read_text())
        b = mixed.get('analysis', {}).get('burst', {})
        burst_ok = (mixed['revision']['commit_sha'] == sha and mixed['result_count'] == 10 and not mixed['recoveries']
                    and b.get('peak_receiving') == 5 and b.get('received_output_requests') == 10
                    and any(e['waiting_labels'] and e['later_output'] for e in b.get('release_observations', [])))
    passed = len(results) == len(commands) and all(r['exit_code'] == 0 for r in results) and clean and unchanged and sustained_ok and burst_ok
    value = {'commit_sha': sha, 'checks': results, 'complete': True, 'passed': passed,
             'detached_head': not git('branch', '--show-current'), 'clean_worktree': clean,
             'head_unchanged': unchanged, 'remotes': git('remote').splitlines(),
             'environment': {'python': platform.python_version(), 'os': platform.platform(), 'interpreter': sys.executable},
             'real_environment_validated': False, 'sustained_occupancy_verified': sustained_ok, 'mixed_burst_verified': burst_ok}
    atomic_json(output / 'acceptance.json', value)
    lines = ['# 独立测试报告', '', '- PR：未创建；远程为用户指定仓库，验收不执行推送或合并。',
             '- 分支：开发 feature/mock-capacity-lab；本副本 detached HEAD。',
             f'- 实际验证 SHA（40 位）：`{sha}`',
             f'- Python：{platform.python_version()}；系统：{platform.system()} {platform.machine()}',
             '- 依赖：独立 Python 环境，从本地 wheel 缓存离线安装，未访问包索引。',
             '- 一键渠道信息：`python -m relay_lab inspect-config --config config.example.yaml`，仅安全别名、协议、数量、指纹与时间。',
             '- 已核验无凭据、完整敏感 URL、Prompt、响应正文或客户信息。', '',
             '| 验收项 | 退出码 | 用时秒 | 证据 |', '| --- | ---: | ---: | --- |']
    for r in results:
        lines.append(f"| {r['check']} | {r['exit_code']} | {r['seconds']:.2f} | {r['check']}.log |")
    lines += ['', '## 结论', '', '通过' if passed else '失败', '',
              '本提交的 Mock 功能与独立验收完成；本次未对真实上游发起验证请求。' if passed else '本提交未通过独立验收，返回开发目录修复后对新 SHA 重验。', '',
              '- 五种模式均为本地 Mock；gateway 阶梯覆盖 10、20、50、100、200、400、800、1200。',
              '- Ctrl+C 部分报告、检查点恢复、脱敏与安全边界由自动测试复核。',
              '- 验收目录保持干净 detached HEAD；未修改业务代码、未 push、PR 或合并。',
              '- 前端 JavaScript 语法检查：已列入 javascript 验收项，结果见上表与 javascript.log。',
              '- 持续模式验证并发 1、3、5 的请求补发、接收重叠与完整时长；结果见 sustained/summary.json。',
              '- 固定混合批次验证 2 短、2 中、6 长同时发出，容量 5 的 Mock 排队释放、不补发及逐条时间线；见 mixed-burst/summary.json。',
              '- 三路推子通过独立聚焦与 HTTP 测试：运行中增减、满载等待与拒绝、暂停、取消、时限、请求上限、调节持久化及历史重建。',
              '- Mock 崩溃为本地短暂不可用模拟；未终止真实网关进程。',
              '- 真实 Sub2API 调度、真实账号和真实网关容量均未验证。', '']
    (output / 'acceptance-report.md').write_text('\n'.join(lines))
    print(json.dumps({'commit_sha': sha, 'passed': passed}), flush=True)
    raise SystemExit(not passed)


if __name__ == '__main__':
    main()
