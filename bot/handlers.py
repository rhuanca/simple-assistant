import asyncio
import os
import traceback
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import ContextTypes

from bot import voice
from bot.agent import AgentError, clear_view_cache, run
from bot.alerts import alert_time, schedule_alert_job, send_test_reminder
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

AUTH_PROMPT = "🔒 Envía la contraseña para usar este bot."

WELCOME = (
    "🛒 *Grocery Bot*\n\n"
    "Llevo tus listas de compras y tus citas.\n\n"
    "Envía /help para ver todo lo que puedo hacer."
)

# Kept free of _ and [ ] so it survives Telegram's legacy Markdown unescaped.
HELP = (
    "🛒 *Listas*\n"
    '• "Agrega leche y huevos" / "Comprar jabón y papel"\n'
    '• "Muéstrame mi lista"\n'
    '• "Quita el jabón" / "Borra el 2"\n'
    '• "Borra todo"\n'
    'Tu lista es privada. Di "la lista común" o "la lista de la casa" para la compartida.\n\n'
    "📅 *Citas*\n"
    '• "Tengo cita el próximo domingo a las 3 con el doctor"\n'
    '• "¿Qué citas tengo?"\n'
    '• "Cancela la del doctor" / "Cancela la segunda"\n'
    "Las citas son personales. Si no me das la hora, pregunto antes de guardar nada.\n\n"
    "🎤 *Voz*\n"
    "Envíame una nota de voz y hago lo mismo. Te muestro lo que entendí.\n\n"
    "⏰ *Recordatorios*\n"
    "Un mensaje al día: las citas el día anterior y la mañana del día, más tus listas "
    "cada pocos días."
)

