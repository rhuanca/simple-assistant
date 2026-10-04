"""Local wall-clock time for appointments.

Everything else in the bot works in UTC, but an appointment is a wall-clock event: "3pm at
the clinic" means 3pm where the user is. The zone comes from the `timezone` setting so it
can be changed without a code edit, and appointment times are stored as naive local ISO
strings (see the v4 migration in storage.py).
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot import storage

FALLBACK_TIMEZONE = "America/La_Paz"
# Spanish names mapped by hand: strftime's %a/%b follow the process locale, which on the
# Pi is C/POSIX (English), and generating an es_* locale there is a deploy step we can
# skip entirely. Display: "dom 16 ago 2026, 15:00" — read back by the model, so keep it
# unambiguous.
DAY_NAMES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]  # datetime.weekday(), 0=Mon
MONTH_NAMES = ["ene", "feb", "mar", "abr", "may", "jun",
               "jul", "ago", "sep", "oct", "nov", "dic"]
# What the model is asked to produce, and what goes in the database.
STORAGE_FORMAT = "%Y-%m-%dT%H:%M"


def get_timezone() -> ZoneInfo:
    name = storage.get_setting("timezone") or FALLBACK_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A typo in the setting shouldn't take the bot down.
        print(f"Unknown timezone {name!r}, falling back to {FALLBACK_TIMEZONE}")
        return ZoneInfo(FALLBACK_TIMEZONE)


def now_local() -> datetime:
    """Current wall-clock time in the configured zone, naive so it compares directly with
    the naive `starts_at` values in the database."""
    return datetime.now(get_timezone()).replace(tzinfo=None)


def parse_local(value: str) -> datetime:
    """Parse a local datetime the model produced. Accepts 'YYYY-MM-DDTHH:MM' and the same
    with a space, which the model tends to emit interchangeably."""
    return datetime.fromisoformat(value.strip().replace(" ", "T"))


def to_storage(moment: datetime) -> str:
    return moment.strftime(STORAGE_FORMAT)


def format_local(value: str | datetime) -> str:
    moment = parse_local(value) if isinstance(value, str) else value
    day = DAY_NAMES[moment.weekday()]
    month = MONTH_NAMES[moment.month - 1]
    return f"{day} {moment.day:02d} {month} {moment.year}, {moment:%H:%M}"
