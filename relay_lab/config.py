import copy
import math
from pathlib import Path

import yaml

DATA_ROOT = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据')
FAULTS = {'normal', 'slow_sse', 'http_401', 'http_429', 'http_500', 'connect_timeout',
          'read_timeout', 'network_drop', 'malformed_sse', 'disconnect', 'missing_done',
          'disable_account', 'partial_accounts', 'upstream_outage', 'jitter'}
DEFAULT = {
    'faders': {'enabled': False, 'targets': [0, 0, 0], 'output_limits': [64, 512, 4096],
               'max_inflight': 120, 'duration': 600, 'max_requests': 10000,
               'refill_interval': 1, 'mock_durations': [2, 6, 18]},
    'mixed_burst': {'enabled': False, 'counts': [2, 2, 6], 'output_limits': [64, 512, 4096],
                    'expected_capacity': 5, 'first_output_timeout': 600, 'idle_timeout': 60,
                    'total_timeout': 900, 'connect_timeout': 10, 'release_window': 10},
    'stage_duration': 0, 'max_stage_requests': 1000,
    'workload': {'profile': 'short', 'output_tokens': 1024, 'limit_field': 'max_tokens'},
    'base_url': None, 'timeout': 0.5, 'connection_limit': 1500, 'samples': 100,
    'min_samples': 100, 'rounds_per_stage': 2, 'stages': [1, 2, 3, 5, 8, 13], 'recovery_timeout': 3.0,
    'recovery_interval': 0.03, 'recovery_successes': 3, 'collapse_streak': 3,
    'mock': {
        'admission_policy': 'reject', 'queue_timeout': 3,
        'seed': 23, 'chunks': 4, 'chunk_delay': 0.004, 'latency': 0.015,
        'jitter': 0.0, 'slow_at': 100000, 'slow_factor': 4.0,
        'crash_at': 100000, 'crash_duration': 0.15, 'fault_duration': 0.12,
        'accounts': [{'capacity': 3, 'latency': 0.015, 'jitter': 0.0,
                      'failure_rate': 0.0, 'cooldown': 0.04}],
    },
    'pool_sizes': [1, 2, 4], 'pool_stages': [1, 2, 3, 5, 8, 13],
    'gateway_stages': [10, 20, 50, 100, 200, 400, 800, 1200],
    'faults': sorted(FAULTS - {'normal'}),
    'long_task': {'steps': 6, 'fail_step': 3, 'fault': 'disconnect', 'max_retries': 3,
                  'retry_delay': 0.05, 'auto_resume': True, 'stream_chunks': 40,
                  'stream_chunk_delay': 0.005, 'fail_attempts': 1},
}


