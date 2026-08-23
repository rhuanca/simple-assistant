import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.agent import AgentError, clear_view_cache, run
from bot.alerts import alert_time, schedule_alert_job
from bot.storage import (
    ADMIN_USER_ROLE,
    DEFAULT_USER_ROLE,
    allow_chat,
    recreate_db,
    find_user_by_username,
    get_admin_chat_ids,
    get_all_settings,
    get_all_users,
    get_setting,
    has_any_users,
    is_admin,
    is_chat_allowed,
    promote_to_admin,
    revoke_user,
    set_role,
    set_setting,
    upsert_user,
)

AUTH_PROMPT = "🔒 Send the password to use this bot.\n🔒 Envía la contraseña para usar este bot."

WELCOME = (
    "🛒 *Grocery Bot*\n\n"
    "I keep your shopping lists and your appointments.\n"
    "Llevo tus listas de compras y tus citas.\n\n"
    "Send /help to see everything I can do.\n"
    "Envía /help para ver todo lo que puedo hacer."
)

# Kept free of _ and [ ] so it survives Telegram's legacy Markdown unescaped.
HELP = (
    "🛒 *Lists / Listas*\n"
    '• "Add milk and eggs" / "Comprar jabón y papel"\n'
    '• "Show my list" / "Muéstrame mi lista"\n'
    '• "Remove milk" / "Quita el jabón" / "Borra el 2"\n'
    '• "Clear the list" / "Borra todo"\n'
    'Your list is private. Say "the common list" / "la lista de la casa" for the shared one.\n\n'
    "📅 *Appointments / Citas*\n"
    '• "I have an appointment next Sunday at 3 with the doctor"\n'
    '• "Tengo cita el próximo domingo a las 3 con el doctor"\n'
    '• "What appointments do I have?" / "¿Qué citas tengo?"\n'
    '• "Cancel the doctor one" / "Cancela la segunda"\n'
    "Appointments are personal. If you don't give a time, I ask before saving anything.\n\n"
    "⏰ *Reminders / Recordatorios*\n"
    "One message a day: appointments the day before and the morning of, plus your lists "
    "every few days."
)

ADMIN_HELP = (
    "\n\n🔧 *Admin*\n"
    "/users — list users and roles\n"
    "/promote @user · /demote @user · /revoke @user\n"
    "/alert on | off | every N — shopping digest\n"
    "/config — view and change settings\n"
    "/resetdb — rebuild the database"
)


