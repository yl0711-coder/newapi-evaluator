"""Live three-channel HTTP load. Waiting is client evidence, not a server queue claim."""
import asyncio
import copy
import threading
import time
from collections import Counter

from .metrics import stage_summary
from .occupancy import Occupancy
from .report import atomic_json
from .resources import Monitor

PROFILES = ('short', 'medium', 'long')


def validate_targets(targets, ceiling):
    if (not isinstance(targets, list) or len(targets) != 3
            or any(type(n) is not int or not 0 <= n <= ceiling for n in targets)
            or sum(targets) > ceiling):
        raise ValueError('请将三个推子的总目标设在本次客户端并发上限以内。')
    return list(targets)


class Faders:
    def __init__(self, config, output, *, record=True):
        self.cfg = copy.deepcopy(config)
        self.output = output
        self.lock = threading.RLock()
        self.targets = list(config['targets'])
        self.paused = False
        self.accepting = True
        self.reason = None
        self.started = time.time()
        self.finished = None
        self.rows = {}
        self.events = []
        self.version = 0
        if record:
            self._event('start')

    def _event(self, action):
        self.version += 1
        self.events.append({'version': self.version, 'at_seconds': time.time() - self.started,
                            'action': action, 'targets': list(self.targets), 'paused': self.paused})
        atomic_json(self.output / 'fader-events.json', {'events': self.events})

    def adjust(self, body):
        with self.lock:
            if not self.accepting:
                raise ValueError('请等待本次测试收尾完成后新建测试。')
            if not isinstance(body, dict) or set(body) - {'targets', 'paused'} or not body:
                raise ValueError('请提供有效的推子目标或补发状态。')
            targets = validate_targets(body.get('targets', self.targets), self.cfg['max_inflight'])
            paused = body.get('paused', self.paused)
            if type(paused) is not bool:
                raise ValueError('请提供有效的补发状态。')
            if len(self.events) >= 10000:
                raise ValueError('请结束当前测试后新建测试，调节记录已达到上限。')
            if targets != self.targets or paused != self.paused:
                self.targets, self.paused = targets, paused
                self._event('pause' if paused else 'adjust')
            return self.version

    def desired(self):
        with self.lock:
            return ([0, 0, 0] if self.paused or not self.accepting else list(self.targets)), self.version

    def close(self, reason):
        with self.lock:
            if self.accepting:
                self.accepting = False
                self.reason = reason
                self._event(reason)

    def track(self, result):
        with self.lock:
            self.rows[result.request_id] = result.public()

    def snapshot(self):
        with self.lock:
            now = self.finished or time.time()
            rows = list(self.rows.values())
            pending = [r for r in rows if r['ended_at'] is None]
            ended = sorted((r for r in rows if r['ended_at'] is not None), key=lambda r: r['ended_at'])
            visible = sorted(pending + ended[-60:], key=lambda r: r['started_at'], reverse=True)
            timeline = []
            for r in visible:
                end = r['ended_at'] or now
                state = ('complete' if r['success'] else r['error']) if r['ended_at'] else (
                    'receiving' if r['first_content_at'] else 'waiting_first_output' if r['headers_at'] else 'waiting_headers')
                rel = lambda t: t - self.started if t is not None else None
                timeline.append({'label': r['burst_label'], 'profile': r['workload_profile'], 'state': state,
                                 'http_status': r['status'] or None, 'output_chars': r['output_units'],
                                 'output_limit': r['output_limit'], 'upstream_error_kind': r['upstream_error_kind'],
                                 'start_seconds': rel(r['started_at']), 'headers_seconds': rel(r['headers_at']),
                                 'first_output_seconds': rel(r['first_content_at']), 'end_seconds': rel(r['ended_at']),
                                 'wait_seconds': (r['first_content_at'] or end) - r['started_at'],
                                 'client_gate_ms': r['queue_ms'],
                                 'silent_seconds': end - r['last_output_at'] if r['last_output_at'] else None})
            channels = []
            for profile, target in zip(PROFILES, self.targets):
                group = [r for r in rows if r['workload_profile'] == profile]
                live = [r for r in group if r['ended_at'] is None]
                channels.append({'profile': profile, 'target': target, 'issued': len(group), 'inflight': len(live),
                                 'waiting': sum(r['first_content_at'] is None for r in live),
                                 'receiving': sum(r['first_content_at'] is not None for r in live),
                                 'headers_received': sum(r['headers_at'] is not None for r in group),
                                 'received_output': sum(r['first_content_at'] is not None for r in group),
                                 'complete': sum(r['success'] for r in group),
                                 'errors': dict(Counter(r['error'] for r in group if r['error']))})
            return {'load_mode': 'faders', 'targets': list(self.targets), 'paused': self.paused,
                    'accepting': self.accepting, 'stop_reason': self.reason, 'version': self.version,
                    'max_inflight': self.cfg['max_inflight'], 'max_requests': self.cfg['max_requests'],
                    'duration': self.cfg['duration'], 'refill_interval': self.cfg['refill_interval'],
                    'output_limits': list(self.cfg['output_limits']), 'elapsed_seconds': now - self.started,
                    'issued_requests': len(rows), 'inflight': len(pending), 'channels': channels,
                    'events': copy.deepcopy(self.events), 'timeline': timeline,
                    'timeline_scope': 'all_pending_and_latest_60_ended; all_requests_in_results_jsonl',
                    'server_queue_verified': False, 'upstream_account_occupancy_verified': False}


