import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid

from .config import DATA_ROOT, data_path, load
from .mock import MockServer
from .report import rebuild
from .runner import Lab
from .security import fingerprint


def parser():
    p = argparse.ArgumentParser(prog='relay-lab', description='中转站稳定性与极限测试；默认只允许本地 Mock')
    sub = p.add_subparsers(dest='command', required=True)
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
