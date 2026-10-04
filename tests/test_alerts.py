import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot import alerts, localtime, storage


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


class FakeBot:
    def __init__(self, fail_voice=False):
        self.messages = []
        self.voices = []
        self.fail_voice = fail_voice

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_voice(self, chat_id, audio):
        if self.fail_voice:
            raise RuntimeError("tts upload failed")
        self.voices.append((chat_id, audio))


class AlertTickVoiceTests(unittest.IsolatedAsyncioTestCase):
    """The daily reminder goes out as text, then best-effort as a voice note."""

    USER_ID = 1
    CHAT_ID = 100

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()

        storage.upsert_user(self.USER_ID, self.CHAT_ID, "user", "Renan")
        today = localtime.now_local().date()
        self.appointment_id = storage.add_appointment("doctor", f"{today}T23:59", self.USER_ID)

    async def _tick(self, bot, configured=True, synthesize=None):
        synthesize = synthesize or mock.Mock(return_value=b"mp3-bytes")
        with mock.patch.object(alerts.voice, "is_configured", return_value=configured):
            with mock.patch.object(alerts.voice, "synthesize", synthesize):
                await alerts.run_alert_tick(SimpleNamespace(bot=bot))
        return synthesize

    async def test_sends_text_then_voice(self):
        bot = FakeBot()
        synthesize = await self._tick(bot)

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.voices, [(self.CHAT_ID, b"mp3-bytes")])
        # The reminder greets the person by name.
        [(_, text)] = bot.messages
        self.assertTrue(text.startswith("👋 Hola, Renan\n\n"), text)
        # The spoken text is the same reminder, minus emoji.
        spoken = synthesize.call_args.args[0]
        self.assertTrue(spoken.startswith("Hola, Renan"), spoken)
        self.assertIn("Recordatorio de citas", spoken)
        self.assertNotIn("📅", spoken)

    async def test_greets_without_a_name_when_none_is_stored(self):
        storage.upsert_user(self.USER_ID, self.CHAT_ID, "user", "")
        bot = FakeBot()
        await self._tick(bot, configured=False)

        [(_, text)] = bot.messages
        self.assertTrue(text.startswith("👋 Hola\n\n"), text)

    async def test_without_a_speech_server_only_text_goes_out(self):
        bot = FakeBot()
        synthesize = await self._tick(bot, configured=False)

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.voices, [])
        synthesize.assert_not_called()

    async def test_a_voice_failure_never_loses_the_text_reminder(self):
        bot = FakeBot(fail_voice=True)
        await self._tick(bot)

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.voices, [])
        # The text was delivered, so the reminder stays marked — no re-send tomorrow.
        [stored] = storage.get_upcoming_appointments(self.USER_ID, "0000")
        self.assertEqual(stored["reminded_same_day"], 1)


if __name__ == "__main__":
    unittest.main()
