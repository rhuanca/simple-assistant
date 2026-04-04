import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "grocery_bot.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_name TEXT NOT NULL DEFAULT 'groceries',
            item_text TEXT NOT NULL,
            added_by TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def add_item(item_text: str, list_name: str = "groceries", added_by: str = "") -> int:
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO items (list_name, item_text, added_by, created_at) VALUES (?, ?, ?, ?)",
        (list_name, item_text, added_by, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    item_id = cursor.lastrowid
    conn.close()
    return item_id


def get_items(list_name: str = "groceries") -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, item_text, added_by, created_at FROM items WHERE list_name = ? ORDER BY created_at",
        (list_name,),
    )
    items = [dict(row) for row in rows.fetchall()]
    conn.close()
    return items


def remove_item(item_text: str, list_name: str = "groceries") -> bool:
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM items WHERE id = (SELECT id FROM items WHERE list_name = ? AND LOWER(item_text) = LOWER(?) LIMIT 1)",
        (list_name, item_text),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def clear_list(list_name: str = "groceries") -> int:
    conn = _connect()
    cursor = conn.execute("DELETE FROM items WHERE list_name = ?", (list_name,))
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count
