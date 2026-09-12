"""Loopback console; keys are per-run memory only, never environment variables or files."""
import asyncio
import copy
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import DATA_ROOT, data_path, load
from .metrics import percentile
from .report import revision
from .runner import Lab
from .security import target_url

MODES = {'account-test': '单号测试', 'pool-test': '号池容量', 'gateway-test': '网关极限',
         'long-task-test': '长任务恢复', 'chaos-test': '故障注入'}
STATIC = Path(__file__).resolve().parent / 'web'


def run_config(body):
    mode = body.get('mode', 'account-test')
    live = body.get('environment') == 'live'
    if mode not in MODES or body.get('environment') not in ('mock', 'live'):
        raise ValueError('请选择有效的测试模式和环境。')
    if live and body.get('confirm_live') is not True:
        raise ValueError('请勾选真实请求确认。')
    if live and mode in ('pool-test', 'chaos-test'):
        raise ValueError('号池拓扑与故障注入目前只支持本地 Mock。')
    stages = body.get('stages', [1])
    samples = body.get('samples', 8)
    timeout = body.get('timeout', 90 if live else 1)
    load_mode = body.get('load_mode', 'requests')
    duration = body.get('stage_duration', 60)
    cap = body.get('max_stage_requests', 1000)
    profile = body.get('workload_profile', 'short')
    output_tokens = body.get('output_tokens', 1024)
    limit_field = body.get('limit_field', 'max_tokens')
    if load_mode in ('mixed_burst', 'faders'):
        # Hidden legacy controls do not participate in fixed-cohort validation.
        stages, samples, timeout, duration, cap, profile, output_tokens = [1], 8, 90 if live else 1, 60, 1000, 'short', 1024
    if load_mode not in ('requests', 'duration', 'mixed_burst', 'faders') or profile not in ('short', 'long'):
        raise ValueError('请选择有效的负载方式。')
    if mode not in ('account-test', 'gateway-test') and (load_mode != 'requests' or profile != 'short'):
        raise ValueError('持续并发与长输出仅用于单号和网关测试。')
    if type(duration) not in (int, float) or not 1 <= duration <= 3600:
        raise ValueError('每阶持续时长应为 1–3600 秒。')
    if type(cap) is not int or not 1 <= cap <= 100000:
        raise ValueError('每阶请求上限应为 1–100000。')
    if type(output_tokens) is not int or not 16 <= output_tokens <= 32768 or limit_field not in ('max_tokens', 'max_completion_tokens'):
        raise ValueError('请选择有效输出参数，上限为 16–32768 token。')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 10 or any(type(c) is not int or not 1 <= c <= 1200 for c in stages):
        raise ValueError('并发阶梯需为 1–1200 之间的升序整数，最多 10 阶。')
    if type(samples) is not int or not 1 <= samples <= 1000:
        raise ValueError('每阶请求数应为 1–1000。')
    if type(timeout) not in (float, int) or not .05 <= timeout <= 600:
        raise ValueError('单请求超时应为 0.05–600 秒。')
    if load_mode == 'duration' and cap < max(stages):
        raise ValueError('每阶请求上限不得小于最高并发。')
    burst = copy.deepcopy(load()['mixed_burst'])
    burst['enabled'] = load_mode == 'mixed_burst'
    if load_mode in ('mixed_burst', 'faders'):
        supplied = body.get('mixed_burst', {})
        if not isinstance(supplied, dict) or set(supplied) - (set(burst) - {'enabled'}):
            raise ValueError('请选择有效的混合批次参数。')
        if load_mode == 'faders':
            supplied = {k: v for k, v in supplied.items() if k not in ('counts', 'output_limits')}
        burst.update(supplied)
        # Validate the shape before using counts to size the actual client connection pool.
        try:
            load(overrides={'mixed_burst': burst})
        except (ValueError, TypeError, KeyError):
            raise ValueError('请选择有效的混合批次长度、数量和超时参数。') from None
        stages = [sum(burst['counts'])]
    faders = copy.deepcopy(load()['faders'])
    faders['enabled'] = load_mode == 'faders'
    if faders['enabled']:
        supplied = body.get('faders', {})
        if not isinstance(supplied, dict) or set(supplied) - (set(faders) - {'enabled', 'mock_durations'}):
            raise ValueError('请选择有效的推子设置。')
        faders.update(supplied)
        try:
            load(overrides={'faders': faders, 'connection_limit': 1200})
        except (ValueError, TypeError, KeyError):
            raise ValueError('请检查推子目标、并发上限、时长及请求上限。') from None
        stages = [max(1, sum(faders['targets']))]
    base_url = None
    model = 'mock-model'
    if live:
        base_url = target_url(body.get('base_url', ''), True)
        model = body.get('model', '')
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError('请填写可调用的模型名称。')
        key = body.get('api_key', '')
        if not isinstance(key, str) or not key.strip() or len(key) > 8192 or '\n' in key or '\r' in key:
            raise ValueError('请填写本次调用使用的 API Key。')
    pool_sizes = body.get('pool_sizes', [1, 2, 4])
    if not isinstance(pool_sizes, list) or not 1 <= len(pool_sizes) <= 5 or any(type(n) is not int or not 1 <= n <= 20 for n in pool_sizes):
        raise ValueError('Mock 账号数量应为 1–20，最多 5 阶。')
    step_count = body.get('steps', 6)
    if type(step_count) is not int or not 1 <= step_count <= 100:
        raise ValueError('长任务步骤数应为 1–100。')
    cfg = load(overrides={
        'base_url': base_url, 'model': model.strip(), 'stages': stages,
        'gateway_stages': stages, 'pool_stages': stages, 'samples': samples,
        'timeout': timeout, 'connection_limit': faders['max_inflight'] if faders['enabled'] else max(stages), 'pool_sizes': pool_sizes,
        'stage_duration': duration if load_mode == 'duration' else 0, 'max_stage_requests': cap,
        'mixed_burst': burst,
        'faders': faders,
        'workload': {'profile': profile, 'output_tokens': output_tokens, 'limit_field': limit_field},
        'recovery_timeout': min(15, timeout) if live else 1, 'recovery_successes': 2,
        'mock': {'accounts': [{'capacity': 3, 'latency': .015, 'cooldown': .04},
                              {'capacity': 5, 'latency': .03, 'cooldown': .05}],
                 'slow_at': 50 if mode == 'gateway-test' else 100000,
                 'slow_factor': 8, 'crash_at': 200 if mode == 'gateway-test' else 100000},
        'long_task': {'steps': step_count, 'fail_step': min(3, step_count),
                      'fail_attempts': 0 if live else 1, 'stream_chunks': 60, 'stream_chunk_delay': .01},
    })
    if (burst['enabled'] or faders['enabled']) and not live:
        policy = body.get('mock_admission_policy', 'queue')
        if policy not in ('queue', 'reject'):
            raise ValueError('请选择有效的 Mock 排队行为。')
        cfg['mock']['admission_policy'] = policy
        cfg['mock']['accounts'] = [{'capacity': burst['expected_capacity'], 'latency': .015, 'cooldown': 0}]
        if faders['enabled']:
            cfg['mock']['queue_timeout'] = burst['first_output_timeout']
    return mode, live, cfg


