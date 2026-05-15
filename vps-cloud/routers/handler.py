"""
routers/handler.py – Handler Panel backend.

Provides real-time device status updates and quick-action commands for the
Handler Panel frontend (static/handler.html).

Device-facing (protected by TPE webhook secret):
  POST /api/handler/device-status  – Device reports battery, GPS, AI filter hit.

Handler/Admin-facing (JWT Bearer auth – role 'handler' or 'admin'):
  GET  /api/handler/devices        – List devices (handlers see only assigned ones).
  GET  /api/handler/status         – Latest status snapshot for a specific device.
  POST /api/handler/lock           – Send LOCK_DEVICE FCM to a specific device.

Admin-only (JWT Bearer auth – role 'admin'):
  GET  /api/handler/assignments    – List all handler↔device assignments.
  POST /api/handler/assignments    – Assign a handler to a device.
  DELETE /api/handler/assignments  – Remove a handler↔device assignment.

WebSocket (JWT via query parameter):
  WS   /ws/handler                 – Real-time device status stream + binary audio relay target.

Device audio relay (webhook secret via query parameter):
  WS   /ws/device-audio/{device_id} – Device streams binary audio; relayed to assigned handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import jwt as _jwt
from db import get_db, get_db_connection
from dependencies import SECRET_KEY, ALGORITHM, role_required
from mqtt_client import mqtt_client as _mqtt_client
from routers.tpe import (
    _effective_webhook_secret,
    _send_mqtt_to_device,
    _send_mqtt_to_all,
    TpePushRequest,
    _VALID_TPE_ACTIONS,
    _build_tpe_payload,
)
from routers.ws_manager import handler_ws as _handler_ws

logger = logging.getLogger(__name__)

router = APIRouter(tags=["handler"])


# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------


def _publish_signaling_fallback(device_id: str, payload: dict) -> None:
    """Best-effort MQTT signaling fallback publish for device signaling topics."""
    _mqtt_client.publish_json(
        _mqtt_client.topic_for_device_signaling(device_id),
        {"device_id": device_id, **payload},
        qos=1,
    )

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


def _verify_ws_token(token: str) -> Optional[dict]:
    """Validate a JWT token from a WebSocket query parameter.

    Returns the decoded payload dict on success, or None if the token is
    missing, expired, or invalid.  Only tokens with role 'admin' or 'handler'
    are accepted.
    """
    if not token:
        return None
    try:
        payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        role = payload.get("role", "handler")
        if role not in ("admin", "handler"):
            return None
        return payload
    except (_jwt.ExpiredSignatureError, _jwt.InvalidTokenError):
        return None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class DeviceStatusReport(BaseModel):
    device_id: str                      # stable device identifier (non-empty; e.g. UUID or Android device ID)
    fcm_token: Optional[str] = None     # FCM registration token (stored for targeted pushes; append-only)
    battery_pct: Optional[int] = None   # 0–100
    lat: Optional[float] = None
    lon: Optional[float] = None
    ai_alert: Optional[bool] = None
    ai_label: Optional[str] = None
    ai_score: Optional[float] = None


class LockRequest(BaseModel):
    device_id: str


class AssignmentRequest(BaseModel):
    handler_id: str
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

    if not body.device_id.strip():
        raise HTTPException(status_code=400, detail="device_id must not be empty")

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
# Handler/Admin panel endpoints  (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------

def _handler_allowed_devices(db: sqlite3.Connection, handler_id: str) -> list[str]:
    """Return the list of device_ids assigned to a handler user."""
    rows = db.execute(
        "SELECT device_id FROM handler_device_assignments WHERE handler_id = ?",
        (handler_id,),
    ).fetchall()
    return [r["device_id"] for r in rows]


def _fetch_devices_by_ids(
    db: sqlite3.Connection, device_ids: list[str], full: bool = False
) -> list[sqlite3.Row]:
    """Query handler_device_status for the given *device_ids*.

    *full* selects all columns; otherwise only summary columns are returned.
    Returns an empty list when *device_ids* is empty.
    """
    if not device_ids:
        return []
    placeholders = ",".join("?" * len(device_ids))
    cols = "*" if full else "device_id, is_online, is_locked, battery_pct, last_seen"
    return db.execute(
        f"SELECT {cols} FROM handler_device_status "
        f"WHERE device_id IN ({placeholders}) ORDER BY last_seen DESC",
        device_ids,
    ).fetchall()


@router.get("/api/handler/devices")
def handler_list_devices(
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """Return a list of devices with their current status.

    Admins receive all devices; handlers receive only their assigned ones.
    """
    if current_user["role"] == "admin":
        rows = db.execute(
            "SELECT device_id, is_online, is_locked, battery_pct, last_seen "
            "FROM handler_device_status ORDER BY last_seen DESC"
        ).fetchall()
    else:
        assigned = _handler_allowed_devices(db, current_user["user_id"])
        rows = _fetch_devices_by_ids(db, assigned)
    return [dict(r) for r in rows]


@router.get("/api/handler/status")
def handler_get_status(
    device_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return the latest status snapshot for a specific device.

    Returns an empty dict when no device_id is supplied (useful for token
    validation by the frontend).  Handlers may only query assigned devices.
    """
    if not device_id:
        return {}

    if current_user["role"] != "admin":
        assigned = _handler_allowed_devices(db, current_user["user_id"])
        if device_id not in assigned:
            raise HTTPException(status_code=403, detail="Access denied to this device.")

    row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?", (device_id,)
    ).fetchone()
    return dict(row) if row else {}


