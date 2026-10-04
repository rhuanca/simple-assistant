"""Scheduled reminders: appointments and the shopping list, in one daily message.

A daily JobQueue tick calls `run_alert_tick`. Appointment reminders are checked every day
(the day before, and the morning of); the shopping list is added only when its own interval
has elapsed. Both land in a single DM per user so nobody gets two notifications.

Keeping the "am I due?" decisions in the DB (rather than an in-memory timer) means a
Raspberry-Pi reboot never loses the schedule — the next daily tick simply re-evaluates.
"""

from datetime import date, datetime, time, timedelta, timezone

from telegram.ext import ContextTypes

from bot import localtime, storage, voice
from bot.agent import format_lists_for

# The job is named so it can be found and replaced when the schedule settings change.
ALERT_JOB_NAME = "daily_alert"
DEFAULT_ALERT_HOUR = 9


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
    blocks = ["📅 Recordatorio de citas"]
    for heading, appointments in (("Hoy", today), ("Mañana", tomorrow)):
        if not appointments:
            continue
        lines = [f"{heading}:"]
        for appointment in appointments:
            lines.append(
                f"• {localtime.format_local(appointment['starts_at'])} — {appointment['title']}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- Spoken script ----------------------------------------------------------
# The voice note gets its own rendition of the reminder: the written layout (row numbers,
# item counts, full dates, "15:00") is exactly what sounds robotic read aloud.


def _join_spoken(parts: list[str]) -> str:
    """Join items the way a sentence would: "leche, pan y huevos"."""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " y " + parts[-1]


def spoken_appointment_reminder(due: list[tuple[dict, str]]) -> str | None:
    """The appointment reminder said aloud: just "hoy"/"mañana", the title, and the time
    in words — the date itself is redundant when you already said "hoy"."""
    if not due:
        return None
    phrases = []
    for day_word, appointments in (
        ("Hoy", [a for a, kind in due if kind == "same_day"]),
        ("Mañana", [a for a, kind in due if kind == "day_before"]),
    ):
        if not appointments:
            continue
        noun = "una cita" if len(appointments) == 1 else "citas"
        told = _join_spoken(
            [f"{a['title']}, {localtime.format_spoken(a['starts_at'])}" for a in appointments]
        )
        phrases.append(f"{day_word} tienes {noun}: {told}.")
    return " ".join(phrases)


def spoken_shopping_lists(user_id: int) -> str | None:
    """The shopping lists said aloud: items joined naturally, no numbers, no counts, and
    empty lists simply not mentioned. None when there is nothing to say."""
    personal = [item["item_text"] for item in storage.get_items(user_id)]
    common = [item["item_text"] for item in storage.get_items(None)]
    phrases = []
    if personal:
        phrases.append(f"En tu lista de compras tienes: {_join_spoken(personal)}.")
    if common:
        phrases.append(f"En la lista común hay: {_join_spoken(common)}.")
    return " ".join(phrases) or None


def compose_reminder(user_id: int, name: str, due: list[tuple[dict, str]],
                     include_lists: bool) -> tuple[str, str, bool] | None:
    """One user's reminder: (text message, spoken script, whether a lists section made it
    in). None when there is nothing to say to this user."""
    lists_section = format_lists_for(user_id) if include_lists else None
    sections = [s for s in (format_appointment_reminder(due), lists_section) if s]
    if not sections:
        return None
    text = "\n\n".join([f"👋 Hola, {name}" if name else "👋 Hola", *sections])
    spoken = " ".join(
        [f"Hola, {name}." if name else "Hola."]
        + [s for s in (
            spoken_appointment_reminder(due),
            spoken_shopping_lists(user_id) if include_lists else None,
        ) if s]
    )
    return text, spoken, lists_section is not None


async def run_alert_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback. Sends each user one message combining any appointment reminders
    due today with the shopping lists, the latter only when its interval has elapsed."""
    now = datetime.now(timezone.utc)
    today = localtime.now_local().date()
    groceries_due = alert_due(now.isoformat())
    sent = 0
    digest_delivered = False

    for user in storage.get_all_users():
        user_id = user["telegram_user_id"]
        # Reminders look ahead to tomorrow, so upcoming must start from today, not "now".
        upcoming = storage.get_upcoming_appointments(user_id, today.isoformat())
        due = due_reminders(upcoming, today)

        composed = compose_reminder(user_id, user["first_name"], due, groceries_due)
        if composed is None:
            continue
        text, spoken, has_lists = composed

        try:
            await context.bot.send_message(user["chat_id"], text)
        except Exception as exc:  # one bad chat shouldn't stop the rest
            print(f"Failed to send alert to {user['chat_id']}: {exc}")
            continue  # not delivered, so leave the reminders unmarked to retry tomorrow

        sent += 1
        if has_lists:
            digest_delivered = True
        for appointment, kind in due:
            storage.mark_appointment_reminded(appointment["id"], kind)

        if voice.is_configured():
            await voice.send_voice_note(context.bot, user["chat_id"], spoken)

    # Only a digest that actually reached someone resets the interval clock; otherwise the
    # next tick (e.g. right after items appear on a list) is free to send one.
    if digest_delivered:
        storage.set_setting("last_alert_at", now.isoformat())
    # A tick with nothing to say used to be indistinguishable from a tick that never ran.
    print(
        f"Alert tick at {now.isoformat()}: sent {sent} reminder(s), "
        f"groceries_due={groceries_due}, digest_delivered={digest_delivered}"
    )


async def send_test_reminder(context, user_id: int, chat_id: int, name: str) -> bool:
    """/alert test: send this admin their reminder as it stands right now, ignoring the
    digest interval and the already-reminded flags, and marking nothing — so testing today
    never swallows tomorrow's real reminder. False when there is nothing to show."""
    today = localtime.now_local().date()
    upcoming = storage.get_upcoming_appointments(user_id, today.isoformat())
    fresh = [dict(a, reminded_day_before=0, reminded_same_day=0) for a in upcoming]
    composed = compose_reminder(user_id, name, due_reminders(fresh, today), include_lists=True)
    if composed is None:
        return False
    text, spoken, _ = composed
    await context.bot.send_message(chat_id, text)
    if voice.is_configured():
        await voice.send_voice_note(context.bot, chat_id, spoken)
    return True


def alert_time() -> time:
    """The daily tick's local time, from settings. A bad `alert_hour` falls back rather than
    stopping the bot from starting."""
    try:
        hour = int(storage.get_setting("alert_hour"))
    except ValueError:
        hour = DEFAULT_ALERT_HOUR
    if not 0 <= hour <= 23:
        hour = DEFAULT_ALERT_HOUR
    return time(hour=hour, tzinfo=localtime.get_timezone())


def schedule_alert_job(job_queue) -> time:
    """(Re)schedule the daily tick from the current settings, replacing any existing one.

    Called at startup and again whenever `alert_hour` or `timezone` changes, so a new schedule
    takes effect without a restart. Returns the time it was scheduled at, for the reply."""
    for job in job_queue.get_jobs_by_name(ALERT_JOB_NAME):
        job.schedule_removal()
    at = alert_time()
    job_queue.run_daily(run_alert_tick, time=at, name=ALERT_JOB_NAME)
    return at
