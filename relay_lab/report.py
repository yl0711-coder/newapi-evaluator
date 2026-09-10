import json
import os
import subprocess
from dataclasses import fields
from pathlib import Path

from .config import data_path
from .model import Result
from .security import redact


def revision():
    root = Path(__file__).resolve().parents[1]
    sha = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True, text=True).stdout.strip()
    return {'commit_sha': sha if len(sha) == 40 else None, 'dirty': bool(dirty)}


def atomic_json(path, value):
    path = data_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Artifacts:
    def __init__(self, output):
        self.output = data_path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        # Never overwrite a previous run. Resume uses a fresh run directory and same checkpoint.
        self.raw = (self.output / 'results.jsonl').open('x', buffering=1)
        self.count = 0

    def append(self, result):
        self.raw.write(json.dumps(result.public(), ensure_ascii=False, allow_nan=False) + '\n')
        self.raw.flush()
        self.count += 1

    def finish(self, summary):
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        atomic_json(self.output / 'summary.json', summary)
        write_markdown(self.output / 'report.md', summary)


def write_markdown(path, summary):
    path = data_path(path)
    summary = redact(summary)
    lines = ['# 中转站极限测试报告', '',
             'Mock 测试结果不代表真实账号质量、Sub2API 调度或真实容量。' if summary['environment'] == 'mock' else
             ('显式授权的真实目标测试；Mock 与真实结果不得混用。' if summary['environment'] == 'live' else
              '环境来源未知，不能据此宣称真实或 Mock 环境验证。'), '',
             f"- 模式：{summary['mode']}", f"- 状态：{summary['status']}",
             f"- 提交：{summary['revision']['commit_sha']}", f"- 工作树有修改：{summary['revision']['dirty']}",
             f"- 请求级指标记录数：{summary['result_count']}",
             '- 输出速度单位：字符/秒；未假设字符等于 token。',
             '- null 表示未观察到或未测得；range_censored 表示最高测试阶梯仍稳定，不能视为绝对上限。',
             '- low_confidence 表示样本数小于配置门槛；此标记不构成统计置信区间。', '',
             '| 阶段 | 并发 | 样本 | 成功率 | 完整率 | P95 ms | 429 比例 | 吞吐 req/s |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for s in summary['stages']:
        latency = '-' if s['p95_latency_ms'] is None else f"{s['p95_latency_ms']:.2f}"
        lines.append(f"| {s['stage']} | {s['concurrency']} | {s['samples']} | {s['success_rate']:.2%} | {s['completeness_rate']:.2%} | {latency} | {s['rate_429']:.2%} | {s['throughput_rps']:.2f} |")
    measured = [s for s in summary['stages'] if 'occupancy' in s]
    if measured:
        lines += ['', '## 持续并发与占用', '',
                  '在途指客户端已进入 HTTP 调用但尚未结束的请求，包含连接和上游排队。接收中指首段内容至请求结束，不证明上游账号同时生成。',
                  '平均值与满并发时间占比按负载窗口计算，排除到时后的收尾及串行恢复探测。曲线在 JSON 的 occupancy.series 中，曲线采样不参与平均值计算。', '',
                  '| 阶段 | 方式 | 目标 | 峰值在途 | 平均在途 | 满并发时间 | 平均接收中 | 负载秒 | 收尾秒 | 停止原因 |',
                  '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
        reasons = {'duration': '时长到达', 'request_count': '请求数完成', 'request_cap': '请求上限提前到达', 'stopped': '用户停止'}
        for s in measured:
            o = s['occupancy']
            lines.append(f"| {s['stage']} | {'持续' if s.get('load_mode') == 'duration' else '按请求数'} | {o['target']} | {o['peak_inflight']} | {o['mean_inflight']:.2f} | {o['target_occupancy_ratio']:.1%} | {o['mean_receiving']:.2f} | {o['load_seconds']:.2f} | {o['drain_seconds']:.2f} | {reasons.get(o['stop_reason'], '未知')} |")
        lines += ['', '上游账号占用：未接入真实服务端证据；客户端并发不得直接当作单账号生成并发。', '', '### 输出负载与失败状态', '']
        for s in measured:
            lines.append(f"- {s['stage']}：负载 {s.get('workload_profile', 'short')}；输出上限 {s.get('output_limit') or '未设置'}；实际平均输出 {s.get('mean_output_chars', 0):.1f} 字符；首段内容后平均持续 {s.get('mean_receiving_seconds', 0):.2f} 秒；HTTP 状态分布 {json.dumps(s['statuses'])}。")
        lines += ['', '输出上限不是最低输出保证。长输出负载到达上限（finish_reason=length）且完整收到结束帧时计为传输完整，截断原因仍记录在请求指标中。']
    lines += ['', '## 极限与模式指标', '', '```json', json.dumps(summary['analysis'], ensure_ascii=False, indent=2), '```', '',
              '## 资源与验证边界', '',
              '资源采样范围见各阶段 resources.scope。嵌入 Mock 时客户端与 Mock 同进程；远端服务进程的 CPU/内存/FD 不由客户端推断。',
              '崩溃点依据连续不可用响应或连接故障判定；Mock 使用短暂 503 模拟崩溃，不终止宿主机进程。',
              '原始 JSONL 仅含指标、摘要散列和内部标识，不含 Prompt、响应正文、凭据或目标 URL。', '']
    path.write_text('\n'.join(lines))


def rebuild(output, *, mode='recovered'):
    output = data_path(output)
    from .metrics import limits, stage_summary
    groups = {}
    count = 0
    with (output / 'results.jsonl').open() as f:
        for line in f:
            try:
                row = json.loads(line)
                expected = {x.name for x in fields(Result)}
                legacy = expected - {'started_at', 'inflight_started_at', 'first_content_at', 'ended_at', 'finish_reason'}
                if set(row) not in (expected, legacy):
                    raise ValueError('Unexpected raw schema')
                count += 1
                if row['phase'] == 'load':
                    groups.setdefault((row['stage'], row['concurrency']), []).append(row)
            except json.JSONDecodeError:
                continue  # A torn final line after abrupt process termination is not a valid sample.
    if (output / 'summary.json').exists():
        summary = json.loads((output / 'summary.json').read_text())
    else:
        manifest = json.loads((output / 'run.json').read_text()) if (output / 'run.json').exists() else {}
        stages = [stage_summary(key[0], key[1], rows, sum(r['latency_ms'] for r in rows) / 1000, 100)
                  for key, rows in groups.items()]
        summary = {'environment': manifest.get('environment', 'unknown'), 'mode': manifest.get('mode', mode), 'status': 'partial_reconstructed',
                   'revision': manifest.get('revision', {'commit_sha': None, 'dirty': None}), 'result_count': count,
                   'stages': stages, 'analysis': limits(stages)}
        # Wall-clock throughput cannot be reconstructed from per-request duration.
        for s in stages:
            s['throughput_rps'] = s['attempts_rps'] = 0
            s['throughput_unavailable'] = True
        atomic_json(output / 'summary.json', summary)
    write_markdown(output / 'report.md', summary)
    return summary