@router.post("/api/handler/lock")
async def handler_lock(
    body: LockRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Send a LOCK_DEVICE FCM to the specified device and record the locked state.

    Handlers may only lock devices they are assigned to.
    """
    if current_user["role"] != "admin":
        assigned = _handler_allowed_devices(db, current_user["user_id"])
        if body.device_id not in assigned:
            raise HTTPException(status_code=403, detail="Access denied to this device.")

    row = db.execute(
        "SELECT device_id FROM handler_device_status WHERE device_id = ?",
        (body.device_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found.")
    result = _send_mqtt_to_device(db, body.device_id, {"action": "LOCK_DEVICE"})

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
    return {"status": "lock_sent", "mqtt": result}


# ---------------------------------------------------------------------------
# Admin-only assignment management
# ---------------------------------------------------------------------------

@router.get("/api/handler/assignments")
def handler_list_assignments(
    current_user: dict = Depends(role_required("admin")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """Return all handler↔device assignments (admin only)."""
    rows = db.execute(
        "SELECT handler_id, device_id FROM handler_device_assignments ORDER BY handler_id, device_id"
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/handler/assignments", status_code=201)
def handler_create_assignment(
    body: AssignmentRequest,
    current_user: dict = Depends(role_required("admin")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Assign a handler user to a device (admin only)."""
    try:
        db.execute(
            "INSERT INTO handler_device_assignments (handler_id, device_id) VALUES (?, ?)",
            (body.handler_id, body.device_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Assignment already exists.")
    return {"handler_id": body.handler_id, "device_id": body.device_id}


@router.delete("/api/handler/assignments")
def handler_delete_assignment(
    handler_id: str,
    device_id: str,
    current_user: dict = Depends(role_required("admin")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Remove a handler↔device assignment (admin only)."""
    result = db.execute(
        "DELETE FROM handler_device_assignments WHERE handler_id = ? AND device_id = ?",
        (handler_id, device_id),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Handler-level TPE (FCM) endpoints
# Mirrors a subset of /api/admin/tpe/* but secured with JWT Bearer auth so
# that handler-role users ("guest controllers") can use the handler panel.
# ---------------------------------------------------------------------------


class _CheckinRequestBody(BaseModel):
    device_id: Optional[str] = None


@router.post("/api/handler/tpe/push")
def handler_tpe_push(
    body: TpePushRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Push an FCM command to a specific device via JWT-authenticated handler panel.

    Handlers may only push to devices they are assigned to.
    Admins may push to any device.  A ``device_id`` is always required.
    """
    if body.action not in _VALID_TPE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{body.action}'.",
        )
    if not body.device_id:
        raise HTTPException(status_code=400, detail="device_id is required.")

    if current_user["role"] != "admin":
        assigned = _handler_allowed_devices(db, current_user["user_id"])
        if body.device_id not in assigned:
            raise HTTPException(status_code=403, detail="Access denied to this device.")

    return _send_mqtt_to_device(db, body.device_id, _build_tpe_payload(body))


@router.post("/api/handler/tpe/checkins/request")
def handler_tpe_checkins_request(
    body: _CheckinRequestBody,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Push a REQUEST_CHECKIN FCM to a device via JWT-authenticated handler panel."""
    if body.device_id:
        if current_user["role"] != "admin":
            assigned = _handler_allowed_devices(db, current_user["user_id"])
            if body.device_id not in assigned:
                raise HTTPException(status_code=403, detail="Access denied to this device.")
        return _send_mqtt_to_device(db, body.device_id, {"action": "REQUEST_CHECKIN"})
    return _send_mqtt_to_all(db, {"action": "REQUEST_CHECKIN"})


@router.get("/api/handler/tpe/events")
def handler_tpe_events(
    limit: int = 100,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """List the most recent TPE device events (JWT-authenticated handler panel)."""
    rows = db.execute(
        "SELECT id, event, reason, session_ts, payload_json, received_at "
        "FROM tpe_events ORDER BY id DESC LIMIT ?",
        (min(limit, 500),),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/handler/tpe/audits")
def handler_tpe_audits(
    limit: int = 50,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """List the most recent adherence audit records (JWT-authenticated handler panel)."""
    rows = db.execute(
        "SELECT id, detection_ratio, last_label, last_score, session_ts, video_filename, received_at "
        "FROM tpe_audit_logs ORDER BY id DESC LIMIT ?",
        (min(limit, 200),),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws/handler")
async def handler_ws_endpoint(websocket: WebSocket, token: str = "") -> None:
    """
    Real-time handler-panel feed.

    Authenticate by passing ``?token=<jwt>`` as a query parameter (browsers
    cannot set custom headers on WebSocket connections).  The token must have
    role 'admin' or 'handler'.

    On connect: sends a ``snapshot`` message with relevant device statuses.
    - Admins receive all devices.
    - Handlers receive only their assigned devices.

    While open: the server pushes ``status_update`` and ``lock`` events
    (each including ``device_id``) whenever a device posts new data, and
    forwards binary audio chunks relayed from assigned devices.  A ``ping``
    frame is sent every 30 s to keep the connection alive through proxies.
    """
    payload = _verify_ws_token(token)
    if payload is None:
        await websocket.close(code=4001)
        return

    role = payload.get("role", "handler")
    user_id = payload.get("sub")

    db = get_db_connection()
    try:
        # Register by user_id so the audio relay can target this socket.
        await _handler_ws.connect(websocket, user_id=user_id)

        if role == "admin":
            rows = db.execute("SELECT * FROM handler_device_status").fetchall()
        else:
            assigned = [
                r["device_id"]
                for r in db.execute(
                    "SELECT device_id FROM handler_device_assignments WHERE handler_id = ?",
                    (user_id,),
                ).fetchall()
            ]
            rows = _fetch_devices_by_ids(db, assigned, full=True)

        await websocket.send_json({
            "type": "snapshot",
            "devices": [dict(r) for r in rows],
        })

        # Keep-alive: send a ping every 30 s from a background task so that
        # the main receive loop is never interrupted by a timeout-cancel, which
        # can leave the WebSocket in an inconsistent state.
        async def _ping_loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

        ping_task = asyncio.create_task(_ping_loop())
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break
                # Handle mic-control commands sent from the handler panel.
                # The panel sends:
                #   {"action": "mic_start", "device_id": "..."}
                #   {"action": "mic_stop",  "device_id": "..."}  (omit device_id to broadcast)
                if msg_type == "websocket.receive":
                    text = msg.get("text")
                    if text:
                        try:
                            cmd = json.loads(text)
                            action = cmd.get("action", "")
                            target_device = cmd.get("device_id") or None
                            if action == "mic_start":
                                await _handler_ws.send_mic_command("START_HOT_MIC", device_id=target_device)
                            elif action == "mic_stop":
                                await _handler_ws.send_mic_command("STOP_HOT_MIC", device_id=target_device)
                            elif action == "webrtc_offer" and target_device:
                                # Route SDP offer from handler to the target device verbatim.
                                sdp = cmd.get("sdp")
                                if sdp is not None:
                                    await _handler_ws.relay_signal_to_device(
                                        target_device,
                                        {"type": "webrtc_offer", "sdp": sdp},
                                    )
                                    _publish_signaling_fallback(target_device, {"type": "webrtc_offer", "sdp": sdp})
                            elif action == "webrtc_ice_candidate" and target_device:
                                # Route ICE candidate from handler to the target device verbatim.
                                candidate = cmd.get("candidate")
                                if candidate is not None:
                                    await _handler_ws.relay_signal_to_device(
                                        target_device,
                                        {"type": "webrtc_ice_candidate", "candidate": candidate},
                                    )
                                    _publish_signaling_fallback(
                                        target_device,
                                        {"type": "webrtc_ice_candidate", "candidate": candidate},
                                    )
                        except Exception:
                            pass  # Ignore malformed frames.
        finally:
            ping_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        _handler_ws.disconnect(websocket, user_id=user_id)
        db.close()


@router.websocket("/ws/device-audio/{device_id}")
async def device_audio_ws_endpoint(
    websocket: WebSocket,
    device_id: str,
    secret: str = "",
) -> None:
    """
    Binary audio relay endpoint for field devices.

    A device authenticates by passing ``?secret=<webhook_secret>`` as a query
    parameter (matching the TPE webhook secret configured for this server).
    Once connected it may stream raw binary audio frames of any size; each
    frame is immediately forwarded to the WebSocket of the Handler currently
    assigned to *device_id* in the ``handler_device_assignments`` table.

    - If no handler is assigned, or the assigned handler is not connected,
      chunks are silently dropped (the device keeps streaming without error).
    - The connection is closed (code 4001) when the secret is invalid.
    - The connection is closed (code 4004) when *device_id* is empty.
    """
    if not device_id.strip():
        await websocket.accept()
        await websocket.close(code=4004)
        return

    db = get_db_connection()
    try:
        expected = _effective_webhook_secret(db)
        if expected and not secrets.compare_digest(secret, expected):
            await websocket.close(code=4001)
            return

        await websocket.accept()

        _handler_ws.connect_signaling_device(device_id, websocket)
        try:
            while True:
                try:
                    msg = await websocket.receive()
                except WebSocketDisconnect:
                    break

                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break

                if msg_type == "websocket.receive":
                    chunk = msg.get("bytes")
                    if chunk:
                        await _handler_ws.relay_audio(device_id, chunk, db)
                    else:
                        # Handle WebRTC signaling messages (text frames) from device.
                        text = msg.get("text")
                        if text:
                            try:
                                sig = json.loads(text)
                                sig_type = sig.get("type", "")
                                if sig_type in ("webrtc_answer", "webrtc_ice_candidate"):
                                    await _handler_ws.relay_signal_to_handler(device_id, sig, db)
                                    _publish_signaling_fallback(device_id, sig)
                            except Exception:
                                pass  # Ignore malformed frames.
        finally:
            _handler_ws.disconnect_signaling_device(device_id, websocket)
    finally:
        db.close()
