import tempfile
import unittest
from datetime import date
from pathlib import Path

from bot import alerts, localtime, storage

MINE = 1
THEIRS = 2


class AppointmentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()


class StorageTests(AppointmentTestCase):
    def test_add_and_list_upcoming_soonest_first(self):
        storage.add_appointment("dentista", "2026-08-20T09:00", MINE)
        storage.add_appointment("doctor", "2026-08-16T15:00", MINE)

        upcoming = storage.get_upcoming_appointments(MINE, "2026-08-09T00:00")
        self.assertEqual([a["title"] for a in upcoming], ["doctor", "dentista"])

    def test_past_appointments_are_not_upcoming(self):
        storage.add_appointment("ya pasó", "2026-08-01T10:00", MINE)
        storage.add_appointment("doctor", "2026-08-16T15:00", MINE)

        upcoming = storage.get_upcoming_appointments(MINE, "2026-08-09T00:00")
        self.assertEqual([a["title"] for a in upcoming], ["doctor"])

    def test_appointments_are_private_to_their_owner(self):
        storage.add_appointment("secreto", "2026-08-16T15:00", THEIRS)

        self.assertEqual(storage.get_upcoming_appointments(MINE, "2026-08-09T00:00"), [])
        self.assertEqual(len(storage.get_upcoming_appointments(THEIRS, "2026-08-09T00:00")), 1)

    def test_cannot_cancel_another_users_appointment(self):
        theirs = storage.add_appointment("secreto", "2026-08-16T15:00", THEIRS)

        self.assertIsNone(storage.cancel_appointment(theirs, MINE))
        self.assertEqual(len(storage.get_upcoming_appointments(THEIRS, "2026-08-09T00:00")), 1)

    def test_cancel_own_appointment(self):
        mine = storage.add_appointment("doctor", "2026-08-16T15:00", MINE)

        self.assertEqual(storage.cancel_appointment(mine, MINE), "doctor")
        self.assertEqual(storage.get_upcoming_appointments(MINE, "2026-08-09T00:00"), [])

    def test_find_by_title_is_case_insensitive_and_partial(self):
        storage.add_appointment("Doctor de gastroenterología", "2026-08-16T15:00", MINE)

        found = storage.find_upcoming_appointments("doctor", MINE, "2026-08-09T00:00")
        self.assertEqual(len(found), 1)
        self.assertEqual(storage.find_upcoming_appointments("dentista", MINE, "2026-08-09T00:00"), [])

    def test_find_by_title_ignores_other_users(self):
        storage.add_appointment("doctor", "2026-08-16T15:00", THEIRS)
        self.assertEqual(storage.find_upcoming_appointments("doctor", MINE, "2026-08-09T00:00"), [])

    def test_mark_reminded(self):
        appointment_id = storage.add_appointment("doctor", "2026-08-16T15:00", MINE)
        storage.mark_appointment_reminded(appointment_id, "same_day")

        [stored] = storage.get_upcoming_appointments(MINE, "2026-08-09T00:00")
        self.assertEqual(stored["reminded_same_day"], 1)
        self.assertEqual(stored["reminded_day_before"], 0)


class LocalTimeTests(AppointmentTestCase):
    def test_default_timezone(self):
        self.assertEqual(str(localtime.get_timezone()), "America/La_Paz")

    def test_unknown_timezone_falls_back(self):
        storage.set_setting("timezone", "Mars/Olympus_Mons")
        self.assertEqual(str(localtime.get_timezone()), localtime.FALLBACK_TIMEZONE)

    def test_parse_accepts_space_or_t_separator(self):
        self.assertEqual(
            localtime.parse_local("2026-08-16 15:00"),
            localtime.parse_local("2026-08-16T15:00"),
        )

    def test_display_format(self):
        self.assertEqual(localtime.format_local("2026-08-16T15:00"), "Sun 16 Aug 2026, 15:00")

    def test_to_storage_drops_seconds(self):
        self.assertEqual(
            localtime.to_storage(localtime.parse_local("2026-08-16T15:00:42")),
            "2026-08-16T15:00",
        )


class DueRemindersTests(AppointmentTestCase):
    TODAY = date(2026, 8, 16)

    def _appointment(self, starts_at, day_before=0, same_day=0):
        return {
            "id": 1,
            "title": "doctor",
            "starts_at": starts_at,
            "reminded_day_before": day_before,
            "reminded_same_day": same_day,
        }

    def test_today_is_due_as_same_day(self):
        due = alerts.due_reminders([self._appointment("2026-08-16T15:00")], self.TODAY)
        self.assertEqual([kind for _, kind in due], ["same_day"])

    def test_tomorrow_is_due_as_day_before(self):
        due = alerts.due_reminders([self._appointment("2026-08-17T15:00")], self.TODAY)
        self.assertEqual([kind for _, kind in due], ["day_before"])

    def test_further_out_is_not_due(self):
        due = alerts.due_reminders([self._appointment("2026-08-20T15:00")], self.TODAY)
        self.assertEqual(due, [])

    def test_past_is_not_due(self):
        due = alerts.due_reminders([self._appointment("2026-08-10T15:00")], self.TODAY)
        self.assertEqual(due, [])

    def test_already_reminded_is_not_repeated(self):
        same_day = self._appointment("2026-08-16T15:00", same_day=1)
        day_before = self._appointment("2026-08-17T15:00", day_before=1)
        self.assertEqual(alerts.due_reminders([same_day, day_before], self.TODAY), [])

    def test_day_before_reminder_does_not_suppress_the_same_day_one(self):
        """An appointment reminded yesterday must still be reminded on the day itself."""
        appointment = self._appointment("2026-08-16T15:00", day_before=1)
        due = alerts.due_reminders([appointment], self.TODAY)
        self.assertEqual([kind for _, kind in due], ["same_day"])


class ReminderMessageTests(AppointmentTestCase):
    def test_none_when_nothing_due(self):
        self.assertIsNone(alerts.format_appointment_reminder([]))

    def test_splits_today_and_tomorrow(self):
        due = [
            ({"title": "doctor", "starts_at": "2026-08-16T15:00"}, "same_day"),
            ({"title": "dentista", "starts_at": "2026-08-17T09:00"}, "day_before"),
        ]
        message = alerts.format_appointment_reminder(due)
        self.assertIn("Today / Hoy:", message)
        self.assertIn("• Sun 16 Aug 2026, 15:00 — doctor", message)
        self.assertIn("Tomorrow / Mañana:", message)
        self.assertIn("• Mon 17 Aug 2026, 09:00 — dentista", message)

    def test_omits_an_empty_section(self):
        due = [({"title": "doctor", "starts_at": "2026-08-16T15:00"}, "same_day")]
        self.assertNotIn("Tomorrow", alerts.format_appointment_reminder(due))


if __name__ == "__main__":
    unittest.main()
