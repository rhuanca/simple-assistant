import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.parser import parse
from bot.storage import add_item, allow_chat, clear_list, get_items, is_chat_allowed, remove_item

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
    result = await parse(text)

    if result.intent == "add":
        if not result.items:
            await update.message.reply_text("What should I add? / ¿Qué agrego?")
            return
        for item in result.items:
            add_item(item, list_name=result.list_name, added_by=user)
        items_str = ", ".join(result.items)
        await update.message.reply_text(f"✅ Added to {result.list_name}: {items_str}")

    elif result.intent == "remove":
        if not result.items:
            await update.message.reply_text("What should I remove? / ¿Qué quito?")
            return
        removed = []
        not_found = []
        for item in result.items:
            if remove_item(item, list_name=result.list_name):
                removed.append(item)
            else:
                not_found.append(item)
        parts = []
        if removed:
            parts.append(f"✅ Removed: {', '.join(removed)}")
        if not_found:
            parts.append(f"❓ Not found: {', '.join(not_found)}")
        await update.message.reply_text("\n".join(parts))

    elif result.intent == "show":
        items = get_items(result.list_name)
        if not items:
            await update.message.reply_text(f"📋 {result.list_name} is empty! / ¡La lista está vacía!")
            return
        lines = [f"📋 *{result.list_name}* ({len(items)} items):"]
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {item['item_text']}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif result.intent == "clear":
        count = clear_list(result.list_name)
        await update.message.reply_text(f"🗑️ Cleared {count} items from {result.list_name}.")

    else:
        await update.message.reply_text(
            "🤔 I didn't understand. Try 'add milk' or 'show list'.\n"
            "No entendí. Prueba 'comprar leche' o 'ver lista'."
        )
