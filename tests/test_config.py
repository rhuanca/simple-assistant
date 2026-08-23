import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import alerts, handlers, storage

ADMIN_ID = 1
ADMIN_CHAT = 100
MEMBER_ID = 2
MEMBER_CHAT = 200


class StubJob:
    def __init__(self, name, at, queue):
        self.name = name
        self.at = at
        self._queue = queue

    def schedule_removal(self):
        self._queue.jobs.remove(self)


class StubJobQueue:
    """Stands in for telegram's JobQueue: enough surface for schedule_alert_job, and it keeps
    every run_daily call so a test can tell "replaced" from "scheduled twice"."""

    def __init__(self):
        self.jobs = []
        self.scheduled = []

    def get_jobs_by_name(self, name):
        return [job for job in self.jobs if job.name == name]

    def run_daily(self, callback, time, name):
        job = StubJob(name, time, self)
        self.jobs.append(job)
        self.scheduled.append(job)
        return job


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id, chat_id):
        self.message = FakeMessage()
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=user_id, username="user", first_name="Someone")

    @property
    def reply(self):
        return self.message.replies[-1]


class FakeContext:
    def __init__(self, args=None, job_queue=None):
        self.args = args or []
        self.job_queue = job_queue


class ConfigTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()

        storage.allow_chat(ADMIN_CHAT)
        storage.upsert_user(ADMIN_ID, ADMIN_CHAT, "admin", "Admin")
        storage.promote_to_admin(ADMIN_ID)
        storage.allow_chat(MEMBER_CHAT)
        storage.upsert_user(MEMBER_ID, MEMBER_CHAT, "member", "Member")


class ParserTests(ConfigTestCase):
    def test_timezone_accepts_an_iana_zone(self):
        self.assertEqual(handlers._parse_timezone(" America/Lima "), "America/Lima")

    def test_timezone_rejects_a_typo(self):
        """localtime.get_timezone() would silently fall back; /config must not let it get
        that far."""
        with self.assertRaises(ValueError):
            handlers._parse_timezone("Mars/Olympus_Mons")

    def test_hour_range(self):
        self.assertEqual(handlers._parse_hour("0"), "0")
        self.assertEqual(handlers._parse_hour("23"), "23")
        for bad in ("24", "-1", "9am", ""):
            with self.assertRaises(ValueError, msg=bad):
                handlers._parse_hour(bad)

    def test_days_must_be_positive(self):
        self.assertEqual(handlers._parse_days("5"), "5")
        for bad in ("0", "-3", "many"):
            with self.assertRaises(ValueError, msg=bad):
                handlers._parse_days(bad)

    def test_bool_accepts_both_languages(self):
        for value in ("on", "TRUE", "yes", "si", "1"):
            self.assertEqual(handlers._parse_bool(value), "true")
        for value in ("off", "false", "no", "0"):
            self.assertEqual(handlers._parse_bool(value), "false")
        with self.assertRaises(ValueError):
            handlers._parse_bool("maybe")


class ConfigStatusTests(ConfigTestCase):
    def test_marks_defaults_and_stored_values(self):
        storage.set_setting("alert_interval_days", "5")
        status = handlers.config_status()

        self.assertIn("alert_interval_days", status)
        self.assertIn("• alert_interval_days = 5  (set)", status)
        self.assertIn("• timezone = America/La_Paz  (default)", status)

    def test_shows_the_resolved_reminder_time_and_last_digest(self):
        status = handlers.config_status()
        self.assertIn("Daily reminder: 09:00 America/La_Paz", status)
        self.assertIn("Last digest: never", status)

    def test_usage_lists_every_editable_key(self):
        usage = handlers.config_usage("nope")
        for key in handlers.CONFIG_KEYS:
            self.assertIn(key, usage)
        self.assertNotIn("last_alert_at", usage)


class ScheduleTests(ConfigTestCase):
    def test_schedules_at_the_configured_local_hour(self):
        storage.set_setting("alert_hour", "8")
        queue = StubJobQueue()

        at = alerts.schedule_alert_job(queue)
        self.assertEqual(at.hour, 8)
        self.assertEqual(str(at.tzinfo), "America/La_Paz")
        self.assertEqual(len(queue.jobs), 1)

    def test_rescheduling_replaces_rather_than_duplicates(self):
        queue = StubJobQueue()
        alerts.schedule_alert_job(queue)
        storage.set_setting("alert_hour", "21")
        alerts.schedule_alert_job(queue)

        self.assertEqual(len(queue.scheduled), 2)
        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(queue.jobs[0].at.hour, 21)

    def test_a_bad_hour_falls_back_instead_of_crashing(self):
        for bad in ("nine", "99"):
            storage.set_setting("alert_hour", bad)
            self.assertEqual(alerts.alert_time().hour, alerts.DEFAULT_ALERT_HOUR)