def merge(base, value):
    result = copy.deepcopy(base)
    for k, v in value.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def validate(cfg):
    def number(value, low, high):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid numeric configuration')

    def integer(value, low, high):
        number(value, low, high)
        if not isinstance(value, int):
            raise ValueError('Expected integer configuration')

    for key in ('stages', 'pool_stages', 'gateway_stages', 'pool_sizes'):
        values = cfg[key]
        if not isinstance(values, list) or not values or values != sorted(set(values)):
            raise ValueError('Stages must be unique ascending integers')
        for value in values:
            integer(value, 1, 20000)
    for key in ('samples', 'min_samples', 'rounds_per_stage', 'connection_limit', 'recovery_successes', 'collapse_streak'):
        integer(cfg[key], 1, 1000000)
    for key in ('timeout', 'recovery_timeout', 'recovery_interval'):
        number(cfg[key], 0.001, 86400)
    number(cfg['stage_duration'], 0, 3600)
    integer(cfg['max_stage_requests'], 1, 100000)
    workload = cfg['workload']
    if workload['profile'] not in ('short', 'long') or workload['limit_field'] not in ('max_tokens', 'max_completion_tokens'):
        raise ValueError('Invalid workload configuration')
    integer(workload['output_tokens'], 16, 32768)
    burst = cfg['mixed_burst']
    faders = cfg['faders']
    if type(faders['enabled']) is not bool:
        raise ValueError('Invalid faders flag')
    from .faders import validate_targets
    integer(faders['max_inflight'], 1, 1200)
    validate_targets(faders['targets'], faders['max_inflight'])
    number(faders['duration'], .1, 3600)
    integer(faders['max_requests'], 1, 100000)
    number(faders['refill_interval'], .1, 60)
    for key in ('output_limits', 'mock_durations'):
        if not isinstance(faders[key], list) or len(faders[key]) != 3:
            raise ValueError('Faders require three channel values')
    for n in faders['output_limits']:
        integer(n, 16, 32768)
    for n in faders['mock_durations']:
        number(n, .01, 600)
    if faders['enabled'] and (burst['enabled'] or cfg['stage_duration'] or cfg['connection_limit'] < faders['max_inflight']):
        raise ValueError('Faders require their own scheduler and enough client connection slots')
    if type(burst['enabled']) is not bool:
        raise ValueError('Invalid mixed burst flag')
    for key in ('counts', 'output_limits'):
        if not isinstance(burst[key], list) or len(burst[key]) != 3:
            raise ValueError('Mixed burst requires short, medium and long values')
    for value in burst['counts']:
        integer(value, 0, 1200)
    integer(sum(burst['counts']), 1, 1200)
    for value in burst['output_limits']:
        integer(value, 16, 32768)
    if burst['output_limits'] != sorted(set(burst['output_limits'])):
        raise ValueError('Mixed output limits must increase')
    integer(burst['expected_capacity'], 1, 1200)
    for key in ('first_output_timeout', 'idle_timeout', 'total_timeout', 'connect_timeout', 'release_window'):
        number(burst[key], .01, 86400)
    if burst['enabled'] and (cfg['stage_duration'] or cfg['connection_limit'] < sum(burst['counts'])):
        raise ValueError('Mixed burst requires no refill and enough client connection slots')
    mock = cfg['mock']
    if mock['admission_policy'] not in ('reject', 'queue'):
        raise ValueError('Unknown Mock admission policy')
    number(mock['queue_timeout'], .001, 86400)
    for key in ('latency', 'chunk_delay', 'jitter', 'crash_duration', 'fault_duration'):
        number(mock[key], 0, 86400)
    for key in ('chunks', 'slow_at', 'crash_at'):
        integer(mock[key], 1, 1000000)
    number(mock['slow_factor'], 1, 1000)
    if not isinstance(mock['accounts'], list) or not mock['accounts']:
        raise ValueError('At least one account required')
    for account in mock['accounts']:
        integer(account.get('capacity', 3), 1, 20000)
        for key in ('latency', 'jitter', 'cooldown'):
            number(account.get(key, 0), 0, 86400)
        number(account.get('failure_rate', 0), 0, 1)
    if not set(cfg['faults']) <= FAULTS:
        raise ValueError('Unknown fault')
    long = cfg['long_task']
    integer(long['steps'], 1, 100000)
    integer(long['fail_step'], 0, long['steps'])
    integer(long['max_retries'], 0, 1000)
    integer(long['fail_attempts'], 0, 1000)
    integer(long['stream_chunks'], 1, 1000000)
    number(long['retry_delay'], 0, 86400)
    number(long['stream_chunk_delay'], 0, 86400)
    if long['fault'] not in FAULTS:
        raise ValueError('Unknown long-task fault')
    return cfg


def load(path=None, overrides=None):
    value = yaml.safe_load(Path(path).read_text()) if path else {}
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a mapping')
    return validate(merge(merge(DEFAULT, value), overrides or {}))


def data_path(path):
    path = Path(path).expanduser().resolve()
    root = DATA_ROOT.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Output must be a child of the dedicated test-data directory')
    return path
