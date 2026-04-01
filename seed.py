"""
seed.py – Initialize the SQLite database and insert a test user.

NOTE: This script is intended for development use only.
      Do not use these credentials in a production environment.

Run once before starting the application (or let the Dockerfile handle it):
    python seed.py
"""

import os
import sqlite3

from passlib.context import CryptContext

DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "camera_site.db")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    NOT NULL UNIQUE,
            secret_code     TEXT    NOT NULL,
            has_paid        INTEGER NOT NULL DEFAULT 0,
            allowed_cameras TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT    NOT NULL,
            stream_slug  TEXT    NOT NULL UNIQUE
        )
        """
    )
    conn.commit()


def seed_cameras(conn: sqlite3.Connection) -> None:
    cameras = [
        ("Front Door", "cam_front"),
        ("Back Yard",  "cam_back"),
        ("Garage",     "cam_garage"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO cameras (display_name, stream_slug) VALUES (?, ?)",
        cameras,
    )
    conn.commit()


def seed_test_user(conn: sqlite3.Connection) -> None:
    hashed_code = pwd_context.hash("testcode123")
    conn.execute(
        """
        INSERT OR IGNORE INTO users (username, secret_code, has_paid, allowed_cameras)
        VALUES (?, ?, ?, ?)
        """,
        ("testuser", hashed_code, 1, "cam_front,cam_back,cam_garage"),
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        init_db(conn)
        seed_cameras(conn)
        seed_test_user(conn)
        print(f"Database ready at '{DATABASE_PATH}'.")
        print("Test user created (if not already present):")
        print("  username    : testuser")
        print("  secret_code : testcode123  (stored as bcrypt hash)")
        print("  has_paid    : True")
        print("  cameras     : cam_front, cam_back, cam_garage")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
