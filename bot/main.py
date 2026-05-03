import os

from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot.handlers import handle_message, help_command, start
from bot.storage import init_db


def _mask_secret(value: str) -> str:
    if len(value) < 8:
        return "(missing or too short)"
    return f"{value[:4]}...{value[-4:]}"


def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: set TELEGRAM_BOT_TOKEN in your .env file")
        print("Get one from @BotFather on Telegram")
        return

    init_db()
    # print(f"Telegram bot token: {_mask_secret(token)}")
    # print(f"Gemini API key:     {_mask_secret(os.getenv('GEMINI_API_KEY', ''))}")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running... (Ctrl+C to stop)")
    app.run_polling()


if __name__ == "__main__":
    main()
