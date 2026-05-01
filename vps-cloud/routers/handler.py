"""
routers/handler.py – Handler Panel backend.

Provides real-time device status updates and quick-action commands for the
Handler Panel frontend (static/handler.html).

Device-facing (protected by TPE webhook secret):
  POST /api/handler/device-status  – Device reports battery, GPS, AI filter hit.

Admin-facing (HTTP Basic Auth):
  GET  /api/handler/devices        – List all known devices.
  GET  /api/handler/status         – Latest status snapshot for a specific device.
  POST /api/handler/lock           – Send LOCK_DEVICE FCM to a specific device.

WebSocket (admin Basic credentials via query parameter):
  WS   /ws/handler                 – Real-time device status stream.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from db import get_db, get_db_connection
from dependencies import ADMIN_PASSWORD, ADMIN_USERNAME, get_admin_user
from routers.tpe import _effective_webhook_secret, _send_fcm_to_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["handler"])


# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS handler_device_status (
        device_id   TEXT PRIMARY KEY,
        fcm_token   TEXT,
        battery_pct INTEGER,
        lat         REAL,
        lon         REAL,
        ai_alert    INTEGER NOT NULL DEFAULT 0,
        ai_label    TEXT,
        ai_score    REAL,
        is_locked   INTEGER NOT NULL DEFAULT 0,
        is_online   INTEGER NOT NULL DEFAULT 0,
        last_seen   TEXT,
        updated_at  TEXT NOT NULL
    )
"""


