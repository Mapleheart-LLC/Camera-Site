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
  DELETE /api/handler/devices/{device_id} – Delete device records (admin only).

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
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import AliasChoices, BaseModel, Field

import jwt as _jwt
from db import get_db, get_db_connection, get_setting, set_setting
from dependencies import SECRET_KEY, ALGORITHM, role_required
from mqtt_client import mqtt_client as _mqtt_client
from routers.tpe import (
    _effective_webhook_secret,
    _send_mqtt_to_device,
    _send_mqtt_to_all,
    TpePushRequest,
    _VALID_TPE_ACTIONS,
    _build_tpe_payload,
    _render_tpe_pairing_qr_png,
)
from routers.ws_manager import handler_ws as _handler_ws

logger = logging.getLogger(__name__)

router = APIRouter(tags=["handler"])


# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------


def _publish_signaling_fallback(device_id: str, payload: dict) -> None:
    """Best-effort MQTT signaling fallback publish for device signaling topics."""
    published = _mqtt_client.publish_json(
        _mqtt_client.topic_for_device_signaling(device_id),
        {"device_id": device_id, **payload},
        qos=1,
    )
    if not published:
        logger.debug("MQTT signaling fallback publish failed for device %s", device_id)

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


def _ensure_limbo_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS limbo_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_text      TEXT NOT NULL,
            source           TEXT NOT NULL DEFAULT 'handler',
            status           TEXT NOT NULL DEFAULT 'pending',
            answer_text      TEXT,
            dismissed_reason TEXT,
            created_at       TEXT NOT NULL,
            answered_at      TEXT,
            answered_by      TEXT,
            publication_tier TEXT NOT NULL DEFAULT 'sensitive',
            public_allowed   INTEGER NOT NULL DEFAULT 0,
            published_at     TEXT,
            published_question_id TEXT
        )
        """
    )


def _ensure_limbo_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(limbo_items)").fetchall()}
    if "publication_tier" not in cols:
        conn.execute("ALTER TABLE limbo_items ADD COLUMN publication_tier TEXT NOT NULL DEFAULT 'sensitive'")
    if "public_allowed" not in cols:
        conn.execute("ALTER TABLE limbo_items ADD COLUMN public_allowed INTEGER NOT NULL DEFAULT 0")
    if "published_at" not in cols:
        conn.execute("ALTER TABLE limbo_items ADD COLUMN published_at TEXT")
    if "published_question_id" not in cols:
        conn.execute("ALTER TABLE limbo_items ADD COLUMN published_question_id TEXT")


def _ensure_booking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_intake (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_handle      TEXT NOT NULL,
            availability_window TEXT NOT NULL,
            session_intent      TEXT NOT NULL,
            location_text       TEXT NOT NULL,
            source              TEXT NOT NULL DEFAULT 'public',
            priority            TEXT NOT NULL DEFAULT 'normal',
            status              TEXT NOT NULL DEFAULT 'new',
            notes               TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            resolved_at         TEXT
        )
        """
    )


