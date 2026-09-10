import asyncio
import copy
import hashlib
import os
import time
import uuid
from contextlib import asynccontextmanager, nullcontext

from .adapter import OpenAIAdapter
from .checkpoint import Checkpoint
from .metrics import limits, stage_summary
from .mock import MockServer
from .network import mock_network_guard
from .report import Artifacts, atomic_json, revision
from .resources import Monitor
from .security import fingerprint


class Lab:
    def __init__(self, config, output, *, stop=None, confirm_live=False, checkpoint=None, task_id='default'):
        self.cfg = config
        self.artifacts = Artifacts(output)
        self.stop = stop or asyncio.Event()
        self.confirm_live = confirm_live
        self.checkpoint = checkpoint or (self.artifacts.output / 'checkpoint.sqlite3')
        self.task_id = task_id
        self.stages = []
        self.recoveries = []
        self.started = time.time()
        self.network = {'loopback_connections': 0, 'blocked_external_attempts': 0}
        self.revision = revision()
        atomic_json(self.artifacts.output / 'run.json', {'revision': self.revision,
                    'environment': 'live' if confirm_live else 'mock', 'started_at': self.started})

    @asynccontextmanager
    async def target(self, mock_config=None):
        mock = None
        if not self.cfg['base_url']:
            mock = await MockServer(mock_config or self.cfg['mock']).start()
        try:
            with mock_network_guard(self.network) if not self.confirm_live else nullcontext():
                async with OpenAIAdapter(self.cfg['base_url'] or mock.base_url,
                                         timeout=self.cfg['timeout'], connection_limit=self.cfg['connection_limit'],
                                         confirm_live=self.confirm_live, api_key=os.environ.get('RELAY_LAB_API_KEY') if self.confirm_live else None,
                                         model=self.cfg.get('model', 'relay-lab-model')) as adapter:
                    yield adapter, mock
        finally:
            if mock:
                await mock.close()

    async def stage(self, adapter, mock, label, concurrency, *, samples=None, **request):
        rows = []
        count = max(concurrency * self.cfg['rounds_per_stage'], self.cfg['samples']) if samples is None else max(concurrency, samples)
        queue = asyncio.Queue()
        for _ in range(count):
            queue.put_nowait(None)
        adapter.peak_active = 0
        if mock:
            mock.peak_active = 0
        start = time.perf_counter()
        monitor = Monitor(adapter, mock is not None)
        monitor_task = asyncio.create_task(monitor.run())

        async def worker():
            while not self.stop.is_set():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                r = await adapter.request(label, concurrency, **request)
                rows.append(r)
                self.artifacts.append(r)

        try:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
        finally:
            monitor.running = False
            await monitor_task
        elapsed = time.perf_counter() - start
        stage = stage_summary(label, concurrency, rows, elapsed, self.cfg['min_samples'], self.cfg['collapse_streak'])
        stage['resources'] = monitor.summary()
        stage['max_observed_inflight'] = mock.peak_active if mock else adapter.peak_active
        self.stages.append(stage)
        return stage, rows

    async def recovery(self, adapter, label, *, account=None, baseline_ms=None):
        start = time.perf_counter()
        success = 0
        probes = 0
        while time.perf_counter() - start < self.cfg['recovery_timeout'] and not self.stop.is_set():
            r = await adapter.request(label, 1, phase='recovery', account=account)
            self.artifacts.append(r)
            probes += 1
            good = r.success and (baseline_ms is None or r.latency_ms <= 2 * baseline_ms)
            success = success + 1 if good else 0
            if success >= self.cfg['recovery_successes']:
                value = {'stage': label, 'recovered': True, 'recovery_time': time.perf_counter() - start, 'probes': probes}
                self.recoveries.append(value)
                return value
            await asyncio.sleep(self.cfg['recovery_interval'])
        value = {'stage': label, 'recovered': False, 'recovery_time': None, 'probes': probes}
        self.recoveries.append(value)
        return value

    async def sweep(self, adapter, mock, label, stages, **request):
        group = []
        times = []
        for concurrency in stages:
            if self.stop.is_set():
                break
            stage, _ = await self.stage(adapter, mock, f'{label}-c{concurrency}', concurrency, **request)
            group.append(stage)
            if mock and mock.health()['healthy_account_count'] == 0:
                continue
            rec = await self.recovery(adapter, f'{label}-recovery-c{concurrency}', account=request.get('account'),
                                      baseline_ms=group[0]['p95_latency_ms'])
            if rec['recovery_time'] is not None:
                times.append(rec['recovery_time'])
        return group, limits(group, max(times) if times else None)

    async def account(self):
        async with self.target() as (adapter, mock):
            stages, analysis = await self.sweep(adapter, mock, 'account', self.cfg['stages'], account=0 if adapter.is_mock else None)
            analysis['admission_gate_passed'] = bool(stages and stages[0]['success_rate'] >= .99 and stages[0]['completeness_rate'] >= .99)
            analysis['admission_gate_low_confidence'] = not stages or stages[0]['low_confidence']
            return analysis

    async def gateway(self):
        cfg = copy.deepcopy(self.cfg['mock'])
        # An unlimited virtual upstream removes account capacity from the gateway test.
        cfg['accounts'] = [{'capacity': max(self.cfg['gateway_stages']) * 2,
                            'latency': cfg['latency'], 'jitter': cfg['jitter'], 'failure_rate': 0, 'cooldown': 0}]
        async with self.target(cfg) as (adapter, mock):
            stages, analysis = await self.sweep(adapter, mock, 'gateway', self.cfg['gateway_stages'])
            stable = analysis['max_stable_concurrency']
            analysis['max_stable_inflight'] = max((s['max_observed_inflight'] for s in stages if s['concurrency'] == stable), default=0)
            analysis['crash_semantics'] = 'temporary_mock_unavailability' if mock else 'observed_target_unavailability'
            return analysis

    async def pool(self):
        if self.cfg['base_url'] or self.confirm_live:
            raise ValueError('Pool topology experiments require the embedded Mock')
        groups = []
        single = None
        for size in self.cfg['pool_sizes']:
            if self.stop.is_set():
                break
            cfg = copy.deepcopy(self.cfg['mock'])
            profiles = cfg['accounts']
            cfg['accounts'] = [copy.deepcopy(profiles[i % len(profiles)]) for i in range(size)]
            capacity = sum(a.get('capacity', 3) for a in cfg['accounts'])
            for scenario in ('healthy', 'one_failed', 'half_failed'):
                if self.stop.is_set():
                    break
                async with self.target(cfg) as (adapter, mock):
                    failed = 0 if scenario == 'healthy' else (1 if scenario == 'one_failed' else (size + 1) // 2)
                    if failed:
                        mock.disable(range(failed), 86400)
                    health = mock.health()
                    stages, analysis = await self.sweep(adapter, mock, f'pool-{size}-{scenario}', self.cfg['pool_stages'])
                    observed = max((s['max_observed_inflight'] for s in stages
                                    if s['concurrency'] == analysis['max_stable_concurrency']), default=0)
                    if failed:
                        for account in mock.accounts:
                            account.disabled_until = 0
                        restored = await self.recovery(adapter, f'pool-{size}-{scenario}-restored')
                        analysis['recovery_time'] = restored['recovery_time']
                    if single is None and scenario == 'healthy':
                        single = (size, observed)
                    analysis.update({'account_count': size, 'scenario': scenario,
                                     'configured_capacity': capacity, 'observed_capacity': observed,
                                     'available_configured_capacity': sum(a.capacity for a in mock.accounts[failed:]),
                                     'capacity_utilization': observed / capacity,
                                     'scaling_efficiency': observed / (single[1] * size / single[0]) if single and single[1] else None,
                                     'healthy_account_count': health['healthy_account_count'],
                                     'failed_account_count': health['failed_account_count']})
                    groups.append(analysis)
        healthy = [g for g in groups if g['scenario'] == 'healthy']
        best = max(healthy, key=lambda x: x['account_count']) if healthy else limits([])
        return {**best, 'pool_scenarios': groups, 'scope': 'virtual_accounts_and_mock_scheduler_only'}

    async def chaos(self):
        if self.cfg['base_url'] or self.confirm_live:
            raise ValueError('Chaos controls require the embedded local Mock')
        groups = []
        for fault in self.cfg['faults']:
            if self.stop.is_set():
                break
            async with self.target() as (adapter, mock):
                baseline, _ = await self.stage(adapter, mock, f'chaos-{fault}-baseline', 1, samples=min(10, self.cfg['samples']))
                duration = self.cfg['mock']['fault_duration']
                request_fault = fault
                if fault == 'disable_account':
                    mock.disable([0], duration)
                    request_fault = 'normal'
                elif fault == 'partial_accounts':
                    mock.disable(range(max(1, len(mock.accounts) // 2)), duration)
                    request_fault = 'normal'
                elif fault == 'upstream_outage':
                    mock.outage_until = time.monotonic() + duration
                    request_fault = 'normal'
                stage, _ = await self.stage(adapter, mock, f'chaos-{fault}', self.cfg['stages'][0], fault=request_fault,
                                           account=0 if fault == 'disable_account' else None)
                rec = await self.recovery(adapter, f'chaos-{fault}-recovery', baseline_ms=baseline['p95_latency_ms'])
                groups.append({'fault': fault, **limits([baseline, stage], rec['recovery_time']), 'recovered': rec['recovered']})
        return {**limits(self.stages, max((r['recovery_time'] for r in self.recoveries if r['recovered']), default=None)), 'fault_scenarios': groups}

    async def long_task(self):
        cfg = self.cfg['long_task']
        if self.confirm_live and cfg['fail_attempts']:
            raise ValueError('Live long tasks cannot use Mock fault injection')
        async with self.target() as (adapter, mock):
            baseline, _ = await self.stage(adapter, mock, 'long-baseline', 1, samples=3)
            stream_stage, stream_rows = await self.stage(adapter, mock, 'long-single-stream', 1, samples=1,
                                                       chunks=cfg['stream_chunks'], chunk_delay=cfg['stream_chunk_delay'])
            rows = []
            start = time.perf_counter()
            task_fingerprint = fingerprint({'steps': cfg['steps'], 'chunks': self.cfg['mock']['chunks'],
                                            'environment': 'live' if self.confirm_live else 'mock',
                                            'model': self.cfg.get('model'), 'target': self.cfg['base_url']})
            with Checkpoint(self.checkpoint, self.task_id, task_fingerprint, cfg['steps']) as store:
                for number in range(1, cfg['steps'] + 1):
                    if self.stop.is_set():
                        break
                    if store.step(number)['confirmed']:
                        continue
                    for _ in range(cfg['max_retries'] + 1):
                        if self.stop.is_set():
                            break
                        step = store.begin(number)
                        fault = cfg['fault'] if number == cfg['fail_step'] and step['attempts'] <= cfg['fail_attempts'] else 'normal'
                        r = await adapter.request('long-steps', 1, step=number, attempt=step['attempts'], key=step['step_id'], fault=fault)
                        rows.append(r)
                        self.artifacts.append(r)
                        if r.success:
                            store.confirm(number, r)
                            break
                        store.failed(number)
                        if not cfg['auto_resume']:
                            break
                        await asyncio.sleep(cfg['retry_delay'])
                    if not store.step(number)['confirmed']:
                        break
                result = store.summary()
            step_stage = stage_summary('long-steps', 1, rows, time.perf_counter() - start, self.cfg['min_samples'])
            self.stages.append(step_stage)
            expected_hash = hashlib.sha256(('x' * self.cfg['mock']['chunks']).encode()).hexdigest() if mock else None
            result['mock_result_verified'] = bool(mock and result['final_complete'] and all(s['output_hash'] == expected_hash for s in result['steps']))
            result['single_stream_complete'] = bool(stream_rows and stream_rows[0].complete)
            result['single_stream_duration_seconds'] = stream_rows[0].latency_ms / 1000 if stream_rows else None
            result['single_stream_output_units'] = stream_rows[0].output_units if stream_rows else 0
            result['mock_single_stream_verified'] = bool(mock and stream_rows and stream_rows[0].output_sha256 ==
                                                        hashlib.sha256(('x' * cfg['stream_chunks']).encode()).hexdigest() and stream_rows[0].complete)
            result['confirmed_steps_reexecuted'] = sum(max(0, n - 1) for n in mock.executions.values()) if mock else None
            rec = await self.recovery(adapter, 'long-recovery')
            return {**limits([baseline, step_stage], rec['recovery_time']), **result,
                    'single_stream_limits': limits([stream_stage], rec['recovery_time']),
                    'limit_scope': 'sequential_steps; single long stream has its own workload baseline'}

    async def run(self, mode):
        status = 'completed'
        analysis = {}
        atomic_json(self.artifacts.output / 'run.json', {'revision': self.revision, 'mode': mode,
                    'environment': 'live' if self.confirm_live else 'mock', 'started_at': self.started})
        try:
            method = {'account-test': self.account, 'pool-test': self.pool, 'gateway-test': self.gateway,
                      'long-task-test': self.long_task, 'chaos-test': self.chaos}[mode]
            analysis = await method()
            if mode == 'long-task-test' and not (analysis['final_complete'] and analysis['single_stream_complete']):
                status = 'incomplete'
        except Exception:
            status = 'failed'
            raise
        finally:
            if self.stop.is_set():
                status = 'interrupted'
            summary = {'schema_version': 1, 'mode': mode, 'environment': 'live' if self.confirm_live else 'mock',
                       'status': status, 'revision': self.revision, 'started_at': self.started,
                       'elapsed_seconds': time.time() - self.started, 'result_count': self.artifacts.count,
                       'stages': self.stages, 'analysis': analysis, 'recoveries': self.recoveries,
                       'network_policy': {'numeric_loopback_only': not self.confirm_live, 'external_requests_authorized': self.confirm_live,
                                          'environment_proxies': False, 'redirects': False,
                                          'socket_audit': self.network if not self.confirm_live else None},
                       'config_fingerprint': fingerprint(self.cfg)}
            self.artifacts.finish(summary)
        return summary