def build_help(for_admin: bool) -> str:
    """The admin section is appended only for admins, so members are not shown commands the
    guard would refuse anyway."""
    return HELP + (ADMIN_HELP if for_admin else "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_chat_allowed(chat_id):
        await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(AUTH_PROMPT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_chat_allowed(update.effective_chat.id):
        await update.message.reply_text(AUTH_PROMPT)
        return
    user_obj = update.effective_user
    await update.message.reply_text(
        build_help(is_admin(user_obj.id if user_obj else 0)), parse_mode=ParseMode.MARKDOWN
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    user_obj = update.effective_user
    user_id = user_obj.id if user_obj else 0
    username = user_obj.username if user_obj and user_obj.username else ""
    first_name = user_obj.first_name if user_obj and user_obj.first_name else "Someone"

    if not is_chat_allowed(chat_id):
        if text.strip() == os.getenv("BOT_PASSWORD", ""):
            is_first_user = not has_any_users()
            allow_chat(chat_id)
            upsert_user(
                telegram_user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
            )
            if is_first_user:
                promote_to_admin(user_id)
            await update.message.reply_text("✅ Authenticated! / ¡Autenticado!\n\n" + WELCOME, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Wrong password. / Contraseña incorrecta.")
        return

    user = first_name
    await update.effective_chat.send_action("typing")
    try:
        reply = await run(text, user=user, user_id=user_id)
    except AgentError as exc:
        reply = exc.user_message
        await _notify_admins(context, f"⚠️ Bot error from {first_name} (chat {chat_id}):\n{exc.admin_detail}")
    except Exception as exc:
        # Never leave the user without a reply.
        reply = "Something went wrong on my side. Please try again. / Algo salió mal, inténtalo de nuevo."
        await _notify_admins(context, f"⚠️ Bot error from {first_name} (chat {chat_id}):\n{exc!r}")
    await update.message.reply_text(reply)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    for admin_chat_id in get_admin_chat_ids():
        try:
            await context.bot.send_message(admin_chat_id, message)
        except Exception as exc:
            print(f"Failed to notify admin {admin_chat_id}: {exc}")


# --- Admin commands ---------------------------------------------------------

ADMIN_ONLY = "🔒 Admin only. / Solo para administradores."


async def _guard_admin(update: Update) -> bool:
    """Return True if the sender may run admin commands, else reply and return False."""
    chat_id = update.effective_chat.id
    user_obj = update.effective_user
    user_id = user_obj.id if user_obj else 0
    if is_chat_allowed(chat_id) and is_admin(user_id):
        return True
    await update.message.reply_text(ADMIN_ONLY)
    return False


def _user_label(user: dict) -> str:
    handle = f"@{user['username']}" if user.get("username") else "(no username)"
    return f"{user.get('first_name') or 'Someone'} {handle} — {user['role']}"


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users yet. / Aún no hay usuarios.")
        return
    lines = ["👥 Users / Usuarios:"] + [f"• {_user_label(u)}" for u in users]
    await update.message.reply_text("\n".join(lines))


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Resolve the target user from the command's first arg (@username). Replies on error."""
    if not context.args:
        await update.message.reply_text("Usage: /promote @username")
        return None
    user = find_user_by_username(context.args[0])
    if user is None:
        await update.message.reply_text(
            f"User {context.args[0]} not found. / Usuario no encontrado."
        )
    return user


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    user = await _resolve_target(update, context)
    if user is None:
        return
    set_role(user["telegram_user_id"], ADMIN_USER_ROLE)
    await update.message.reply_text(f"✅ {_user_label({**user, 'role': ADMIN_USER_ROLE})}")


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    user = await _resolve_target(update, context)
    if user is None:
        return
    set_role(user["telegram_user_id"], DEFAULT_USER_ROLE)
    await update.message.reply_text(f"✅ {_user_label({**user, 'role': DEFAULT_USER_ROLE})}")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    user = await _resolve_target(update, context)
    if user is None:
        return
    revoke_user(user["telegram_user_id"])
    await update.message.reply_text(
        f"🚫 Access revoked for {_user_label(user)}. / Acceso revocado."
    )


RESET_CONFIRM = "CONFIRM"

RESET_WARNING = (
    "⚠️ */resetdb* deletes every list, every user and all settings, and builds an empty "
    "database.\n\n"
    "The current database is kept as a timestamped backup file next to it, but the bot will "
    "not read it again. Everyone except you will have to send the password again.\n\n"
    f"Send `/resetdb {RESET_CONFIRM}` to go ahead."
)


async def resetdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return

    if not context.args or context.args[0].upper() != RESET_CONFIRM:
        await update.message.reply_text(RESET_WARNING, parse_mode=ParseMode.MARKDOWN)
        return

    user_obj = update.effective_user
    backup = recreate_db(
        keep_admin={
            "telegram_user_id": user_obj.id,
            "chat_id": update.effective_chat.id,
            "username": user_obj.username or "",
            "first_name": user_obj.first_name or "Someone",
        }
    )
    clear_view_cache()

    kept = "Previous database saved as " + backup.name if backup else "There was no database to back up"
    await update.message.reply_text(
        f"♻️ Database recreated. You are still an admin.\n{kept}.\n{_reschedule(context)}"
    )


# --- Configuration ----------------------------------------------------------


def _parse_timezone(value: str) -> str:
    """Stricter than localtime.get_timezone(), which swallows a bad zone and falls back so a
    typo can never take the bot down. Rejecting on the way in means that fallback is never
    reached in the first place."""
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone '{name}'. Use an IANA name like America/La_Paz.")
    return name


def _parse_hour(value: str) -> str:
    if not value.isdigit() or not 0 <= int(value) <= 23:
        raise ValueError("The hour must be a whole number from 0 to 23.")
    return str(int(value))


def _parse_days(value: str) -> str:
    if not value.isdigit() or int(value) < 1:
        raise ValueError("The interval must be a whole number of days, 1 or more.")
    return str(int(value))


_BOOLEANS = {
    "true": "true", "on": "true", "yes": "true", "si": "true", "1": "true",
    "false": "false", "off": "false", "no": "false", "0": "false",
}


def _parse_bool(value: str) -> str:
    try:
        return _BOOLEANS[value.strip().lower()]
    except KeyError:
        raise ValueError("Use on or off.")


# key -> (parser, one-line help). Adding a setting is one entry here.
CONFIG_KEYS = {
    "timezone": (_parse_timezone, "IANA zone, e.g. America/La_Paz"),
    "alert_hour": (_parse_hour, "0-23, local time of the daily reminder"),
    "alert_interval_days": (_parse_days, "days between shopping digests (1 or more)"),
    "alert_enabled": (_parse_bool, "on or off, for the shopping digest"),
}

# Changing these two moves the daily job, so it has to be rescheduled to take effect.
RESCHEDULES = {"timezone", "alert_hour"}


def config_status() -> str:
    """Every setting with its value, and whether it is stored or still the built-in default.
    `last_alert_at` is shown as status and is deliberately not editable."""
    stored = get_all_settings()
    lines = ["⚙️ Settings / Configuración", ""]
    for key in CONFIG_KEYS:
        origin = "set" if key in stored else "default"
        # No column padding: Telegram renders this in a proportional font, so it would only
        # look aligned here and ragged on the phone.
        lines.append(f"• {key} = {get_setting(key)}  ({origin})")
    at = alert_time()
    lines += [
        "",
        f"Last digest: {get_setting('last_alert_at') or 'never'}",
        f"Daily reminder: {at.strftime('%H:%M')} {at.tzinfo}",
        "",
        "Change / Cambiar:  /config <key> <value>",
        "  /config timezone America/Lima",
        "  /config alert_hour 8",
    ]
    return "\n".join(lines)


def config_usage(reason: str) -> str:
    lines = [f"⚠️ {reason}", "", "Usage: /config <key> <value>"]
    lines += [f"• {key} — {hint}" for key, (_, hint) in CONFIG_KEYS.items()]
    return "\n".join(lines)


def _reschedule(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Move the running daily job onto the new schedule, so no restart is needed."""
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:  # only reachable if the bot runs without the job-queue extra
        return "⚠️ Saved, but I could not reschedule — restart the bot to apply it."
    at = schedule_alert_job(job_queue)
    return f"⏰ Daily reminder now at {at.strftime('%H:%M')} {at.tzinfo}."


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(config_status())
        return

    key = args[0].lower()
    if key not in CONFIG_KEYS:
        await update.message.reply_text(config_usage(f"Unknown setting '{args[0]}'."))
        return
    if len(args) < 2:
        await update.message.reply_text(config_usage(f"/config {key} needs a value."))
        return

    parse, _hint = CONFIG_KEYS[key]
    try:
        value = parse(" ".join(args[1:]))
    except ValueError as exc:
        await update.message.reply_text(config_usage(str(exc)))
        return

    set_setting(key, value)
    reply = f"✅ {key} = {value}"
    if key in RESCHEDULES:
        reply += "\n" + _reschedule(context)
    await update.message.reply_text(reply)


def _alert_status() -> str:
    enabled = get_setting("alert_enabled") == "true"
    interval = get_setting("alert_interval_days")
    last = get_setting("last_alert_at") or "never"
    state = "ON" if enabled else "OFF"
    return (
        f"⏰ Alert: {state}\n"
        f"Interval: every {interval} day(s)\n"
        f"Last sent: {last}"
    )


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(_alert_status())
        return

    sub = args[0].lower()
    if sub == "on":
        set_setting("alert_enabled", "true")
    elif sub == "off":
        set_setting("alert_enabled", "false")
    elif sub == "every" and len(args) >= 2 and args[1].isdigit() and int(args[1]) > 0:
        set_setting("alert_interval_days", args[1])
    else:
        await update.message.reply_text("Usage: /alert [on|off|every <N> days]")
        return
    await update.message.reply_text(_alert_status())
