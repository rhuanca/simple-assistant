import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "grocery_bot.db"
DEFAULT_LIST = "groceries"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_name TEXT NOT NULL DEFAULT 'groceries',
                item_text TEXT NOT NULL,
                added_by TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)


def add_item(item_text: str, list_name: str = DEFAULT_LIST, added_by: str = "") -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO items (list_name, item_text, added_by, created_at) VALUES (?, ?, ?, ?)",
            (list_name, item_text, added_by, datetime.now(timezone.utc).isoformat()),
        )
        return cursor.lastrowid


def get_items(list_name: str = DEFAULT_LIST) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, item_text, added_by, created_at FROM items WHERE list_name = ? ORDER BY created_at",
            (list_name,),
        )
        return [dict(row) for row in rows.fetchall()]


def remove_item(item_text: str, list_name: str = DEFAULT_LIST) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM items WHERE id = (SELECT id FROM items WHERE list_name = ? AND LOWER(item_text) = LOWER(?) LIMIT 1)",
            (list_name, item_text),
        )
        return cursor.rowcount > 0


def clear_list(list_name: str = DEFAULT_LIST) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM items WHERE list_name = ?", (list_name,))
        return cursor.rowcount


def is_chat_allowed(chat_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,))
        return row.fetchone() is not None


def allow_chat(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO allowed_chats (chat_id) VALUES (?)", (chat_id,))