def migrate_handler(conn: sqlite3.Connection) -> None:
    """Create or migrate the handler_device_status table.

    Handles upgrading the old singleton schema (id INTEGER PRIMARY KEY CHECK(id=1))
    to the new multi-tenant schema (device_id TEXT PRIMARY KEY).
    """
    info = conn.execute("PRAGMA table_info(handler_device_status)").fetchall()
    col_names = {row[1] for row in info}  # row[1] is the column name

    if not info:
        # Fresh install – create directly.
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
        return

    if "device_id" not in col_names:
        # Old singleton schema detected – rename, recreate, migrate the one row.
        conn.execute(
            "ALTER TABLE handler_device_status RENAME TO _handler_device_status_v1"
        )
        conn.execute(_CREATE_TABLE_SQL)
        old = conn.execute(
            "SELECT * FROM _handler_device_status_v1 WHERE id = 1"
        ).fetchone()
        if old:
            old = dict(old)
            conn.execute(
                """
                INSERT INTO handler_device_status
                    (device_id, fcm_token, battery_pct, lat, lon,
                     ai_alert, ai_label, ai_score, is_locked, is_online, last_seen, updated_at)
                VALUES ('default', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    old.get("battery_pct"),
                    old.get("lat"),
                    old.get("lon"),
                    old.get("ai_alert", 0),
                    old.get("ai_label"),
                    old.get("ai_score"),
                    old.get("is_locked", 0),
                    old.get("is_online", 0),
                    old.get("last_seen"),
                    old.get("updated_at"),
                ),
            )
        conn.execute("DROP TABLE _handler_device_status_v1")
        conn.commit()
        return

    # Schema is current – nothing to do.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _HandlerWSManager:
    def __init__(self) -> None:
        self._connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, data: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_handler_ws = _HandlerWSManager()


def _verify_admin_creds(creds_b64: str) -> bool:
    """Validate a base64-encoded ``user:pass`` string against admin credentials."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return False
    try:
        # Recalculate correct padding (btoa() output may omit trailing '=' chars)
        rem = len(creds_b64) % 4
        padded = creds_b64 + ("=" * ((4 - rem) % 4))
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
        user, _, pwd = decoded.partition(":")
    except Exception:
        return False
    ok_user = secrets.compare_digest(user.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    ok_pass = secrets.compare_digest(pwd.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    return ok_user and ok_pass


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class DeviceStatusReport(BaseModel):
    device_id: str                      # stable device identifier
    fcm_token: Optional[str] = None     # FCM registration token (stored for targeted pushes)
    battery_pct: Optional[int] = None   # 0–100
    lat: Optional[float] = None
    lon: Optional[float] = None
    ai_alert: Optional[bool] = None
    ai_label: Optional[str] = None
    ai_score: Optional[float] = None


class LockRequest(BaseModel):
    device_id: str


# ---------------------------------------------------------------------------
# Device-facing endpoint
# ---------------------------------------------------------------------------

@router.post("/api/handler/device-status")
async def handler_device_status(
    body: DeviceStatusReport,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Device posts its current state: battery level, GPS coordinates, and AI
    filter alert information.  Protected by the TPE webhook secret so only
    the paired app can reach this endpoint.

    ``device_id`` is a stable identifier chosen by the device (e.g. its Android
    device ID or a UUID generated on first launch).  ``fcm_token`` is optional
    but required for targeted FCM pushes (lock, toy commands).
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    now = _now_iso()
    db.execute(
        """
        INSERT INTO handler_device_status
            (device_id, fcm_token, battery_pct, lat, lon, ai_alert, ai_label, ai_score,
             is_locked, is_online, last_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            fcm_token   = COALESCE(excluded.fcm_token,   fcm_token),
            battery_pct = COALESCE(excluded.battery_pct, battery_pct),
            lat         = COALESCE(excluded.lat,         lat),
            lon         = COALESCE(excluded.lon,         lon),
            ai_alert    = COALESCE(excluded.ai_alert,    ai_alert),
            ai_label    = COALESCE(excluded.ai_label,    ai_label),
            ai_score    = COALESCE(excluded.ai_score,    ai_score),
            is_online   = 1,
            last_seen   = excluded.last_seen,
            updated_at  = excluded.updated_at
        """,
        (
            body.device_id,
            body.fcm_token,
            body.battery_pct,
            body.lat,
            body.lon,
            1 if body.ai_alert else 0,
            body.ai_label,
            body.ai_score,
            now,
            now,
        ),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?", (body.device_id,)
    ).fetchone()
    await _handler_ws.broadcast({"type": "status_update", **dict(row)})
    return {"status": "received"}


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/api/handler/devices")
def handler_list_devices(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """Return a list of all known devices with their current status."""
    rows = db.execute(
        "SELECT device_id, is_online, is_locked, battery_pct, last_seen "
        "FROM handler_device_status ORDER BY last_seen DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/handler/status")
def handler_get_status(
    device_id: Optional[str] = Query(default=None),
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return the latest status snapshot for a specific device (or an empty dict
    when no device_id is supplied — useful for credential validation)."""
    if not device_id:
        return {}
    row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?", (device_id,)
    ).fetchone()
    return dict(row) if row else {}


@router.post("/api/handler/lock")
async def handler_lock(
    body: LockRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Send a LOCK_DEVICE FCM to the specified device and record the locked state
    in the status table.
    """
    row = db.execute(
        "SELECT fcm_token FROM handler_device_status WHERE device_id = ?",
        (body.device_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found.")
    if not row["fcm_token"]:
        raise HTTPException(
            status_code=409,
            detail="Device has no FCM token registered — cannot send lock command.",
        )

    result = _send_fcm_to_token(db, row["fcm_token"], {"action": "LOCK_DEVICE"})

    now = _now_iso()
    db.execute(
        """
        UPDATE handler_device_status
        SET is_locked = 1, updated_at = ?
        WHERE device_id = ?
        """,
        (now, body.device_id),
    )
    db.commit()

    await _handler_ws.broadcast({"type": "lock", "device_id": body.device_id, "is_locked": 1})
    return {"status": "lock_sent", "fcm": result}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws/handler")
async def handler_ws_endpoint(websocket: WebSocket, creds: str = "") -> None:
    """
    Real-time handler-panel feed.

    Admin authenticates by passing ``?creds=<base64(user:pass)>`` as a query
    parameter (browsers cannot set custom headers on WebSocket connections).

    On connect: sends a ``snapshot`` message with all known device statuses.
    While open: the server pushes ``status_update`` and ``lock`` events
    (each including ``device_id``) whenever a device posts new data.  A
    ``ping`` frame is sent every 30 s to keep the connection alive through proxies.
    """
    if not _verify_admin_creds(creds):
        await websocket.close(code=4001)
        return

    db = get_db_connection()
    try:
        await _handler_ws.connect(websocket)
        rows = db.execute("SELECT * FROM handler_device_status").fetchall()
        await websocket.send_json({
            "type": "snapshot",
            "devices": [dict(r) for r in rows],
        })

        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        _handler_ws.disconnect(websocket)
        db.close()
