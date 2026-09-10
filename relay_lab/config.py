import copy
import math
from pathlib import Path

import yaml

DATA_ROOT = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据')
FAULTS = {'normal', 'slow_sse', 'http_401', 'http_429', 'http_500', 'connect_timeout',
          'read_timeout', 'network_drop', 'malformed_sse', 'disconnect', 'missing_done',
          'disable_account', 'partial_accounts', 'upstream_outage', 'jitter'}
DEFAULT = {
    'base_url': None, 'timeout': 0.5, 'connection_limit': 1500, 'samples': 100,
    'min_samples': 100, 'rounds_per_stage': 2, 'stages': [1, 2, 3, 5, 8, 13], 'recovery_timeout': 3.0,
    'recovery_interval': 0.03, 'recovery_successes': 3, 'collapse_streak': 3,
    'mock': {
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
    mock = cfg['mock']
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