async def run(lab):
    control, cfg = lab.faders, lab.cfg['faders']
    rows, tasks = [], {}
    serial = [0, 0, 0]
    issued = 0
    started = time.monotonic()
    next_refill = started
    seen_version = 0
    label = 'live-faders'
    async with lab.target() as (adapter, mock):
        occupancy = lab.live_occupancy = adapter.occupancy = Occupancy(label, sum(cfg['targets']))
        occupancy.set_target(sum(cfg['targets']))
        adapter.on_progress = control.track
        lab.current_phase = 'faders'
        monitor = Monitor(adapter, mock is not None)
        monitoring = asyncio.create_task(monitor.run())

        async def worker(index, name, concurrency):
            options = ({'chunks': 40, 'chunk_delay': cfg['mock_durations'][index] / 40} if adapter.is_mock else {})
            result = await adapter.request(label, max(1, concurrency), workload=PROFILES[index],
                                           output_tokens=cfg['output_limits'][index],
                                           limit_field=lab.cfg['workload']['limit_field'], burst_label=name,
                                           account=0 if adapter.is_mock else None, **options)
            rows.append(result)
            lab.artifacts.append(result)

        try:
            while True:
                now = time.monotonic()
                for task in list(tasks):
                    if task.done():
                        del tasks[task]
                        task.result()
                if lab.stop.is_set():
                    control.close('stopped')
                    occupancy.close_load('stopped')
                    break
                if now - started >= cfg['duration'] or issued >= cfg['max_requests']:
                    reason = 'duration' if now - started >= cfg['duration'] else 'request_cap'
                    control.close(reason)
                    occupancy.close_load(reason)
                    lab.current_phase = 'draining'
                desired, version = control.desired()
                if version != seen_version:
                    occupancy.set_target(sum(desired))
                    next_refill, seen_version = now, version
                if not control.accepting and not tasks:
                    break
                if control.accepting and now >= next_refill:
                    counts = Counter(tasks.values())
                    needs = [max(0, desired[i] - counts[i]) for i in range(3)]
                    budget = min(cfg['max_inflight'] - len(tasks), cfg['max_requests'] - issued)
                    # Interleave channels in each refill wave. No hidden small client connection pool.
                    for n in range(max(needs)):
                        for i in range(3):
                            if n >= needs[i] or budget <= 0:
                                continue
                            serial[i] += 1
                            task = asyncio.create_task(worker(i, 'SML'[i] + str(serial[i]), sum(desired)))
                            tasks[task] = i
                            issued += 1
                            budget -= 1
                    next_refill = now + cfg['refill_interval']
                try:
                    await asyncio.wait_for(lab.stop.wait(), .05)
                except TimeoutError:
                    pass
        finally:
            control.close('stopped' if lab.stop.is_set() else 'finished')
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            monitor.running = False
            await monitoring
            occupancy.finish(control.reason)
            adapter.on_progress = None
            control.finished = time.time()
        stage = stage_summary(label, max(1, occupancy.peak), rows, time.monotonic() - started, lab.cfg['min_samples'])
        stage.update({'load_mode': 'faders', 'workload_profile': 'mixed', 'output_limit': None,
                      'occupancy': occupancy.snapshot(), 'resources': monitor.summary(),
                      'max_observed_inflight': mock.peak_active if mock else adapter.peak_active})
        lab.stages.append(stage)
    return {'max_stable_concurrency': None, 'capacity_limit_determined': False, 'low_confidence': True,
            'concurrency_scope': 'client_requests_only', 'faders': control.snapshot()}
