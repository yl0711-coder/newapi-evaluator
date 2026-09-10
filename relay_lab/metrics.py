import math
from collections import Counter


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p / 100
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def stage_summary(stage, concurrency, results, elapsed, min_samples, collapse_streak=3):
    rows = [r.public() if hasattr(r, 'public') else r for r in results]
    count = len(rows)
    good = [r for r in rows if r['success']]
    streak = longest = 0
    for r in rows:
        unavailable = r['collapse'] or r['error'] in ('connection_error', 'connection_pool_exhausted') or r['status'] == 503
        streak = streak + 1 if unavailable else 0
        longest = max(longest, streak)
    return {
        'stage': stage, 'concurrency': concurrency, 'samples': count,
        'success_rate': len(good) / count if count else 0,
        'completeness_rate': sum(r['complete'] for r in rows) / count if count else 0,
        'rate_429': sum(r['status'] == 429 for r in rows) / count if count else 0,
        'p50_latency_ms': percentile([r['latency_ms'] for r in good], 50),
        'p95_latency_ms': percentile([r['latency_ms'] for r in good], 95),
        'p99_latency_ms': percentile([r['latency_ms'] for r in good], 99),
        'p95_all_latency_ms': percentile([r['latency_ms'] for r in rows], 95),
        'p95_ttft_ms': percentile([r['ttft_ms'] for r in rows if r['ttft_ms'] is not None], 95),
        'p95_queue_ms': percentile([r['queue_ms'] for r in rows], 95),
        'output_units_per_second': sum(r['output_units_per_second'] for r in good) / len(good) if good else 0,
        'throughput_rps': len(good) / elapsed if elapsed else 0,
        'attempts_rps': count / elapsed if elapsed else 0,
        'elapsed_seconds': elapsed, 'errors': dict(Counter(r['error'] for r in rows if r['error'])),
        'statuses': dict(Counter(str(r['status']) for r in rows)),
        'connection_errors': sum(r['error'] in ('connection_error', 'connection_pool_exhausted', 'connect_timeout') for r in rows),
        'unavailable_streak': longest, 'collapsed': longest >= collapse_streak,
        'low_confidence': count < min_samples,
    }


def limits(stages, recovery_time=None):
    # Stage order is load order. Call separately for each pool size/scenario.
    baseline_stage = stages[0] if stages else None
    baseline = baseline_stage['p95_latency_ms'] if baseline_stage else None
    def first(predicate):
        return next((s['concurrency'] for s in stages if predicate(s)), None)
    def stable(s):
        return (s['success_rate'] >= .99 and s['completeness_rate'] >= .99 and
                baseline is not None and s['p95_latency_ms'] is not None and
                s['p95_latency_ms'] <= 2 * baseline and not s['collapsed'])
    return {
        'baseline': {'concurrency': baseline_stage['concurrency'], 'p95_latency_ms': baseline,
                     'samples': baseline_stage['samples']} if baseline_stage else None,
        'slow_point': first(lambda s: baseline is not None and s['p95_latency_ms'] is not None and s['p95_latency_ms'] > 2 * baseline),
        'unstable_point': first(lambda s: s['success_rate'] < .99 or s['completeness_rate'] < .99),
        'rate_limit_point': first(lambda s: s['rate_429'] >= .05),
        'collapse_point': first(lambda s: s['collapsed']),
        'max_stable_concurrency': max((s['concurrency'] for s in stages if stable(s)), default=0),
        'recovery_time': recovery_time,
        'low_confidence': not stages or any(s['low_confidence'] for s in stages),
        'range_censored': bool(stages) and all(stable(s) for s in stages),
    }
