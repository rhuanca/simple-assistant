import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# DB path is env-overridable (GROCERY_BOT_DB) so tests can point at a temp file.
DB_PATH = Path(os.getenv("GROCERY_BOT_DB", Path(__file__).parent.parent / "grocery_bot.db"))
DEFAULT_LIST = "groceries"
ADMIN_USER_ROLE = "admin"
DEFAULT_USER_ROLE = "member"

# Alert settings live in the `settings` table; these are the seeded defaults.
DEFAULT_SETTINGS = {
    "alert_enabled": "true",
    "alert_interval_days": "3",
    "alert_hour": "9",
    "last_alert_at": "",
}


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
    # v3: per-person lists (owner_user_id, NULL = shared/common list) + settings.
    # Existing items keep owner_user_id = NULL, so they become the common list.
    """
    ALTER TABLE items ADD COLUMN owner_user_id INTEGER;
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
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


def _owner_clause(owner_user_id: int | None) -> tuple[str, tuple]:
    """WHERE fragment + params for filtering by owner. None means the common list."""
    if owner_user_id is None:
        return "owner_user_id IS NULL", ()
    return "owner_user_id = ?", (owner_user_id,)


def _reachable_clause(acting_user_id: int | None) -> tuple[str, tuple]:
    """WHERE fragment + params for the items `acting_user_id` is allowed to touch: their own
    personal items plus the common list. Ids are global, so anything addressed by id must be
    filtered through this — otherwise a guessed id reaches another person's list."""
    if acting_user_id is None:
        return "owner_user_id IS NULL", ()
    return "(owner_user_id = ? OR owner_user_id IS NULL)", (acting_user_id,)


def add_item(item_text: str, owner_user_id: int | None = None, added_by: str = "") -> int:
    """Add an item. owner_user_id=None puts it on the common list; otherwise a personal list."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO items (list_name, item_text, added_by, created_at, owner_user_id) VALUES (?, ?, ?, ?, ?)",
            (DEFAULT_LIST, item_text, added_by, datetime.now(timezone.utc).isoformat(), owner_user_id),
        )
        return cursor.lastrowid


def get_items(owner_user_id: int | None = None) -> list[dict]:
    clause, params = _owner_clause(owner_user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, item_text, added_by, created_at FROM items WHERE {clause} ORDER BY created_at",
            params,
        )
        return [dict(row) for row in rows.fetchall()]


def remove_item(item_text: str, owner_user_id: int | None = None) -> bool:
    clause, params = _owner_clause(owner_user_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            f"DELETE FROM items WHERE id = (SELECT id FROM items WHERE {clause} AND LOWER(item_text) = LOWER(?) LIMIT 1)",
            (*params, item_text),
        )
        return cursor.rowcount > 0


def get_item_by_id(item_id: int, acting_user_id: int | None = None) -> dict | None:
    """Look up an item the user is allowed to see. Returns None for another person's item,
    which is indistinguishable from a missing id — callers must not reveal the difference."""
    clause, params = _reachable_clause(acting_user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT id, list_name, item_text, added_by, created_at FROM items "
            f"WHERE id = ? AND {clause}",
            (item_id, *params),
        ).fetchone()
        return dict(row) if row else None


def remove_item_by_id(item_id: int, acting_user_id: int | None = None) -> str | None:
    """Delete an item the user is allowed to touch. The ownership check is part of the DELETE
    so a caller cannot widen it by checking first and deleting second."""
    clause, params = _reachable_clause(acting_user_id)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"DELETE FROM items WHERE id = ? AND {clause} RETURNING item_text",
            (item_id, *params),
        ).fetchone()
        return row[0] if row else None


def clear_list(owner_user_id: int | None = None) -> int:
    clause, params = _owner_clause(owner_user_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(f"DELETE FROM items WHERE {clause}", params)
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


def is_admin(telegram_user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = ? AND role = ?",
            (telegram_user_id, ADMIN_USER_ROLE),
        )
        return row.fetchone() is not None


def get_all_users() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT telegram_user_id, chat_id, username, first_name, role FROM users ORDER BY created_at"
        )
        return [dict(row) for row in rows.fetchall()]


def find_user_by_username(username: str) -> dict | None:
    """Look up a user by @username (case-insensitive, leading @ optional)."""
    name = username.lstrip("@").strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT telegram_user_id, chat_id, username, first_name, role FROM users WHERE LOWER(username) = LOWER(?)",
            (name,),
        ).fetchone()
        return dict(row) if row else None


def set_role(telegram_user_id: int, role: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE telegram_user_id = ?",
            (role, datetime.now(timezone.utc).isoformat(), telegram_user_id),
        )
        return cursor.rowcount > 0


def revoke_user(telegram_user_id: int) -> bool:
    """Remove a user and de-authorize their chat so they must re-enter the password."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "DELETE FROM users WHERE telegram_user_id = ? RETURNING chat_id",
            (telegram_user_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (row[0],))
        return True


def get_setting(key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row[0]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
