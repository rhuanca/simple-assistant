import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "grocery_bot.db"
DEFAULT_LIST = "groceries"
ADMIN_USER_ROLE = "admin"
DEFAULT_USER_ROLE = "member"


MIGRATIONS = [
    # v1: initial schema
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_name TEXT NOT NULL DEFAULT 'groceries',
        item_text TEXT NOT NULL,
        added_by TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS allowed_chats (
        chat_id INTEGER PRIMARY KEY
    );
    """,
    # v2: users table with admin/member roles
    """
    CREATE TABLE IF NOT EXISTS users (
        telegram_user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        role TEXT NOT NULL DEFAULT 'member',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        for version, sql in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )


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


def has_any_users() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM users LIMIT 1")
        return row.fetchone() is not None


def upsert_user(
    telegram_user_id: int,
    chat_id: int,
    username: str = "",
    first_name: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (
                telegram_user_id,
                chat_id,
                username,
                first_name,
                role,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (telegram_user_id, chat_id, username, first_name, DEFAULT_USER_ROLE, now, now),
        )


def promote_to_admin(telegram_user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE telegram_user_id = ?",
            (ADMIN_USER_ROLE, datetime.now(timezone.utc).isoformat(), telegram_user_id),
        )


def get_admin_chat_ids() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM users WHERE role = ?",
            (ADMIN_USER_ROLE,),
        )
        return [row[0] for row in rows.fetchall()]
