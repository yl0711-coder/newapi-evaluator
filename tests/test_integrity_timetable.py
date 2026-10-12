"""Scheduled behavioral sampling, durable retries and historical compatibility."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from shared import registry as registry_module
from shared.registry import Registry
from features.integrity.conclusions import conclusion
from features.integrity.strategies import get_strategy
from features.stability.app import storage, timetable, timetable_executor as executor, integrity
from features.stability.app.main import ScheduleInput, app
from features.stability.app.timetable_report import report, method_result
from features.stability.app.timetable_contract import upgrade_config


class TimetableContractTests(unittest.TestCase):
    def plan(self, config=None, zone='Asia/Shanghai', channels=(1,)):
        return {'id': 7, 'timezone': zone, 'layered_config': {**timetable.validate_config(config or {}), 'registry_channel_ids': list(channels)}}

    def test_full_day_two_methods_and_retry_bound(self):
        for day in (date(2026, 10, 8), date(2026, 10, 10)):
            plan = self.plan(channels=range(1, 6))
            rows = timetable.build_slots(plan, day)
            self.assertEqual(len(rows), 480)
            self.assertEqual({r['clock'][3:] for r in rows}, {'00', '30'})
            self.assertEqual({r['method'] for r in rows}, {'modeltrace', 'nerfed'})
            self.assertTrue(all(r['dependency'] is None for r in rows))
            p = timetable.preview(plan['layered_config'], plan['timezone'], day)
            self.assertEqual((p['initial_requests'], p['max_requests']), (1440, 5760))
            self.assertEqual(p['max_retries'], 3)

    def test_synchronized_empty_invalid_and_dst(self):
        self.assertEqual(timetable.build_slots(self.plan({'modeltrace_times': [], 'nerfed_times': []}), date(2026, 10, 8)), [])
        for value in (['9:20'], ['09:21'], ['24:00'], ['09:20', '09:20'], '09:20', [True]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                timetable.validate_config({'modeltrace_times': value, 'nerfed_times': value})
        with self.assertRaises(ValueError):
            timetable.validate_config({'modeltrace_times': [], 'nerfed_times': ['09:20']})
        plan = self.plan({'modeltrace_times': ['01:30', '02:30', '23:50'], 'nerfed_times': ['01:30', '02:30', '23:50']}, 'America/New_York')
        rows = timetable.build_slots(plan, date(2026, 3, 8))
        self.assertEqual(len(rows), 4)
        self.assertNotIn('02:30', [r['clock'] for r in rows])
        fold = timetable.build_slots(plan, date(2026, 11, 1))
        self.assertEqual(next(r['due'] for r in fold if r['clock'] == '01:30'), 1793511000)
        self.assertTrue(all(r['deadline'] - r['due'] == 600 for r in fold))

    def test_legacy_sampling_upgrade_keeps_only_completely_empty_plans_empty(self):
        for mt, canary in (([], []), ([], ['10:00']), (['10:00'], []), (['10:00'], ['10:00'])):
            with self.subTest(mt=mt, canary=canary):
                old = {'modeltrace_times': mt, 'canary_times': canary, 'baseline_ids': {'1': 7}}
                upgraded = upgrade_config(old, 'layered-integrity-v2')
                expected = [] if not mt and not canary else timetable.DEFAULTS['modeltrace_times']
                self.assertEqual(upgraded['modeltrace_times'], expected)
                self.assertEqual(upgraded['nerfed_times'], expected)
                self.assertEqual(upgraded['legacy_plan']['config'], old)
                self.assertNotIn('baseline_ids', upgraded)
        self.assertEqual(upgrade_config({}, 'layered-integrity-v1')['modeltrace_times'], timetable.DEFAULTS['modeltrace_times'])


class TimetableExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sampling-synthetic-')
        self.old_registry, self.old_db = registry_module._registry, storage.DB_PATH
        storage.close()
        self.registry = Registry(Path(self.temp.name))
        registry_module._registry = self.registry
        storage.DB_PATH = Path(self.temp.name) / 'stability.db'
        self.channel = self.registry.save({'name': 'Synthetic sampling', 'base_url': 'https://synthetic.example/v1', 'api_key': 'synthetic-sampling-credential', 'multiplier': 2.5, 'status': 'online'})
        self.targets = [{'registry_channel_id': self.channel['id'], 'model': 'gpt-6-astra', 'protocol': 'responses'}]
        self.day = date(2026, 10, 8)
        self.start = timetable.local_epoch(self.day, '00:00', ZoneInfo('Asia/Shanghai'))
        self.now = self.start + 36000
        self.data = ScheduleInput(name='Synthetic daily', daily_times='00:00', timezone='Asia/Shanghai', targets=self.targets,
            layered_config={'modeltrace_times': ['10:00', '10:10'], 'nerfed_times': ['10:00', '10:10']}).model_dump()
        with patch('time.time', return_value=self.start):
            self.id = storage.save_schedule_targets(self.data, self.targets)
        self.plan = storage.get_schedule(self.id)
        self.run = timetable.reconcile(self.plan, self.day, now=self.start)
        self.sent = []

    async def asyncTearDown(self):
        await integrity.stop()
        storage.close()
        storage.DB_PATH, registry_module._registry = self.old_db, self.old_registry
        self.temp.cleanup()

    def row(self, method='modeltrace', clock='10:00'):
        return next(r for r in timetable.slots(self.run) if r['method'] == method and r['clock'] == clock)

    async def send(self, channel, probe, *, before_send):
        await before_send()
        self.sent.append(probe['id'])
        return {'status': 'completed', 'text': ' '.join(['42'] * 332), 'input_tokens_reported': 4390, 'output_tokens_reported': 2050}

    async def finish(self, method='modeltrace', sender=None):
        for _ in range(60):
            with patch('time.time', return_value=self.now):
                row = executor.select_ready(self.now)
                if row is None:
                    return self.row(method)
                await executor.execute_probe(row, sender=sender or self.send)
            self.now += .001
        self.fail('fixed request graph did not settle')

    async def test_two_independent_methods_logical_denominators_and_export(self):
        for method in timetable.METHODS:
            row = await self.finish(method)
            self.assertEqual(row['status'], 'completed')
            self.assertEqual((row['summary']['planned'], row['summary']['attempted'], row['summary']['valid']), (3, 3, 3))
            self.assertEqual(row['summary']['physical_attempts'], 3)
        self.assertEqual(len(self.sent), 6)
        self.assertEqual(len(set(self.sent)), 6)
        with patch('time.time', return_value=self.now):
            table = report(run_id=self.run)
        self.assertNotIn('canary', table['rows'][0])
        self.assertFalse(table['rows'][0]['conclusion']['identity_authenticated'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            detail = (await client.get(f'/api/runs/{self.run}')).json()
            exported = (await client.get(f'/api/runs/{self.run}/export')).json()
        self.assertEqual(detail, exported)
        self.assertNotIn('synthetic-sampling-credential', json.dumps(exported))

    async def test_first_failure_three_retries_then_stop(self):
        async def disconnected(channel, probe, *, before_send):
            await before_send(); self.sent.append(probe['id'])
            return {'status': 'network_error'}
        row = await self.finish(sender=disconnected)
        self.assertEqual((row['summary']['attempted'], row['summary']['physical_attempts'], row['summary']['request_errors']), (3, 12, 3))
        self.assertEqual(row['summary']['not_run'], 0)
        await self.finish(sender=disconnected)
        self.assertEqual(len(self.sent), 24)
        with patch('time.time', return_value=self.now + 601):
            self.assertIn('无法判定', report(run_id=self.run)['rows'][0]['status'])

    async def test_retry_success_stops_and_final_score_ignores_early_disconnect(self):
        counts = {}
        async def recovered(channel, probe, *, before_send):
            await before_send(); self.sent.append(probe['id'])
            counts[probe['id']] = counts.get(probe['id'], 0) + 1
            return {'status': 'timeout'} if counts[probe['id']] == 1 else {'status': 'completed', 'text': ' '.join(['42'] * 332)}
        row = await self.finish(sender=recovered)
        self.assertEqual((row['summary']['valid'], row['summary']['physical_attempts'], row['summary']['request_errors']), (3, 6, 0))
        self.assertEqual(row['summary']['score']['valid_answers'], 3)
        self.assertEqual(row['status'], 'completed')

    async def test_http_error_does_not_retry_and_preflight_connection_failure_does(self):
        async def http_error(channel, probe, *, before_send):
            await before_send(); self.sent.append(probe['id'])
            return {'status': 'rate_limited'}
        row = await self.finish(sender=http_error)
        self.assertEqual(row['summary']['physical_attempts'], 3)
        async def dns_failure(channel, probe, *, before_send):
            self.sent.append(probe['id'])
            return {'status': 'network_error'}
        self.now = self.start + 36600
        row = await self.finish('nerfed', sender=dns_failure)
        row = self.row('nerfed', '10:10')
        self.assertEqual(row['summary']['physical_attempts'], 12)

    async def test_unknown_cancel_resume_never_retries_uncertain_send(self):
        async def interrupted(channel, probe, *, before_send):
            await before_send(); self.sent.append(probe['id']); raise asyncio.CancelledError()
        with patch('time.time', return_value=self.now), self.assertRaises(asyncio.CancelledError):
            await executor.execute_probe(self.row(), sender=interrupted)
        original = self.row()
        with patch('time.time', return_value=self.now):
            executor.resume_run(self.run)
        row = await self.finish()
        self.assertEqual(row['summary']['unknown'], 1)
        self.assertEqual(row['summary']['physical_attempts'], 3)
        self.assertEqual(self.sent.count(get_strategy('modeltrace').probes[0].probe_id), 1)
        self.assertEqual(self.row()['job_id'], original['job_id'])

    async def test_save_pause_delete_rechecked_at_final_permit(self):
        async def race(channel, probe, *, before_send):
            with patch('time.time', return_value=self.now + 1):
                storage.save_schedule_targets({**self.data, 'id': self.id, 'enabled': False}, self.targets)
            await before_send(); self.fail('paused plan sent')
        await self.finish(sender=race)
        self.assertEqual(self.sent, [])
        storage.delete_schedule(self.id)
        timetable.synchronize_controls()
        self.assertEqual(storage.get_run(self.run)['status'], 'incomplete')

    async def test_restart_reconstructs_terminal_without_resend(self):
        await self.finish()
        original = self.row()
        timetable.update_slot(original['slot_key'], 'pending', summary={})
        with patch('time.time', return_value=self.now + 601):
            executor.settle_pending(self.now + 601)
        self.assertEqual(self.row()['summary'], original['summary'])
        self.assertEqual(len(self.sent), 6)
        self.assertEqual(storage.pending_runs(100), [])

    async def test_edit_future_reopens_past_never_catches_up(self):
        empty = {'modeltrace_times': [], 'nerfed_times': []}
        with patch('time.time', return_value=self.now + 1):
            storage.save_schedule_targets({**self.data, 'id': self.id, 'layered_config': empty}, self.targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now + 1)
        with patch('time.time', return_value=self.now + 2):
            storage.save_schedule_targets({**self.data, 'id': self.id}, self.targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now + 2)
        self.assertEqual(self.row(clock='10:10')['status'], 'pending')
        self.assertEqual(self.row()['status'], 'cancelled')

    async def test_midnight_existing_plan_keeps_current_window(self):
        tomorrow = date(2026, 10, 9)
        schedule = {**self.plan, 'layered_config': {**self.plan['layered_config'], 'modeltrace_times': ['00:00'], 'nerfed_times': ['00:00']}}
        with storage.cursor() as cur:
            cur.execute('UPDATE schedules SET updated_at=updated_at+1,layered_config_json=? WHERE id=?', (storage.dumps(schedule['layered_config']), self.id))
        timetable.synchronize_controls()
        midnight = timetable.local_epoch(tomorrow, '00:00', ZoneInfo(schedule['timezone']))
        run_id = timetable.reconcile(storage.get_schedule(self.id), tomorrow, now=midnight + 5)
        self.assertTrue(all(s['status'] == 'pending' for s in timetable.slots(run_id)))

    async def test_retention_archives_v3_before_deleting_parent_preserves_attempts(self):
        await self.finish()
        job = executor.store().job(self.row()['job_id'])
        with patch('time.time', return_value=self.now):
            storage.finish_run(self.run, 'completed', {})
            storage.update_notification(self.run, 'disabled')
        self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 1)
        self.assertEqual(report(run_id=self.run)['total'], 0)
        self.assertIsNone(timetable.reconcile(self.plan, self.day, now=self.now + 6 * 86400))
        self.assertEqual(executor.store().job(job['job_id'])['consumed'], job['consumed'])

    async def test_v182_schema_migration_and_frozen_history_export(self):
        # Synthetic schema copied from v1.8.2; no Git history or business database is needed at runtime.
        with self.registry.connect() as conn:
            conn.executescript('''
                CREATE TABLE integrity_timetable_controls (
                  schedule_id INTEGER PRIMARY KEY, revision REAL NOT NULL, enabled INTEGER NOT NULL,
                  timezone TEXT NOT NULL, config_json TEXT NOT NULL);
                CREATE TABLE integrity_timetable_days (
                  schedule_id INTEGER NOT NULL, budget_date TEXT NOT NULL, run_id INTEGER NOT NULL UNIQUE,
                  cancelled INTEGER NOT NULL DEFAULT 0, limits_json TEXT NOT NULL DEFAULT '{}',
                  budget_timezone TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(schedule_id,budget_date));
                CREATE TABLE integrity_occurrences (
                  slot_key TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
                  budget_date TEXT NOT NULL, clock TEXT NOT NULL, registry_channel_id INTEGER NOT NULL,
                  method TEXT NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL, dependency TEXT,
                  priority INTEGER NOT NULL, revision REAL NOT NULL, snapshot_json TEXT NOT NULL,
                  job_id TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                  summary_json TEXT NOT NULL DEFAULT '{}', last_probe REAL NOT NULL DEFAULT 0,
                  updated_at REAL NOT NULL);
            ''')
        config = {**self.plan['layered_config'], 'modeltrace_times': [], 'canary_times': ['10:00'],
                  'baseline_ids': {str(self.channel['id']): 7}}
        config.pop('nerfed_times')
        old = {**self.plan, 'enabled': False, 'plan_version': 'layered-integrity-v2', 'layered_config': config}
        run_id = storage.create_run(old, self.start, source='timetable-v2')
        snapshot = storage.get_run(run_id)['snapshot']
        summary = {'planned': 192, 'attempted': 192, 'valid': 192, 'not_run': 0, 'unknown': 0,
                   'request_errors': 0, 'baseline_id': 7,
                   'score': {'status': 'degraded', 'correct': 150, 'outcomes': {str(i): i < 150 for i in range(192)}}}
        with self.registry.connect() as conn:
            conn.execute('INSERT INTO integrity_timetable_days(schedule_id,budget_date,run_id,budget_timezone) VALUES(?,?,?,?)',
                         (self.id, str(self.day), run_id, old['timezone']))
            conn.execute('''INSERT INTO integrity_occurrences(slot_key,schedule_id,run_id,budget_date,clock,
                registry_channel_id,method,due,deadline,priority,revision,snapshot_json,status,summary_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (f'timetable:{self.id}:{int(self.now)}:{self.channel["id"]}:canary', self.id, run_id,
                 str(self.day), '10:00', self.channel['id'], 'canary', self.now, self.now + 3600, 1,
                 self.start, storage.dumps(snapshot), 'completed', storage.dumps(summary), self.now))
        storage.finish_run(run_id, 'completed', {})
        with storage.cursor() as cur:
            cur.execute('UPDATE schedules SET enabled=0,plan_version=?,layered_config_json=? WHERE id=?',
                        ('layered-integrity-v2', storage.dumps(config), self.id))
        storage.close()
        with patch('time.time', return_value=self.now + 5):
            storage.init()
        migrated = storage.get_schedule(self.id)
        self.assertEqual(migrated['plan_version'], timetable.VERSION)
        self.assertFalse(migrated['enabled'])
        self.assertEqual(migrated['layered_config']['modeltrace_times'], timetable.DEFAULTS['modeltrace_times'])
        self.assertEqual(migrated['layered_config']['nerfed_times'], timetable.DEFAULTS['nerfed_times'])
        self.assertEqual(migrated['layered_config']['target_bindings'], config['target_bindings'])
        self.assertEqual(migrated['layered_config']['legacy_plan']['config'], config)
        storage.close()
        with patch('time.time', return_value=self.now + 50):
            storage.init()
        self.assertEqual(storage.get_schedule(self.id), migrated)
        self.registry.save({**self.registry.get(self.channel['id']), 'name': 'Changed current name',
                            'multiplier': 9.0, 'api_key': ''}, self.channel['id'], self.registry.get(self.channel['id'])['version'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            detail = await client.get(f'/api/runs/{run_id}')
            export = await client.get(f'/api/runs/{run_id}/export')
        self.assertEqual((detail.status_code, export.status_code), (200, 200))
        self.assertEqual(detail.json(), export.json())
        self.assertEqual(detail.json()['snapshot'], snapshot)
        row = detail.json()['table_report']['rows'][0]
        self.assertEqual((row['canary']['current_score'], row['canary']['baseline_id']), ('150/192', 7))
        self.assertEqual((row['channel_name'], row['channel_multiplier'], row['timezone']),
                         ('Synthetic sampling', 2.5, old['timezone']))
        self.assertEqual(self.sent, [])

    async def test_reselects_current_window_after_both_shared_guards_are_acquired(self):
        from features.integrity.transport import send_capacity
        waiting, entered = asyncio.Event(), asyncio.Event()
        clock = [self.now]
        @asynccontextmanager
        async def capacity():
            waiting.set()
            async with send_capacity():
                yield
        async def interrupted(channel, probe, *, before_send):
            await before_send()
            self.sent.append(probe['id'])
            entered.set()
            raise asyncio.CancelledError()
        with patch('time.time', side_effect=lambda: clock[0]), patch.object(executor, 'send_capacity', capacity), \
                patch.object(executor, 'send_probe_with_capacity', interrupted):
            async with send_capacity():
                task = asyncio.create_task(executor.dispatch())
                await asyncio.wait_for(waiting.wait(), 2)
                self.assertTrue(all(r['job_id'] is None for r in timetable.slots(self.run)))
                clock[0] += 601
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.gather(task, return_exceptions=True)
            executor.settle_pending(clock[0])
        self.assertTrue(all(r['status'] == 'expired' and r['job_id'] is None
                            for r in timetable.slots(self.run) if r['clock'] == '10:00'))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sum(r['summary'].get('unknown', 0) for r in timetable.slots(self.run)), 1)

    async def test_channels_and_methods_rotate_at_probe_boundaries(self):
        second = self.registry.save({'name': 'Synthetic fairness', 'base_url': 'https://fairness.synthetic.example/v1',
            'api_key': 'synthetic-fairness-credential', 'multiplier': 1, 'status': 'online'})
        targets = self.targets + [{'registry_channel_id': second['id'], 'model': 'gpt-6-astra', 'protocol': 'responses'}]
        with patch('time.time', return_value=self.now - 1):
            storage.save_schedule_targets({**self.data, 'id': self.id, 'targets': targets}, targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now - 1)
        order = []
        for _ in range(8):
            with patch('time.time', return_value=self.now):
                row = executor.select_ready(self.now)
                order.append((row['registry_channel_id'], row['method']))
                await executor.execute_probe(row, sender=self.send)
            self.now += .001
        expected = {(channel, method) for channel in (self.channel['id'], second['id']) for method in timetable.METHODS}
        self.assertEqual(set(order[:4]), expected)
        self.assertEqual(order[4:], order[:4])
        self.assertEqual(len(self.sent), 8)

    async def test_orphan_parent_and_missing_control_recover_from_committed_source(self):
        tomorrow = date(2026, 10, 9)
        midnight = timetable.local_epoch(tomorrow, '00:00', ZoneInfo(self.plan['timezone']))
        orphan = storage.create_run(self.plan, midnight, source='timetable-v3')
        self.assertEqual(timetable.reconcile(self.plan, tomorrow, now=midnight), orphan)
        self.assertEqual(len(timetable.slots(orphan)), 4)
        with storage.cursor() as cur:
            cur.execute('UPDATE schedules SET updated_at=updated_at+1 WHERE id=?', (self.id,))
        with self.registry.connect() as conn:
            conn.execute('DELETE FROM integrity_sampling_controls WHERE schedule_id=?', (self.id,))
        fresh = storage.get_schedule(self.id)
        self.assertEqual(timetable.reconcile(fresh, self.day, now=self.start), self.run)
        with self.registry.connect() as conn:
            control = conn.execute('SELECT * FROM integrity_sampling_controls WHERE schedule_id=?', (self.id,)).fetchone()
        self.assertEqual(control['revision'], fresh['updated_at'])
        self.assertEqual(json.loads(control['config_json']), fresh['layered_config'])

    async def test_timezone_edit_rejects_old_utc_slot_and_preserves_budget_timezone(self):
        with patch('time.time', return_value=self.now):
            await executor.execute_probe(executor.select_ready(self.now), sender=self.send)
        first = self.row()
        with patch('time.time', return_value=self.now + .001):
            storage.save_schedule_targets({**self.data, 'id': self.id, 'timezone': 'UTC'}, self.targets)
        timetable.reconcile(storage.get_schedule(self.id), self.day, now=self.now + .001)
        with patch('time.time', return_value=self.now):
            await executor.execute_probe(first, sender=self.send)
        self.assertEqual(len(self.sent), 1)
        future = next(r for r in timetable.slots(self.run) if r['status'] == 'pending')
        with patch('time.time', return_value=future['due']):
            await executor.execute_probe(executor.select_ready(future['due']), sender=self.send)
        budget = executor.store().daily_budget(budget_key=timetable.budget_key(self.id), budget_date=str(self.day), timezone='Asia/Shanghai')
        self.assertEqual(budget['attempted_requests'], 2)
        self.assertTrue(all(executor.store().job(r['job_id'])['timezone'] == 'Asia/Shanghai'
                            for r in timetable.slots(self.run) if r['job_id']))

    async def test_archive_commit_interruption_remains_recoverable(self):
        await self.finish()
        with patch('time.time', return_value=self.now):
            storage.finish_run(self.run, 'completed', {})
            storage.update_notification(self.run, 'disabled')
        original_cursor = storage.cursor
        @contextmanager
        def interrupted_cursor():
            with original_cursor() as cur:
                class InterruptedDelete:
                    def execute(self, sql, parameters=()):
                        if sql.startswith('DELETE FROM runs'):
                            raise RuntimeError('synthetic interruption after archive commit')
                        return cur.execute(sql, parameters)
                yield InterruptedDelete()
        with patch.object(storage, 'cursor', interrupted_cursor), self.assertRaisesRegex(RuntimeError, 'after archive commit'):
            storage.prune_run_history(self.now + 6 * 86400, 5)
        self.assertIsNotNone(storage.get_run(self.run))
        self.assertEqual(report(run_id=self.run)['total'], 0)
        self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 1)
        self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 0)

    async def test_retention_keeps_active_and_unknown_days(self):
        async def interrupted(channel, probe, *, before_send):
            await before_send()
            raise asyncio.CancelledError()
        with patch('time.time', return_value=self.now), self.assertRaises(asyncio.CancelledError):
            await executor.execute_probe(executor.select_ready(self.now), sender=interrupted)
        job = executor.store().job(self.row()['job_id'])
        self.assertEqual(job['consumed']['unknown_requests'], 1)
        for status in ('running', 'incomplete'):
            with storage.cursor() as cur:
                cur.execute("UPDATE runs SET status=?,created_at=?,finished_at=?,notify_status='disabled' WHERE id=?",
                            (status, self.now, self.now, self.run))
            self.assertEqual(storage.prune_run_history(self.now + 6 * 86400, 5), 0)
            self.assertIsNotNone(storage.get_run(self.run))
            self.assertTrue(report(run_id=self.run)['rows'])
            self.assertEqual(executor.store().job(job['job_id'])['consumed'], job['consumed'])


class ConclusionTests(unittest.TestCase):
    def methods(self, models=('gpt-6-astra', 'gpt-6-astra')):
        conditions = {'provider': 'registry:1:synthetic', 'model': 'gpt-6-astra', 'protocol': 'responses',
                      'parameters': {'reasoning_effort': 'low', 'stream': False, 'retry_policy': 'connection-retries-v1'}}
        return {method: {'status': 'completed', 'valid': 3, 'unknown': 0, 'not_run': 0, 'request_errors': 0,
                'score': {'status': 'scored', 'prediction': model, 'conditions': conditions,
                          'results': [{'model': model, 'probability': .9, 'score': 2}, {'model': 'other', 'probability': .1, 'score': 1}]}}
                for method, model in zip(timetable.METHODS, models)}

    def test_supported_agreement_conflict_and_unknown(self):
        self.assertEqual(conclusion(self.methods())['label'], '更接近 Astra')
        self.assertEqual(conclusion(self.methods(('gpt-6-luna',) * 2))['label'], '更接近 gpt-6-luna')
        self.assertIn('无法判定', conclusion(self.methods(('gpt-6-astra', 'gpt-6-luna')))['label'])
        for key, value in (('unknown', 1), ('not_run', 1), ('request_errors', 1), ('valid', 1)):
            data = self.methods(); data['nerfed'][key] = value
            self.assertIn('无法判定', conclusion(data)['label'])
        for key, value in (('probability', .7), ('score', 1.2)):
            data = self.methods(); data['nerfed']['score']['results'][0][key] = value
            self.assertIn('无法判定', conclusion(data)['label'])
        data = self.methods(); data['nerfed']['score']['conditions'] = {**data['nerfed']['score']['conditions'], 'provider': 'changed'}
        self.assertIn('无法判定', conclusion(data)['label'])

    def test_legacy_canary_report_retains_baseline_and_errors(self):
        slot = {'method': 'canary', 'status': 'completed', 'reason': '', 'slot_key': 'timetable:synthetic',
                'summary': {'attempted': 192, 'not_run': 0, 'unknown': 0, 'request_errors': 0, 'baseline_id': 1,
                            'score': {'status': 'degraded', 'correct': 150, 'outcomes': {str(i): True for i in range(192)}}}}
        self.assertEqual(method_result(slot)['state'], 'degraded')
        slot['summary']['request_errors'] = 1
        self.assertEqual(method_result(slot)['state'], 'review')
