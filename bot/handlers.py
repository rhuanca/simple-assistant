import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.agent import AgentError, run
from bot.storage import (
    allow_chat,
    get_admin_chat_ids,
    has_any_users,
    is_chat_allowed,
    promote_to_admin,
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

    upsert_user(
        telegram_user_id=user_id,
        chat_id=chat_id,
        username=username,
        first_name=first_name,
    )

    user = first_name
    await update.effective_chat.send_action("typing")
    try:
        reply = await run(text, user=user)
    except AgentError as exc:
        reply = exc.user_message
        await _notify_admins(context, f"⚠️ Bot error from {first_name} (chat {chat_id}):\n{exc.admin_detail}")
    await update.message.reply_text(reply)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    for admin_chat_id in get_admin_chat_ids():
        try:
            await context.bot.send_message(admin_chat_id, message)
        except Exception as exc:
            print(f"Failed to notify admin {admin_chat_id}: {exc}")