def _ensure_puppy_mail_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS puppy_mail_threads (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_name    TEXT,
            sender_contact TEXT,
            source         TEXT NOT NULL DEFAULT 'web',
            status         TEXT NOT NULL DEFAULT 'open',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS puppy_mail_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id       INTEGER NOT NULL,
            author          TEXT NOT NULL,
            body            TEXT NOT NULL,
            delivery_status TEXT,
            created_at      TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES puppy_mail_threads(id)
        )
        """
    )


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
        _ensure_limbo_table(conn)
        _ensure_limbo_columns(conn)
        _ensure_booking_table(conn)
        _ensure_puppy_mail_tables(conn)
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
        _ensure_limbo_table(conn)
        _ensure_limbo_columns(conn)
        _ensure_booking_table(conn)
        _ensure_puppy_mail_tables(conn)
        conn.commit()
        return

    # Schema is current – ensure auxiliary tables exist.
    _ensure_limbo_table(conn)
    _ensure_limbo_columns(conn)
    _ensure_booking_table(conn)
    _ensure_puppy_mail_tables(conn)
    conn.commit()


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
    # Device identifier in request body; optional here because endpoint also accepts X-Device-ID header fallback.
    device_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("device_id", "deviceId"),
    )
    fcm_token: Optional[str] = None     # FCM registration token (stored for targeted pushes; append-only)
    battery_pct: Optional[int] = Field( # 0–100
        default=None,
        validation_alias=AliasChoices(
            "battery_pct",
            "battery",
            "battery_level",
            "batteryPercent",
            "battery_percentage",
        ),
    )
    lat: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("lat", "latitude"),
    )
    lon: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("lon", "lng", "longitude"),
    )
    ai_alert: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("ai_alert", "aiAlert", "ai_filter_hit", "alert"),
    )
    ai_label: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ai_label", "aiLabel", "label"),
    )
    ai_score: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("ai_score", "aiScore", "score"),
    )


class LockRequest(BaseModel):
    device_id: str


class AssignmentRequest(BaseModel):
    handler_id: str
    device_id: str


class HandlerAnswerPayload(BaseModel):
    answer: str


class PublicStatusUpdateRequest(BaseModel):
    days_caged_start_date: Optional[str] = None
    days_caged_paused: Optional[bool] = None
    days_caged_accumulated_days: Optional[int] = None
    days_locked_goal_days: Optional[int] = None
    current_status_mode: Optional[str] = None
    tasks_completed: Optional[int] = None
    confessions_posted: Optional[int] = None


class LimboCreateRequest(BaseModel):
    prompt_text: str
    source: Optional[str] = "handler"


class LimboAnswerRequest(BaseModel):
    answer_text: str


class LimboDismissRequest(BaseModel):
    reason: Optional[str] = ""


class LimboGovernanceUpdateRequest(BaseModel):
    publication_tier: Optional[str] = None
    public_allowed: Optional[bool] = None


class BookingCreateRequest(BaseModel):
    contact_handle: str
    availability_window: str
    session_intent: str
    location_text: str
    source: Optional[str] = "public"


class BookingStatusUpdateRequest(BaseModel):
    status: str
    priority: Optional[str] = None
    notes: Optional[str] = None


class PuppyMailCreateRequest(BaseModel):
    sender_name: Optional[str] = None
    sender_contact: Optional[str] = None
    message: str
    source: Optional[str] = "web"


class PuppyMailReplyRequest(BaseModel):
    body: str
    author: Optional[str] = "m0chii's Handler"


class PuppyMailStatusUpdateRequest(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Device-facing endpoint
# ---------------------------------------------------------------------------

@router.post("/api/handler/device-status")
async def handler_device_status(
    body: DeviceStatusReport,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
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
            logger.warning(
                "Rejected /api/handler/device-status from %s: invalid webhook secret (device_id=%r)",
                (request.client.host if request and request.client else "unknown"),
                (body.device_id or x_device_id),
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "Invalid webhook secret. "
                    "Send Authorization: Bearer <tpe_webhook_secret>."
                ),
            )

    body_device_id = (body.device_id or "").strip()
    header_device_id = (x_device_id or "").strip()
    resolved_device_id = body_device_id or header_device_id
    if not resolved_device_id:
        logger.warning(
            "Rejected /api/handler/device-status from %s: missing device_id in body/header",
            (request.client.host if request and request.client else "unknown"),
        )
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
            resolved_device_id,
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
        "SELECT * FROM handler_device_status WHERE device_id = ?", (resolved_device_id,)
    ).fetchone()
    await _handler_ws.broadcast({"type": "status_update", **dict(row)})
    return {"status": "received", "device_id": resolved_device_id}


# ---------------------------------------------------------------------------
# Public intake endpoints (booking + puppy mail)
# ---------------------------------------------------------------------------


@router.post("/api/booking", status_code=201)
def create_booking_intake(
    payload: BookingCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Public booking intake endpoint used by retained public surfaces."""
    contact_handle = (payload.contact_handle or "").strip()
    availability_window = (payload.availability_window or "").strip()
    session_intent = (payload.session_intent or "").strip()
    location_text = (payload.location_text or "").strip()
    source = (payload.source or "public").strip() or "public"

    if not contact_handle:
        raise HTTPException(status_code=400, detail="contact_handle is required")
    if not availability_window:
        raise HTTPException(status_code=400, detail="availability_window is required")
    if not session_intent:
        raise HTTPException(status_code=400, detail="session_intent is required")
    if not location_text:
        raise HTTPException(status_code=400, detail="location_text is required")

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO booking_intake
            (contact_handle, availability_window, session_intent, location_text,
             source, priority, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'normal', 'new', '', ?, ?)
        """,
        (
            contact_handle,
            availability_window,
            session_intent,
            location_text,
            source,
            now,
            now,
        ),
    )
    db.commit()
    return {"id": cur.lastrowid, "status": "new"}


@router.post("/api/puppy-mail", status_code=201)
def create_puppy_mail_thread(
    payload: PuppyMailCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Public puppy-mail intake endpoint for message widget/page submissions."""
    body = (payload.message or "").strip()
    sender_name = (payload.sender_name or "").strip() or "Anonymous"
    sender_contact = (payload.sender_contact or "").strip()
    source = (payload.source or "web").strip() or "web"
    if not body:
        raise HTTPException(status_code=400, detail="message is required")

    now = _now_iso()
    thread_cur = db.execute(
        """
        INSERT INTO puppy_mail_threads
            (sender_name, sender_contact, source, status, created_at, updated_at)
        VALUES (?, ?, ?, 'open', ?, ?)
        """,
        (sender_name, sender_contact, source, now, now),
    )
    thread_id = int(thread_cur.lastrowid)
    db.execute(
        """
        INSERT INTO puppy_mail_messages
            (thread_id, author, body, delivery_status, created_at)
        VALUES (?, ?, ?, 'received', ?)
        """,
        (thread_id, sender_name, body, now),
    )
    db.commit()
    return {"thread_id": thread_id, "status": "open"}


