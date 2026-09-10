"""Fixed-cohort observations. Client timing is evidence, never proof of server queuing."""
import time
from collections import Counter


def plan(config):
    return [{'label': prefix + str(i + 1), 'profile': profile, 'output_limit': limit}
            for profile, prefix, count, limit in zip(('short', 'medium', 'long'), ('S', 'M', 'L'),
                                                     config['counts'], config['output_limits'])
            for i in range(count)]


def snapshot(rows, config, now=None):
    rows = [dict(r) for r in rows]
    now = time.time() if now is None else now
    start = min((r['started_at'] for r in rows), default=now)
    finished = bool(rows) and all(r.get('ended_at') is not None for r in rows)
    end = max(r['ended_at'] for r in rows) if finished else now
    completed = sorted((r for r in rows if r['success']), key=lambda r: r['ended_at'])
    releases = [r for r in completed if r.get('workload_profile') in ('short', 'medium')]
    timeline = []
    events = []
    for r in sorted(rows, key=lambda r: (r['started_at'], r.get('burst_label', ''))):
        first, ended = r.get('first_content_at'), r.get('ended_at')
        began = r.get('inflight_started_at')
        if began is not None:
            events.extend([(began, 1, 0), (ended or end, -1, 0)])
        if first is not None:
            events.extend([(first, 0, 1), (ended or end, 0, -1)])
        state = 'complete' if r['success'] else r['error'] if ended else 'receiving' if first is not None else 'waiting_first_output'
        previous = [p for p in completed if first is not None and r['started_at'] < p['ended_at'] <= first
                    and first - p['ended_at'] <= config['release_window']]
        release = previous[-1] if previous else None
        relative = lambda value: None if value is None else max(0, value - start)
        timeline.append({'label': r.get('burst_label', ''), 'profile': r.get('workload_profile', 'short'),
                         'output_limit': r.get('output_limit'), 'state': state, 'http_status': r['status'] or None,
                         'upstream_error_kind': r.get('upstream_error_kind', ''),
                         'start_seconds': relative(r['started_at']), 'headers_seconds': relative(r.get('headers_at')),
                         'first_output_seconds': relative(first), 'last_output_seconds': relative(r.get('last_output_at')),
                         'end_seconds': relative(ended), 'elapsed_seconds': max(0, (ended or end) - r['started_at']),
                         'wait_seconds': max(0, (first or ended or end) - r['started_at']),
                         'output_chars': r['output_units'], 'piece_count': r.get('piece_count', 0),
                         'max_gap_seconds': r.get('max_output_gap_ms', 0) / 1000,
                         'silent_seconds': max(0, (ended or end) - r['last_output_at']) if r.get('last_output_at') else None,
                         'finish_reason': r.get('finish_reason', ''),
                         'after_release': release['burst_label'] if release else None,
                         'release_delay_seconds': first - release['ended_at'] if release else None})
    observations = []
    for r in releases:
        pending = [p for p in rows if p['started_at'] < r['ended_at'] and
                   (p.get('first_content_at') is None or p['first_content_at'] > r['ended_at']) and
                   (p.get('ended_at') is None or p['ended_at'] > r['ended_at'])]
        observations.append({'released_label': r['burst_label'], 'released_profile': r['workload_profile'],
                             'at_seconds': r['ended_at'] - start,
                             'waiting_labels': [p['burst_label'] for p in pending],
                             'later_output': [{'label': p['burst_label'], 'delay_seconds': p['first_content_at'] - r['ended_at']}
                                              for p in pending if p.get('first_content_at') is not None],
                             'ended_without_output': [p['burst_label'] for p in pending if p.get('ended_at') is not None
                                                      and p.get('first_content_at') is None]})
    active = receiving = peak = receiving_peak = 0
    for _, a, b in sorted(events):
        active += a
        receiving += b
        peak, receiving_peak = max(peak, active), max(receiving_peak, receiving)
    inflight_starts = [r['inflight_started_at'] for r in rows if r.get('inflight_started_at') is not None]
    return {'load_mode': 'mixed_burst', 'planned_requests': sum(config['counts']), 'issued_requests': len(rows),
            'finished_requests': sum(r.get('ended_at') is not None for r in rows),
            'reference_capacity': config['expected_capacity'], 'peak_inflight': peak, 'peak_receiving': receiving_peak,
            'launch_spread_ms': (max(inflight_starts) - min(inflight_starts)) * 1000 if inflight_starts else None,
            'received_output_requests': sum(r.get('first_content_at') is not None for r in rows),
            'total_output_chars': sum(r['output_units'] for r in rows),
            'errors': dict(Counter(r['error'] for r in rows if r['error'])),
            'early_http_errors': sum(r['status'] >= 400 and r.get('ended_at') is not None
                                     and r['ended_at'] - r['started_at'] <= 1 for r in rows),
            'http_error_window_seconds': 1, 'release_window_seconds': config['release_window'],
            'first_output_timeout_seconds': config['first_output_timeout'], 'idle_timeout_seconds': config['idle_timeout'],
            'total_timeout_seconds': config['total_timeout'], 'elapsed_seconds': max(0, end - start),
            'server_queue_verified': False, 'upstream_account_occupancy_verified': False,
            'timeline': timeline, 'release_observations': observations}