class Job:
    def __init__(self, mode, environment, output, identifier=None):
        self.id = identifier or uuid.uuid4().hex
        self.mode, self.environment = mode, environment
        self.output = data_path(output)
        self.started = time.time()
        self.status = 'running'
        self.message = ''
        self.summary = None
        self.lab = None
        self.burst_config = None
        self.loop = None
        self.stop = None
        self.thread = None
        self.lock = threading.RLock()
        self.total = self.load_count = self.success = self.complete = 0
        self.latencies, self.ttfts = deque(maxlen=10000), deque(maxlen=10000)
        self.recent = deque(maxlen=30)
        self.errors = Counter()
        self.current_stage = ''

    def record(self, row):
        with self.lock:
            self.total += 1
            self.current_stage = row['stage']
            self.recent.append(row)
            if row['phase'] == 'load':
                self.load_count += 1
                self.success += bool(row['success'])
                self.complete += bool(row['complete'])
                if row['success']:
                    self.latencies.append(row['latency_ms'])
                if row['ttft_ms'] is not None:
                    self.ttfts.append(row['ttft_ms'])
                if row['error']:
                    self.errors[row['error']] += 1

    def public(self, detail=True):
        with self.lock:
            duration = self.summary.get('elapsed_seconds', 0) if self.summary else time.time() - self.started
            value = {'id': self.id, 'mode': self.mode, 'name': MODES[self.mode], 'environment': self.environment,
                     'status': self.status, 'started_at': self.started, 'elapsed_seconds': duration,
                     'message': self.message, 'current_stage': self.current_stage,
                     'metrics': {'requests': self.total, 'load_requests': self.load_count,
                                 'success_rate': self.success / self.load_count if self.load_count else None,
                                 'completeness_rate': self.complete / self.load_count if self.load_count else None,
                                 'p95_latency_ms': percentile(self.latencies, 95),
                                 'p50_ttft_ms': percentile(self.ttfts, 50), 'errors': dict(self.errors),
                                 'latency_window_samples': len(self.latencies)},
                     'downloads': [name for name in ('report.md', 'summary.json', 'results.jsonl', 'fader-events.json') if (self.output / name).is_file()]}
            if detail:
                value['occupancy'] = self.lab.live_occupancy.snapshot() if self.lab and self.lab.live_occupancy else None
                value['phase'] = self.lab.current_phase if self.lab else self.status
                value['recent'] = list(self.recent)
                value['stages'] = copy.deepcopy(self.summary['stages'] if self.summary else self.lab.stages if self.lab else [])
                value['analysis'] = copy.deepcopy(self.summary.get('analysis', {}) if self.summary else {})
                value['revision'] = self.summary.get('revision') if self.summary else None
                value['faders'] = (self.lab.faders.snapshot() if self.lab and self.lab.faders
                                   else self.summary.get('analysis', {}).get('faders') if self.summary else None)
                from .burst import snapshot
                value['burst'] = (self.lab.burst_snapshot() if self.lab and self.lab.cfg['mixed_burst']['enabled']
                                  else self.summary.get('analysis', {}).get('burst') if self.summary
                                  else snapshot([], self.burst_config) if self.burst_config else None)
            return value


