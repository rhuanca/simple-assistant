import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.agent import AgentError, clear_view_cache, run
from bot.storage import (
    ADMIN_USER_ROLE,
    DEFAULT_USER_ROLE,
    allow_chat,
    recreate_db,
    find_user_by_username,
    get_admin_chat_ids,
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
    "Send me messages to manage your lists!\n"
    "Envíame mensajes para manejar tus listas!\n\n"
    "Try / Prueba:\n"
    '• "Add milk and eggs"\n'
    '• "Comprar jabón y papel"\n'
    '• "Show my list"\n'
    '• "Muéstrame la lista"\n'
    '• "Remove milk"\n'
    '• "Quita el jabón"\n'
    '• "Clear the list"\n'
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_chat_allowed(chat_id):
        await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(AUTH_PROMPT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_chat_allowed(update.effective_chat.id):
        await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(AUTH_PROMPT)


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
        f"♻️ Database recreated. You are still an admin.\n{kept}."
    )


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