ADMIN_HELP = (
    "\n\n🔧 *Admin*\n"
    "/users — ver usuarios y roles\n"
    "/promote @usuario · /demote @usuario · /revoke @usuario\n"
    "/alert on | off | every N | test — recordatorio de compras\n"
    "/config — ver y cambiar la configuración\n"
    "/resetdb — reconstruir la base de datos"
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
    first_name = user_obj.first_name if user_obj and user_obj.first_name else "Alguien"

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
            await update.message.reply_text("✅ ¡Autenticado!\n\n" + WELCOME, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Contraseña incorrecta.")
        return

    await _run_and_reply(update, context, text)


_TYPING_REFRESH_SECONDS = 4


async def _with_typing(chat, coro, refresh: float = _TYPING_REFRESH_SECONDS):
    """Run `coro` keeping the typing indicator alive until it finishes. A single chat
    action fades after ~5 seconds, which is shorter than transcription plus the agent on
    a slow day — sent once, the dots die mid-wait and the chat looks dead."""
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            await chat.send_action("typing")
            await asyncio.wait({task}, timeout=refresh)
    finally:
        if not task.done():
            task.cancel()
    return await task


async def _run_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, prefix: str = ""
) -> None:
    """Send `text` through the agent and reply, never leaving the user without an answer.
    `prefix` is prepended to the reply (the voice handler uses it to echo the transcript)."""
    chat_id = update.effective_chat.id
    user_obj = update.effective_user
    user_id = user_obj.id if user_obj else 0
    first_name = user_obj.first_name if user_obj and user_obj.first_name else "Alguien"

    try:
        reply = await _with_typing(
            update.effective_chat, run(text, user=first_name, user_id=user_id)
        )
    except AgentError as exc:
        reply = exc.user_message
        await _notify_admins(context, f"⚠️ Error del bot de {first_name} (chat {chat_id}):\n{exc.admin_detail}")
    except Exception as exc:
        # Never leave the user without a reply.
        reply = "Algo salió mal de mi lado. Inténtalo de nuevo."
        await _notify_admins(context, f"⚠️ Error del bot de {first_name} (chat {chat_id}):\n{exc!r}")
    await update.message.reply_text(prefix + reply)

    # Optional voice reply (the `voice_replies` setting, off by default). Only the reply is
    # spoken, not the echoed transcript — the user just said that part themselves.
    if get_setting("voice_replies") == "true" and voice.is_configured():
        spoken = voice.speakable(reply)
        if spoken:
            await voice.send_voice_note(context.bot, chat_id, spoken)


VOICE_NOT_CONFIGURED = "🎤 Los mensajes de voz no están configurados en este bot."
VOICE_NOT_UNDERSTOOD = "🎤 No entendí nada — inténtalo de nuevo."
VOICE_FAILED = "🎤 No pude procesar ese mensaje de voz. Inténtalo de nuevo o escríbelo."


def format_transcript(transcript: str) -> str:
    """The heard-text echo shown above the reply, so a mis-transcription is obvious."""
    return f"🎤 «{transcript}»\n\n"


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_chat_allowed(update.effective_chat.id):
        # The password must be typed; a spoken one would depend on transcription luck.
        await update.message.reply_text(AUTH_PROMPT)
        return
    if not voice.is_configured():
        await update.message.reply_text(VOICE_NOT_CONFIGURED)
        return

    async def fetch_and_transcribe() -> str:
        file = await update.message.voice.get_file()
        audio = bytes(await file.download_as_bytearray())
        # The speech client is sync; a thread keeps transcription off the event loop.
        return await asyncio.to_thread(voice.transcribe, audio)

    try:
        text = await _with_typing(update.effective_chat, fetch_and_transcribe())
    except Exception as exc:
        await update.message.reply_text(VOICE_FAILED)
        user_obj = update.effective_user
        first_name = user_obj.first_name if user_obj and user_obj.first_name else "Alguien"
        await _notify_admins(
            context,
            f"⚠️ Error de transcripción de voz de {first_name} "
            f"(chat {update.effective_chat.id}):\n{exc!r}",
        )
        return

    if not text:
        await update.message.reply_text(VOICE_NOT_UNDERSTOOD)
        return
    await _run_and_reply(update, context, text, prefix=format_transcript(text))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB's global error handler. Known transient noise becomes one journal line instead
    of a 40-line traceback every few seconds (the Pi's journald is size-capped); anything
    unexpected still logs in full."""
    error = context.error
    if isinstance(error, Conflict):
        print(
            "Telegram Conflict: another instance is polling with this bot token — "
            "find and stop the duplicate (pgrep -af bot.main)."
        )
        return
    if isinstance(error, (TimedOut, NetworkError)):
        print(f"Telegram network hiccup (will retry): {error}")
        return
    print(f"Unhandled error: {error!r}")
    if error is not None:
        traceback.print_exception(type(error), error, error.__traceback__)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    for admin_chat_id in get_admin_chat_ids():
        try:
            await context.bot.send_message(admin_chat_id, message)
        except Exception as exc:
            print(f"Failed to notify admin {admin_chat_id}: {exc}")


# --- Admin commands ---------------------------------------------------------

ADMIN_ONLY = "🔒 Solo para administradores."


async def _guard_admin(update: Update) -> bool:
    """Return True if the sender may run admin commands, else reply and return False."""
    chat_id = update.effective_chat.id
    user_obj = update.effective_user
    user_id = user_obj.id if user_obj else 0
    if is_chat_allowed(chat_id) and is_admin(user_id):
        return True
    await update.message.reply_text(ADMIN_ONLY)
    return False


# Roles are stored in English ("admin"/"member"); translate only at display time.
ROLE_LABELS = {ADMIN_USER_ROLE: "administrador", DEFAULT_USER_ROLE: "miembro"}


def _user_label(user: dict) -> str:
    handle = f"@{user['username']}" if user.get("username") else "(sin usuario)"
    role = ROLE_LABELS.get(user["role"], user["role"])
    return f"{user.get('first_name') or 'Alguien'} {handle} — {role}"


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("Aún no hay usuarios.")
        return
    lines = ["👥 Usuarios:"] + [f"• {_user_label(u)}" for u in users]
    await update.message.reply_text("\n".join(lines))


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Resolve the target user from the command's first arg (@username). Replies on error."""
    if not context.args:
        command = update.message.text.split()[0]
        await update.message.reply_text(f"Uso: {command} @usuario")
        return None
    user = find_user_by_username(context.args[0])
    if user is None:
        await update.message.reply_text(f"Usuario {context.args[0]} no encontrado.")
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
    await update.message.reply_text(f"🚫 Acceso revocado para {_user_label(user)}.")


RESET_CONFIRM = "CONFIRMAR"

RESET_WARNING = (
    "⚠️ */resetdb* borra todas las listas, todos los usuarios y toda la configuración, y "
    "crea una base de datos vacía.\n\n"
    "La base de datos actual se guarda como copia de respaldo con fecha, pero el bot no "
    "volverá a leerla. Todos menos tú tendrán que enviar la contraseña de nuevo.\n\n"
    f"Envía `/resetdb {RESET_CONFIRM}` para continuar."
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
            "first_name": user_obj.first_name or "Alguien",
        }
    )
    clear_view_cache()

    kept = "Base de datos anterior guardada como " + backup.name if backup else "No había base de datos que respaldar"
    await update.message.reply_text(
        f"♻️ Base de datos recreada. Sigues siendo administrador.\n{kept}.\n{_reschedule(context)}"
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
        raise ValueError(f"Zona horaria desconocida '{name}'. Usa un nombre IANA como America/La_Paz.")
    return name


def _parse_hour(value: str) -> str:
    if not value.isdigit() or not 0 <= int(value) <= 23:
        raise ValueError("La hora debe ser un número entero de 0 a 23.")
    return str(int(value))


def _parse_days(value: str) -> str:
    if not value.isdigit() or int(value) < 1:
        raise ValueError("El intervalo debe ser un número entero de días, 1 o más.")
    return str(int(value))


_BOOLEANS = {
    "true": "true", "on": "true", "yes": "true", "si": "true", "1": "true",
    "false": "false", "off": "false", "no": "false", "0": "false",
}


def _parse_bool(value: str) -> str:
    try:
        return _BOOLEANS[value.strip().lower()]
    except KeyError:
        raise ValueError("Usa on u off.")


# key -> (parser, one-line help). Adding a setting is one entry here.
CONFIG_KEYS = {
    "timezone": (_parse_timezone, "zona IANA, p. ej. America/La_Paz"),
    "alert_hour": (_parse_hour, "0-23, hora local del recordatorio diario"),
    "alert_interval_days": (_parse_days, "días entre recordatorios de compras (1 o más)"),
    "alert_enabled": (_parse_bool, "on u off, para el recordatorio de compras"),
    "voice_replies": (_parse_bool, "on u off, responder también con una nota de voz"),
}

# Changing these two moves the daily job, so it has to be rescheduled to take effect.
RESCHEDULES = {"timezone", "alert_hour"}


def config_status() -> str:
    """Every setting with its value, and whether it is stored or still the built-in default.
    `last_alert_at` is shown as status and is deliberately not editable."""
    stored = get_all_settings()
    lines = ["⚙️ Configuración", ""]
    for key in CONFIG_KEYS:
        origin = "definido" if key in stored else "por defecto"
        # No column padding: Telegram renders this in a proportional font, so it would only
        # look aligned here and ragged on the phone.
        lines.append(f"• {key} = {get_setting(key)}  ({origin})")
    at = alert_time()
    lines += [
        "",
        f"Último recordatorio de compras: {get_setting('last_alert_at') or 'nunca'}",
        f"Recordatorio diario: {at.strftime('%H:%M')} {at.tzinfo}",
        "",
        "Cambiar:  /config <clave> <valor>",
        "  /config timezone America/Lima",
        "  /config alert_hour 8",
    ]
    return "\n".join(lines)


def config_usage(reason: str) -> str:
    lines = [f"⚠️ {reason}", "", "Uso: /config <clave> <valor>"]
    lines += [f"• {key} — {hint}" for key, (_, hint) in CONFIG_KEYS.items()]
    return "\n".join(lines)


def _reschedule(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Move the running daily job onto the new schedule, so no restart is needed."""
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:  # only reachable if the bot runs without the job-queue extra
        return "⚠️ Guardado, pero no pude reprogramar — reinicia el bot para aplicarlo."
    at = schedule_alert_job(job_queue)
    return f"⏰ Recordatorio diario ahora a las {at.strftime('%H:%M')} {at.tzinfo}."


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(config_status())
        return

    key = args[0].lower()
    if key not in CONFIG_KEYS:
        await update.message.reply_text(config_usage(f"Ajuste desconocido '{args[0]}'."))
        return
    if len(args) < 2:
        await update.message.reply_text(config_usage(f"/config {key} necesita un valor."))
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
    last = get_setting("last_alert_at") or "nunca"
    state = "ACTIVADA" if enabled else "DESACTIVADA"
    return (
        f"⏰ Alerta: {state}\n"
        f"Intervalo: cada {interval} día(s)\n"
        f"Último envío: {last}"
    )


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(_alert_status())
        return

    sub = args[0].lower()
    if sub == "test":
        user_obj = update.effective_user
        sent = await send_test_reminder(
            context, user_obj.id, update.effective_chat.id, user_obj.first_name or ""
        )
        if not sent:
            await update.message.reply_text(
                "Nada que recordar ahora mismo: sin citas para hoy o mañana y con las "
                "listas vacías."
            )
        return
    if sub == "on":
        set_setting("alert_enabled", "true")
    elif sub == "off":
        set_setting("alert_enabled", "false")
    elif sub == "every" and len(args) >= 2 and args[1].isdigit() and int(args[1]) > 0:
        set_setting("alert_interval_days", args[1])
    else:
        await update.message.reply_text("Uso: /alert [on|off|every <N>|test]")
        return
    await update.message.reply_text(_alert_status())
