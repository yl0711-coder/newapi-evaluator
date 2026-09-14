import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid

from .config import DATA_ROOT, data_path, load, validate
from .mock import MockServer
from .report import rebuild
from .runner import Lab
from .security import fingerprint


def parser():
    p = argparse.ArgumentParser(prog='relay-lab', description='中转站稳定性与极限测试；默认只允许本地 Mock')
    sub = p.add_subparsers(dest='command', required=True)
    ui = sub.add_parser('ui', help='启动本地测试控制台')
    ui.add_argument('--port', type=int, default=8878)
    server = sub.add_parser('mock-server', help='启动仅绑定 127.0.0.1 的 Mock')
    server.add_argument('--config')
    server.add_argument('--port', type=int, default=8877)
    for name in ('account-test', 'pool-test', 'gateway-test', 'long-task-test', 'chaos-test'):
        run = sub.add_parser(name)
        run.add_argument('--config')
        run.add_argument('--output', default=None)
        run.add_argument('--base-url', default=None)
        run.add_argument('--confirm-live', action='store_true')
        run.add_argument('--checkpoint', default=None)
        run.add_argument('--task-id', default='default')
        if name in ('account-test', 'gateway-test'):
            run.add_argument('--mixed-burst', action='store_true', help='一次同时发出固定混合批次，不补发、不追加恢复探测')
            run.add_argument('--duration', type=float, help='每阶持续补发秒数，0 为按请求数')
            run.add_argument('--max-requests', type=int, help='持续模式每阶请求上限')
            run.add_argument('--long-output', action='store_true', help='使用长输出负载')
            run.add_argument('--output-tokens', type=int)
            run.add_argument('--output-limit-field', choices=('max_tokens', 'max_completion_tokens'))
    report = sub.add_parser('report', help='从已保存结果重建 Markdown 报告')
    report.add_argument('--output', required=True)
    inspect = sub.add_parser('inspect-config', help='一键提取脱敏目标与配置摘要')
    inspect.add_argument('--config')
    return p


async def execute(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.command == 'ui':
        from .console import serve
        return await serve(args.port, stop)
    if args.command == 'report':
        value = rebuild(args.output)
        print(json.dumps({'status': value['status'], 'result_count': value['result_count']}))
        return 0
    cfg = load(args.config)
    if args.command == 'inspect-config':
        print(json.dumps({'target_alias': 'configured-target' if cfg['base_url'] else 'embedded-local-mock',
                          'protocol': 'openai-chat-completions-sse', 'model_count': 1,
                          'account_profiles': len(cfg['mock']['accounts']), 'config_fingerprint': fingerprint(cfg),
                          'extracted_at': time.time()}, ensure_ascii=False))
        return 0
    if args.command == 'mock-server':
        if not 0 <= args.port <= 65535:
            raise ValueError('Port out of range')
        server = await MockServer(cfg['mock']).start(args.port)
        print(json.dumps({'service': 'relay-lab-mock', 'host': '127.0.0.1', 'port': server.port}), flush=True)
        try:
            await stop.wait()
        finally:
            await server.close()
        return 0
    if args.base_url:
        cfg['base_url'] = args.base_url
    if args.confirm_live and not cfg['base_url']:
        raise ValueError('--confirm-live requires an explicit target')
    if args.command in ('account-test', 'gateway-test'):
        if args.mixed_burst:
            cfg['mixed_burst']['enabled'] = True
            cfg['stage_duration'] = 0
        if args.duration is not None:
            cfg['stage_duration'] = args.duration
        if args.max_requests is not None:
            cfg['max_stage_requests'] = args.max_requests
        if args.long_output:
            cfg['workload']['profile'] = 'long'
        if args.output_tokens is not None:
            cfg['workload']['output_tokens'] = args.output_tokens
        if args.output_limit_field is not None:
            cfg['workload']['limit_field'] = args.output_limit_field
        validate(cfg)
    output = args.output or DATA_ROOT / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    # Task IDs are hashed before storage so a caller cannot accidentally persist a secret as an ID.
    task_id = fingerprint(args.task_id)
    lab = Lab(cfg, output, stop=stop, confirm_live=args.confirm_live, checkpoint=args.checkpoint, task_id=task_id)
    summary = await lab.run(args.command)
    print(json.dumps({'status': summary['status'], 'mode': summary['mode'], 'result_count': summary['result_count']}, ensure_ascii=False))
    return 130 if summary['status'] == 'interrupted' else (0 if summary['status'] == 'completed' else 1)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(execute(args))
    except Exception:
        # Never echo raw exceptions, paths, target URLs, configuration values or response bodies.
        print('relay-lab: configuration, output or local Mock validation failed; review README constraints.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
