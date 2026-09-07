from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from features.stability.app import scheduler


def epoch(hour, minute=0, day=7):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


class ScheduleStartTests(unittest.TestCase):
    def schedule(self, created_at):
        return {"id": 1, "timezone": "Asia/Shanghai", "daily_times": "10:30,14:00,15:30,18:00",
                "created_at": created_at}

    def test_new_schedule_does_not_backfill_times_before_creation(self):
        with patch.object(scheduler.storage, "list_schedules", return_value=[self.schedule(epoch(16))]), \
                patch.object(scheduler.storage, "create_run") as create:
            self.assertEqual(scheduler.ensure_due_runs(epoch(16, 30)), 0)
            create.assert_not_called()

    def test_new_schedule_starts_at_next_due_time(self):
        schedule = self.schedule(epoch(16))
        with patch.object(scheduler.storage, "list_schedules", return_value=[schedule]), \
                patch.object(scheduler.storage, "create_run", return_value=1) as create:
            self.assertEqual(scheduler.ensure_due_runs(epoch(18, 1)), 1)
            create.assert_called_once_with(schedule, epoch(18))

    def test_existing_schedule_still_catches_up_after_downtime(self):
        with patch.object(scheduler.storage, "list_schedules", return_value=[self.schedule(epoch(9, day=6))]), \
                patch.object(scheduler.storage, "create_run", return_value=1) as create:
            self.assertEqual(scheduler.ensure_due_runs(epoch(16)), 3)
            self.assertEqual([call.args[1] for call in create.call_args_list], [epoch(10, 30), epoch(14), epoch(15, 30)])


if __name__ == "__main__":
    unittest.main()
