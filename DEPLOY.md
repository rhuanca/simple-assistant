# Deploying Grocery Bot on a Raspberry Pi 3

A step-by-step guide to running this bot 24/7 on a Raspberry Pi 3, surviving reboots, with logs readable from your phone via Telegram.

## What you'll end up with

- Bot running under **systemd** as your user — auto-starts at boot, auto-restarts on crash
- Logs in **journald** with size capped to keep the SD card happy
- A `/logs` admin command in Telegram so you can read recent log lines from your phone

## Prerequisites

- Raspberry Pi 3 with Raspberry Pi OS (64-bit Bookworm or newer recommended)
- SSH access to the Pi as your normal user
- Telegram bot token and Gemini API key in hand
- Internet on the Pi (`ping 1.1.1.1`)

> **Memory note:** A Pi 3 has 1 GB RAM. langchain + langgraph + python-telegram-bot together can spike near that ceiling. If you hit "killed" messages in the logs, add a 1 GB swap file (see Troubleshooting at the end).

---

## Quick deploy (script)

Once you've finished Steps 1–3 below (uv installed, code cloned with `.env` filled in, optional `/logs` command added), the rest is automated:

```bash
chmod +x deploy.sh
./deploy.sh
```

The script handles Steps 4–6 in one shot: journald size cap, systemd unit, lingering, enable + (re)start. It's idempotent — safe to re-run after every `git pull`. It prompts before each `sudo` step and never reads `.env`.

The full step-by-step below is the manual reference. Read it once to understand what the script does, then use the script for day-to-day deploys.

---

## Step 1 — Install uv and Python 3.13

uv handles Python installs on ARM, so you don't have to compile from source.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc            # or open a new shell
uv python install 3.13
uv --version                 # confirm
```

## Step 2 — Get the code on the Pi

```bash
mkdir -p ~/src && cd ~/src
git clone <your-repo-url> simple-assistant
cd simple-assistant
uv sync
```

Confirm the bot starts manually before we wrap it in systemd:

```bash
cp .env.example .env
nano .env                    # paste TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, BOT_PASSWORD
uv run python -m bot.main
```

You should see `Bot is running...`. Send your bot a message in Telegram, send the password, confirm it replies. Then `Ctrl+C` to stop.

## Step 3 — Add the `/logs` admin command

This lets you read recent log lines straight from Telegram on your phone.

### 3a — Add `is_admin()` to `bot/storage.py`

Add at the end of the file:

```python
def is_admin(telegram_user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = ? AND role = ?",
            (telegram_user_id, ADMIN_USER_ROLE),
        ).fetchone()
        return row is not None
```

### 3b — Add the handler in `bot/handlers.py`

Add at the top of the file (with the other imports):

```python
import asyncio

from bot.storage import is_admin
```

Add this function near `handle_message`:

```python
LOGS_UNIT = "grocery-bot.service"
LOGS_MAX_CHARS = 3900   # Telegram caps messages at 4096

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    user_id = user_obj.id if user_obj else 0
    if not is_admin(user_id):
        return

    args = context.args or []
    try:
        n = max(1, min(int(args[0]), 200)) if args else 50
    except ValueError:
        n = 50

    proc = await asyncio.create_subprocess_exec(
        "journalctl", "--user", "-u", LOGS_UNIT, "-n", str(n), "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace") or "(no logs)"
    if len(text) > LOGS_MAX_CHARS:
        text = text[-LOGS_MAX_CHARS:]
    await update.message.reply_text(f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN)
```

### 3c — Register the command in `bot/main.py`

In `main()`, alongside the other handlers:

```python
from bot.handlers import handle_message, help_command, logs_command, start
...
app.add_handler(CommandHandler("logs", logs_command))
```

Now `/logs` returns the last 50 lines, `/logs 100` returns 100 (capped at 200).

---

## Step 4 — Make journald gentle on the SD card

Edit the system-wide journald config:

```bash
sudo nano /etc/systemd/journald.conf
```

Set (uncomment as needed):

```
[Journal]
Storage=persistent
SystemMaxUse=50M
SystemKeepFree=200M
```

Then reload:

```bash
sudo systemctl restart systemd-journald
```

This caps disk usage at 50 MB total. If you'd rather have logs only in RAM (zero SD writes, lost on reboot), use `Storage=volatile` instead.

## Step 5 — Create the systemd user unit

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/grocery-bot.service
```

Paste:

```ini
[Unit]
Description=Grocery Bot Telegram service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/src/simple-assistant
ExecStart=%h/.local/bin/uv run python -m bot.main
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=default.target
```

`%h` expands to your home directory at runtime. Adjust `WorkingDirectory` if you cloned somewhere other than `~/src/simple-assistant`. Confirm `~/.local/bin/uv` exists (`which uv`); if not, edit the path.

## Step 6 — Enable lingering and start the service

```bash
sudo loginctl enable-linger $USER          # lets your user services run at boot without you logged in
systemctl --user daemon-reload
systemctl --user enable --now grocery-bot.service
systemctl --user status grocery-bot.service
```

You should see `active (running)`. Press `q` to exit the status view.

## Step 7 — Verify end-to-end

```bash
journalctl --user -u grocery-bot -f        # tail the live logs
```

In another window or on your phone, message the bot. You should see request lines scroll by. Then send `/logs` from your authenticated admin account — the bot should reply with the last 50 lines as a code block.

Reboot the Pi (`sudo reboot`) and confirm the bot comes back on its own.

---

## Day-to-day operations

| Task | Command |
| --- | --- |
| View live logs | `journalctl --user -u grocery-bot -f` |
| Last N lines | `journalctl --user -u grocery-bot -n 200` |
| Restart bot | `systemctl --user restart grocery-bot` |
| Stop bot | `systemctl --user stop grocery-bot` |
| Service status | `systemctl --user status grocery-bot` |
| Update code | `cd ~/src/simple-assistant && git pull && uv sync && systemctl --user restart grocery-bot` |
| Disable auto-start | `systemctl --user disable grocery-bot` |

The DB migration system runs automatically on every restart, so `git pull` + `restart` is a complete deploy step.

---

## Troubleshooting

**Service won't start (`status` shows `failed`).** Check the journal: `journalctl --user -u grocery-bot -n 100`. Most common: wrong path to `uv` in the unit file (`which uv` to find it), or `.env` missing.

**Bot dies with "Killed" in the logs.** Out of memory. Add a swap file:

```bash
sudo dphys-swapfile swapoff
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

Then `systemctl --user restart grocery-bot`.

**`/logs` command returns nothing in Telegram but `journalctl` works in shell.** The command runs as the same user, so it should see the journal. If it still fails, check that `--user` works for that user: `journalctl --user -n 5`. If the user has no journal directory yet, restarting the service generates one.

**Want to add `/tail` (live streaming for ~60s).** Doable but requires editing a Telegram message on a timer (~3s polls). Skipped from this guide to keep it simple. Open a follow-up if you want it added.
