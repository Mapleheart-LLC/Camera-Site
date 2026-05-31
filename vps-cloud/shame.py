"""Helpers for tracking and scoring public shame/humiliation telemetry."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


_EVENT_POINTS: dict[str, int] = {
    "drool_comment": 6,
    "drool_reaction_added": 3,
    "drool_reaction_removed": -1,
    "booking_submitted": 10,
    "drool_item_archived": 8,
}

_ESCALATION_LEVELS: tuple[tuple[str, int, str], ...] = (
    ("catastrophic", 900, "full_lockdown"),
    ("severe", 600, "public_task_burst"),
    ("high", 320, "intensify_protocol"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_shame_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shame_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type    TEXT NOT NULL,
            points        INTEGER NOT NULL,
            metadata_json TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shame_events_created_at ON shame_events(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shame_events_type_created ON shame_events(event_type, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shame_escalations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            level         TEXT NOT NULL,
            trigger_score INTEGER NOT NULL,
            action_hint   TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            note          TEXT,
            created_at    TEXT NOT NULL,
            resolved_at   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shame_escalations_status_created ON shame_escalations(status, created_at DESC)"
    )


def _target_escalation(score: int) -> tuple[str, int, str] | None:
    for level, threshold, action_hint in _ESCALATION_LEVELS:
        if score >= threshold:
            return (level, threshold, action_hint)
    return None


def maybe_enqueue_escalation(conn: sqlite3.Connection, score: int) -> dict[str, Any] | None:
    target = _target_escalation(int(score))
    if target is None:
        return None

    level, threshold, action_hint = target
    existing = conn.execute(
        """
        SELECT id
        FROM shame_escalations
        WHERE level = ? AND status IN ('pending', 'active')
        ORDER BY id DESC
        LIMIT 1
        """,
        (level,),
    ).fetchone()
    if existing:
        return None

    created_at = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO shame_escalations (level, trigger_score, action_hint, status, note, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (
            level,
            int(score),
            action_hint,
            f"Auto-queued when shame score crossed {threshold}.",
            created_at,
        ),
    )
    return {
        "id": int(cur.lastrowid),
        "level": level,
        "trigger_score": int(score),
        "action_hint": action_hint,
        "status": "pending",
        "note": f"Auto-queued when shame score crossed {threshold}.",
        "created_at": created_at,
    }


def record_shame_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    points: int | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    """Insert a shame event and return the row id.

    The caller controls transaction commit.
    """
    ensure_shame_tables(conn)
    resolved_points = int(points if points is not None else _EVENT_POINTS.get(event_type, 1))
    metadata_json = json.dumps(metadata or {}, ensure_ascii=True)
    cursor = conn.execute(
        """
        INSERT INTO shame_events (event_type, points, metadata_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (event_type, resolved_points, metadata_json, created_at or _now_iso()),
    )
    summary = compute_shame_summary(conn)
    maybe_enqueue_escalation(conn, int(summary["score"]))
    return int(cursor.lastrowid)


