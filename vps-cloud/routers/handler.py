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
    POST /api/handler/tpe/vault/*    – Vault controls routed to assigned device.
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
from collections import Counter
import json
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
    _create_tpe_pairing_code,
)
from routers.ws_manager import handler_ws as _handler_ws

logger = logging.getLogger(__name__)

router = APIRouter(tags=["handler"])

PUBLIC_COUNTER_ONE_LABEL_OPTIONS = [
    "Edges",
    "Denials",
    "Teases",
    "Punishments",
    "Obedience Points",
    "Strikes",
]
PUBLIC_COUNTER_TWO_LABEL_OPTIONS = [
    "Orgasms",
    "Allowed Releases",
    "Ruined Orgasms",
    "Relapses",
    "Reward Claims",
    "Completion Count",
]
PUBLIC_MODE_OPTIONS = [
    "Service",
    "Training",
    "Locked",
    "Tease Protocol",
    "Discipline",
    "Recovery",
    "Maintenance",
    "Free Day",
]
PUBLIC_STATUS_PRESETS = {
    "strict": {
        "tasks_label": "Denials",
        "confessions_label": "Allowed Releases",
        "current_status_mode": "Discipline",
    },
    "tease": {
        "tasks_label": "Edges",
        "confessions_label": "Ruined Orgasms",
        "current_status_mode": "Tease Protocol",
    },
    "progress": {
        "tasks_label": "Obedience Points",
        "confessions_label": "Reward Claims",
        "current_status_mode": "Training",
    },
    "punishment": {
        "tasks_label": "Strikes",
        "confessions_label": "Relapses",
        "current_status_mode": "Locked",
    },
}


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
        device_name TEXT,
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


def _ensure_rule_engine_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handler_rule_engine_rules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            scope_device_id TEXT,
            trigger_type    TEXT NOT NULL,
            threshold_value TEXT,
            action_type     TEXT NOT NULL,
            action_payload  TEXT,
            cooldown_sec    INTEGER NOT NULL DEFAULT 1800,
            last_fired_at   TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handler_rule_engine_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id          INTEGER,
            device_id        TEXT NOT NULL,
            trigger_snapshot TEXT,
            action_sent      INTEGER NOT NULL DEFAULT 0,
            result_json      TEXT,
            created_at       TEXT NOT NULL,
            FOREIGN KEY(rule_id) REFERENCES handler_rule_engine_rules(id)
        )
        """
    )


def _ensure_evidence_vault_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handler_evidence_vault (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id          TEXT,
            category           TEXT NOT NULL,
            severity           TEXT NOT NULL DEFAULT 'medium',
            title              TEXT NOT NULL,
            summary            TEXT,
            consequence_action TEXT,
            source_event_id    INTEGER,
            source_audit_id    INTEGER,
            source_upload_id   INTEGER,
            metadata_json      TEXT,
            public_visible     INTEGER NOT NULL DEFAULT 0,
            created_by         TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """
    )


def _ensure_behavior_log_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_behavior_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT,
            source      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            event_value TEXT,
            payload_json TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tpe_behavior_logs_created_at ON tpe_behavior_logs(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tpe_behavior_logs_device ON tpe_behavior_logs(device_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handler_evidence_attachments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id  INTEGER NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'url',
            label        TEXT,
            url          TEXT,
            metadata_json TEXT,
            created_at   TEXT NOT NULL,
            FOREIGN KEY(evidence_id) REFERENCES handler_evidence_vault(id) ON DELETE CASCADE
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
        _ensure_rule_engine_tables(conn)
        _ensure_evidence_vault_tables(conn)
        _ensure_behavior_log_table(conn)
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
        _ensure_rule_engine_tables(conn)
        _ensure_evidence_vault_tables(conn)
        _ensure_behavior_log_table(conn)
        conn.commit()
        return

    # Schema is current – ensure required columns + auxiliary tables exist.
    if "device_name" not in col_names:
        conn.execute("ALTER TABLE handler_device_status ADD COLUMN device_name TEXT")

    _ensure_limbo_table(conn)
    _ensure_limbo_columns(conn)
    _ensure_booking_table(conn)
    _ensure_puppy_mail_tables(conn)
    _ensure_rule_engine_tables(conn)
    _ensure_evidence_vault_tables(conn)
    _ensure_behavior_log_table(conn)
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
    device_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("device_name", "deviceName", "name"),
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


class RuleEngineRuleCreateRequest(BaseModel):
    name: str
    enabled: bool = True
    scope_device_id: Optional[str] = None
    trigger_type: str
    threshold_value: Optional[str] = None
    action_type: str
    action_payload: Optional[dict] = None
    cooldown_sec: int = 1800


class RuleEngineRuleUpdateRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    scope_device_id: Optional[str] = None
    trigger_type: Optional[str] = None
    threshold_value: Optional[str] = None
    action_type: Optional[str] = None
    action_payload: Optional[dict] = None
    cooldown_sec: Optional[int] = None


class RuleEngineEvaluateRequest(BaseModel):
    device_id: str


class EvidenceCreateRequest(BaseModel):
    device_id: Optional[str] = None
    category: str = "consequence"
    severity: str = "medium"
    title: str
    summary: Optional[str] = None
    consequence_action: Optional[str] = None
    source_event_id: Optional[int] = None
    source_audit_id: Optional[int] = None
    source_upload_id: Optional[int] = None
    metadata: Optional[dict] = None
    public_visible: bool = False


class EvidenceAttachmentCreateRequest(BaseModel):
    kind: str = "url"
    label: Optional[str] = None
    url: Optional[str] = None
    metadata: Optional[dict] = None


class EvidencePromoteRequest(BaseModel):
    device_id: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    severity: str = "medium"
    consequence_action: Optional[str] = None
    public_visible: bool = False


class VaultAddEntryRequest(BaseModel):
    device_id: str
    site: Optional[str] = ""
    username: Optional[str] = ""
    password: str
    notes: Optional[str] = ""


class VaultUpdateEntryRequest(BaseModel):
    device_id: str
    site: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    notes: Optional[str] = None


class VaultLockRequest(BaseModel):
    device_id: str
    duration_minutes: int = 60


class VaultChangeBlockRequest(BaseModel):
    device_id: str
    enabled: bool


class VaultImportEntryRequest(BaseModel):
    site: Optional[str] = ""
    username: Optional[str] = ""
    password: str
    notes: Optional[str] = ""


class VaultImportRequest(BaseModel):
    device_id: str
    entries: List[VaultImportEntryRequest]


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
    tasks_label: Optional[str] = None
    confessions_label: Optional[str] = None
    tasks_completed: Optional[int] = None
    confessions_posted: Optional[int] = None
    public_booking_enabled: Optional[bool] = None
    public_screen_share_approved: Optional[bool] = None


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

    resolved_device_name = (body.device_name or "").strip() or None

    now = _now_iso()
    db.execute(
        """
        INSERT INTO handler_device_status
            (device_id, device_name, fcm_token, battery_pct, lat, lon, ai_alert, ai_label, ai_score,
             is_locked, is_online, last_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            device_name = COALESCE(excluded.device_name, device_name),
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
            resolved_device_name,
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
    db.execute(
        """
        INSERT INTO tpe_behavior_logs (device_id, source, event_type, event_value, payload_json, created_at)
        VALUES (?, 'device_status', 'status_report', ?, ?, ?)
        """,
        (
            resolved_device_id,
            "ai_alert" if body.ai_alert else "status_ok",
            json.dumps(
                {
                    "battery_pct": body.battery_pct,
                    "lat": body.lat,
                    "lon": body.lon,
                    "ai_alert": bool(body.ai_alert),
                    "ai_label": body.ai_label,
                    "ai_score": body.ai_score,
                    "device_name": resolved_device_name,
                }
            ),
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


def _assert_handler_device_access(
    db: sqlite3.Connection,
    current_user: dict,
    device_id: str,
) -> None:
    if current_user.get("role") == "admin":
        return
    assigned = _handler_allowed_devices(db, current_user["user_id"])
    if device_id not in assigned:
        raise HTTPException(status_code=403, detail="Access denied to this device.")


_RULE_TRIGGER_TYPES = {
    "battery_below",
    "ai_alert_true",
    "offline_for_minutes",
}

_RULE_ACTION_TYPES = {
    "lock_device",
    "vault_lock_all",
    "show_overlay",
    "set_sub_status",
    "set_change_block",
}

_EVIDENCE_SEVERITIES = {"low", "medium", "high", "critical"}
_EVIDENCE_CATEGORIES = {
    "consequence",
    "proof",
    "compliance",
    "violation",
    "system",
}


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _should_fire_rule(rule: sqlite3.Row, device_status: sqlite3.Row, now: datetime) -> tuple[bool, dict]:
    trigger = str(rule["trigger_type"] or "").strip()
    threshold = str(rule["threshold_value"] or "").strip()
    snapshot: dict[str, object] = {
        "trigger_type": trigger,
        "threshold": threshold,
    }

    if trigger == "battery_below":
        threshold_int = int(threshold or "0")
        battery = int(device_status["battery_pct"] or 0)
        snapshot["battery_pct"] = battery
        return battery <= threshold_int, snapshot

    if trigger == "ai_alert_true":
        ai_alert = int(device_status["ai_alert"] or 0) == 1
        snapshot["ai_alert"] = ai_alert
        return ai_alert, snapshot

    if trigger == "offline_for_minutes":
        mins = int(threshold or "0")
        last_seen = _parse_iso_utc(device_status["last_seen"])
        if last_seen is None:
            snapshot["last_seen_missing"] = True
            return False, snapshot
        delta_mins = int((now - last_seen).total_seconds() // 60)
        snapshot["offline_minutes"] = delta_mins
        return delta_mins >= mins, snapshot

    snapshot["unsupported_trigger"] = True
    return False, snapshot


def _dispatch_rule_action(
    db: sqlite3.Connection,
    device_id: str,
    action_type: str,
    action_payload_raw: Optional[str],
) -> dict:
    payload: dict[str, str] = {}
    if action_payload_raw:
        try:
            decoded = json.loads(action_payload_raw)
            if isinstance(decoded, dict):
                payload = {str(k): str(v) for k, v in decoded.items() if v is not None}
        except json.JSONDecodeError:
            payload = {}

    if action_type == "lock_device":
        return _send_mqtt_to_device(db, device_id, {"action": "LOCK_DEVICE"})

    if action_type == "vault_lock_all":
        minutes = payload.get("duration_minutes", "60")
        return _send_mqtt_to_device(
            db,
            device_id,
            {
                "action": "VAULT_LOCK_ALL",
                "duration_minutes": minutes,
            },
        )

    if action_type == "show_overlay":
        return _send_mqtt_to_device(
            db,
            device_id,
            {
                "action": "SHOW_OVERLAY",
                "title": payload.get("title", "Handler Notice"),
                "message": payload.get("message", "Immediate compliance required."),
                "image_url": payload.get("image_url", ""),
            },
        )

    if action_type == "set_sub_status":
        return _send_mqtt_to_device(
            db,
            device_id,
            {
                "action": "SET_SUB_STATUS",
                "status": payload.get("status", "discipline"),
            },
        )

    if action_type == "set_change_block":
        enabled = payload.get("enabled", "true").lower() in {"1", "true", "yes", "on"}
        return _send_mqtt_to_device(
            db,
            device_id,
            {
                "action": "VAULT_SET_CHANGE_BLOCK",
                "enabled": "true" if enabled else "false",
            },
        )

    return {"sent": 0, "failed": 1}


def _evaluate_rule_engine_for_device(db: sqlite3.Connection, device_id: str) -> dict:
    status_row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if not status_row:
        raise HTTPException(status_code=404, detail="Device not found.")

    now = datetime.now(timezone.utc)
    rules = db.execute(
        "SELECT * FROM handler_rule_engine_rules WHERE enabled = 1"
    ).fetchall()

    fired = 0
    skipped_cooldown = 0
    checked = 0

    for rule in rules:
        scope = str(rule["scope_device_id"] or "").strip()
        if scope and scope != device_id:
            continue
        checked += 1

        cooldown_sec = max(0, int(rule["cooldown_sec"] or 0))
        last_fired_at = _parse_iso_utc(rule["last_fired_at"])
        if last_fired_at and cooldown_sec > 0:
            elapsed = (now - last_fired_at).total_seconds()
            if elapsed < cooldown_sec:
                skipped_cooldown += 1
                continue

        should_fire, snapshot = _should_fire_rule(rule, status_row, now)
        if not should_fire:
            continue

        action_type = str(rule["action_type"] or "")
        action_result = _dispatch_rule_action(
            db,
            device_id,
            action_type,
            rule["action_payload"],
        )
        fired += 1

        now_iso = _now_iso()
        db.execute(
            "UPDATE handler_rule_engine_rules SET last_fired_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, rule["id"]),
        )
        db.execute(
            """
            INSERT INTO handler_rule_engine_events
                (rule_id, device_id, trigger_snapshot, action_sent, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rule["id"],
                device_id,
                json.dumps(snapshot),
                1 if int(action_result.get("sent", 0)) > 0 else 0,
                json.dumps(action_result),
                now_iso,
            ),
        )

    db.commit()
    return {
        "device_id": device_id,
        "checked": checked,
        "fired": fired,
        "skipped_cooldown": skipped_cooldown,
    }


def _extract_device_from_payload_json(payload_json: Optional[str]) -> Optional[str]:
    if not payload_json:
        return None
    try:
        obj = json.loads(payload_json)
        if isinstance(obj, dict):
            raw = obj.get("device_id") or obj.get("deviceId")
            if raw is not None:
                device_id = str(raw).strip()
                return device_id or None
    except Exception:
        return None
    return None


def _evidence_with_attachments(db: sqlite3.Connection, evidence_row: sqlite3.Row) -> dict:
    item = dict(evidence_row)
    attachments = db.execute(
        "SELECT id, evidence_id, kind, label, url, metadata_json, created_at "
        "FROM handler_evidence_attachments WHERE evidence_id = ? ORDER BY id ASC",
        (item["id"],),
    ).fetchall()
    item["attachments"] = [dict(a) for a in attachments]
    return item


def _assert_evidence_access(
    db: sqlite3.Connection,
    current_user: Optional[dict],
    device_id: Optional[str],
) -> None:
    if current_user is None or current_user.get("role") == "admin":
        return
    if not device_id:
        return
    _assert_handler_device_access(db, current_user, device_id)


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
    cols = "*" if full else "device_id, device_name, is_online, is_locked, battery_pct, last_seen"
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
            "SELECT device_id, device_name, is_online, is_locked, battery_pct, last_seen "
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


def _safe_choice(value: Optional[str], allowed: List[str], default: str) -> str:
    raw = (value or "").strip()
    return raw if raw in allowed else default


@router.get("/api/handler/public-status")
def handler_get_public_status(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return editable public status settings used by public pages."""
    tasks_label = _safe_choice(
        get_setting(db, "public_tasks_label", "Edges"),
        PUBLIC_COUNTER_ONE_LABEL_OPTIONS,
        "Edges",
    )
    confessions_label = _safe_choice(
        get_setting(db, "public_confessions_label", "Orgasms"),
        PUBLIC_COUNTER_TWO_LABEL_OPTIONS,
        "Orgasms",
    )
    current_mode = _safe_choice(
        get_setting(db, "current_status_mode", "Service"),
        PUBLIC_MODE_OPTIONS,
        "Service",
    )
    return {
        "days_caged_start_date": get_setting(db, "days_caged_start_date", ""),
        "days_caged_paused": _safe_bool(get_setting(db, "days_caged_paused", "false")),
        "days_caged_accumulated_days": _safe_int(get_setting(db, "days_caged_accumulated_days", "0")),
        "days_locked_goal_days": _safe_int(get_setting(db, "days_locked_goal_days", "0")),
        "current_status_mode": current_mode,
        "tasks_label": tasks_label,
        "confessions_label": confessions_label,
        "counter_one_options": PUBLIC_COUNTER_ONE_LABEL_OPTIONS,
        "counter_two_options": PUBLIC_COUNTER_TWO_LABEL_OPTIONS,
        "mode_options": PUBLIC_MODE_OPTIONS,
        "presets": PUBLIC_STATUS_PRESETS,
        "tasks_completed": _safe_int(get_setting(db, "public_tasks_completed", "0")),
        "confessions_posted": _safe_int(get_setting(db, "public_confessions_posted", "0")),
        "public_booking_enabled": _safe_bool(get_setting(db, "public_booking_enabled", "false")),
        "public_screen_share_approved": _safe_bool(get_setting(db, "public_screen_share_approved", "false")),
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
    if payload.tasks_label is not None:
        tasks_label = payload.tasks_label.strip()
        if tasks_label not in PUBLIC_COUNTER_ONE_LABEL_OPTIONS:
            raise HTTPException(status_code=400, detail="tasks_label is not a valid option")
        set_setting(db, "public_tasks_label", tasks_label)
    if payload.confessions_label is not None:
        confessions_label = payload.confessions_label.strip()
        if confessions_label not in PUBLIC_COUNTER_TWO_LABEL_OPTIONS:
            raise HTTPException(status_code=400, detail="confessions_label is not a valid option")
        set_setting(db, "public_confessions_label", confessions_label)
    if payload.current_status_mode is not None:
        current_mode = payload.current_status_mode.strip()
        if current_mode not in PUBLIC_MODE_OPTIONS:
            raise HTTPException(status_code=400, detail="current_status_mode is not a valid option")
        set_setting(db, "current_status_mode", current_mode)
    if payload.tasks_completed is not None:
        if payload.tasks_completed < 0:
            raise HTTPException(status_code=400, detail="tasks_completed must be >= 0")
        set_setting(db, "public_tasks_completed", str(payload.tasks_completed))
    if payload.confessions_posted is not None:
        if payload.confessions_posted < 0:
            raise HTTPException(status_code=400, detail="confessions_posted must be >= 0")
        set_setting(db, "public_confessions_posted", str(payload.confessions_posted))
    if payload.public_booking_enabled is not None:
        set_setting(
            db,
            "public_booking_enabled",
            "true" if payload.public_booking_enabled else "false",
        )
    if payload.public_screen_share_approved is not None:
        set_setting(
            db,
            "public_screen_share_approved",
            "true" if payload.public_screen_share_approved else "false",
        )

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


@router.get("/api/handler/tpe/rule-engine/rules")
def handler_rule_engine_list_rules(
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    rows = db.execute(
        "SELECT * FROM handler_rule_engine_rules ORDER BY id DESC"
    ).fetchall()
    rules = [dict(r) for r in rows]
    if current_user.get("role") == "admin":
        return rules
    assigned = set(_handler_allowed_devices(db, current_user["user_id"]))
    return [r for r in rules if not r.get("scope_device_id") or r.get("scope_device_id") in assigned]


@router.post("/api/handler/tpe/rule-engine/rules")
def handler_rule_engine_create_rule(
    body: RuleEngineRuleCreateRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    trigger_type = body.trigger_type.strip()
    action_type = body.action_type.strip()
    if trigger_type not in _RULE_TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported trigger_type '{trigger_type}'")
    if action_type not in _RULE_ACTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported action_type '{action_type}'")

    scope_device_id = (body.scope_device_id or "").strip() or None
    if scope_device_id:
        _assert_handler_device_access(db, current_user, scope_device_id)

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO handler_rule_engine_rules
            (name, enabled, scope_device_id, trigger_type, threshold_value,
             action_type, action_payload, cooldown_sec, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            body.name.strip(),
            1 if body.enabled else 0,
            scope_device_id,
            trigger_type,
            body.threshold_value,
            action_type,
            json.dumps(body.action_payload or {}),
            max(0, min(body.cooldown_sec, 7 * 24 * 60 * 60)),
            now,
            now,
        ),
    )
    db.commit()
    rule_id = int(cur.lastrowid)
    row = db.execute(
        "SELECT * FROM handler_rule_engine_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    return dict(row)


@router.patch("/api/handler/tpe/rule-engine/rules/{rule_id}")
def handler_rule_engine_update_rule(
    rule_id: int,
    body: RuleEngineRuleUpdateRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute(
        "SELECT * FROM handler_rule_engine_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")

    if row["scope_device_id"]:
        _assert_handler_device_access(db, current_user, row["scope_device_id"])

    updates: dict[str, object] = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
    if body.scope_device_id is not None:
        scope = body.scope_device_id.strip() or None
        if scope:
            _assert_handler_device_access(db, current_user, scope)
        updates["scope_device_id"] = scope
    if body.trigger_type is not None:
        trigger_type = body.trigger_type.strip()
        if trigger_type not in _RULE_TRIGGER_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported trigger_type '{trigger_type}'")
        updates["trigger_type"] = trigger_type
    if body.threshold_value is not None:
        updates["threshold_value"] = body.threshold_value
    if body.action_type is not None:
        action_type = body.action_type.strip()
        if action_type not in _RULE_ACTION_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported action_type '{action_type}'")
        updates["action_type"] = action_type
    if body.action_payload is not None:
        updates["action_payload"] = json.dumps(body.action_payload)
    if body.cooldown_sec is not None:
        updates["cooldown_sec"] = max(0, min(body.cooldown_sec, 7 * 24 * 60 * 60))

    if not updates:
        return dict(row)

    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = list(updates.values()) + [rule_id]
    db.execute(
        f"UPDATE handler_rule_engine_rules SET {set_clause} WHERE id = ?",
        params,
    )
    db.commit()
    updated = db.execute(
        "SELECT * FROM handler_rule_engine_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/api/handler/tpe/rule-engine/rules/{rule_id}")
def handler_rule_engine_delete_rule(
    rule_id: int,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute(
        "SELECT * FROM handler_rule_engine_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    if row["scope_device_id"]:
        _assert_handler_device_access(db, current_user, row["scope_device_id"])
    db.execute("DELETE FROM handler_rule_engine_rules WHERE id = ?", (rule_id,))
    db.commit()
    return {"deleted": True, "rule_id": rule_id}


@router.post("/api/handler/tpe/rule-engine/evaluate")
def handler_rule_engine_evaluate(
    body: RuleEngineEvaluateRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    return _evaluate_rule_engine_for_device(db, body.device_id)


@router.get("/api/handler/tpe/rule-engine/events")
def handler_rule_engine_events(
    device_id: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    capped_limit = max(1, min(limit, 500))
    if device_id:
        _assert_handler_device_access(db, current_user, device_id)
        rows = db.execute(
            "SELECT * FROM handler_rule_engine_events WHERE device_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (device_id, capped_limit),
        ).fetchall()
        return [dict(r) for r in rows]

    if current_user.get("role") == "admin":
        rows = db.execute(
            "SELECT * FROM handler_rule_engine_events ORDER BY id DESC LIMIT ?",
            (capped_limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    assigned = _handler_allowed_devices(db, current_user["user_id"])
    if not assigned:
        return []
    placeholders = ",".join("?" for _ in assigned)
    rows = db.execute(
        f"SELECT * FROM handler_rule_engine_events WHERE device_id IN ({placeholders}) "
        "ORDER BY id DESC LIMIT ?",
        [*assigned, capped_limit],
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/handler/tpe/vault/entry/add")
def handler_tpe_vault_add_entry(
    body: VaultAddEntryRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    payload = {
        "action": "VAULT_ADD_ENTRY",
        "site": body.site or "",
        "username": body.username or "",
        "password": body.password,
        "notes": body.notes or "",
    }
    mqtt = _send_mqtt_to_device(db, body.device_id, payload)
    return {"status": "vault_add_sent", "mqtt": mqtt}


@router.patch("/api/handler/tpe/vault/entry/{entry_id}")
def handler_tpe_vault_update_entry(
    entry_id: str,
    body: VaultUpdateEntryRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    payload = {
        "action": "VAULT_UPDATE_ENTRY",
        "id": entry_id,
        **({"site": body.site} if body.site is not None else {}),
        **({"username": body.username} if body.username is not None else {}),
        **({"password": body.password} if body.password is not None else {}),
        **({"notes": body.notes} if body.notes is not None else {}),
    }
    mqtt = _send_mqtt_to_device(db, body.device_id, payload)
    return {"status": "vault_update_sent", "entry_id": entry_id, "mqtt": mqtt}


@router.delete("/api/handler/tpe/vault/entry/{entry_id}")
def handler_tpe_vault_delete_entry(
    entry_id: str,
    device_id: str,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, device_id)
    mqtt = _send_mqtt_to_device(
        db,
        device_id,
        {"action": "VAULT_DELETE_ENTRY", "id": entry_id},
    )
    return {"status": "vault_delete_sent", "entry_id": entry_id, "mqtt": mqtt}


@router.post("/api/handler/tpe/vault/entry/{entry_id}/lock")
def handler_tpe_vault_lock_entry(
    entry_id: str,
    body: VaultLockRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    minutes = max(1, min(int(body.duration_minutes), 43200))
    mqtt = _send_mqtt_to_device(
        db,
        body.device_id,
        {
            "action": "VAULT_LOCK_ENTRY",
            "id": entry_id,
            "duration_minutes": str(minutes),
        },
    )
    return {
        "status": "vault_lock_entry_sent",
        "entry_id": entry_id,
        "duration_minutes": minutes,
        "mqtt": mqtt,
    }


@router.post("/api/handler/tpe/vault/lock-all")
def handler_tpe_vault_lock_all(
    body: VaultLockRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    minutes = max(1, min(int(body.duration_minutes), 43200))
    mqtt = _send_mqtt_to_device(
        db,
        body.device_id,
        {"action": "VAULT_LOCK_ALL", "duration_minutes": str(minutes)},
    )
    return {
        "status": "vault_lock_all_sent",
        "duration_minutes": minutes,
        "mqtt": mqtt,
    }


@router.post("/api/handler/tpe/vault/change-block")
def handler_tpe_vault_change_block(
    body: VaultChangeBlockRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)
    mqtt = _send_mqtt_to_device(
        db,
        body.device_id,
        {
            "action": "VAULT_SET_CHANGE_BLOCK",
            "enabled": "true" if body.enabled else "false",
        },
    )
    return {"status": "vault_change_block_sent", "enabled": body.enabled, "mqtt": mqtt}


@router.post("/api/handler/tpe/vault/import")
def handler_tpe_vault_import(
    body: VaultImportRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _assert_handler_device_access(db, current_user, body.device_id)

    if not body.entries:
        raise HTTPException(status_code=400, detail="entries is required")
    if len(body.entries) > 500:
        raise HTTPException(status_code=400, detail="entries exceeds max batch size (500)")

    sent = 0
    failed = 0
    for entry in body.entries:
        if not entry.password.strip():
            failed += 1
            continue
        payload = {
            "action": "VAULT_ADD_ENTRY",
            "site": entry.site or "",
            "username": entry.username or "",
            "password": entry.password,
            "notes": entry.notes or "",
        }
        result = _send_mqtt_to_device(db, body.device_id, payload)
        sent += int(result.get("sent", 0))
        failed += int(result.get("failed", 0))

    return {
        "status": "vault_import_dispatched",
        "requested": len(body.entries),
        "sent": sent,
        "failed": failed,
    }


@router.get("/api/handler/tpe/vault/events")
def handler_tpe_vault_events(
    device_id: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    if device_id:
        _assert_handler_device_access(db, current_user, device_id)

    rows = db.execute(
        "SELECT id, event, reason, session_ts, payload_json, received_at "
        "FROM tpe_events WHERE lower(event) LIKE 'vault_%' "
        + ("AND json_extract(payload_json, '$.device_id') = ? " if device_id else "")
        + "ORDER BY id DESC LIMIT ?",
        ((device_id, min(limit, 500)) if device_id else (min(limit, 500),)),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/handler/tpe/evidence")
def handler_tpe_evidence_create(
    body: EvidenceCreateRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    category = body.category.strip().lower()
    severity = body.severity.strip().lower()
    if category not in _EVIDENCE_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unsupported category '{category}'")
    if severity not in _EVIDENCE_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"Unsupported severity '{severity}'")

    device_id = (body.device_id or "").strip() or None
    _assert_evidence_access(db, current_user, device_id)

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO handler_evidence_vault
            (device_id, category, severity, title, summary, consequence_action,
             source_event_id, source_audit_id, source_upload_id, metadata_json,
             public_visible, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            category,
            severity,
            body.title.strip(),
            body.summary,
            body.consequence_action,
            body.source_event_id,
            body.source_audit_id,
            body.source_upload_id,
            json.dumps(body.metadata or {}),
            0,
            str(current_user.get("user_id") or current_user.get("username") or "handler"),
            now,
            now,
        ),
    )
    db.commit()
    evidence_id = int(cur.lastrowid)
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    return _evidence_with_attachments(db, row)


@router.get("/api/handler/tpe/evidence")
def handler_tpe_evidence_list(
    device_id: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    capped_limit = max(1, min(limit, 500))
    filters = []
    params: list[object] = []

    device_id_norm = (device_id or "").strip() or None
    if device_id_norm:
        _assert_evidence_access(db, current_user, device_id_norm)
        filters.append("device_id = ?")
        params.append(device_id_norm)
    elif current_user.get("role") != "admin":
        assigned = _handler_allowed_devices(db, current_user["user_id"])
        if assigned:
            placeholders = ",".join("?" for _ in assigned)
            filters.append(f"(device_id IS NULL OR device_id IN ({placeholders}))")
            params.extend(assigned)
        else:
            filters.append("device_id IS NULL")

    if category:
        filters.append("category = ?")
        params.append(category.strip().lower())
    if severity:
        filters.append("severity = ?")
        params.append(severity.strip().lower())

    sql = "SELECT * FROM handler_evidence_vault"
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(capped_limit)

    rows = db.execute(sql, params).fetchall()
    return [_evidence_with_attachments(db, r) for r in rows]


@router.get("/api/handler/tpe/evidence/{evidence_id}")
def handler_tpe_evidence_get(
    evidence_id: int,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evidence item not found")
    _assert_evidence_access(db, current_user, row["device_id"])
    return _evidence_with_attachments(db, row)


@router.post("/api/handler/tpe/evidence/{evidence_id}/attachments")
def handler_tpe_evidence_add_attachment(
    evidence_id: int,
    body: EvidenceAttachmentCreateRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    evidence_row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if not evidence_row:
        raise HTTPException(status_code=404, detail="Evidence item not found")
    _assert_evidence_access(db, current_user, evidence_row["device_id"])

    now = _now_iso()
    db.execute(
        """
        INSERT INTO handler_evidence_attachments
            (evidence_id, kind, label, url, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            (body.kind or "url").strip().lower(),
            body.label,
            body.url,
            json.dumps(body.metadata or {}),
            now,
        ),
    )
    db.execute(
        "UPDATE handler_evidence_vault SET updated_at = ? WHERE id = ?",
        (now, evidence_id),
    )
    db.commit()

    refreshed = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    return _evidence_with_attachments(db, refreshed)


@router.post("/api/handler/tpe/evidence/promote/event/{event_id}")
def handler_tpe_evidence_promote_event(
    event_id: int,
    body: EvidencePromoteRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    event_row = db.execute(
        "SELECT * FROM tpe_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if not event_row:
        raise HTTPException(status_code=404, detail="Event not found")

    device_id = (body.device_id or "").strip() or _extract_device_from_payload_json(event_row["payload_json"])
    _assert_evidence_access(db, current_user, device_id)

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO handler_evidence_vault
            (device_id, category, severity, title, summary, consequence_action,
             source_event_id, metadata_json, public_visible, created_by, created_at, updated_at)
        VALUES (?, 'consequence', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            body.severity.strip().lower(),
            (body.title or f"Event: {event_row['event']}").strip(),
            body.summary or event_row["reason"],
            body.consequence_action,
            event_id,
            event_row["payload_json"] or json.dumps({}),
            0,
            str(current_user.get("user_id") or current_user.get("username") or "handler"),
            now,
            now,
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (int(cur.lastrowid),),
    ).fetchone()
    return _evidence_with_attachments(db, row)


@router.post("/api/handler/tpe/evidence/promote/audit/{audit_id}")
def handler_tpe_evidence_promote_audit(
    audit_id: int,
    body: EvidencePromoteRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    audit_row = db.execute(
        "SELECT * FROM tpe_audit_logs WHERE id = ?",
        (audit_id,),
    ).fetchone()
    if not audit_row:
        raise HTTPException(status_code=404, detail="Audit record not found")

    device_id = (body.device_id or "").strip() or None
    _assert_evidence_access(db, current_user, device_id)

    metadata = {
        "detection_ratio": audit_row["detection_ratio"],
        "last_label": audit_row["last_label"],
        "last_score": audit_row["last_score"],
        "session_ts": audit_row["session_ts"],
        "video_filename": audit_row["video_filename"],
    }

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO handler_evidence_vault
            (device_id, category, severity, title, summary, consequence_action,
             source_audit_id, metadata_json, public_visible, created_by, created_at, updated_at)
        VALUES (?, 'proof', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            body.severity.strip().lower(),
            (body.title or f"Audit proof #{audit_id}").strip(),
            body.summary or "Adherence audit promoted to evidence vault.",
            body.consequence_action,
            audit_id,
            json.dumps(metadata),
            0,
            str(current_user.get("user_id") or current_user.get("username") or "handler"),
            now,
            now,
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (int(cur.lastrowid),),
    ).fetchone()
    return _evidence_with_attachments(db, row)


@router.post("/api/handler/tpe/evidence/promote/upload/{upload_id}")
def handler_tpe_evidence_promote_upload(
    upload_id: int,
    body: EvidencePromoteRequest,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    upload_row = db.execute(
        "SELECT * FROM tpe_uploads WHERE id = ?",
        (upload_id,),
    ).fetchone()
    if not upload_row:
        raise HTTPException(status_code=404, detail="Upload record not found")

    device_id = (body.device_id or "").strip() or None
    _assert_evidence_access(db, current_user, device_id)

    now = _now_iso()
    cur = db.execute(
        """
        INSERT INTO handler_evidence_vault
            (device_id, category, severity, title, summary, consequence_action,
             source_upload_id, metadata_json, public_visible, created_by, created_at, updated_at)
        VALUES (?, 'proof', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            body.severity.strip().lower(),
            (body.title or f"Upload proof #{upload_id}").strip(),
            body.summary or "Device upload promoted to evidence vault.",
            body.consequence_action,
            upload_id,
            json.dumps({
                "filename": upload_row["filename"],
                "content_type": upload_row["content_type"],
                "size_bytes": upload_row["size_bytes"],
            }),
            0,
            str(current_user.get("user_id") or current_user.get("username") or "handler"),
            now,
            now,
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (int(cur.lastrowid),),
    ).fetchone()
    return _evidence_with_attachments(db, row)


@router.post("/api/handler/tpe/evidence/{evidence_id}/publish")
def handler_tpe_evidence_publish(
    evidence_id: int,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evidence item not found")
    _assert_evidence_access(db, current_user, row["device_id"])

    now = _now_iso()
    db.execute(
        "UPDATE handler_evidence_vault SET public_visible = 1, updated_at = ? WHERE id = ?",
        (now, evidence_id),
    )
    db.commit()
    refreshed = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    return _evidence_with_attachments(db, refreshed)


@router.post("/api/handler/tpe/evidence/{evidence_id}/unpublish")
def handler_tpe_evidence_unpublish(
    evidence_id: int,
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evidence item not found")
    _assert_evidence_access(db, current_user, row["device_id"])

    now = _now_iso()
    db.execute(
        "UPDATE handler_evidence_vault SET public_visible = 0, updated_at = ? WHERE id = ?",
        (now, evidence_id),
    )
    db.commit()
    refreshed = db.execute(
        "SELECT * FROM handler_evidence_vault WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    return _evidence_with_attachments(db, refreshed)


@router.get("/api/public/tpe/evidence")
def public_tpe_evidence_feed(
    limit: int = 50,
    db: sqlite3.Connection = Depends(get_db),
) -> list:
    capped_limit = max(1, min(limit, 200))
    rows = db.execute(
        "SELECT id, device_id, category, severity, title, summary, consequence_action, created_at "
        "FROM handler_evidence_vault WHERE public_visible = 1 "
        "ORDER BY id DESC LIMIT ?",
        (capped_limit,),
    ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        attachments = db.execute(
            "SELECT kind, label, url, created_at FROM handler_evidence_attachments "
            "WHERE evidence_id = ? ORDER BY id ASC",
            (item["id"],),
        ).fetchall()
        item["attachments"] = [dict(a) for a in attachments]
        result.append(item)
    return result


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

    payload = _build_tpe_payload(body)
    result = _send_mqtt_to_device(db, body.device_id, payload)

    db.execute(
        """
        INSERT INTO tpe_behavior_logs (device_id, source, event_type, event_value, payload_json, created_at)
        VALUES (?, 'handler_command', 'command_push', ?, ?, ?)
        """,
        (
            body.device_id,
            body.action,
            json.dumps(payload),
            _now_iso(),
        ),
    )
    db.commit()
    return result


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


@router.get("/api/handler/tpe/behavior-insights")
def handler_tpe_behavior_insights(
    days: int = 14,
    device_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return lightweight behavioral learning metrics for the handler panel.

    Insights are computed from existing TPE logs (events, audits, check-ins,
    tasks) over a configurable rolling window.
    """

    def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        try:
            normalized = str(ts).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    selected_days = max(1, min(int(days), 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=selected_days)
    cutoff_iso = cutoff.isoformat()

    scoped_device_id = (device_id or "").strip() or None
    allowed_devices: Optional[set[str]] = None

    if current_user["role"] != "admin":
        assigned = set(_handler_allowed_devices(db, current_user["user_id"]))
        if scoped_device_id and scoped_device_id not in assigned:
            raise HTTPException(status_code=403, detail="Access denied to this device.")
        allowed_devices = assigned

    event_rows = db.execute(
        "SELECT event, reason, payload_json, received_at FROM tpe_events WHERE received_at >= ? ORDER BY received_at DESC",
        (cutoff_iso,),
    ).fetchall()
    audit_rows = db.execute(
        "SELECT detection_ratio, last_score, received_at FROM tpe_audit_logs WHERE received_at >= ? ORDER BY received_at DESC",
        (cutoff_iso,),
    ).fetchall()
    checkin_rows = db.execute(
        "SELECT mood_score, checked_in_at FROM tpe_checkins WHERE checked_in_at >= ? ORDER BY checked_in_at DESC",
        (cutoff_iso,),
    ).fetchall()
    task_rows = db.execute(
        "SELECT status, created_at, completed_at FROM tpe_tasks WHERE created_at >= ? OR completed_at >= ?",
        (cutoff_iso, cutoff_iso),
    ).fetchall()
    vital_rows = db.execute(
        "SELECT device_id, heart_rate, steps, timestamp FROM device_vitals "
        "WHERE timestamp >= ? ORDER BY timestamp DESC",
        (cutoff_iso,),
    ).fetchall()
    behavior_rows = db.execute(
        "SELECT device_id, source, event_type, event_value, payload_json, created_at "
        "FROM tpe_behavior_logs WHERE created_at >= ? ORDER BY created_at DESC",
        (cutoff_iso,),
    ).fetchall()

    filtered_events = []
    hourly_hist = [0] * 24
    event_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    social_action_counter: Counter[str] = Counter()
    social_platform_counter: Counter[str] = Counter()
    phone_event_counter: Counter[str] = Counter()

    for row in event_rows:
        payload = {}
        raw_payload = row["payload_json"]
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except Exception:
                payload = {}

        event_device_id = str(
            payload.get("device_id")
            or payload.get("deviceId")
            or payload.get("target_device")
            or ""
        ).strip() or None

        if scoped_device_id and event_device_id and event_device_id != scoped_device_id:
            continue
        if allowed_devices is not None and event_device_id and event_device_id not in allowed_devices:
            continue

        filtered_events.append(row)
        event_name = (row["event"] or "unknown").strip().lower()
        event_counter[event_name] += 1

        if event_name == "social_interaction" or (
            payload.get("platform") is not None and payload.get("action") is not None
        ):
            platform = str(payload.get("platform") or "unknown").strip().lower()
            action = str(payload.get("action") or "unknown").strip().lower()
            social_platform_counter[platform] += 1
            social_action_counter[action] += 1

        # Events emitted directly from on-device services/hooks (not handler UI).
        if event_name in {
            "app_blocked",
            "override_used",
            "tone_block",
            "mdm_executed",
            "permission_to_speak",
            "xposed_coverage",
            "silent_selfie",
            "device_health_sync_success",
            "device_health_sync_failed",
            "device_health_sync_empty",
            "health_connect_toggle",
            "health_connect_toggle_failed",
            "text_replacement_rule_added",
            "text_replacement_rule_removed",
            "filter_settings_saved",
            "handler_settings_saved",
            "remote_injection_mode_set",
            "device_admin_activated",
            "device_admin_activation_pending",
            "device_admin_deactivated",
            "device_admin_deactivate_failed",
            "vault_settings_saved",
            "mqtt_event_received",
            "app_lifecycle",
            "typing_session_metrics",
        }:
            phone_event_counter[event_name] += 1

        reason = (row["reason"] or "").strip()
        if reason:
            reason_counter[reason] += 1

        evt_ts = _parse_iso(row["received_at"])
        if evt_ts is not None:
            hourly_hist[evt_ts.hour] += 1

    mood_values = [int(r["mood_score"]) for r in checkin_rows if r["mood_score"] is not None]
    mood_avg = round(sum(mood_values) / len(mood_values), 2) if mood_values else None

    mid = datetime.now(timezone.utc) - timedelta(days=max(1, selected_days // 2))
    recent_moods = []
    prior_moods = []
    for r in checkin_rows:
        ts = _parse_iso(r["checked_in_at"])
        if ts is None or r["mood_score"] is None:
            continue
        if ts >= mid:
            recent_moods.append(int(r["mood_score"]))
        else:
            prior_moods.append(int(r["mood_score"]))
    mood_delta = None
    if recent_moods and prior_moods:
        mood_delta = round((sum(recent_moods) / len(recent_moods)) - (sum(prior_moods) / len(prior_moods)), 2)

    completed_statuses = {"completed", "done", "verified", "success"}
    task_total = 0
    task_completed = 0
    for row in task_rows:
        created = _parse_iso(row["created_at"])
        completed_at = _parse_iso(row["completed_at"])
        if (created and created >= cutoff) or (completed_at and completed_at >= cutoff):
            task_total += 1
            status = (row["status"] or "").strip().lower()
            if status in completed_statuses:
                task_completed += 1
    task_completion_rate = round((task_completed / task_total) * 100, 1) if task_total else None

    ratios = [float(r["detection_ratio"]) for r in audit_rows if r["detection_ratio"] is not None]
    audit_ratio_avg = round(sum(ratios) / len(ratios), 4) if ratios else None
    high_risk_count = sum(1 for r in ratios if r >= 0.6)

    top_event_types = [
        {"event": ev, "count": cnt}
        for ev, cnt in event_counter.most_common(6)
    ]
    top_reasons = [
        {"reason": rsn, "count": cnt}
        for rsn, cnt in reason_counter.most_common(5)
    ]
    top_social_actions = [
        {"action": action, "count": cnt}
        for action, cnt in social_action_counter.most_common(8)
    ]
    top_social_platforms = [
        {"platform": platform, "count": cnt}
        for platform, cnt in social_platform_counter.most_common(8)
    ]

    learning_signals: list[dict] = []
    if mood_delta is not None:
        learning_signals.append(
            {
                "title": "Mood trend",
                "value": "improving" if mood_delta > 0 else "declining" if mood_delta < 0 else "flat",
                "detail": f"Delta {mood_delta:+.2f} vs previous period",
            }
        )
    if task_completion_rate is not None:
        learning_signals.append(
            {
                "title": "Task follow-through",
                "value": f"{task_completion_rate}%",
                "detail": f"{task_completed}/{task_total} tasks completed",
            }
        )
    if audit_ratio_avg is not None:
        learning_signals.append(
            {
                "title": "Audit risk pressure",
                "value": f"{high_risk_count} high-risk",
                "detail": f"Avg detection ratio {audit_ratio_avg:.4f}",
            }
        )
    if top_reasons:
        learning_signals.append(
            {
                "title": "Most common trigger",
                "value": top_reasons[0]["reason"],
                "detail": f"Seen {top_reasons[0]['count']} times",
            }
        )

    behavior_filtered = []
    behavior_event_counter: Counter[str] = Counter()
    command_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    ai_alert_status_reports = 0
    battery_points = []

    for row in behavior_rows:
        log_device_id = str(row["device_id"] or "").strip() or None
        if scoped_device_id and log_device_id and log_device_id != scoped_device_id:
            continue
        if allowed_devices is not None and log_device_id and log_device_id not in allowed_devices:
            continue

        behavior_filtered.append(row)
        source = str(row["source"] or "unknown")
        event_type = str(row["event_type"] or "unknown")
        event_value = str(row["event_value"] or "")

        source_counter[source] += 1
        behavior_event_counter[event_type] += 1
        if source == "handler_command" and event_value:
            command_counter[event_value] += 1

        payload = {}
        raw_payload = row["payload_json"]
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except Exception:
                payload = {}

        if source == "device_status":
            if payload.get("ai_alert") is True:
                ai_alert_status_reports += 1
            if payload.get("battery_pct") is not None:
                try:
                    battery_points.append(int(payload.get("battery_pct")))
                except Exception:
                    pass

    avg_battery = round(sum(battery_points) / len(battery_points), 1) if battery_points else None
    low_battery_count = sum(1 for b in battery_points if b <= 20)

    vital_filtered = []
    hr_points = []
    step_points = []
    for row in vital_rows:
        v_device_id = str(row["device_id"] or "").strip() or None
        if scoped_device_id and v_device_id and v_device_id != scoped_device_id:
            continue
        if allowed_devices is not None and v_device_id and v_device_id not in allowed_devices:
            continue

        vital_filtered.append(row)
        try:
            hr = int(row["heart_rate"] or 0)
        except Exception:
            hr = 0
        try:
            steps = int(row["steps"] or 0)
        except Exception:
            steps = 0
        if hr > 0:
            hr_points.append(hr)
        if steps > 0:
            step_points.append(steps)

    avg_heart_rate = round(sum(hr_points) / len(hr_points), 1) if hr_points else None
    max_heart_rate = max(hr_points) if hr_points else None
    step_total = sum(step_points) if step_points else 0
    step_avg = round(step_total / len(step_points), 1) if step_points else None

    if command_counter:
        top_cmd, top_cmd_count = command_counter.most_common(1)[0]
        learning_signals.append(
            {
                "title": "Most used handler command",
                "value": top_cmd,
                "detail": f"Issued {top_cmd_count} times in window",
            }
        )
    if avg_battery is not None:
        learning_signals.append(
            {
                "title": "Battery stability",
                "value": f"avg {avg_battery}%",
                "detail": f"Low-battery reports (<=20%): {low_battery_count}",
            }
        )
    if ai_alert_status_reports:
        learning_signals.append(
            {
                "title": "AI alert pressure",
                "value": str(ai_alert_status_reports),
                "detail": "AI alert flags seen in device status reports",
            }
        )
    if avg_heart_rate is not None:
        learning_signals.append(
            {
                "title": "On-device vitals",
                "value": f"avg {avg_heart_rate} bpm",
                "detail": (
                    f"Peak {max_heart_rate} bpm, total logged steps {step_total}"
                    if max_heart_rate is not None
                    else f"Total logged steps {step_total}"
                ),
            }
        )
    if phone_event_counter:
        top_phone_event, top_phone_count = phone_event_counter.most_common(1)[0]
        learning_signals.append(
            {
                "title": "Phone-side trigger hotspot",
                "value": top_phone_event,
                "detail": f"Observed {top_phone_count} times in window",
            }
        )
    if top_social_actions:
        top_social = top_social_actions[0]
        learning_signals.append(
            {
                "title": "Top social interaction",
                "value": top_social["action"],
                "detail": f"Observed {top_social['count']} interactions",
            }
        )

    top_behavior_events = [
        {"event": ev, "count": cnt}
        for ev, cnt in behavior_event_counter.most_common(8)
    ]
    top_commands = [
        {"command": ev, "count": cnt}
        for ev, cnt in command_counter.most_common(8)
    ]

    return {
        "days": selected_days,
        "device_id": scoped_device_id,
        "window_start": cutoff_iso,
        "event_count": len(filtered_events),
        "checkin_count": len(checkin_rows),
        "task_count": task_total,
        "audit_count": len(audit_rows),
        "mood_avg": mood_avg,
        "mood_delta": mood_delta,
        "task_completion_rate": task_completion_rate,
        "audit_ratio_avg": audit_ratio_avg,
        "high_risk_count": high_risk_count,
        "top_event_types": top_event_types,
        "top_reasons": top_reasons,
        "social_interaction_count": sum(social_action_counter.values()),
        "top_social_actions": top_social_actions,
        "top_social_platforms": top_social_platforms,
        "phone_event_count": sum(phone_event_counter.values()),
        "top_phone_events": [
            {"event": ev, "count": cnt}
            for ev, cnt in phone_event_counter.most_common(8)
        ],
        "hourly_activity": hourly_hist,
        "vitals_sample_count": len(vital_filtered),
        "avg_heart_rate": avg_heart_rate,
        "max_heart_rate": max_heart_rate,
        "step_total": step_total,
        "step_avg": step_avg,
        "behavior_log_count": len(behavior_filtered),
        "behavior_sources": [{"source": src, "count": cnt} for src, cnt in source_counter.most_common(6)],
        "top_behavior_events": top_behavior_events,
        "top_commands": top_commands,
        "avg_battery": avg_battery,
        "low_battery_count": low_battery_count,
        "ai_alert_status_reports": ai_alert_status_reports,
        "learning_signals": learning_signals,
    }


@router.get("/api/handler/tpe/qr")
def handler_tpe_pairing_qr(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
):
    """Serve the TPE pairing QR for the JWT-authenticated handler panel."""
    return _render_tpe_pairing_qr_png(db)


@router.post("/api/handler/tpe/pairing-code")
def handler_tpe_pairing_code(
    _current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
):
    """Generate a one-time pairing code for manual app pairing in handler panel."""
    return _create_tpe_pairing_code(db)


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
