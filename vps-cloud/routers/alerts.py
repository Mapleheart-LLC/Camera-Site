"""
routers/alerts.py – Creator stream-alert settings and real-time alert WebSocket.

Allows creators to configure per-event overlay alerts (tips, subscriptions,
follows) and provides a lightweight WebSocket endpoint that stream overlays
(e.g. OBS Browser Source) connect to for real-time event delivery.

Architecture
------------
* ``creator_alert_settings`` – per-creator, per-event configuration.
* ``stream_alerts``          – ring-buffer log of emitted alerts (pruned on write).
* ``WS /ws/alerts/{handle}`` – public, read-only WebSocket that polls the
  ``stream_alerts`` table every second and pushes new rows as JSON.  Using
  DB-backed polling means:
    - no shared in-process state between threads/workers,
    - overlays automatically catch up after a brief disconnect,
    - sync write-path hooks can call ``dispatch_alert`` without async glue.

Endpoints (creator auth)
------------------------
  GET    /api/creator/alert-settings          – list all alert configs for this creator
  PATCH  /api/creator/alert-settings/{event}  – update a single event config
  POST   /api/creator/alert-settings/test/{event} – fire a test alert via WebSocket

Overlay endpoint (public, read-only)
-------------------------------------
  WS     /ws/alerts/{creator_handle}           – OBS Browser Source / overlay connects here

Internal helpers
----------------
  dispatch_alert(creator_handle, event_type, data, db)
    Called from monetization.py (tips), member.py (follows), subscriptions.py
    and creator.py (subscriptions / gifts).  Inserts a row into stream_alerts
    if that event type is enabled for the creator.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from db import get_db, get_db_connection
from dependencies import get_current_creator

router = APIRouter(tags=["alerts"])

logger = logging.getLogger(__name__)

# Valid event types — used in CHECK constraints and as URL path params.
_VALID_EVENTS = frozenset({"tip", "subscribe", "follow"})

# Keep at most this many alerts per creator in the ring-buffer table.
_ALERT_HISTORY_LIMIT = 200

# WebSocket poll interval (seconds).
_WS_POLL_INTERVAL = 1.0

# Default message templates (use {username}, {amount}, {tier} as placeholders).
_DEFAULT_TEMPLATES: dict[str, str] = {
    "tip":       "🎉 {username} just tipped {amount}!",
    "subscribe": "🌟 {username} just subscribed!",
    "follow":    "🐾 {username} is now following!",
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AlertSettingUpdate(BaseModel):
    enabled: Optional[bool] = None
    message_template: Optional[str] = Field(None, max_length=300)
    min_amount_cents: Optional[int] = Field(None, ge=0)   # tip filter
    duration_ms: Optional[int] = Field(None, ge=500, le=30000)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def dispatch_alert(
    creator_handle: str,
    event_type: str,
    data: dict[str, Any],
    db: sqlite3.Connection,
) -> None:
    """Insert a real-time alert for *creator_handle* if that event is enabled.

    Parameters
    ----------
    creator_handle:
        The creator whose overlay should receive the alert.
    event_type:
        One of ``"tip"``, ``"subscribe"``, or ``"follow"``.
    data:
        Arbitrary dict serialised to JSON and stored in ``stream_alerts.payload``.
        Should include at least ``{"username": str}``.  Tips should also include
        ``{"amount_cents": int}``.
    db:
        An open SQLite connection (provided by FastAPI dependency or called
        directly from sync endpoint handlers).
    """
    if event_type not in _VALID_EVENTS:
        return

    # Check if this event is enabled for the creator (default on if no row yet).
    cfg = db.execute(
        "SELECT enabled, message_template, min_amount_cents FROM creator_alert_settings "
        "WHERE creator_handle = ? AND event_type = ?",
        (creator_handle, event_type),
    ).fetchone()

    if cfg and not cfg["enabled"]:
        return

    # Apply tip minimum filter.
    if event_type == "tip" and cfg and cfg["min_amount_cents"]:
        if data.get("amount_cents", 0) < cfg["min_amount_cents"]:
            return

    # Render message template.
    template = (cfg and cfg["message_template"]) or _DEFAULT_TEMPLATES.get(event_type, "")
    username = data.get("username") or data.get("display_name") or "Someone"
    amount_cents = data.get("amount_cents", 0)
    amount_str = f"${amount_cents / 100:.2f}" if amount_cents else ""
    tier = data.get("tier_name") or ""
    try:
        message = template.format(username=username, amount=amount_str, tier=tier)
    except (KeyError, ValueError):
        message = template  # use raw template if format fails

    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps({**data, "message": message})

    db.execute(
        """
        INSERT INTO stream_alerts (creator_handle, event_type, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (creator_handle, event_type, payload_json, now),
    )

    # Prune old alerts: keep only the most recent _ALERT_HISTORY_LIMIT rows.
    db.execute(
        """
        DELETE FROM stream_alerts
        WHERE creator_handle = ?
          AND id NOT IN (
              SELECT id FROM stream_alerts
               WHERE creator_handle = ?
               ORDER BY id DESC
               LIMIT ?
          )
        """,
        (creator_handle, creator_handle, _ALERT_HISTORY_LIMIT),
    )

    db.commit()


