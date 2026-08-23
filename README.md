# Grocery Bot

A Telegram bot with two jobs: shopping lists and a personal appointment schedule.
Understands natural language in English and Spanish, and always replies in the language you
used. Backed by Gemini Flash.

```
Add milk and eggs                                  Comprar jabón y papel
Show my list                                       Muéstrame la lista
Remove milk        /  Borra el 2                   Quita el jabón
Clear the list                                     Borra todo

I have an appointment next Sunday at 3 with the doctor
Tengo cita el próximo domingo a las 3 con el doctor
What appointments do I have?                       ¿Qué citas tengo?
Cancel the doctor one                              Cancela la segunda
```

`/help` lists everything in chat — lists, appointments and reminders, plus the admin and
configuration commands when an admin asks.

## Setup

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, pick a name and username
3. Copy the token

### 4. Get a Gemini API key

Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key (free
tier: 1500 req/day).

### 5. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
TELEGRAM_BOT_TOKEN=your-telegram-token
GEMINI_API_KEY=your-gemini-key
BOT_PASSWORD=choose-a-secret-password
```

`LANGSMITH_TRACING=true` (with `LANGSMITH_PROJECT`) optionally enables tracing.

### 6. Run

```bash
uv run python -m bot.main
```

## Authentication

The bot asks for a password on first contact. Share `BOT_PASSWORD` with anyone you want to
grant access; once authenticated, the chat is remembered. **The first person to authenticate
becomes the admin.**

## Lists

Everyone has their own **personal** list, and there is one **common** list shared by the
household. Personal is the default — the common list is used only when you say so:

- "Add light bulbs to the common list" / "Agrega un foco a la lista de la casa"

Items are numbered, and you can delete by number ("borra el 2") or by name ("quita el pan").
Personal lists are private: nobody else can see or change yours.

## Appointments

Appointments are always personal — there is no shared schedule.

Relative dates work ("next Sunday", "mañana a las 10", "el viernes a las 8am") because every
message carries the current local date and timezone. **If you give a day but no time, the bot
asks for the time and saves nothing until you answer.** It always reads the resolved date back
to you, so a misunderstanding is obvious immediately:

```
You:  tengo cita el proximo domingo con el doctor de gastroenterologia
Bot:  ...necesito saber a qué hora será.
You:  a las 3 de la tarde
Bot:  ¡Listo! [...] domingo 16 de agosto de 2026, a las 15:00.
```

To cancel, name it ("cancela la del doctor") or point at a row from the list ("la segunda").

## Reminders

One scheduled DM per user, once a day, containing whichever sections apply:

- **Appointments** — the day before, and the morning of.
- **Shopping lists** — only when the configured interval has elapsed (default: every 3 days).

You never get two separate notifications. Users with nothing due are skipped.

Admins control it with `/alert`:

```
/alert                 show status
/alert on | off        enable or disable the shopping digest
/alert every 5         change the shopping interval to 5 days
```

Note `/alert` only governs the shopping digest; appointment reminders are always on.

## Admin commands

The first authenticated user is the admin and can promote others.

| Command | Effect |
|---|---|
| `/users` | List users and their roles |
| `/promote @username` | Make someone an admin |
| `/demote @username` | Back to member |
| `/revoke @username` | Remove access entirely |
| `/alert ...` | Shopping digest settings (above) |
| `/config` | Show every setting and change it (below) |
| `/resetdb` | Show what a reset would destroy |
| `/resetdb CONFIRM` | Rebuild an empty database (see below) |

## Settings

Stored in a `settings` table, read at each use, with these defaults:

| Key | Default | Meaning |
|---|---|---|
| `alert_enabled` | `true` | Whether the shopping digest is sent |
| `alert_interval_days` | `3` | Days between shopping digests |
| `alert_hour` | `9` | Hour of the daily tick, **in local time** |
| `timezone` | `America/La_Paz` | Clock that appointments and `alert_hour` use |

Defaults are a read-time fallback and are never written to the table, which is how `/config`
can show whether a value was set or is still the default.

Admins change them from Telegram:

```
/config                                show every setting, plus the next reminder time
/config timezone America/Lima          validated against the IANA database
/config alert_hour 8                   0-23, local time
/config alert_interval_days 5
/config alert_enabled off
```

Changing `timezone` or `alert_hour` **reschedules the running reminder job**, so it takes
effect without restarting the service. `last_alert_at` is shown but cannot be edited.

## Database

SQLite, at `grocery_bot.db` in the project root. Override the location with the
`GROCERY_BOT_DB` environment variable (the tests use this).

**Schema changes apply themselves.** `init_db()` runs at every startup, compares the
`schema_migrations` table against the migration list, and applies whatever is missing. Upgrading
is just pulling the new code and restarting — existing items, users and authorized chats are
left untouched, and running it twice is a no-op. Migrations are positional and append-only:
add to the end of `MIGRATIONS`, never reorder or remove entries.

`/resetdb CONFIRM` moves the current database aside to a timestamped `.bak-` file and builds an
empty one. Every list, user and setting is destroyed; the admin who ran it stays authorized so
they are not locked out, and everyone else must send the password again. Backup files are
gitignored.

## Tests

```bash
uv run python -m unittest discover -s tests
```

Stdlib `unittest`, no external test dependencies. Each test points `storage.DB_PATH` at a
throwaway database, so nothing touches your real data.

## Deployment

See [DEPLOY.md](DEPLOY.md) — runs as a systemd user service via `deploy.sh`.