class Console:
    def __init__(self, data_dir=None, history=True):
        self.data_dir = data_path(data_dir or DATA_ROOT / 'console')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.lock = threading.RLock()
        if history:
            self._history()

    def _history(self):
        paths = list((self.data_dir / 'runs').glob('*/summary.json'))
        delivery = DATA_ROOT / 'delivery.json'
        if delivery.exists():
            try:
                paths += [data_path(r['summary']) for r in json.loads(delivery.read_text()).get('example_reports', [])]
            except (ValueError, OSError, KeyError):
                pass
        for path in sorted(set(paths), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:30]:
            try:
                summary = json.loads(path.read_text())
                if summary.get('mode') not in MODES:
                    continue
                job = Job(summary['mode'], summary['environment'], path.parent,
                          uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex)
                job.summary = summary
                job.status = summary['status']
                job.started = summary['started_at']
                for line in (path.parent / 'results.jsonl').read_text().splitlines():
                    job.record(json.loads(line))
                self.jobs[job.id] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def create(self, body):
        mode, live, cfg = run_config(body)
        key = body.pop('api_key', '') if live else None
        with self.lock:
            if any(j.status in ('running', 'stopping') for j in self.jobs.values()):
                raise ValueError('已有测试正在运行，请等待完成或先停止当前测试。')
            identifier = uuid.uuid4().hex
            job = Job(mode, 'live' if live else 'mock', self.data_dir / 'runs' / identifier, identifier)
            job.burst_config = cfg['mixed_burst'] if cfg['mixed_burst']['enabled'] else None
            self.jobs[job.id] = job

        async def run():
            job.loop = asyncio.get_running_loop()
            job.stop = asyncio.Event()
            if job.status == 'stopping':
                job.stop.set()
            lab = None
            try:
                lab = Lab(cfg, job.output, stop=job.stop, confirm_live=live, api_key=key)
                job.lab = lab
                append = lab.artifacts.append
                def record(result):
                    append(result)
                    job.record(result.public())
                lab.artifacts.append = record
                summary = await lab.run(mode)
                with job.lock:
                    job.summary = summary
                    job.status = summary['status']
            except Exception:
                with job.lock:
                    job.status = 'failed'
                    job.message = '测试未能完成，请检查接口配置、模型名称与输出目录。'
                    if (job.output / 'summary.json').exists():
                        job.summary = json.loads((job.output / 'summary.json').read_text())
            finally:
                if lab:
                    lab.api_key = None
                job.lab = None
                job.loop = None

        job.thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True, name='relay-ui-job')
        job.thread.start()
        return job

    def cancel(self, identifier):
        job = self.jobs[identifier]
        with job.lock:
            if job.status in ('running', 'stopping'):
                job.status = 'stopping'
                if job.loop and job.stop:
                    job.loop.call_soon_threadsafe(job.stop.set)
        return job

    def adjust_faders(self, identifier, body):
        job = self.jobs[identifier]
        with job.lock:
            if job.status != 'running' or not job.lab or not job.lab.faders:
                raise ValueError('请在运行中的推子测试里调整负载。')
            job.lab.faders.adjust(body)
        return job

    def handler(self):
        console = self
        class Handler(BaseHTTPRequestHandler):
            server_version = 'RelayLab'
            def log_message(self, *args):
                pass  # No URLs, request headers or payloads in access logs.

            def respond(self, status, value, content_type='application/json; charset=utf-8', download=None):
                body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if isinstance(value, (dict, list)) else value
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
                if download:
                    self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                port = self.server.server_port
                if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                    self.respond(403, {'error': '仅允许本机访问。'})
                    return False
                origin = self.headers.get('Origin')
                if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                    self.respond(403, {'error': '不接受其他页面发起的请求。'})
                    return False
                return True

            def do_GET(self):
                if not self.allowed():
                    return
                try:
                    if self.path in ('/', '/app.css', '/app.js', '/faders.js'):
                        name = 'index.html' if self.path == '/' else self.path[1:]
                        kind = {'index.html': 'text/html', 'app.css': 'text/css', 'app.js': 'text/javascript', 'faders.js': 'text/javascript'}[name]
                        return self.respond(200, (STATIC / name).read_bytes(), kind + '; charset=utf-8')
                    if self.path == '/api/state':
                        with console.lock:
                            jobs = [j.public(False) for j in console.jobs.values()]
                        return self.respond(200, {'service': 'relay-lab-console', 'ui_schema_version': 4, 'pid': os.getpid(), 'csrf': console.token,
                                                 'revision': revision(), 'jobs': sorted(jobs, key=lambda j: j['started_at'], reverse=True)})
                    match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(report\.md|summary\.json|results\.jsonl|fader-events\.json))?', self.path)
                    if match and match[1] in console.jobs:
                        job = console.jobs[match[1]]
                        if match[2]:
                            path = job.output / match[2]
                            if path.is_file():
                                return self.respond(200, path.read_bytes(), 'text/plain; charset=utf-8', match[2])
                        else:
                            return self.respond(200, job.public())
                    self.respond(404, {'error': '未找到页面或测试记录。'})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.respond(500, {'error': '暂时无法读取测试记录。'})

            def do_POST(self):
                if not self.allowed():
                    return
                if not secrets.compare_digest(self.headers.get('X-Relay-UI', ''), console.token):
                    return self.respond(403, {'error': '页面会话已失效，请刷新页面。'})
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 65536 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.respond(400, {'error': '请求格式无效。'})
                    self.connection.settimeout(5)
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        return self.respond(400, {'error': '请求格式无效。'})
                    if self.path == '/api/jobs':
                        return self.respond(202, console.create(body).public())
                    match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/faders', self.path)
                    if match and match[1] in console.jobs:
                        return self.respond(200, console.adjust_faders(match[1], body).public())
                    match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/stop', self.path)
                    if match and match[1] in console.jobs:
                        return self.respond(200, console.cancel(match[1]).public())
                    self.respond(404, {'error': '未找到测试任务。'})
                except ValueError as error:
                    # Only our known validation messages; never echo YAML/JSON/URL parser input.
                    message = str(error)
                    safe = message if message.startswith(('请', '已有', '并发', '每阶', '单请求', '号池', 'Mock 账号', '长任务')) else '接口地址或参数无效，请检查输入。'
                    self.respond(400, {'error': safe})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.respond(500, {'error': '无法启动测试，请检查配置后重试。'})
        return Handler


async def serve(port=8878, stop=None):
    if not 0 <= port <= 65535:
        raise ValueError('Port out of range')
    console = Console()
    server = ThreadingHTTPServer(('127.0.0.1', port), console.handler())
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f'中转站控制台：http://127.0.0.1:{server.server_port}', flush=True)
    try:
        await (stop or asyncio.Event()).wait()
    finally:
        for job in list(console.jobs.values()):
            console.cancel(job.id)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        # Preserve partial reports before stopping the process.
        for job in list(console.jobs.values()):
            if job.thread and job.thread.is_alive():
                await asyncio.to_thread(job.thread.join, 605)
    return 0
