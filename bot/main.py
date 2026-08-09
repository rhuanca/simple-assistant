import os
from datetime import time, timezone

from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot.alerts import run_alert_tick
from bot.handlers import (
    alert_command,
    demote_command,
    handle_message,
    help_command,
    promote_command,
    resetdb_command,
    revoke_command,
    start,
    users_command,
)
from bot.storage import get_setting, init_db


def _mask_secret(value: str) -> str:
    if len(value) < 8:
        return "(missing or too short)"
    return f"{value[:4]}...{value[-4:]}"


def main():
    load_dotenv(override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: set TELEGRAM_BOT_TOKEN in your .env file")
        print("Get one from @BotFather on Telegram")
        return

    init_db()
    # print(f"Telegram bot token: {_mask_secret(token)}")
    # print(f"Gemini API key:     {_mask_secret(os.getenv('GEMINI_API_KEY', ''))}")
    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(f"LangSmith tracing: ENABLED (project: {project})")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("promote", promote_command))
    app.add_handler(CommandHandler("demote", demote_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("resetdb", resetdb_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily tick that self-checks whether the alert interval has elapsed (see bot/alerts.py).
    try:
        alert_hour = int(get_setting("alert_hour"))
    except ValueError:
        alert_hour = 9
    app.job_queue.run_daily(run_alert_tick, time=time(hour=alert_hour, tzinfo=timezone.utc))

    print("Bot is running... (Ctrl+C to stop)")
    app.run_polling()


if __name__ == "__main__":
    main()
