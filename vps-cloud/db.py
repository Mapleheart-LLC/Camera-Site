"""
db.py – Shared SQLite database helpers.

Centralised here so that multiple modules (main.py, routers/admin.py,
routers/interactive.py) can share a consistent database path and connection
factory without creating circular imports.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))

DATABASE_PATH: str = os.environ.get(
    "DATABASE_PATH", os.path.join(_BASE_DIR, "camera_site.db")
)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    """FastAPI dependency: yield an open SQLite connection and close on exit."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def ensure_user_account_schema(conn: sqlite3.Connection) -> None:
    """Backfill legacy ``users`` columns needed by auth and admin user management."""
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    if not rows:
        return

    existing_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in rows
    }
    changed = False

    for column_name, column_def in [
        ("username", "TEXT NOT NULL DEFAULT ''"),
        ("password_hash", "TEXT NOT NULL DEFAULT ''"),
        ("role", "TEXT NOT NULL DEFAULT 'handler'"),
    ]:
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_def}")
        changed = True

    if changed:
        conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a runtime setting value from the settings table, or *default* if absent."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a runtime setting in the settings table."""
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
