"""Execute all five public CLI modes with the shipped examples and audit actual artifacts."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from relay_lab.config import data_path
from relay_lab.report import atomic_json, revision

MODES = {'account-test': 'account', 'pool-test': 'pool', 'gateway-test': 'gateway',
         'long-task-test': 'long-task', 'chaos-test': 'chaos'}
LIMIT_KEYS = {'baseline', 'slow_point', 'unstable_point', 'rate_limit_point', 'collapse_point',
              'max_stable_concurrency', 'recovery_time', 'low_confidence'}


def check(value, raw):
    assert value['environment'] == 'mock' and value['status'] == 'completed'
    assert value['revision'] == revision()
    assert LIMIT_KEYS <= value['analysis'].keys()
    assert value['result_count'] == len(raw) and len(raw) > 0
    assert value['network_policy']['socket_audit']['loopback_connections'] > 0
    assert value['network_policy']['socket_audit']['blocked_external_attempts'] == 0
    assert not value['network_policy']['external_requests_authorized']
    assert len({r['request_id'] for r in raw}) == len(raw)
    for stage in value['stages']:
        rows = [r for r in raw if r['stage'] == stage['stage'] and r['phase'] == 'load']
        assert stage['samples'] == len(rows)
        if rows:
            assert stage['success_rate'] == sum(r['success'] for r in rows) / len(rows)
    mode = value['mode']
    if mode == 'account-test':
        assert value['analysis']['rate_limit_point'] is not None
        assert value['analysis']['admission_gate_passed']
    elif mode == 'pool-test':
        scenarios = value['analysis']['pool_scenarios']
        assert len(scenarios) == 9
        assert any(s['failed_account_count'] > 0 for s in scenarios)
        assert all({'configured_capacity', 'observed_capacity', 'capacity_utilization', 'scaling_efficiency',
                    'healthy_account_count', 'failed_account_count'} <= s.keys() for s in scenarios)
        assert all(LIMIT_KEYS <= s.keys() for s in scenarios)
    elif mode == 'gateway-test':
        assert [s['concurrency'] for s in value['stages']] == [10, 20, 50, 100, 200, 400, 800, 1200]
        assert value['analysis']['slow_point'] is not None
        assert value['analysis']['collapse_point'] is not None
        assert all('resources' in s for s in value['stages'])
    elif mode == 'long-task-test':
        a = value['analysis']
        assert a['first_failure_step'] == 4 and a['recovery_count'] >= 1
        assert a['mock_result_verified'] and a['mock_single_stream_verified']
        assert a['confirmed_steps_reexecuted'] == 0
        assert a['single_stream_duration_seconds'] >= 14.5
    elif mode == 'chaos-test':
        assert len(value['analysis']['fault_scenarios']) == 14
        assert all(c['recovered'] for c in value['analysis']['fault_scenarios'])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    output = data_path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    evidence = {'revision': revision(), 'environment': 'mock', 'modes': {}}
    for mode, config in MODES.items():
        command = [sys.executable, '-m', 'relay_lab', mode, '--config', f'configs/{config}.yaml', '--output', str(output / config)]
        print('Running ' + mode, flush=True)
        cp = subprocess.run(command, cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True, timeout=300)
        (output / (config + '-cli.log')).write_text(cp.stdout + cp.stderr)
        if cp.returncode:
            raise RuntimeError(mode + ' failed; inspect safe CLI log')
        value = json.loads((output / config / 'summary.json').read_text())
        raw = [json.loads(line) for line in (output / config / 'results.jsonl').read_text().splitlines()]
        check(value, raw)
        evidence['modes'][mode] = {'passed': True, 'result_count': value['result_count'],
                                  'summary_sha256': hashlib.sha256((output / config / 'summary.json').read_bytes()).hexdigest(),
                                  'network_audit': value['network_policy']['socket_audit']}
        print(mode + ' passed: ' + str(value['result_count']) + ' results', flush=True)
        atomic_json(output / 'e2e-evidence.json', evidence)
    print('All five Mock CLI modes passed', flush=True)


if __name__ == '__main__':
    main()
