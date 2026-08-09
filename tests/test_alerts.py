import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import alerts, storage


class AlertDueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()
        self.now = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)

    def _set_last(self, days_ago):
        stamp = (self.now - timedelta(days=days_ago)).isoformat()
        storage.set_setting("last_alert_at", stamp)

    def test_disabled_never_due(self):
        storage.set_setting("alert_enabled", "false")
        self._set_last(10)
        self.assertFalse(alerts.alert_due(self.now.isoformat()))

    def test_never_sent_is_due(self):
        # last_alert_at defaults to "" -> due immediately.
        self.assertTrue(alerts.alert_due(self.now.isoformat()))

    def test_before_interval_not_due(self):
        self._set_last(2)  # interval default is 3 days
        self.assertFalse(alerts.alert_due(self.now.isoformat()))

    def test_exactly_interval_is_due(self):
        self._set_last(3)
        self.assertTrue(alerts.alert_due(self.now.isoformat()))

    def test_overdue_is_due(self):
        self._set_last(5)
        self.assertTrue(alerts.alert_due(self.now.isoformat()))

    def test_custom_interval_respected(self):
        storage.set_setting("alert_interval_days", "7")
        self._set_last(5)
        self.assertFalse(alerts.alert_due(self.now.isoformat()))
        self._set_last(7)
        self.assertTrue(alerts.alert_due(self.now.isoformat()))


if __name__ == "__main__":
    unittest.main()
