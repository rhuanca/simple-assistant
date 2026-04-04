# Grocery Bot

A Telegram bot for managing shared grocery and household shopping lists. Supports English and Spanish.

Uses Gemini Flash to understand natural language messages like:
- "Add milk and eggs"
- "Comprar jabón y papel"
- "Show my list" / "Muéstrame la lista"
- "Remove milk" / "Quita el jabón"
- "Clear the list" / "Borra todo"

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

Go to [Google AI Studio](https://aistudio.google.com/apikey) and create an API key (free tier: 1500 req/day).

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

### 6. Run

```bash
uv run python -m bot.main
```

## Authentication

The bot requires a password on first contact. Share the `BOT_PASSWORD` with anyone you want to grant access. Once authenticated, the chat is remembered permanently.

## Lists

By default items go to the **groceries** list. Mention "casa", "house", or "home" to use the **house** list:

- "Add light bulbs to the house list"
- "Agrega un foco a la lista de la casa"
