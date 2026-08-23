import os

from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot.alerts import schedule_alert_job
from bot.handlers import (
    alert_command,
    config_command,
    demote_command,
    handle_message,
    help_command,
    promote_command,
    resetdb_command,
    revoke_command,
    start,
    users_command,
)
from bot.storage import init_db


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
    app.add_handler(CommandHandler("config", config_command))
    app.add_handler(CommandHandler("resetdb", resetdb_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily tick for appointment reminders, which also carries the shopping list when its
    # interval has elapsed (see bot/alerts.py). The hour is LOCAL: "the morning of" only
    # means anything in the user's own timezone. /config reschedules this same job.
    at = schedule_alert_job(app.job_queue)
    print(f"Daily reminder at {at.strftime('%H:%M')} {at.tzinfo}")

    print("Bot is running... (Ctrl+C to stop)")
    app.run_polling()


if __name__ == "__main__":
    main()
