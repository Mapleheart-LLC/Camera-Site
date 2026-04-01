"""
seed.py – Initialize the SQLite database and seed camera data.

NOTE: This script is intended for development use only.
      Users are no longer pre-seeded here; they are created on first login
      via Fanvue OAuth 2.0.

Run once before starting the application (or let the Dockerfile handle it):
    python seed.py
"""

import os
import sqlite3

DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "camera_site.db")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT    PRIMARY KEY,
            fanvue_id    TEXT    NOT NULL UNIQUE,
            access_level INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name         TEXT    NOT NULL,
            stream_slug          TEXT    NOT NULL UNIQUE,
            minimum_access_level INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()


def seed_cameras(conn: sqlite3.Connection) -> None:
    cameras = [
        # (display_name, stream_slug, minimum_access_level)
        ("Lobby",     "cam_lobby",     1),  # visible to all followers (free)
        ("VIP Room",  "cam_vip_room",  2),  # requires Tier 1 subscription
        ("Backstage", "cam_backstage", 3),  # requires Tier 2+ subscription
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO cameras (display_name, stream_slug, minimum_access_level)
        VALUES (?, ?, ?)
        """,
        cameras,
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        init_db(conn)
        seed_cameras(conn)
        print(f"Database ready at '{DATABASE_PATH}'.")
        print("Cameras seeded:")
        print("  cam_lobby     – minimum_access_level 1 (free followers)")
        print("  cam_vip_room  – minimum_access_level 2 (Tier 1 subscribers)")
        print("  cam_backstage – minimum_access_level 3 (Tier 2+ subscribers)")
        print("Users are created automatically on first Fanvue OAuth login.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