class ConfigCommandTests(ConfigTestCase):
    async def _run(self, user_id, chat_id, args, queue=None):
        update = FakeUpdate(user_id, chat_id)
        await handlers.config_command(update, FakeContext(args, queue))
        return update.reply

    async def test_members_cannot_read_or_change_settings(self):
        reply = await self._run(MEMBER_ID, MEMBER_CHAT, ["alert_hour", "3"])
        self.assertEqual(reply, handlers.ADMIN_ONLY)
        self.assertEqual(storage.get_all_settings(), {})

    async def test_admin_with_no_args_sees_the_settings(self):
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, [])
        self.assertIn("⚙️ Settings", reply)

    async def test_setting_the_hour_saves_and_reschedules(self):
        queue = StubJobQueue()
        alerts.schedule_alert_job(queue)

        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["alert_hour", "8"], queue)

        self.assertIn("alert_hour = 8", reply)
        self.assertIn("08:00 America/La_Paz", reply)
        self.assertEqual(storage.get_setting("alert_hour"), "8")
        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(queue.jobs[0].at.hour, 8)

    async def test_setting_the_timezone_reschedules_too(self):
        queue = StubJobQueue()
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["timezone", "America/Lima"], queue)

        self.assertIn("America/Lima", reply)
        self.assertEqual(str(queue.jobs[0].at.tzinfo), "America/Lima")

    async def test_interval_does_not_touch_the_schedule(self):
        queue = StubJobQueue()
        await self._run(ADMIN_ID, ADMIN_CHAT, ["alert_interval_days", "5"], queue)

        self.assertEqual(storage.get_setting("alert_interval_days"), "5")
        self.assertEqual(queue.scheduled, [])

    async def test_a_rejected_value_changes_nothing(self):
        queue = StubJobQueue()
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["timezone", "Nowhere/Nope"], queue)

        self.assertIn("Unknown timezone", reply)
        self.assertEqual(storage.get_all_settings(), {})
        self.assertEqual(queue.scheduled, [])

    async def test_unknown_key_shows_the_usage(self):
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["bot_password", "hunter2"])
        self.assertIn("Unknown setting", reply)
        self.assertEqual(storage.get_all_settings(), {})

    async def test_last_alert_at_is_not_editable(self):
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["last_alert_at", "1999-01-01"])
        self.assertIn("Unknown setting", reply)

    async def test_a_key_without_a_value_shows_the_usage(self):
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["alert_hour"])
        self.assertIn("needs a value", reply)

    async def test_saving_without_a_job_queue_says_restart(self):
        reply = await self._run(ADMIN_ID, ADMIN_CHAT, ["alert_hour", "7"], None)
        self.assertIn("restart", reply.lower())
        self.assertEqual(storage.get_setting("alert_hour"), "7")


class HelpTests(ConfigTestCase):
    async def _help(self, user_id, chat_id):
        update = FakeUpdate(user_id, chat_id)
        await handlers.help_command(update, FakeContext())
        return update.reply

    async def test_members_do_not_see_admin_commands(self):
        reply = await self._help(MEMBER_ID, MEMBER_CHAT)
        self.assertIn("Appointments", reply)
        self.assertNotIn("/config", reply)
        self.assertNotIn("/resetdb", reply)

    async def test_admins_see_the_admin_section(self):
        reply = await self._help(ADMIN_ID, ADMIN_CHAT)
        self.assertIn("/config", reply)
        self.assertIn("/resetdb", reply)

    async def test_unauthenticated_chats_get_the_password_prompt(self):
        update = FakeUpdate(999, 999)
        await handlers.help_command(update, FakeContext())
        self.assertEqual(update.reply, handlers.AUTH_PROMPT)

    def test_help_covers_all_three_domains(self):
        text = handlers.build_help(for_admin=False)
        for topic in ("Lists", "Appointments", "Reminders"):
            self.assertIn(topic, text)


if __name__ == "__main__":
    unittest.main()
