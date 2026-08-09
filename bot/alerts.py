"""Scheduled reminders: appointments and the shopping list, in one daily message.

A daily JobQueue tick calls `run_alert_tick`. Appointment reminders are checked every day
(the day before, and the morning of); the shopping list is added only when its own interval
has elapsed. Both land in a single DM per user so nobody gets two notifications.

Keeping the "am I due?" decisions in the DB (rather than an in-memory timer) means a
Raspberry-Pi reboot never loses the schedule — the next daily tick simply re-evaluates.
"""

from datetime import date, datetime, timedelta, timezone

from telegram.ext import ContextTypes

from bot import localtime, storage
from bot.agent import format_lists_for


def alert_due(now_iso: str) -> bool:
    """Pure decision: should an alert fire at `now_iso`? Reads settings from storage."""
    if storage.get_setting("alert_enabled") != "true":
        return False
    last = storage.get_setting("last_alert_at")
    if not last:
        return True
    try:
        interval_days = int(storage.get_setting("alert_interval_days"))
    except ValueError:
        interval_days = 3
    now = datetime.fromisoformat(now_iso)
    elapsed = now - datetime.fromisoformat(last)
    return elapsed.total_seconds() >= interval_days * 86400


def due_reminders(appointments: list[dict], today: date) -> list[tuple[dict, str]]:
    """Pure decision: which appointments need a reminder today, and which kind. Takes the
    date as a parameter so it can be tested without freezing the clock, like alert_due."""
    due = []
    for appointment in appointments:
        starts_on = localtime.parse_local(appointment["starts_at"]).date()
        if starts_on == today and not appointment["reminded_same_day"]:
            due.append((appointment, "same_day"))
        elif starts_on == today + timedelta(days=1) and not appointment["reminded_day_before"]:
            due.append((appointment, "day_before"))
    return due


def format_appointment_reminder(due: list[tuple[dict, str]]) -> str | None:
    """Render today's appointment reminders. None when there is nothing to say."""
    if not due:
        return None
    today = [a for a, kind in due if kind == "same_day"]
    tomorrow = [a for a, kind in due if kind == "day_before"]
    blocks = ["📅 Appointment reminder / Recordatorio de citas"]
    for heading, appointments in (("Today / Hoy", today), ("Tomorrow / Mañana", tomorrow)):
        if not appointments:
            continue
        lines = [f"{heading}:"]
        for appointment in appointments:
            lines.append(
                f"• {localtime.format_local(appointment['starts_at'])} — {appointment['title']}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def run_alert_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback. Sends each user one message combining any appointment reminders
    due today with the shopping lists, the latter only when its interval has elapsed."""
    now = datetime.now(timezone.utc)
    today = localtime.now_local().date()
    groceries_due = alert_due(now.isoformat())

    for user in storage.get_all_users():
        user_id = user["telegram_user_id"]
        # Reminders look ahead to tomorrow, so upcoming must start from today, not "now".
        upcoming = storage.get_upcoming_appointments(user_id, today.isoformat())
        due = due_reminders(upcoming, today)

        sections = [section for section in (
            format_appointment_reminder(due),
            format_lists_for(user_id) if groceries_due else None,
        ) if section]
        if not sections:
            continue

        try:
            await context.bot.send_message(user["chat_id"], "\n\n".join(sections))
        except Exception as exc:  # one bad chat shouldn't stop the rest
            print(f"Failed to send alert to {user['chat_id']}: {exc}")
            continue  # not delivered, so leave the reminders unmarked to retry tomorrow

        for appointment, kind in due:
            storage.mark_appointment_reminded(appointment["id"], kind)

    if groceries_due:
        storage.set_setting("last_alert_at", now.isoformat())
