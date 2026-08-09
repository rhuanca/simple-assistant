"""Scheduled shopping-list reminder.

A daily JobQueue tick calls `run_alert_tick`, which fires only when the configured
interval has elapsed since the last alert. Keeping the "am I due?" decision in the DB
(rather than an in-memory timer) means a Raspberry-Pi reboot never loses the schedule —
the next daily tick simply re-evaluates.
"""

from datetime import datetime, timezone

from telegram.ext import ContextTypes

from bot import storage
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


async def run_alert_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback. If the interval has elapsed, DM each user their lists."""
    now = datetime.now(timezone.utc)
    if not alert_due(now.isoformat()):
        return

    for user in storage.get_all_users():
        message = format_lists_for(user["telegram_user_id"])
        if message is None:
            continue
        try:
            await context.bot.send_message(user["chat_id"], message)
        except Exception as exc:  # one bad chat shouldn't stop the rest
            print(f"Failed to send alert to {user['chat_id']}: {exc}")

    storage.set_setting("last_alert_at", now.isoformat())
