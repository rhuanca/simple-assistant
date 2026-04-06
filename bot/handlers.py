import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.agent import run
from bot.storage import allow_chat, is_chat_allowed

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

    if not is_chat_allowed(chat_id):
        if text.strip() == os.getenv("BOT_PASSWORD", ""):
            allow_chat(chat_id)
            await update.message.reply_text("✅ Authenticated! / ¡Autenticado!\n\n" + WELCOME, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Wrong password. / Contraseña incorrecta.")
        return

    user = update.effective_user.first_name or "Someone"
    reply = await run(text, user=user)
    await update.message.reply_text(reply)