def _streak_days(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT SUBSTR(created_at, 1, 10) AS day_key
        FROM shame_events
        GROUP BY day_key
        ORDER BY day_key DESC
        LIMIT 60
        """
    ).fetchall()
    if not rows:
        return 0

    available_days = {
        str((row["day_key"] if isinstance(row, sqlite3.Row) else row[0]) or "")
        for row in rows
    }
    streak = 0
    day = datetime.now(timezone.utc).date()
    while day.isoformat() in available_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def compute_shame_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute a public-facing shame summary from events and aggregate counters."""
    ensure_shame_tables(conn)

    events_30 = conn.execute(
        """
        SELECT COALESCE(SUM(points), 0) AS total, COUNT(*) AS count
        FROM shame_events
        WHERE created_at >= datetime('now', '-30 days')
        """
    ).fetchone()
    events_7 = conn.execute(
        """
        SELECT COALESCE(SUM(points), 0) AS total
        FROM shame_events
        WHERE created_at >= datetime('now', '-7 days')
        """
    ).fetchone()

    def _safe_count(table: str) -> int:
        try:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
            return int((row["n"] if row else 0) or 0)
        except sqlite3.OperationalError:
            return 0

    items_n = _safe_count("drool_archive")
    comments_n = _safe_count("drool_comments")
    reactions_n = _safe_count("drool_reactions")
    bookings_n = _safe_count("booking_intake")

    event_score_30 = int((events_30["total"] if events_30 else 0) or 0)
    event_count_30 = int((events_30["count"] if events_30 else 0) or 0)
    event_score_7 = int((events_7["total"] if events_7 else 0) or 0)

    # Weighted long-lived exposure + recency pressure.
    score = max(
        0,
        int(
            event_score_30
            + (items_n * 6)
            + (comments_n * 4)
            + (reactions_n * 2)
            + (bookings_n * 8)
        ),
    )

    if score >= 900:
        tier = "catastrophic"
    elif score >= 600:
        tier = "severe"
    elif score >= 320:
        tier = "high"
    elif score >= 140:
        tier = "rising"
    else:
        tier = "warming"

    active_escalation_row = conn.execute(
        """
        SELECT id, level, trigger_score, action_hint, status, note, created_at
        FROM shame_escalations
        WHERE status IN ('pending', 'active')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    pending_count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM shame_escalations WHERE status = 'pending'"
    ).fetchone()

    return {
        "score": score,
        "tier": tier,
        "streak_days": _streak_days(conn),
        "event_score_30d": event_score_30,
        "event_score_7d": event_score_7,
        "event_count_30d": event_count_30,
        "totals": {
            "drool_items": items_n,
            "drool_comments": comments_n,
            "drool_reactions": reactions_n,
            "booking_requests": bookings_n,
        },
        "pending_escalations": int((pending_count_row["n"] if pending_count_row else 0) or 0),
        "active_escalation": dict(active_escalation_row) if active_escalation_row else None,
    }


def list_shame_escalations(
    conn: sqlite3.Connection,
    *,
    status_filter: str = "all",
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_shame_tables(conn)
    sf = (status_filter or "all").strip().lower()
    lim = max(1, min(int(limit), 500))
    if sf == "all":
        rows = conn.execute(
            """
            SELECT id, level, trigger_score, action_hint, status, note, created_at, resolved_at
            FROM shame_escalations
            ORDER BY id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, level, trigger_score, action_hint, status, note, created_at, resolved_at
            FROM shame_escalations
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sf, lim),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_shame_escalation(conn: sqlite3.Connection, escalation_id: int, *, note: str = "") -> dict[str, Any]:
    ensure_shame_tables(conn)
    row = conn.execute(
        "SELECT id, status FROM shame_escalations WHERE id = ?",
        (int(escalation_id),),
    ).fetchone()
    if not row:
        raise ValueError("escalation_not_found")
    if str(row["status"]).lower() == "resolved":
        return {"id": int(escalation_id), "resolved": True, "already_resolved": True}

    resolved_at = _now_iso()
    conn.execute(
        """
        UPDATE shame_escalations
        SET status = 'resolved', note = ?, resolved_at = ?
        WHERE id = ?
        """,
        ((note or "").strip(), resolved_at, int(escalation_id)),
    )
    return {"id": int(escalation_id), "resolved": True, "resolved_at": resolved_at}


def activate_shame_escalation(conn: sqlite3.Connection, escalation_id: int, *, note: str = "") -> dict[str, Any]:
    ensure_shame_tables(conn)
    eid = int(escalation_id)
    row = conn.execute(
        "SELECT id, status, note FROM shame_escalations WHERE id = ?",
        (eid,),
    ).fetchone()
    if not row:
        raise ValueError("escalation_not_found")

    current_status = str(row["status"] or "").strip().lower()
    if current_status == "resolved":
        raise ValueError("escalation_resolved")
    if current_status == "active":
        return {"id": eid, "activated": True, "already_active": True}

    # Keep at most one active escalation at a time.
    conn.execute(
        "UPDATE shame_escalations SET status = 'pending' WHERE status = 'active' AND id != ?",
        (eid,),
    )

    note_text = (note or "").strip()
    if note_text:
        conn.execute(
            "UPDATE shame_escalations SET status = 'active', note = ? WHERE id = ?",
            (note_text, eid),
        )
    else:
        conn.execute(
            "UPDATE shame_escalations SET status = 'active' WHERE id = ?",
            (eid,),
        )
    return {"id": eid, "activated": True, "status": "active"}
