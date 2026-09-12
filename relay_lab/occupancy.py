"""Exact client occupancy integrals and a bounded sampled timeline, never upstream inference."""
import threading
import time


class Occupancy:
    def __init__(self, stage, target, duration=0, clock=time.perf_counter):
        self.clock = clock
        self.lock = threading.RLock()
        self.stage, self.target, self.duration = stage, target, duration
        self.start = self.last = clock()
        self.end = self.start + duration if duration else None
        self.active = self.receiving = self.peak = self.receiving_peak = 0
        self.area = self.receiving_area = self.full_seconds = 0.0
        self.reason = None
        self.finished = None
        self.interval = .1
        self.dynamic = False
        self.points = [{'seconds': 0.0, 'inflight': 0, 'receiving': 0}]

    def _accrue(self, now):
        until = min(now, self.end) if self.end is not None else now
        dt = max(0, until - self.last)
        self.area += dt * self.active
        self.receiving_area += dt * self.receiving
        self.full_seconds += dt if self.active >= self.target else 0
        self.last = max(self.last, until)

    def observe(self, active, receiving):
        with self.lock:
            now = self.clock()
            self._accrue(now)
            self.active, self.receiving = active, receiving
            self.peak = max(self.peak, active)
            self.receiving_peak = max(self.receiving_peak, receiving)

    def set_target(self, target):
        with self.lock:
            self._accrue(self.clock())
            self.dynamic = True
            self.target = target
            self.sample(force=True)

    def sample(self, force=False):
        with self.lock:
            now = self.clock()
            self._accrue(now)
            elapsed = now - self.start
            if force or elapsed - self.points[-1]['seconds'] >= self.interval:
                self.points.append({'seconds': elapsed, 'inflight': self.active, 'receiving': self.receiving})
                if self.dynamic:
                    self.points[-1]['target'] = self.target
                if len(self.points) > 1200:
                    self.points = self.points[:1] + self.points[2::2]
                    self.interval *= 2

    def close_load(self, reason):
        with self.lock:
            if self.reason is not None:
                return
            now = self.clock()
            self._accrue(now)
            self.end = min(now, self.end) if self.end is not None else now
            self.reason = reason

    def finish(self, reason):
        with self.lock:
            self.close_load(reason)
            self.sample(force=True)
            self.finished = self.clock()

    def snapshot(self):
        with self.lock:
            now = self.finished if self.finished is not None else self.clock()
            self._accrue(now)
            window = max(0, (min(now, self.end) if self.end is not None else now) - self.start)
            draining = self.end is not None and now >= self.end
            return {
                'stage': self.stage, 'scope': 'client_requests_only', 'target': self.target,
                'phase': 'completed' if self.finished is not None else 'draining' if draining else 'load',
                'current_inflight': self.active, 'current_receiving': self.receiving,
                'peak_inflight': self.peak, 'peak_receiving': self.receiving_peak,
                'mean_inflight': self.area / window if window else 0,
                'mean_receiving': self.receiving_area / window if window else 0,
                'target_occupancy_ratio': None if self.dynamic else self.full_seconds / window if window else 0,
                'dynamic_target': self.dynamic,
                'requested_duration_seconds': self.duration, 'load_seconds': window,
                'drain_seconds': max(0, now - self.end) if draining else 0,
                'stop_reason': self.reason or ('duration' if draining else None),
                'upstream_account_occupancy': None,
                'series': [dict(point) for point in self.points],
            }