# ---------------------------------------------------------------------------
# Handler/Admin panel endpoints  (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------

def _handler_allowed_devices(db: sqlite3.Connection, handler_id: str) -> list[str]:
    """Return device_ids assigned to a handler by id or legacy username key."""
    username_row = db.execute(
        "SELECT username FROM users WHERE id = ? LIMIT 1",
        (handler_id,),
    ).fetchone()
    username = username_row["username"] if username_row else handler_id
    rows = db.execute(
        "SELECT device_id FROM handler_device_assignments WHERE handler_id IN (?, ?)",
        (handler_id, username),
    ).fetchall()
    return [r["device_id"] for r in rows]


def _normalize_handler_assignment_id(db: sqlite3.Connection, raw_handler_id: str) -> str:
    """Resolve assignment input to canonical user_id (accepts user_id or username)."""
    candidate = (raw_handler_id or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="handler_id (user ID or username) is required.")

    by_id = db.execute(
        "SELECT id FROM users WHERE id = ? LIMIT 1",
        (candidate,),
    ).fetchone()
    if by_id:
        return by_id["id"]

    by_username = db.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE LIMIT 1",
        (candidate,),
    ).fetchone()
    if by_username:
        return by_username["id"]

    raise HTTPException(
        status_code=404,
        detail="Handler user not found for the provided ID or username.",
    )


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
    response: Response,
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
        known_total = db.execute(
            "SELECT COUNT(*) AS n FROM handler_device_status"
        ).fetchone()["n"]
        if known_total > 0 and len(rows) == 0:
            response.headers["X-Handler-Devices-Notice"] = "no-assignment"
            response.headers["X-Handler-Devices-Known-Total"] = str(known_total)
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
            raise HTTPException(
                status_code=403,
                detail="Access denied to this device. Ask an admin to assign this device to your handler account.",
            )

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