# ---------------------------------------------------------------------------
# Creator alert-settings REST API
# ---------------------------------------------------------------------------

def _ensure_settings(db: sqlite3.Connection, handle: str) -> None:
    """Create default rows for all event types if they don't exist yet."""
    now = datetime.now(timezone.utc).isoformat()
    for event_type in sorted(_VALID_EVENTS):
        db.execute(
            """
            INSERT OR IGNORE INTO creator_alert_settings
                (creator_handle, event_type, enabled, message_template,
                 min_amount_cents, duration_ms, created_at, updated_at)
            VALUES (?, ?, 1, ?, 0, 5000, ?, ?)
            """,
            (handle, event_type, _DEFAULT_TEMPLATES[event_type], now, now),
        )
    db.commit()


@router.get("/api/creator/alert-settings")
def get_alert_settings(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return alert configuration for all event types for this creator."""
    _ensure_settings(db, handle)
    rows = db.execute(
        """
        SELECT event_type, enabled, message_template, min_amount_cents, duration_ms,
               updated_at
          FROM creator_alert_settings
         WHERE creator_handle = ?
         ORDER BY event_type
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.patch("/api/creator/alert-settings/{event_type}")
def update_alert_setting(
    event_type: str,
    payload: AlertSettingUpdate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update the alert config for a single event type."""
    if event_type not in _VALID_EVENTS:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"event_type must be one of: {sorted(_VALID_EVENTS)}",
        )

    _ensure_settings(db, handle)

    updates: dict[str, Any] = {}
    if payload.enabled is not None:
        updates["enabled"] = int(payload.enabled)
    if payload.message_template is not None:
        updates["message_template"] = payload.message_template
    if payload.min_amount_cents is not None:
        updates["min_amount_cents"] = payload.min_amount_cents
    if payload.duration_ms is not None:
        updates["duration_ms"] = payload.duration_ms

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE creator_alert_settings SET {set_clause} "
            "WHERE creator_handle = ? AND event_type = ?",
            list(updates.values()) + [handle, event_type],
        )
        db.commit()

    row = db.execute(
        "SELECT event_type, enabled, message_template, min_amount_cents, "
        "duration_ms, updated_at FROM creator_alert_settings "
        "WHERE creator_handle = ? AND event_type = ?",
        (handle, event_type),
    ).fetchone()
    return dict(row)


@router.post("/api/creator/alert-settings/test/{event_type}", status_code=status.HTTP_200_OK)
def send_test_alert(
    event_type: str,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Inject a test alert so the creator can preview it in their overlay."""
    if event_type not in _VALID_EVENTS:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"event_type must be one of: {sorted(_VALID_EVENTS)}",
        )

    test_data: dict[str, Any] = {"username": "TestUser", "is_test": True}
    if event_type == "tip":
        test_data["amount_cents"] = 500
    dispatch_alert(handle, event_type, test_data, db)
    return {"detail": "Test alert dispatched."}


# ---------------------------------------------------------------------------
# Public WebSocket overlay endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/alerts/{creator_handle}")
async def alerts_websocket(creator_handle: str, ws: WebSocket):
    """Real-time alert stream for stream overlays (OBS Browser Source etc.).

    Connects to the WebSocket, receives ``stream_alerts`` rows for
    *creator_handle* in real time.  No authentication required — this is a
    read-only public events feed.

    Each JSON message has the shape::

        {
          "id": 42,
          "event_type": "tip",
          "payload": {"username": "fan123", "amount_cents": 500, "message": "🎉 fan123 tipped $5.00!"},
          "created_at": "2026-04-06T02:01:00+00:00"
        }
    """
    await ws.accept()
    creator_handle = creator_handle.lower().strip()

    # Anchor to alerts that already exist at connect time — only deliver *new* ones.
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM stream_alerts WHERE creator_handle = ?",
            (creator_handle,),
        ).fetchone()
        last_id: int = row["max_id"]

    try:
        while True:
            await asyncio.sleep(_WS_POLL_INTERVAL)
            with get_db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, event_type, payload, created_at
                      FROM stream_alerts
                     WHERE creator_handle = ? AND id > ?
                     ORDER BY id ASC
                     LIMIT 50
                    """,
                    (creator_handle, last_id),
                ).fetchall()
            for alert_row in rows:
                await ws.send_json(
                    {
                        "id": alert_row["id"],
                        "event_type": alert_row["event_type"],
                        "payload": json.loads(alert_row["payload"]),
                        "created_at": alert_row["created_at"],
                    }
                )
                last_id = alert_row["id"]
    except (WebSocketDisconnect, Exception):
        pass
