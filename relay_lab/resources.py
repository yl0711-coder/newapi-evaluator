import asyncio
import os
import resource
import sys
import time


def sample():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    try:
        fd = len(os.listdir('/proc/self/fd' if sys.platform.startswith('linux') else '/dev/fd'))
    except OSError:
        fd = None
    return {'cpu_seconds': usage.ru_utime + usage.ru_stime,
            'peak_rss_bytes': usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'file_descriptors': fd}


class Monitor:
    def __init__(self, adapter, embedded):
        self.adapter = adapter
        self.embedded = embedded
        self.samples = []
        self.running = False

    async def run(self):
        self.running = True
        while self.running:
            self.samples.append({**sample(), 'active_connections': self.adapter.active, 'time': time.perf_counter()})
            if self.adapter.occupancy:
                self.adapter.occupancy.sample()
            await asyncio.sleep(.02)

    def summary(self):
        if not self.samples:
            self.samples = [{**sample(), 'active_connections': 0, 'time': time.perf_counter()}]
        first, last = self.samples[0], self.samples[-1]
        cpu = max(0, last['cpu_seconds'] - first['cpu_seconds']) / max(last['time'] - first['time'], 1e-9) * 100
        return {'scope': 'client_and_embedded_mock_process' if self.embedded else 'client_process_only',
                'cpu_percent_one_core': cpu, 'peak_rss_bytes': max(s['peak_rss_bytes'] for s in self.samples),
                'peak_file_descriptors': max((s['file_descriptors'] for s in self.samples if s['file_descriptors'] is not None), default=None),
                'connection_pool_limit': self.adapter.connection_limit,
                'peak_active_connections': self.adapter.peak_active,
                'sample_count': len(self.samples), 'remote_process_metrics_available': False}