@router.delete("/api/handler/devices/{device_id}")
async def handler_delete_device(
    device_id: str,
    _current_user: dict = Depends(role_required("admin")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Delete a device and linked handler/pairing rows (admin only)."""
    row = db.execute(
        "SELECT device_id, fcm_token FROM handler_device_status WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found.")

    fcm_token = row["fcm_token"]

    db.execute("DELETE FROM handler_device_assignments WHERE device_id = ?", (device_id,))
    db.execute("DELETE FROM handler_device_status WHERE device_id = ?", (device_id,))
    # Pairing rows are keyed by device_id; also remove any legacy row keyed by
    # status fcm_token.
    pairing_keys = {device_id}
    if fcm_token:
        pairing_keys.add(fcm_token)
    db.executemany(
        "DELETE FROM tpe_paired_devices WHERE fcm_token = ?",
        [(key,) for key in pairing_keys],
    )
    db.commit()

    await _handler_ws.broadcast({"type": "device_deleted", "device_id": device_id})
    return {"deleted": True, "device_id": device_id}


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
    resolved_handler_id = _normalize_handler_assignment_id(db, body.handler_id)
    try:
        db.execute(
            "INSERT INTO handler_device_assignments (handler_id, device_id) VALUES (?, ?)",
            (resolved_handler_id, body.device_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Assignment already exists.")
    return {"handler_id": resolved_handler_id, "device_id": body.device_id}


@router.delete("/api/handler/assignments")
def handler_delete_assignment(
    handler_id: str,
    device_id: str,
    current_user: dict = Depends(role_required("admin")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Remove a handler↔device assignment (admin only)."""
    resolved_handler_id = _normalize_handler_assignment_id(db, handler_id)
    username_row = db.execute(
        "SELECT username FROM users WHERE id = ? LIMIT 1",
        (resolved_handler_id,),
    ).fetchone()
    username = username_row["username"] if username_row else resolved_handler_id
    delete_result = db.execute(
        """
        DELETE FROM handler_device_assignments
        WHERE device_id = ? AND handler_id IN (?, ?)
        """,
        (device_id, resolved_handler_id, username),
    )
    db.commit()
    if delete_result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Booking + Puppy Mail queue APIs (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------


@router.get("/api/handler/booking")
def handler_list_booking_queue(
    status_filter: str = Query(default="all", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    allowed = {"all", "new", "qualified", "scheduled", "done"}
    sf = (status_filter or "all").strip().lower()
    if sf not in allowed:
        raise HTTPException(status_code=400, detail="Invalid booking status filter")
    if sf == "all":
        rows = db.execute(
            """
            SELECT id, contact_handle, availability_window, session_intent, location_text,
                   source, priority, status, notes, created_at, updated_at, resolved_at
            FROM booking_intake
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT id, contact_handle, availability_window, session_intent, location_text,
                   source, priority, status, notes, created_at, updated_at, resolved_at
            FROM booking_intake
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sf, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/handler/booking/{booking_id}/status")
def handler_update_booking_status(
    booking_id: int,
    payload: BookingStatusUpdateRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute("SELECT id FROM booking_intake WHERE id = ?", (booking_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Booking item not found")

    status_value = (payload.status or "").strip().lower()
    if status_value not in {"new", "qualified", "scheduled", "done"}:
        raise HTTPException(status_code=400, detail="status must be new, qualified, scheduled, or done")
    priority_value = None
    if payload.priority is not None:
        priority_value = payload.priority.strip().lower()
        if priority_value not in {"low", "normal", "high"}:
            raise HTTPException(status_code=400, detail="priority must be low, normal, or high")

    updates = ["status = ?", "updated_at = ?"]
    values: list = [status_value, _now_iso()]
    if priority_value is not None:
        updates.append("priority = ?")
        values.append(priority_value)
    if payload.notes is not None:
        updates.append("notes = ?")
        values.append(payload.notes.strip())
    if status_value == "done":
        updates.append("resolved_at = ?")
        values.append(_now_iso())
    values.append(booking_id)

    db.execute(f"UPDATE booking_intake SET {', '.join(updates)} WHERE id = ?", values)
    db.commit()
    return {"updated": True, "id": booking_id, "status": status_value}


@router.get("/api/handler/puppy-mail/threads")
def handler_list_puppy_mail_threads(
    status_filter: str = Query(default="all", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    allowed = {"all", "open", "resolved"}
    sf = (status_filter or "all").strip().lower()
    if sf not in allowed:
        raise HTTPException(status_code=400, detail="Invalid puppy-mail status filter")

    where_clause = ""
    params: list = []
    if sf != "all":
        where_clause = "WHERE t.status = ?"
        params.append(sf)
    params.append(limit)

    rows = db.execute(
        f"""
        SELECT t.id, t.sender_name, t.sender_contact, t.source, t.status,
               t.created_at, t.updated_at,
               (
                 SELECT m.body
                 FROM puppy_mail_messages m
                 WHERE m.thread_id = t.id
                 ORDER BY m.id DESC
                 LIMIT 1
               ) AS latest_message,
               (
                 SELECT m.created_at
                 FROM puppy_mail_messages m
                 WHERE m.thread_id = t.id
                 ORDER BY m.id DESC
                 LIMIT 1
               ) AS latest_message_at,
               (
                 SELECT COUNT(*)
                 FROM puppy_mail_messages m
                 WHERE m.thread_id = t.id
               ) AS message_count
        FROM puppy_mail_threads t
        {where_clause}
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/handler/puppy-mail/threads/{thread_id}")
def handler_get_puppy_mail_thread(
    thread_id: int,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    thread = db.execute(
        """
        SELECT id, sender_name, sender_contact, source, status, created_at, updated_at
        FROM puppy_mail_threads
        WHERE id = ?
        """,
        (thread_id,),
    ).fetchone()
    if not thread:
        raise HTTPException(status_code=404, detail="Puppy-mail thread not found")

    messages = db.execute(
        """
        SELECT id, thread_id, author, body, delivery_status, created_at
        FROM puppy_mail_messages
        WHERE thread_id = ?
        ORDER BY id ASC
        """,
        (thread_id,),
    ).fetchall()
    return {"thread": dict(thread), "messages": [dict(m) for m in messages]}


@router.post("/api/handler/puppy-mail/threads/{thread_id}/reply")
def handler_reply_puppy_mail_thread(
    thread_id: int,
    payload: PuppyMailReplyRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute("SELECT id FROM puppy_mail_threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Puppy-mail thread not found")

    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="body is required")
    author = (payload.author or "m0chii's Handler").strip() or "m0chii's Handler"
    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO puppy_mail_messages
            (thread_id, author, body, delivery_status, created_at)
        VALUES (?, ?, ?, 'sent', ?)
        """,
        (thread_id, author, body, now),
    )
    db.execute(
        "UPDATE puppy_mail_threads SET updated_at = ?, status = 'open' WHERE id = ?",
        (now, thread_id),
    )
    db.commit()
    return {"id": cur.lastrowid, "thread_id": thread_id, "author": author, "created_at": now}


@router.post("/api/handler/puppy-mail/threads/{thread_id}/status")
def handler_update_puppy_mail_thread_status(
    thread_id: int,
    payload: PuppyMailStatusUpdateRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute("SELECT id FROM puppy_mail_threads WHERE id = ?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Puppy-mail thread not found")
    status_value = (payload.status or "").strip().lower()
    if status_value not in {"open", "resolved"}:
        raise HTTPException(status_code=400, detail="status must be open or resolved")
    now = _now_iso()
    db.execute(
        "UPDATE puppy_mail_threads SET status = ?, updated_at = ? WHERE id = ?",
        (status_value, now, thread_id),
    )
    db.commit()
    return {"updated": True, "id": thread_id, "status": status_value}


# ---------------------------------------------------------------------------
# Handler-native Questions APIs (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------


@router.get("/api/handler/questions")
def handler_list_unanswered_questions(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """Return unanswered questions for handler workflows."""
    rows = db.execute(
        """
        SELECT id, text, created_at
        FROM questions
        WHERE answer IS NULL
        ORDER BY created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/api/handler/questions/answered")
def handler_list_answered_questions(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """Return answered questions for handler review/history UI."""
    rows = db.execute(
        """
        SELECT id, text, answer, is_public, created_at
        FROM questions
        WHERE answer IS NOT NULL
        ORDER BY created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/api/handler/questions/{question_id}/answer")
def handler_answer_question(
    question_id: str,
    payload: HandlerAnswerPayload,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Answer a question and publish it immediately (always-public policy)."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Question not found.")

    db.execute(
        "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ?",
        (payload.answer, question_id),
    )
    db.commit()
    return {
        "id": question_id,
        "message": "Answer saved and question is now public.",
    }


@router.delete("/api/handler/questions/{question_id}", status_code=204)
def handler_delete_question(
    question_id: str,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> None:
    """Delete a question (handler/admin)."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Question not found.")
    db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Public status settings (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------


def _safe_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _safe_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() == "true"


@router.get("/api/handler/public-status")
def handler_get_public_status(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return editable public status settings used by public pages."""
    return {
        "days_caged_start_date": get_setting(db, "days_caged_start_date", ""),
        "days_caged_paused": _safe_bool(get_setting(db, "days_caged_paused", "false")),
        "days_caged_accumulated_days": _safe_int(get_setting(db, "days_caged_accumulated_days", "0")),
        "days_locked_goal_days": _safe_int(get_setting(db, "days_locked_goal_days", "0")),
        "current_status_mode": get_setting(db, "current_status_mode", ""),
        "tasks_completed": _safe_int(get_setting(db, "public_tasks_completed", "0")),
        "confessions_posted": _safe_int(get_setting(db, "public_confessions_posted", "0")),
    }


@router.post("/api/handler/public-status")
def handler_update_public_status(
    payload: PublicStatusUpdateRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Update public status/counter settings consumed by the public UI."""
    if payload.days_caged_start_date is not None:
        set_setting(db, "days_caged_start_date", payload.days_caged_start_date.strip())
    if payload.days_caged_paused is not None:
        set_setting(db, "days_caged_paused", "true" if payload.days_caged_paused else "false")
    if payload.days_caged_accumulated_days is not None:
        if payload.days_caged_accumulated_days < 0:
            raise HTTPException(status_code=400, detail="days_caged_accumulated_days must be >= 0")
        set_setting(db, "days_caged_accumulated_days", str(payload.days_caged_accumulated_days))
    if payload.days_locked_goal_days is not None:
        if payload.days_locked_goal_days < 0:
            raise HTTPException(status_code=400, detail="days_locked_goal_days must be >= 0")
        set_setting(db, "days_locked_goal_days", str(payload.days_locked_goal_days))
    if payload.current_status_mode is not None:
        set_setting(db, "current_status_mode", payload.current_status_mode.strip())
    if payload.tasks_completed is not None:
        if payload.tasks_completed < 0:
            raise HTTPException(status_code=400, detail="tasks_completed must be >= 0")
        set_setting(db, "public_tasks_completed", str(payload.tasks_completed))
    if payload.confessions_posted is not None:
        if payload.confessions_posted < 0:
            raise HTTPException(status_code=400, detail="confessions_posted must be >= 0")
        set_setting(db, "public_confessions_posted", str(payload.confessions_posted))

    return {"updated": True}


# ---------------------------------------------------------------------------
# Limbo queue (JWT Bearer, role 'handler' or 'admin')
# ---------------------------------------------------------------------------


@router.get("/api/handler/limbo")
def handler_list_limbo_items(
    status_filter: str = Query(default="pending", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    """List limbo queue items for handler workflow."""
    allowed_statuses = {"pending", "answered", "dismissed", "all"}
    sf = (status_filter or "pending").strip().lower()
    if sf not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid limbo status filter")

    if sf == "all":
        rows = db.execute(
            """
             SELECT id, prompt_text, source, status, answer_text, dismissed_reason,
                 created_at, answered_at, answered_by,
                 publication_tier, public_allowed, published_at, published_question_id
            FROM limbo_items
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = db.execute(
            """
             SELECT id, prompt_text, source, status, answer_text, dismissed_reason,
                 created_at, answered_at, answered_by,
                 publication_tier, public_allowed, published_at, published_question_id
            FROM limbo_items
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (sf, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/handler/limbo", status_code=201)
def handler_create_limbo_item(
    payload: LimboCreateRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Create a new pending limbo prompt."""
    prompt = (payload.prompt_text or "").strip()
    source = (payload.source or "handler").strip() or "handler"
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt_text is required")

    created_at = _now_iso()
    cur = db.execute(
        """
        INSERT INTO limbo_items (prompt_text, source, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (prompt, source, created_at),
    )
    db.commit()
    return {
        "id": cur.lastrowid,
        "prompt_text": prompt,
        "source": source,
        "status": "pending",
        "created_at": created_at,
    }


@router.post("/api/handler/limbo/{item_id}/answer")
def handler_answer_limbo_item(
    item_id: int,
    payload: LimboAnswerRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Resolve a limbo prompt by answering it."""
    answer = (payload.answer_text or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer_text is required")

    row = db.execute(
        "SELECT id FROM limbo_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Limbo item not found")

    answered_at = _now_iso()
    db.execute(
        """
        UPDATE limbo_items
        SET status = 'answered', answer_text = ?, dismissed_reason = NULL,
            answered_at = ?, answered_by = ?
        WHERE id = ?
        """,
        (answer, answered_at, current_user.get("user_id", ""), item_id),
    )
    db.commit()
    return {"id": item_id, "status": "answered", "answered_at": answered_at}


@router.post("/api/handler/limbo/{item_id}/dismiss")
def handler_dismiss_limbo_item(
    item_id: int,
    payload: LimboDismissRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Dismiss a limbo prompt without answering."""
    row = db.execute(
        "SELECT id FROM limbo_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Limbo item not found")

    answered_at = _now_iso()
    dismissed_reason = (payload.reason or "").strip()
    db.execute(
        """
        UPDATE limbo_items
        SET status = 'dismissed', dismissed_reason = ?, answer_text = NULL,
            answered_at = ?, answered_by = ?
        WHERE id = ?
        """,
        (dismissed_reason, answered_at, current_user.get("user_id", ""), item_id),
    )
    db.commit()
    return {"id": item_id, "status": "dismissed", "answered_at": answered_at}


@router.post("/api/handler/limbo/{item_id}/governance")
def handler_update_limbo_governance(
    item_id: int,
    payload: LimboGovernanceUpdateRequest,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Update publication governance flags for a limbo item."""
    row = db.execute("SELECT id FROM limbo_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Limbo item not found")

    updates = []
    values: list = []
    if payload.publication_tier is not None:
        tier = payload.publication_tier.strip().lower()
        if tier not in {"safe", "sensitive", "extreme"}:
            raise HTTPException(status_code=400, detail="publication_tier must be safe, sensitive, or extreme")
        updates.append("publication_tier = ?")
        values.append(tier)
    if payload.public_allowed is not None:
        updates.append("public_allowed = ?")
        values.append(1 if payload.public_allowed else 0)

    if not updates:
        return {"updated": False}

    values.append(item_id)
    db.execute(f"UPDATE limbo_items SET {', '.join(updates)} WHERE id = ?", values)
    db.commit()
    return {"updated": True, "id": item_id}


@router.post("/api/handler/limbo/{item_id}/publish")
def handler_publish_limbo_item(
    item_id: int,
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Publish an answered limbo item to the public questions feed."""
    row = db.execute(
        """
        SELECT id, prompt_text, answer_text, status, public_allowed, published_question_id, publication_tier
        FROM limbo_items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Limbo item not found")
    if row["status"] != "answered":
        raise HTTPException(status_code=409, detail="Limbo item must be answered before publishing")
    if not bool(row["public_allowed"]):
        raise HTTPException(status_code=409, detail="Public publish is blocked by governance")
    if row["published_question_id"]:
        return {"published": True, "question_id": row["published_question_id"], "already_published": True}

    question_id = str(uuid.uuid4())
    now = _now_iso()
    db.execute(
        """
        INSERT INTO questions (id, text, answer, is_public, created_at, source_type, publication_tier)
        VALUES (?, ?, ?, 1, ?, 'limbo', ?)
        """,
        (question_id, row["prompt_text"], row["answer_text"], now, row["publication_tier"]),
    )
    db.execute(
        "UPDATE limbo_items SET published_at = ?, published_question_id = ? WHERE id = ?",
        (now, question_id, item_id),
    )

    confessions_raw = get_setting(db, "public_confessions_posted")
    if confessions_raw is not None:
        try:
            confessions_val = int(confessions_raw)
            if confessions_val >= 0:
                set_setting(db, "public_confessions_posted", str(confessions_val + 1))
        except ValueError:
            pass

    db.commit()
    return {"published": True, "question_id": question_id, "already_published": False}


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


@router.get("/api/handler/tpe/qr")
def handler_tpe_pairing_qr(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
):
    """Serve the TPE pairing QR for the JWT-authenticated handler panel."""
    return _render_tpe_pairing_qr_png(db)


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
            snapshot_notice = None
            known_total = len(rows)
        else:
            assigned = _handler_allowed_devices(db, user_id)
            rows = _fetch_devices_by_ids(db, assigned, full=True)
            known_total = db.execute("SELECT COUNT(*) AS n FROM handler_device_status").fetchone()["n"]
            snapshot_notice = "no-assignment" if known_total > 0 and len(rows) == 0 else None

        snapshot_payload = {
            "type": "snapshot",
            "devices": [dict(r) for r in rows],
        }
        if snapshot_notice:
            snapshot_payload["notice"] = snapshot_notice
            snapshot_payload["known_total"] = known_total
        await websocket.send_json(snapshot_payload)

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
                                    payload = {"type": "webrtc_ice_candidate", "candidate": candidate}
                                    await _handler_ws.relay_signal_to_device(
                                        target_device,
                                        payload,
                                    )
                                    _publish_signaling_fallback(
                                        target_device,
                                        payload,
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
