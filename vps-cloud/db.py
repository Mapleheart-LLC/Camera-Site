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
