"""
routers/tpe.py – TPE Accountability Partner integration

Wires the mochii.live backend into the TPE Android app
(https://github.com/lonelydwarffiles/tpeapp), acting as a drop-in replacement
for the reference Node.js backend while extending it with the full mochii admin
surface.

Device-facing endpoints (URL paths match what the app hardcodes):
  POST /api/pair              – Register device FCM token (QR code pairing)
  POST /api/audit/upload      – Upload adherence audit video + ML scores
  POST /api/tpe/webhook       – Receive punishment/reward consequence events
  POST /api/tpe/task/status   – Report task completion / failure from device
  POST /api/tpe/checkin       – Daily mood/compliance check-in from device

Admin endpoints (HTTP Basic auth):
  GET    /api/admin/tpe/devices              – List paired devices
  DELETE /api/admin/tpe/devices/{token}      – Unpair a device
  GET    /api/admin/tpe/settings             – Get current filter/remote-control settings
  PATCH  /api/admin/tpe/settings             – Update settings
  POST   /api/admin/tpe/push                 – Push a raw FCM message to all paired devices
  GET    /api/admin/tpe/events               – List consequence events (punishment / reward log)
  GET    /api/admin/tpe/audits               – List audit upload records

  Task Assignment & Verification (mirrors TPE app's Task system):
  POST   /api/admin/tpe/tasks                – Create a task and push TASK_ASSIGNED FCM
  GET    /api/admin/tpe/tasks                – List all tasks
  GET    /api/admin/tpe/tasks/{task_id}      – Get a single task
  PATCH  /api/admin/tpe/tasks/{task_id}      – Update task status manually
  DELETE /api/admin/tpe/tasks/{task_id}      – Delete a task

  Daily Check-ins:
  GET    /api/admin/tpe/checkins             – List check-in history
  POST   /api/admin/tpe/checkins/request     – Push REQUEST_CHECKIN FCM to prompt a check-in

  Rule Reminders:
  POST   /api/admin/tpe/rules                – Create a rule
  GET    /api/admin/tpe/rules                – List all active rules
  DELETE /api/admin/tpe/rules/{rule_id}      – Delete a rule
  POST   /api/admin/tpe/rules/{rule_id}/remind – Push a RULE_REMINDER FCM immediately

FCM delivery
------------
firebase-admin is initialised lazily the first time a push is needed.
Credentials are loaded from (in priority order):
  1. GOOGLE_APPLICATION_CREDENTIALS env var  (path to service-account JSON file)
  2. ``tpe_fcm_service_account_json`` settings key  (JSON content stored in DB)

If neither is present the push endpoint returns 503 until credentials are added.

Environment variables
---------------------
TPE_PAIRING_TOKEN   Shared secret encoded in the partner QR code
TPE_WEBHOOK_SECRET  Bearer token the Android app sends with webhook events
TPE_AUDIT_PATH      Directory for uploaded audit videos  (default /app/data/tpe_audits)
GOOGLE_APPLICATION_CREDENTIALS  Path to Firebase service-account JSON (optional)
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, root_validator, validator

from db import get_db, get_db_connection
from dependencies import get_admin_user
from discord_webhook import maybe_send_counter_update_notification
from mqtt_client import mqtt_client as _mqtt_client, reload_mqtt
from routers.ws_manager import handler_ws as _handler_ws

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TPE_PAIRING_TOKEN  = os.environ.get("TPE_PAIRING_TOKEN", "")
_TPE_WEBHOOK_SECRET = os.environ.get("TPE_WEBHOOK_SECRET", "")
_TPE_AUDIT_PATH     = Path(os.environ.get("TPE_AUDIT_PATH", "/app/data/tpe_audits"))
_TPE_UPLOAD_PATH    = Path(os.environ.get("TPE_UPLOAD_PATH", "/app/data/tpe_uploads"))

_MAX_AUDIT_VIDEO_BYTES  = 200 * 1024 * 1024  # 200 MB
_MAX_UPLOAD_BYTES       = 50  * 1024 * 1024  # 50 MB for screenshots / short recordings
_CHUNK_SIZE_BYTES       = 256 * 1024          # 256 KB read chunks for streaming uploads
_MQTT_COMMAND_TOPIC_SUFFIX_RE = re.compile(r"/\{device_id\}/commands/?$")
_PAIRING_CODE_TTL_SECONDS = int(os.environ.get("TPE_PAIRING_CODE_TTL_SECONDS", "600"))
_TPE_AUTO_PAIR_ENABLED = os.environ.get("TPE_AUTO_PAIR_ENABLED", "true")
_TPE_AUTO_PAIR_KEY = os.environ.get("TPE_AUTO_PAIR_KEY", "")

# ---------------------------------------------------------------------------
# MQTT dispatch
# ---------------------------------------------------------------------------


def _ensure_mqtt_ready(db: sqlite3.Connection) -> None:
    _mqtt_client.start(db)
    if not _mqtt_client.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "MQTT is not configured. "
                "Set MQTT_BROKER_HOST (or tpe_mqtt_broker_host in settings)."
            ),
        )
    if not _mqtt_client.wait_until_connected(timeout=5.0):
        detail = "MQTT broker is enabled but not connected yet. Try again in a few seconds."
        if _mqtt_client.last_connect_rc is not None:
            detail += f" last_rc={_mqtt_client.last_connect_rc}"
        if _mqtt_client.last_connect_error:
            detail += f" last_error={_mqtt_client.last_connect_error}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )


def _known_device_ids(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        "SELECT device_id FROM handler_device_status WHERE device_id IS NOT NULL"
    ).fetchall()
    return [did for did in (str(r["device_id"]).strip() for r in rows) if did]


def _send_mqtt_to_all(db: sqlite3.Connection, data: dict[str, str]) -> dict[str, int]:
    """Publish a command payload to all known devices over MQTT."""
    _ensure_mqtt_ready(db)
    device_ids = _known_device_ids(db)
    if not device_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No known devices registered.",
        )

    sent = failed = 0
    for device_id in device_ids:
        topic = _mqtt_client.topic_for_device_command(device_id)
        if _mqtt_client.publish_json(topic, data, qos=1):
            sent += 1
        else:
            failed += 1
    logger.info("TPE MQTT push: sent=%d failed=%d", sent, failed)
    return {"sent": sent, "failed": failed}


def _send_mqtt_to_device(db: sqlite3.Connection, device_id: str, data: dict[str, str]) -> dict[str, int]:
    """Publish a command payload to one device over MQTT."""
    sanitized_device_id = (device_id or "").strip()
    if not sanitized_device_id:
        raise HTTPException(status_code=400, detail="device_id is required.")

    _ensure_mqtt_ready(db)

    topic = _mqtt_client.topic_for_device_command(sanitized_device_id)
    if _mqtt_client.publish_json(topic, data, qos=1):
        logger.info("TPE MQTT targeted push: sent=1 device=%s", sanitized_device_id)
        return {"sent": 1, "failed": 0}
    logger.warning("TPE MQTT targeted push failed for device %s", sanitized_device_id)
    return {"sent": 0, "failed": 1}


def _send_fcm_to_all(db: sqlite3.Connection, data: dict[str, str]) -> dict[str, int]:
    """Backward-compatible alias for existing callers."""
    return _send_mqtt_to_all(db, data)


def _send_fcm_to_token(db: sqlite3.Connection, fcm_token: str, data: dict[str, str]) -> dict[str, int]:
    """Backward-compatible alias that resolves token→device_id when available."""
    row = db.execute(
        "SELECT device_id FROM handler_device_status WHERE fcm_token = ? LIMIT 1",
        (fcm_token,),
    ).fetchone()
    if not row or not str(row["device_id"] or "").strip():
        raise HTTPException(
            status_code=404,
            detail="No device_id mapping found for the provided token.",
        )
    return _send_mqtt_to_device(db, str(row["device_id"]).strip(), data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename_part(value: str, max_len: int = 64) -> str:
    """Strip any characters that could cause path traversal or filesystem issues.

    Allows only ASCII alphanumeric characters, hyphens, and underscores.
    This is used to sanitize user-supplied values (e.g. task_id, file extension)
    before embedding them into filesystem paths.
    """
    sanitised = re.sub(r"[^A-Za-z0-9_\-]", "_", value)
    return sanitised[:max_len]


def _ext_from_content_type(content_type: str, fallback: str = ".bin") -> str:
    """Return a safe file extension derived solely from a MIME content-type string.

    Only inspects the MIME type portion — no user-supplied filename is used —
    so the result is always a fixed known extension, never attacker-controlled.
    """
    mime = content_type.lower().split(";")[0].strip()
    _MAP = {
        "image/jpeg":       ".jpg",
        "image/jpg":        ".jpg",
        "image/png":        ".png",
        "image/gif":        ".gif",
        "image/webp":       ".webp",
        "video/mp4":        ".mp4",
        "video/webm":       ".webm",
        "application/octet-stream": fallback,
    }
    return _MAP.get(mime, fallback)


def _effective_pairing_token(db: sqlite3.Connection) -> str:
    """Return the active pairing token: settings table > env var."""
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'tpe_pairing_token'"
    ).fetchone()
    return (row["value"].strip() if row and row["value"] else "") or _TPE_PAIRING_TOKEN


def _resolve_setting(
    db: sqlite3.Connection,
    env_key: str,
    db_key: str,
    default: str = "",
) -> str:
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val != "":
        return env_val
    setting_row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (db_key,),
    ).fetchone()
    if setting_row and setting_row["value"] is not None and setting_row["value"] != "":
        return str(setting_row["value"]).strip()
    return default


def _parse_bool_string(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_value(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row["value"] is not None:
        return str(row["value"])
    return default


def _setting_int(db: sqlite3.Connection, key: str, default: int = 0) -> int:
    raw = _setting_value(db, key, str(default)).strip()
    try:
        return int(raw)
    except Exception:
        return default


def _set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    now = _now_iso()
    db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )


def _build_tpe_pairing_payload(db: sqlite3.Connection) -> Dict[str, str]:
    pairing_token = _effective_pairing_token(db)
    if not pairing_token:
        raise HTTPException(
            status_code=503,
            detail="TPE pairing token is not configured. Set TPE_PAIRING_TOKEN.",
        )

    base_url = os.environ.get("BASE_URL", "")
    row = db.execute("SELECT value FROM settings WHERE key = 'base_url'").fetchone()
    if row and row["value"]:
        base_url = row["value"].rstrip("/")

    if base_url.startswith("https://"):
        ws_base = "wss://" + base_url[len("https://"):]
    elif base_url.startswith("http://"):
        ws_base = "ws://" + base_url[len("http://"):]
    else:
        ws_base = base_url

    webhook_secret = _effective_webhook_secret(db)

    signaling_url = ""
    live_session = db.execute(
        "SELECT id FROM tpe_review_sessions WHERE ended_at IS NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if live_session and ws_base:
        signaling_url = f"{ws_base}/api/tpe/signal/{live_session['id']}"

    qr_payload: Dict[str, str] = {
        "endpoint": base_url,
        "pairing_token": pairing_token,
    }
    if webhook_secret:
        qr_payload["webhook_secret"] = webhook_secret
    if signaling_url:
        qr_payload["signaling_url"] = signaling_url

    mqtt_broker_host = _resolve_setting(db, "MQTT_BROKER_HOST", "tpe_mqtt_broker_host", "")
    if mqtt_broker_host:
        mqtt_broker_port = _resolve_setting(db, "MQTT_BROKER_PORT", "tpe_mqtt_broker_port", "1883")
        mqtt_tls_value = _resolve_setting(db, "MQTT_TLS_ENABLED", "tpe_mqtt_tls_enabled", "false")
        mqtt_tls_enabled = _parse_bool_string(mqtt_tls_value)
        mqtt_username = _resolve_setting(db, "MQTT_USERNAME", "tpe_mqtt_username", "")
        mqtt_password = _resolve_setting(db, "MQTT_PASSWORD", "tpe_mqtt_password", "")
        mqtt_topic_template = _resolve_setting(
            db,
            "MQTT_COMMAND_TOPIC_TEMPLATE",
            "tpe_mqtt_command_topic_template",
            "tpeapp/device/{device_id}/commands",
        )
        mqtt_topic_prefix = _MQTT_COMMAND_TOPIC_SUFFIX_RE.sub("", mqtt_topic_template).rstrip("/")

        mqtt_scheme = "mqtts" if mqtt_tls_enabled else "mqtt"
        qr_payload["mqtt_broker_uri"] = f"{mqtt_scheme}://{mqtt_broker_host}:{mqtt_broker_port}"
        if mqtt_username:
            qr_payload["mqtt_username"] = mqtt_username
        if mqtt_password:
            qr_payload["mqtt_password"] = mqtt_password
        if mqtt_topic_prefix:
            qr_payload["mqtt_topic_prefix"] = mqtt_topic_prefix

    return qr_payload


def _render_tpe_pairing_qr_png(db: sqlite3.Connection) -> Response:
    try:
        import qrcode
        from qrcode.image.pil import PilImage
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="qrcode library is not installed. Add qrcode[pil] to requirements.",
        )

    payload = json.dumps(_build_tpe_pairing_payload(db))

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PilImage, fill_color="black", back_color="white")

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


def _cleanup_expired_pairing_codes(db: sqlite3.Connection) -> None:
    db.execute(
        "DELETE FROM tpe_pairing_codes WHERE used_at IS NULL AND expires_at <= ?",
        (_now_iso(),),
    )


def _generate_pairing_code(length: int = 6) -> str:
    alphabet = "23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _create_tpe_pairing_code(db: sqlite3.Connection) -> dict:
    _cleanup_expired_pairing_codes(db)

    payload = _build_tpe_pairing_payload(db)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(_PAIRING_CODE_TTL_SECONDS, 60))
    now_iso = now.isoformat()
    expires_iso = expires_at.isoformat()

    code = ""
    for _ in range(20):
        candidate = _generate_pairing_code()
        row = db.execute(
            "SELECT 1 FROM tpe_pairing_codes WHERE code = ? AND used_at IS NULL AND expires_at > ?",
            (candidate, now_iso),
        ).fetchone()
        if not row:
            code = candidate
            break
    if not code:
        raise HTTPException(status_code=500, detail="Failed to allocate pairing code")

    db.execute(
        "INSERT INTO tpe_pairing_codes (code, payload_json, expires_at, created_at, used_at) VALUES (?, ?, ?, ?, NULL)",
        (code, json.dumps(payload), expires_iso, now_iso),
    )
    db.commit()

    # Never expose pairing token in code-generation responses.
    response_payload = {k: v for k, v in payload.items() if k != "pairing_token"}
    return {
        "pairing_code": code,
        "expires_at": expires_iso,
        "ttl_seconds": max(_PAIRING_CODE_TTL_SECONDS, 60),
        "payload": response_payload,
    }


def _effective_webhook_secret(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'tpe_webhook_secret'"
    ).fetchone()
    return (row["value"].strip() if row and row["value"] else "") or _TPE_WEBHOOK_SECRET


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Device-facing router (no prefix – paths match tpeapp exactly)
device_router = APIRouter(tags=["tpe-device"])

# Admin router
admin_router = APIRouter(prefix="/api/admin/tpe", tags=["tpe-admin"])


# ---------------------------------------------------------------------------
# DB migration helper (called from main.py's migrate())
# ---------------------------------------------------------------------------

def migrate_tpe(conn: sqlite3.Connection) -> None:
    """Create TPE tables if they don't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_paired_devices (
            fcm_token  TEXT PRIMARY KEY,
            paired_at  TEXT NOT NULL,
            last_seen  TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT NOT NULL,
            reason      TEXT,
            session_ts  INTEGER,
            payload_json TEXT,
            received_at TEXT NOT NULL
        )
        """
    )
    # Add payload_json column to existing installations that predate it.
    try:
        conn.execute("ALTER TABLE tpe_events ADD COLUMN payload_json TEXT")
    except Exception:
        pass  # Column already exists – safe to ignore.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_audit_logs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_ratio  REAL,
            last_label       TEXT,
            last_score       REAL,
            session_ts       INTEGER,
            video_filename   TEXT,
            received_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_tasks (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            deadline_ms  INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            proof_note   TEXT,
            proof_photo  TEXT,
            created_at   TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    # Add proof_photo column to existing installations.
    try:
        conn.execute("ALTER TABLE tpe_tasks ADD COLUMN proof_photo TEXT")
    except Exception:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_checkins (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            mood_score    INTEGER,
            note          TEXT,
            checked_in_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text  TEXT NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_review_sessions (
            id               TEXT PRIMARY KEY,
            created_at       TEXT NOT NULL,
            ended_at         TEXT,
            device_fcm_token TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_uploads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            filename     TEXT NOT NULL,
            content_type TEXT,
            size_bytes   INTEGER,
            received_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_pairing_codes (
            code        TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            used_at     TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_ai_tool_calls (
            id                         INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name                  TEXT NOT NULL,
            device_id                  TEXT NOT NULL,
            action                     TEXT NOT NULL,
            prompt_context             TEXT,
            parameters_json            TEXT,
            payload_json               TEXT NOT NULL,
            transmission_status        TEXT NOT NULL,
            transmission_response_json TEXT,
            error                      TEXT,
            created_at                 TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_ai_social_drafts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            platform            TEXT NOT NULL,
            content             TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            prompt_context      TEXT,
            posted_results_json TEXT,
            created_at          TEXT NOT NULL,
            reviewed_at         TEXT
        )
        """
    )
    conn.commit()


# ===========================================================================
# Device-facing endpoints
# ===========================================================================


class PairRequest(BaseModel):
    fcm_token: str
    pairing_token: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    mqtt_client_id: Optional[str] = None


class PairCodeRequest(BaseModel):
    fcm_token: str
    pairing_code: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    mqtt_client_id: Optional[str] = None


class PairAutoRequest(BaseModel):
    fcm_token: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    mqtt_client_id: Optional[str] = None
    auto_pair_key: Optional[str] = None


@device_router.post("/api/pair")
def tpe_pair(
    body: PairRequest,
    background_tasks: BackgroundTasks,
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Register a TPE device's FCM token.

    The Android app calls this after scanning the partner QR code.
    Body: ``{"fcm_token": "...", "pairing_token": "..."}``
    """
    fcm_token = body.fcm_token.strip()
    if not fcm_token:
        raise HTTPException(status_code=400, detail="Missing or invalid fcm_token")

    expected = _effective_pairing_token(db)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="TPE pairing is not configured. Set TPE_PAIRING_TOKEN.",
        )

    if not secrets.compare_digest(body.pairing_token, expected):
        raise HTTPException(status_code=403, detail="Invalid pairing_token")

    body_device_id = (body.device_id or "").strip()
    device_name = (body.device_name or "").strip() or None
    mqtt_client_id = (body.mqtt_client_id or "").strip()
    header_device_id = (x_device_id or "").strip()
    device_id = body_device_id or mqtt_client_id or header_device_id or fcm_token
    if not device_id:
        raise HTTPException(status_code=400, detail="Missing device identifier")

    # tpeapp uses device_id as its stable token key; store paired row keyed by device_id.
    paired_device_token = device_id

    now = _now_iso()
    db.execute(
        """
        INSERT INTO tpe_paired_devices (fcm_token, paired_at, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(fcm_token) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (paired_device_token, now, now),
    )
    db.execute(
        """
        INSERT INTO handler_device_status (device_id, device_name, fcm_token, is_online, last_seen, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            device_name = COALESCE(excluded.device_name, device_name),
            fcm_token = excluded.fcm_token,
            is_online = 1,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at
        """,
        (device_id, device_name, fcm_token, now, now),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if row:
        background_tasks.add_task(_handler_ws.broadcast, {"type": "status_update", **dict(row)})
    logger.info("TPE device paired/refreshed: %s…", device_id[:16])
    return {"status": "paired"}


@device_router.post("/api/pair/code")
def tpe_pair_with_code(
    body: PairCodeRequest,
    background_tasks: BackgroundTasks,
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    db: sqlite3.Connection = Depends(get_db),
):
    """Pair a device using a short-lived one-time pairing code."""
    fcm_token = body.fcm_token.strip()
    if not fcm_token:
        raise HTTPException(status_code=400, detail="Missing or invalid fcm_token")

    pairing_code = (body.pairing_code or "").strip().upper()
    if not re.fullmatch(r"[2-9]{6}", pairing_code):
        raise HTTPException(status_code=400, detail="pairing_code must be a 6-digit code")

    _cleanup_expired_pairing_codes(db)
    row = db.execute(
        "SELECT payload_json, expires_at, used_at FROM tpe_pairing_codes WHERE code = ?",
        (pairing_code,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Invalid or expired pairing_code")
    if row["used_at"]:
        raise HTTPException(status_code=409, detail="pairing_code already used")

    now_iso = _now_iso()
    if str(row["expires_at"]) <= now_iso:
        db.execute("DELETE FROM tpe_pairing_codes WHERE code = ?", (pairing_code,))
        db.commit()
        raise HTTPException(status_code=410, detail="pairing_code expired")

    payload: dict[str, Any]
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}

    token_from_code = str(payload.get("pairing_token") or "").strip()
    expected = _effective_pairing_token(db)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="TPE pairing is not configured. Set TPE_PAIRING_TOKEN.",
        )
    if not token_from_code or not secrets.compare_digest(token_from_code, expected):
        raise HTTPException(status_code=409, detail="pairing_code is no longer valid")

    body_device_id = (body.device_id or "").strip()
    device_name = (body.device_name or "").strip() or None
    mqtt_client_id = (body.mqtt_client_id or "").strip()
    header_device_id = (x_device_id or "").strip()
    device_id = body_device_id or mqtt_client_id or header_device_id or fcm_token
    if not device_id:
        raise HTTPException(status_code=400, detail="Missing device identifier")

    now = _now_iso()
    db.execute(
        """
        INSERT INTO tpe_paired_devices (fcm_token, paired_at, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(fcm_token) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (device_id, now, now),
    )
    db.execute(
        """
        INSERT INTO handler_device_status (device_id, device_name, fcm_token, is_online, last_seen, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            device_name = COALESCE(excluded.device_name, device_name),
            fcm_token = excluded.fcm_token,
            is_online = 1,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at
        """,
        (device_id, device_name, fcm_token, now, now),
    )
    db.execute(
        "UPDATE tpe_pairing_codes SET used_at = ? WHERE code = ?",
        (now, pairing_code),
    )
    db.commit()

    status_row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if status_row:
        background_tasks.add_task(_handler_ws.broadcast, {"type": "status_update", **dict(status_row)})

    logger.info("TPE device paired via code: %s…", device_id[:16])
    return {
        "status": "paired",
        "endpoint": payload.get("endpoint", ""),
        "webhook_secret": payload.get("webhook_secret", ""),
        "mqtt_broker_uri": payload.get("mqtt_broker_uri", ""),
        "mqtt_username": payload.get("mqtt_username", ""),
        "mqtt_password": payload.get("mqtt_password", ""),
        "mqtt_topic_prefix": payload.get("mqtt_topic_prefix", ""),
        "signaling_url": payload.get("signaling_url", ""),
    }


@device_router.post("/api/pair/auto")
def tpe_pair_auto(
    body: PairAutoRequest,
    background_tasks: BackgroundTasks,
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    x_auto_pair_key: Optional[str] = Header(default=None, alias="X-Auto-Pair-Key"),
    db: sqlite3.Connection = Depends(get_db),
):
    """Auto-pair a device for zero-step enrollment when auto-pair is enabled."""
    auto_pair_enabled_raw = _resolve_setting(
        db,
        "TPE_AUTO_PAIR_ENABLED",
        "tpe_auto_pair_enabled",
        _TPE_AUTO_PAIR_ENABLED,
    )
    if not _parse_bool_string(auto_pair_enabled_raw):
        raise HTTPException(status_code=403, detail="Auto pairing is disabled")

    expected_auto_pair_key = _resolve_setting(
        db,
        "TPE_AUTO_PAIR_KEY",
        "tpe_auto_pair_key",
        _TPE_AUTO_PAIR_KEY,
    )
    if expected_auto_pair_key:
        provided_auto_pair_key = (
            (body.auto_pair_key or "").strip() or (x_auto_pair_key or "").strip()
        )
        if not secrets.compare_digest(provided_auto_pair_key, expected_auto_pair_key):
            raise HTTPException(status_code=401, detail="Invalid auto_pair_key")

    fcm_token = body.fcm_token.strip()
    if not fcm_token:
        raise HTTPException(status_code=400, detail="Missing or invalid fcm_token")

    body_device_id = (body.device_id or "").strip()
    device_name = (body.device_name or "").strip() or None
    mqtt_client_id = (body.mqtt_client_id or "").strip()
    header_device_id = (x_device_id or "").strip()
    device_id = body_device_id or mqtt_client_id or header_device_id or fcm_token
    if not device_id:
        raise HTTPException(status_code=400, detail="Missing device identifier")

    now = _now_iso()
    db.execute(
        """
        INSERT INTO tpe_paired_devices (fcm_token, paired_at, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(fcm_token) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (device_id, now, now),
    )
    db.execute(
        """
        INSERT INTO handler_device_status (device_id, device_name, fcm_token, is_online, last_seen, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            device_name = COALESCE(excluded.device_name, device_name),
            fcm_token = excluded.fcm_token,
            is_online = 1,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at
        """,
        (device_id, device_name, fcm_token, now, now),
    )
    db.commit()

    status_row = db.execute(
        "SELECT * FROM handler_device_status WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if status_row:
        background_tasks.add_task(_handler_ws.broadcast, {"type": "status_update", **dict(status_row)})

    response_payload: dict[str, Any] = {}
    try:
        response_payload = _build_tpe_pairing_payload(db)
    except HTTPException:
        # Pairing token may be intentionally unset in auto-pair mode.
        response_payload = {}

    logger.info("TPE device auto-paired: %s…", device_id[:16])
    return {
        "status": "paired",
        "auto_paired": True,
        "endpoint": response_payload.get("endpoint", ""),
        "webhook_secret": response_payload.get("webhook_secret", ""),
        "mqtt_broker_uri": response_payload.get("mqtt_broker_uri", ""),
        "mqtt_username": response_payload.get("mqtt_username", ""),
        "mqtt_password": response_payload.get("mqtt_password", ""),
        "mqtt_topic_prefix": response_payload.get("mqtt_topic_prefix", ""),
        "signaling_url": response_payload.get("signaling_url", ""),
    }


@device_router.post("/api/audit/upload")
async def tpe_audit_upload(
    video: UploadFile = File(...),
    scores: str = Form(default="{}"),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive an adherence audit video + ML detection scores from the Android app.

    Multipart form fields:
      ``video``   – .mp4 file (max 200 MB)
      ``scores``  – JSON string:
                    ``{"detection_ratio": 0.8, "last_label": "...", "last_score": 0.9, "session_ts": 1234567890}``
    """
    if video.content_type not in ("video/mp4", "application/octet-stream"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {video.content_type}. Only video/mp4 is accepted.",
        )

    _TPE_AUDIT_PATH.mkdir(parents=True, exist_ok=True)

    filename = f"audit_{int(datetime.now(timezone.utc).timestamp() * 1000)}.mp4"
    dest = _TPE_AUDIT_PATH / filename

    total = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await video.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_AUDIT_VIDEO_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail="Audit video exceeds the 200 MB size limit.",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("TPE audit video save failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save audit video.")

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(scores)
    except Exception:
        pass

    now = _now_iso()
    db.execute(
        """
        INSERT INTO tpe_audit_logs
            (detection_ratio, last_label, last_score, session_ts, video_filename, received_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            parsed.get("detection_ratio"),
            parsed.get("last_label"),
            parsed.get("last_score"),
            parsed.get("session_ts"),
            filename,
            now,
        ),
    )
    db.commit()
    logger.info(
        "TPE audit received: file=%s detection_ratio=%s",
        filename,
        parsed.get("detection_ratio"),
    )
    return {"status": "received", "file": filename, "scores": parsed}


@device_router.post("/api/tpe/webhook")
async def tpe_webhook(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive punishment/reward consequence events dispatched by the Android app's
    ``ConsequenceDispatcher`` via ``WebhookManager``.

    Expected body:
    ``{"event": "punishment"|"reward", "reason": "...", "timestamp": <epoch_ms>}``

    The app must be configured with:
      Webhook URL:   ``https://<your-domain>/api/tpe/webhook``
      Bearer token:  value of ``TPE_WEBHOOK_SECRET`` / ``tpe_webhook_secret`` setting
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = body.get("event", "unknown")
    reason = body.get("reason", "")
    session_ts = body.get("timestamp")

    if not event or not isinstance(event, str):
        raise HTTPException(
            status_code=400, detail="event must be a non-empty string"
        )

    # Capture the full payload for richer event types (e.g. device_location,
    # app_inventory, app_installed / app_uninstalled, override_used).
    payload_json: Optional[str] = None
    try:
        payload_json = json.dumps(body)
    except Exception:
        pass

    db.execute(
        "INSERT INTO tpe_events (event, reason, session_ts, payload_json, received_at) VALUES (?, ?, ?, ?, ?)",
        (event, reason, session_ts, payload_json, _now_iso()),
    )
    db.commit()

    normalized_event = str(event or "").strip().lower()
    if normalized_event in {"edge_recorded", "orgasm_recorded"}:
        # Keep public counters in sync with app-side edge/orgasm quick buttons.
        edge_val = body.get("edge_count")
        orgasm_val = body.get("orgasm_count")
        try:
            parsed_edge = int(edge_val) if edge_val is not None else None
        except Exception:
            parsed_edge = None
        try:
            parsed_orgasm = int(orgasm_val) if orgasm_val is not None else None
        except Exception:
            parsed_orgasm = None

        if normalized_event == "edge_recorded":
            current = _setting_int(db, "public_tasks_completed", 0)
            next_val = parsed_edge if parsed_edge is not None else max(current + 1, 0)
            _set_setting(db, "public_tasks_completed", str(max(next_val, 0)))
        else:
            current = _setting_int(db, "public_confessions_posted", 0)
            next_val = parsed_orgasm if parsed_orgasm is not None else max(current + 1, 0)
            _set_setting(db, "public_confessions_posted", str(max(next_val, 0)))

        db.commit()

        try:
            await maybe_send_counter_update_notification(
                event_type=normalized_event,
                edge_count=body.get("edge_count"),
                orgasm_count=body.get("orgasm_count"),
                source=body.get("source") or "tpe_webhook",
                device_id=body.get("device_id") or "",
                timestamp_ms=body.get("timestamp"),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Counter Discord notification failed for tpe webhook: %s", exc)

    logger.info("TPE webhook: event=%s reason=%r", event, reason)
    return {"status": "received"}


@device_router.get("/api/tpe/notification-command-policy")
def tpe_get_notification_command_policy(
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return runtime notification command policy for the paired device app.

    Reads JSON from ``settings.tpe_notification_command_policy_json``.
    Auth uses the same optional Bearer webhook secret as ``/api/tpe/webhook``.
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    row = db.execute(
        "SELECT value FROM settings WHERE key = 'tpe_notification_command_policy_json'"
    ).fetchone()
    raw = (row["value"] if row and row["value"] is not None else "")
    if not str(raw).strip():
        return {}

    try:
        parsed = json.loads(str(raw))
    except Exception:
        logger.warning("Invalid tpe_notification_command_policy_json; returning empty policy")
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


# ===========================================================================
# Admin endpoints
# ===========================================================================


@admin_router.get("/devices")
def tpe_list_devices(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all paired TPE devices."""
    rows = db.execute(
        "SELECT fcm_token, paired_at, last_seen FROM tpe_paired_devices ORDER BY paired_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.delete("/devices/{fcm_token}")
def tpe_unpair_device(
    fcm_token: str,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Unpair (remove) a TPE device by its FCM token."""
    cur = db.execute(
        "DELETE FROM tpe_paired_devices WHERE fcm_token = ?", (fcm_token,)
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"status": "unpaired"}


# Settings keys kept in the main settings table under tpe_* namespace.
_TPE_SETTING_KEYS = {
    "tpe_pairing_token",
    "tpe_webhook_secret",
    "tpe_fcm_service_account_json",
    "tpe_filter_threshold",
    "tpe_filter_strict_mode",
    "tpe_filter_blocked_classes",
    "tpe_notification_blocklist",
    "tpe_restricted_vocabulary",
    "tpe_strict_tone_mode",
    "tpe_notification_command_policy_json",
    "tpe_mqtt_broker_host",
    "tpe_mqtt_broker_port",
    "tpe_mqtt_username",
    "tpe_mqtt_password",
    "tpe_mqtt_client_id",
    "tpe_mqtt_keepalive",
    "tpe_mqtt_tls_enabled",
    "tpe_mqtt_tls_ca_cert",
    "tpe_mqtt_tls_client_cert",
    "tpe_mqtt_tls_client_key",
    "tpe_mqtt_tls_insecure",
    "tpe_mqtt_command_topic_template",
    "tpe_mqtt_signaling_topic_template",
    "tpe_mqtt_device_signaling_topic_template",
    "tpe_mqtt_presence_enabled",
    "tpe_mqtt_presence_heartbeat_topic",
    "tpe_mqtt_presence_status_topic",
}

_TPE_MQTT_SETTING_KEYS = {key for key in _TPE_SETTING_KEYS if key.startswith("tpe_mqtt_")}


@admin_router.get("/settings")
def tpe_get_settings(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return current TPE filter/remote-control settings."""
    rows = db.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'tpe_%'"
    ).fetchall()
    result = {r["key"]: r["value"] for r in rows}
    # Redact sensitive fields
    for secret_key in (
        "tpe_pairing_token",
        "tpe_webhook_secret",
        "tpe_fcm_service_account_json",
        "tpe_mqtt_password",
        "tpe_mqtt_tls_client_key",
    ):
        if result.get(secret_key):
            result[secret_key] = "***"
    return result


class TpeSettingsPatch(BaseModel):
    tpe_pairing_token: Optional[str] = None
    tpe_webhook_secret: Optional[str] = None
    tpe_fcm_service_account_json: Optional[str] = None
    tpe_filter_threshold: Optional[str] = None
    tpe_filter_strict_mode: Optional[str] = None
    tpe_filter_blocked_classes: Optional[str] = None
    tpe_notification_blocklist: Optional[str] = None
    tpe_restricted_vocabulary: Optional[str] = None
    tpe_strict_tone_mode: Optional[str] = None
    tpe_notification_command_policy_json: Optional[str] = None
    tpe_mqtt_broker_host: Optional[str] = None
    tpe_mqtt_broker_port: Optional[str] = None
    tpe_mqtt_username: Optional[str] = None
    tpe_mqtt_password: Optional[str] = None
    tpe_mqtt_client_id: Optional[str] = None
    tpe_mqtt_keepalive: Optional[str] = None
    tpe_mqtt_tls_enabled: Optional[str] = None
    tpe_mqtt_tls_ca_cert: Optional[str] = None
    tpe_mqtt_tls_client_cert: Optional[str] = None
    tpe_mqtt_tls_client_key: Optional[str] = None
    tpe_mqtt_tls_insecure: Optional[str] = None
    tpe_mqtt_command_topic_template: Optional[str] = None
    tpe_mqtt_signaling_topic_template: Optional[str] = None
    tpe_mqtt_device_signaling_topic_template: Optional[str] = None
    tpe_mqtt_presence_enabled: Optional[str] = None
    tpe_mqtt_presence_heartbeat_topic: Optional[str] = None
    tpe_mqtt_presence_status_topic: Optional[str] = None


@admin_router.patch("/settings")
def tpe_update_settings(
    body: TpeSettingsPatch,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update one or more TPE settings."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    for key, value in updates.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()
    response: dict[str, Any] = {"updated": list(updates.keys())}
    mqtt_updates = sorted(key for key in updates if key in _TPE_MQTT_SETTING_KEYS)
    if mqtt_updates:
        try:
            reload_mqtt(db)
            response["mqtt_reloaded"] = True
        except Exception as exc:
            logger.exception(
                "Failed to reload MQTT after TPE settings update (keys: %s)",
                mqtt_updates,
            )
            response["mqtt_reloaded"] = False
            response["mqtt_error"] = str(exc)
        response["mqtt_enabled"] = _mqtt_client.enabled
    return response


@admin_router.post("/pairing-code")
def tpe_create_pairing_code(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Generate a short-lived one-time pairing code for manual app pairing."""
    return _create_tpe_pairing_code(db)


class TpePushRequest(BaseModel):
    """
    Flexible FCM push payload.  ``action`` maps to the FCM ``data.action`` field
    understood by ``PartnerFcmService`` in the TPE app.

    All field values are strings (FCM data payloads are string-only).
    Pass ``device_id`` to target a specific device; omit to broadcast to all
    paired devices.

    Field reference by action group:
      UPDATE_SETTINGS              threshold, strict, blocked_classes, nudenet_enabled
      UPDATE_NOTIFICATION_BLOCKLIST blocklist (JSON array str)
      UPDATE_RESTRICTED_VOCABULARY  vocabulary (JSON array str)
      UPDATE_TONE_COMPLIANCE        strict_tone_mode (bool str)
      SEND_NOTIFICATION            title, body, channel_id (opt)
      CLEAR_NOTIFICATIONS          (no fields)
      NEW_QUESTION                 question_id, question_preview (opt)
      RULE_REMINDER                rule_id, rule_text
      REQUEST_CHECKIN              (no fields)
      TASK_ASSIGNED                task_id, task_title, task_desc (opt), deadline_ms
      SET_RITUALS                  steps (JSON array str)
      SET_RITUAL_TIMES             morning_minutes, evening_minutes
      SET_HONORIFIC                honorific
      SET_HONORIFIC_ENABLED        enabled (bool str)
    SET_DISCORD_QL_HONORIFIC     honorific
    SET_DISCORD_QL_HONORIFIC_ENABLED enabled (bool str)
    SET_DISCORD_HONORIFIC_USERS  users (JSON array str)
    ADD_DISCORD_HONORIFIC_USER   user
    REMOVE_DISCORD_HONORIFIC_USER user
      SET_PTS_ENABLED              enabled (bool str)
      SET_PTS_APPROVED             packages (JSON array str)
      APP_PERMISSION_RESPONSE      request_id, granted (bool str)
      SET_GATING_ENABLED           enabled (bool str)
      SET_GATING_APPROVED          packages (JSON array str)
      SET_GEOFENCES                geofences (JSON array str)
      SET_GEOFENCE_ENABLED         enabled (bool str)
      SET_AFFIRMATIONS             affirmations (JSON array str)
      SHOW_AFFIRMATION             text
      SET_MANTRA_ENABLED           enabled (bool str)
      SET_MANTRA_INTERVAL          minutes (int str)
      START_CORNER_TIME            duration_minutes, title (opt)
      CANCEL_ESCALATION            (no fields)
      SET_SUB_STATUS               status
      SET_HANDLER_SYSTEM_PROMPT    prompt
      SET_HANDLER_API_KEY          api_key
      SET_HANDLER_ENDPOINT         endpoint
      SET_HANDLER_MODEL            handler_model (→ FCM key "model")
      LOVENSE_COMMAND              toy_command, toy_level (0–20 str)
      SET_LOVENSE_SCHEDULES        schedules (JSON array str)
      PAVLOK_COMMAND               pavlok_cmd, pavlok_intensity (0–255), pavlok_duration_ms
      OPEN_APP / FORCE_STOP_APP / DISABLE_APP / ENABLE_APP /
        CLEAR_APP_CACHE / UNINSTALL_APP / SUSPEND_APP / UNSUSPEND_APP  app_name
      OPEN_URL                     url
      SET_BRIGHTNESS               value (0–255 str)
      SCREEN_ON / SCREEN_OFF       (no fields)
      SET_SCREEN_TIMEOUT           ms (int str)
      SHOW_OVERLAY                 title, message, image_url (opt)
      SET_ORIENTATION              landscape (bool str)
      SET_ROTATION / SET_AUTO_ROTATE  enabled (bool str)
      SET_WALLPAPER                url
      SET_VOLUME                   stream, level (0–100 str), max (bool str, opt)
      SET_RINGER_MODE              mode (normal/vibrate/silent)
      PLAY_AUDIO                   url
      SPEAK_TEXT                   text
      LOCK_DEVICE / DISMISS_KEYGUARD  (no fields)
      SET_WIFI / SET_MOBILE_DATA / SET_AIRPLANE_MODE / SET_BLUETOOTH  enabled (bool str)
      CONNECT_WIFI                 ssid, password (opt)
      TAKE_SCREENSHOT / GET_LOCATION  (no fields)
      RECORD_SCREEN                duration_sec (int str)
      SET_FLASHLIGHT               enabled (bool str)
      SET_DND                      policy (all/priority/alarms_only/total_silence)
      SET_ALARM                    title, time_ms (epoch ms str)
      SET_NFC                      enabled (bool str)
      SET_FONT_SIZE                scale (float str, e.g. "1.15")
      START_REVIEW                 session_id, signaling_url
            toy.live.control             toy_mode, toy_command, toy_level, toy_pattern (opt),
                                                                     toy_duration_ms (opt), toy_sequence (opt JSON)
    device.file.read            path, as_base64 (opt), max_bytes (opt)
    device.file.write           path, content or content_base64, append (opt)
    device.file.delete          path
    """

    action: str
    device_id: Optional[str] = None  # target a specific device; omit to broadcast
    command_id: Optional[str] = None  # client-generated correlation ID for execution ACK
    payload: Optional[Dict[str, Any]] = None  # legacy nested payload shape from older panels

    @root_validator(pre=True)
    def _normalize_legacy_payload(cls, values: Any) -> Any:
        """Accept legacy nested payloads and coerce primitive values to strings.

        Older clients may send ``{"payload": {...}}`` and/or numeric/boolean
        values for fields that are stored as strings in FCM data payloads.
        """
        if not isinstance(values, dict):
            return values

        data = dict(values)
        payload = data.get("payload")
        if isinstance(payload, dict):
            for key, raw_value in payload.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                if data.get(key_text) is None:
                    data[key_text] = raw_value

        # Map common legacy keys used by quick-action clients.
        if data.get("toy_command") is None and data.get("command") is not None:
            data["toy_command"] = data.get("command")
        if data.get("toy_level") is None and data.get("intensity") is not None:
            data["toy_level"] = data.get("intensity")
        if data.get("toy_duration_ms") is None and data.get("duration_ms") is not None:
            data["toy_duration_ms"] = data.get("duration_ms")

        for key, raw_value in list(data.items()):
            if key == "payload" or raw_value is None:
                continue
            if isinstance(raw_value, str):
                continue
            if isinstance(raw_value, (dict, list)):
                data[key] = json.dumps(raw_value, ensure_ascii=False)
            else:
                data[key] = str(raw_value)

        return data

    # UPDATE_SETTINGS / NudeNet
    threshold: Optional[str] = None
    strict: Optional[str] = None
    blocked_classes: Optional[str] = None
    nudenet_enabled: Optional[str] = None

    # UPDATE_NOTIFICATION_BLOCKLIST
    blocklist: Optional[str] = None

    # UPDATE_RESTRICTED_VOCABULARY
    vocabulary: Optional[str] = None

    # UPDATE_TONE_COMPLIANCE
    strict_tone_mode: Optional[str] = None

    # UPDATE_TEXT_REPLACEMENT_DICT
    text_replacement_dict: Optional[str] = None

    # SEND_NOTIFICATION
    title: Optional[str] = None
    body: Optional[str] = None
    channel_id: Optional[str] = None
    can_reply: Optional[str] = None
    thread_id: Optional[str] = None

    # SHOW_OVERLAY
    message: Optional[str] = None
    image_url: Optional[str] = None

    # NEW_QUESTION
    question_id: Optional[str] = None
    question_preview: Optional[str] = None

    # Generic identifier (used by id-addressed actions such as VAULT_*)
    id: Optional[str] = None

    # RULE_REMINDER
    rule_id: Optional[str] = None
    rule_text: Optional[str] = None

    # TASK_ASSIGNED
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    task_desc: Optional[str] = None
    deadline_ms: Optional[str] = None

    # SET_RITUALS
    steps: Optional[str] = None

    # SET_RITUAL_TIMES
    morning_minutes: Optional[str] = None
    evening_minutes: Optional[str] = None

    # SET_HONORIFIC
    honorific: Optional[str] = None
    users: Optional[str] = None
    user: Optional[str] = None

    # SET_PTS_APPROVED / SET_GATING_APPROVED
    packages: Optional[str] = None

    # APP_PERMISSION_RESPONSE
    request_id: Optional[str] = None
    granted: Optional[str] = None

    # Generic boolean toggle (SET_HONORIFIC_ENABLED, SET_PTS_ENABLED, SET_GATING_ENABLED,
    # SET_GEOFENCE_ENABLED, SET_MANTRA_ENABLED, SET_WIFI, SET_MOBILE_DATA,
    # SET_AIRPLANE_MODE, SET_BLUETOOTH, SET_FLASHLIGHT, SET_NFC, SET_ROTATION,
    # SET_AUTO_ROTATE, VAULT_SET_CHANGE_BLOCK, etc.)
    enabled: Optional[str] = None

    # SET_GEOFENCES
    geofences: Optional[str] = None

    # SET_AFFIRMATIONS
    affirmations: Optional[str] = None

    # SPEAK_TEXT / SHOW_AFFIRMATION
    text: Optional[str] = None

    # SET_MANTRA_INTERVAL
    minutes: Optional[str] = None

    # START_CORNER_TIME (duration_minutes also used by VAULT_LOCK_*)
    duration_minutes: Optional[str] = None

    # SET_SUB_STATUS
    status: Optional[str] = None

    # SET_HANDLER_SYSTEM_PROMPT
    prompt: Optional[str] = None

    # SET_HANDLER_API_KEY
    api_key: Optional[str] = None

    # SET_HANDLER_ENDPOINT
    endpoint: Optional[str] = None

    # SET_HANDLER_MODEL — named handler_model here to avoid Pydantic v2 reserved-name
    # collision; serialized as "model" in the FCM data dict.
    handler_model: Optional[str] = None

    # LOVENSE_COMMAND
    toy_command: Optional[str] = None
    toy_level: Optional[str] = None

    # toy.live.control
    toy_mode: Optional[str] = None
    toy_pattern: Optional[str] = None
    toy_duration_ms: Optional[str] = None
    toy_sequence: Optional[str] = None

    # SET_LOVENSE_SCHEDULES
    schedules: Optional[str] = None

    # PAVLOK_COMMAND
    pavlok_cmd: Optional[str] = None
    pavlok_intensity: Optional[str] = None
    pavlok_duration_ms: Optional[str] = None

    # App management (OPEN_APP, FORCE_STOP_APP, DISABLE_APP, ENABLE_APP,
    # CLEAR_APP_CACHE, UNINSTALL_APP, SUSPEND_APP, UNSUSPEND_APP)
    app_name: Optional[str] = None

    # OPEN_URL / PLAY_AUDIO / SET_WALLPAPER
    url: Optional[str] = None

    # SET_BRIGHTNESS (0–255)
    value: Optional[str] = None

    # SET_SCREEN_TIMEOUT
    ms: Optional[str] = None

    # SET_ORIENTATION
    landscape: Optional[str] = None

    # SET_VOLUME
    stream: Optional[str] = None
    level: Optional[str] = None
    max: Optional[str] = None

    # SET_RINGER_MODE / SET_DND
    mode: Optional[str] = None
    policy: Optional[str] = None

    # SET_ALARM
    time_ms: Optional[str] = None

    # RECORD_SCREEN
    duration_sec: Optional[str] = None

    # CONNECT_WIFI
    ssid: Optional[str] = None
    password: Optional[str] = None

    # SET_FONT_SIZE
    scale: Optional[str] = None

    # VPN / app inventory orchestration
    poll_id: Optional[str] = None
    include_system: Optional[str] = None
    full_snapshot: Optional[str] = None
    provider_mode: Optional[str] = None
    vpn_profile_id: Optional[str] = None
    vpn_policy_json: Optional[str] = None
    apps_json: Optional[str] = None

    # START_REVIEW
    session_id: Optional[str] = None
    signaling_url: Optional[str] = None

    # device.file.*
    path: Optional[str] = None
    relative_path: Optional[str] = None
    content: Optional[str] = None
    content_base64: Optional[str] = None
    as_base64: Optional[str] = None
    max_bytes: Optional[str] = None
    append: Optional[str] = None


_VALID_TPE_ACTIONS = {
    # Content filter / NudeNet
    "UPDATE_SETTINGS",
    # Notifications & messaging
    "UPDATE_NOTIFICATION_BLOCKLIST",
    "SEND_NOTIFICATION",
    "CLEAR_NOTIFICATIONS",
    "INCOMING_PROXY_SMS",
    "SET_PROXY_SMS_CAN_REPLY",
    "SET_SMS_THREAD_CAN_REPLY",
    "TOGGLE_THREAD_CAN_REPLY",
    "NEW_QUESTION",
    "RULE_REMINDER",
    # Tone & vocabulary
    "UPDATE_RESTRICTED_VOCABULARY",
    "UPDATE_TEXT_REPLACEMENT_POLICY",
    "UPDATE_TEXT_REPLACEMENT_DICT",
    "UPDATE_TONE_COMPLIANCE",
    # Tasks
    "TASK_ASSIGNED",
    # Check-in & review
    "REQUEST_CHECKIN",
    "START_REVIEW",
    # Rituals
    "SET_RITUALS",
    "SET_RITUAL_TIMES",
    # Honorifics
    "SET_HONORIFIC",
    "SET_HONORIFIC_ENABLED",
    "SET_DISCORD_QL_HONORIFIC",
    "SET_DISCORD_QL_HONORIFIC_ENABLED",
    "SET_DISCORD_HONORIFIC_USERS",
    "ADD_DISCORD_HONORIFIC_USER",
    "REMOVE_DISCORD_HONORIFIC_USER",
    # Permission to Speak
    "SET_PTS_ENABLED",
    "SET_PTS_APPROVED",
    # App gating / geofencing
    "APP_PERMISSION_RESPONSE",
    "SET_GATING_ENABLED",
    "SET_GATING_APPROVED",
    "SET_GEOFENCES",
    "SET_GEOFENCE_ENABLED",
    # Affirmations & mantras
    "SET_AFFIRMATIONS",
    "SHOW_AFFIRMATION",
    "SET_MANTRA_ENABLED",
    "SET_MANTRA_INTERVAL",
    # Consequences
    "START_CORNER_TIME",
    "CANCEL_ESCALATION",
    # Sub status
    "SET_SUB_STATUS",
    # Handler / AI chat settings
    "SET_HANDLER_SYSTEM_PROMPT",
    "SET_HANDLER_API_KEY",
    "SET_HANDLER_ENDPOINT",
    "SET_HANDLER_MODEL",
    # Password vault
    "VAULT_ADD_ENTRY",
    "VAULT_UPDATE_ENTRY",
    "VAULT_DELETE_ENTRY",
    "VAULT_LOCK_ENTRY",
    "VAULT_LOCK_ALL",
    "VAULT_SET_CHANGE_BLOCK",
    # Lovense toy control
    "LOVENSE_COMMAND",
    "SET_LOVENSE_SCHEDULES",
    # Pavlok control
    "PAVLOK_COMMAND",
    # App management
    "OPEN_APP",
    "FORCE_STOP_APP",
    "DISABLE_APP",
    "ENABLE_APP",
    "CLEAR_APP_CACHE",
    "UNINSTALL_APP",
    "SUSPEND_APP",
    "UNSUSPEND_APP",
    # Screen & display
    "OPEN_URL",
    "SET_BRIGHTNESS",
    "SCREEN_ON",
    "SCREEN_OFF",
    "SET_SCREEN_TIMEOUT",
    "SHOW_OVERLAY",
    "SET_ORIENTATION",
    "SET_ROTATION",
    "SET_AUTO_ROTATE",
    "SET_WALLPAPER",
    # Audio & sound
    "SET_VOLUME",
    "SET_RINGER_MODE",
    "PLAY_AUDIO",
    "STOP_AUDIO",
    "SPEAK_TEXT",
    # Lock screen & access
    "LOCK_DEVICE",
    "DISMISS_KEYGUARD",
    # Network & connectivity
    "SET_WIFI",
    "SET_MOBILE_DATA",
    "SET_AIRPLANE_MODE",
    "SET_BLUETOOTH",
    "CONNECT_WIFI",
    # VPN control
    "SET_VPN_POLICY",
    "SET_VPN_PROVIDER_PROFILE",
    "VPN_CONNECT",
    "VPN_DISCONNECT",
    "VPN_STATUS_POLL",
    # App inventory polling
    "APP_LIST_POLL",
    "APP_LIST_PUSH",
    # Camera & sensors
    "TAKE_SCREENSHOT",
    "RECORD_SCREEN",
    "SET_FLASHLIGHT",
    "GET_LOCATION",
    # Device settings
    "SET_DND",
    "SET_ALARM",
    "SET_NFC",
    "SET_FONT_SIZE",
    "SET_CLIPBOARD",
    # Device file access
    "device.file.read",
    "device.file.write",
    "device.file.delete",
    # Live toy sessions (non-Pavlok)
    "toy.live.control",
}


_SENSITIVE_AI_ACTIONS = {"LOCK_DEVICE", "PAVLOK_COMMAND", "LOVENSE_COMMAND"}
_TPE_PUSH_MODEL_FIELDS = (
    set(getattr(TpePushRequest, "model_fields", {}).keys())
    or set(getattr(TpePushRequest, "__fields__", {}).keys())
)
_ALLOWED_TOOL_PARAMETER_KEYS = _TPE_PUSH_MODEL_FIELDS - {"action", "device_id"}


class ExecuteDeviceCommandToolCall(BaseModel):
    device_id: str
    action: str
    parameters: Optional[Dict[str, str]] = None

    @validator("device_id")
    def validate_device_id(cls, value: str) -> str:
        device_id = (value or "").strip()
        if not device_id:
            raise ValueError("device_id is required.")
        return device_id

    @validator("action")
    def validate_action(cls, value: str) -> str:
        action = (value or "").strip()
        if action not in _VALID_TPE_ACTIONS:
            raise ValueError(f"Unknown action '{action}'.")
        return action


class ExecuteDeviceCommandBridgeRequest(ExecuteDeviceCommandToolCall):
    prompt_context: Optional[str] = None


EXECUTE_DEVICE_COMMAND_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "execute_device_command",
    "description": "Dispatch a validated device command over backend MQTT transport.",
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
            "action": {"type": "string", "enum": sorted(_VALID_TPE_ACTIONS)},
            "parameters": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["device_id", "action"],
        "additionalProperties": False,
    },
}


def _insert_ai_tool_audit_row(
    db: sqlite3.Connection,
    *,
    tool_name: str,
    device_id: str,
    action: str,
    prompt_context: Optional[str],
    parameters: Optional[Dict[str, str]],
    payload: Dict[str, str],
    transmission_status: str,
    transmission_response: Optional[Dict[str, int]],
    error: Optional[str] = None,
    created_at: Optional[str] = None,
) -> None:
    db.execute(
        """
        INSERT INTO tpe_ai_tool_calls (
            tool_name,
            device_id,
            action,
            prompt_context,
            parameters_json,
            payload_json,
            transmission_status,
            transmission_response_json,
            error,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tool_name,
            device_id,
            action,
            prompt_context,
            json.dumps(parameters or {}, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
            transmission_status,
            json.dumps(transmission_response or {}, ensure_ascii=False),
            error,
            created_at or _now_iso(),
        ),
    )
    db.commit()


def execute_device_command(
    db: sqlite3.Connection,
    tool_call: ExecuteDeviceCommandToolCall,
    *,
    prompt_context: Optional[str] = None,
) -> Dict[str, Any]:
    parameters = tool_call.parameters or {}
    unsupported_parameters = sorted(set(parameters.keys()) - _ALLOWED_TOOL_PARAMETER_KEYS)
    if unsupported_parameters:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported parameters for execute_device_command: "
                f"{unsupported_parameters}. "
                f"Allowed parameter keys: {sorted(_ALLOWED_TOOL_PARAMETER_KEYS)}"
            ),
        )

    push_payload = {"action": tool_call.action, "device_id": tool_call.device_id, **parameters}
    payload = _build_tpe_payload(TpePushRequest(**push_payload))

    created_at = _now_iso()
    log_data = {
        "timestamp": created_at,
        "tool_name": "execute_device_command",
        "device_id": tool_call.device_id,
        "action": tool_call.action,
        "prompt_context": prompt_context,
        "parameters": parameters,
        "payload": payload,
    }
    if tool_call.action in _SENSITIVE_AI_ACTIONS:
        logger.warning("AI tool call dispatch requested: %s", json.dumps(log_data, ensure_ascii=False))
    else:
        logger.info("AI tool call dispatch requested: %s", json.dumps(log_data, ensure_ascii=False))

    try:
        transmission = _send_mqtt_to_device(db, tool_call.device_id, payload)
        transmission_status = (
            "sent" if transmission.get("sent", 0) > 0 and transmission.get("failed", 0) == 0 else "failed"
        )
        _insert_ai_tool_audit_row(
            db,
            tool_name="execute_device_command",
            device_id=tool_call.device_id,
            action=tool_call.action,
            prompt_context=prompt_context,
            parameters=parameters,
            payload=payload,
            transmission_status=transmission_status,
            transmission_response=transmission,
            created_at=created_at,
        )
        logger.info(
            "AI tool call transmission completed: %s",
            json.dumps(
                {
                    "timestamp": created_at,
                    "device_id": tool_call.device_id,
                    "action": tool_call.action,
                    "status": transmission_status,
                    "response": transmission,
                },
                ensure_ascii=False,
            ),
        )
        return {
            "tool_name": "execute_device_command",
            "timestamp": created_at,
            "device_id": tool_call.device_id,
            "action": tool_call.action,
            "payload": payload,
            "transmission_status": transmission_status,
            "transmission": transmission,
        }
    except Exception as exc:
        _insert_ai_tool_audit_row(
            db,
            tool_name="execute_device_command",
            device_id=tool_call.device_id,
            action=tool_call.action,
            prompt_context=prompt_context,
            parameters=parameters,
            payload=payload,
            transmission_status="error",
            transmission_response=None,
            error=str(exc),
            created_at=created_at,
        )
        logger.exception(
            "AI tool call transmission failed: %s",
            json.dumps(
                {
                    "timestamp": created_at,
                    "device_id": tool_call.device_id,
                    "action": tool_call.action,
                    "prompt_context": prompt_context,
                    "payload": payload,
                },
                ensure_ascii=False,
            ),
        )
        raise


def _build_tpe_payload(body: "TpePushRequest") -> "dict[str, str]":
    """Build the FCM data dict from a ``TpePushRequest``.

    All values must be strings per FCM spec; ``None`` fields are omitted.
    Returns a dict with ``"action"`` set plus every non-``None`` optional field.
    """
    data: dict[str, str] = {"action": body.action}
    field_map = {
        # Correlation
        "command_id":         body.command_id,
        # UPDATE_SETTINGS / NudeNet
        "threshold":          body.threshold,
        "strict":             body.strict,
        "blocked_classes":    body.blocked_classes,
        "nudenet_enabled":    body.nudenet_enabled,
        # UPDATE_NOTIFICATION_BLOCKLIST
        "blocklist":          body.blocklist,
        # UPDATE_RESTRICTED_VOCABULARY
        "vocabulary":         body.vocabulary,
        # UPDATE_TONE_COMPLIANCE
        "strict_tone_mode":   body.strict_tone_mode,
        # SEND_NOTIFICATION
        "title":              body.title,
        "body":               body.body,
        "channel_id":         body.channel_id,
        "thread_id":          body.thread_id,
        "can_reply":          body.can_reply,
        # SHOW_OVERLAY
        "message":            body.message,
        "image_url":          body.image_url,
        # NEW_QUESTION
        "question_id":        body.question_id,
        "question_preview":   body.question_preview,
        # Generic identifier (VAULT_* and similar)
        "id":                 body.id,
        # RULE_REMINDER
        "rule_id":            body.rule_id,
        "rule_text":          body.rule_text,
        # TASK_ASSIGNED
        "task_id":            body.task_id,
        "task_title":         body.task_title,
        "task_desc":          body.task_desc,
        "deadline_ms":        body.deadline_ms,
        # SET_RITUALS
        "steps":              body.steps,
        # SET_RITUAL_TIMES
        "morning_minutes":    body.morning_minutes,
        "evening_minutes":    body.evening_minutes,
        # SET_HONORIFIC
        "honorific":          body.honorific,
        "users":             body.users,
        "user":              body.user,
        # SET_PTS_APPROVED / SET_GATING_APPROVED
        "packages":           body.packages,
        # APP_PERMISSION_RESPONSE
        "request_id":         body.request_id,
        "granted":            body.granted,
        # Generic boolean toggle
        "enabled":            body.enabled,
        # SET_GEOFENCES
        "geofences":          body.geofences,
        # SET_AFFIRMATIONS
        "affirmations":       body.affirmations,
        # SPEAK_TEXT / SHOW_AFFIRMATION
        "text":               body.text,
        # SET_MANTRA_INTERVAL
        "minutes":            body.minutes,
        # START_CORNER_TIME / VAULT_LOCK_*
        "duration_minutes":   body.duration_minutes,
        # SET_SUB_STATUS
        "status":             body.status,
        # SET_HANDLER_SYSTEM_PROMPT
        "prompt":             body.prompt,
        # SET_HANDLER_API_KEY
        "api_key":            body.api_key,
        # SET_HANDLER_ENDPOINT
        "endpoint":           body.endpoint,
        # SET_HANDLER_MODEL — handler_model serialized to FCM key "model"
        "model":              body.handler_model,
        # LOVENSE_COMMAND
        "toy_command":        body.toy_command,
        "toy_level":          body.toy_level,
        # toy.live.control
        "toy_mode":           body.toy_mode,
        "toy_pattern":        body.toy_pattern,
        "toy_duration_ms":    body.toy_duration_ms,
        "toy_sequence":       body.toy_sequence,
        # SET_LOVENSE_SCHEDULES
        "schedules":          body.schedules,
        # PAVLOK_COMMAND
        "pavlok_cmd":         body.pavlok_cmd,
        "pavlok_intensity":   body.pavlok_intensity,
        "pavlok_duration_ms": body.pavlok_duration_ms,
        # App management
        "app_name":           body.app_name,
        # OPEN_URL / PLAY_AUDIO / SET_WALLPAPER
        "url":                body.url,
        # SET_BRIGHTNESS
        "value":              body.value,
        # SET_SCREEN_TIMEOUT
        "ms":                 body.ms,
        # SET_ORIENTATION
        "landscape":          body.landscape,
        # SET_VOLUME
        "stream":             body.stream,
        "level":              body.level,
        "max":                body.max,
        # SET_RINGER_MODE / SET_DND
        "mode":               body.mode,
        "policy":             body.policy,
        # SET_ALARM
        "time_ms":            body.time_ms,
        # RECORD_SCREEN
        "duration_sec":       body.duration_sec,
        # CONNECT_WIFI
        "ssid":               body.ssid,
        "password":           body.password,
        # SET_FONT_SIZE
        "scale":              body.scale,
        # VPN / app inventory orchestration
        "poll_id":            body.poll_id,
        "include_system":     body.include_system,
        "full_snapshot":      body.full_snapshot,
        "provider_mode":      body.provider_mode,
        "vpn_profile_id":     body.vpn_profile_id,
        "vpn_policy_json":    body.vpn_policy_json,
        "apps_json":          body.apps_json,
        # START_REVIEW
        "session_id":         body.session_id,
        "signaling_url":      body.signaling_url,
        # device.file.*
        "path":               body.path,
        "relative_path":      body.relative_path,
        "content":            body.content,
        "content_base64":     body.content_base64,
        "as_base64":          body.as_base64,
        "max_bytes":          body.max_bytes,
        "append":             body.append,
    }
    for field, val in field_map.items():
        if val is not None:
            data[field] = val
    return data


@admin_router.post("/push")
def tpe_push_settings(
    body: TpePushRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Push an FCM data message to all paired TPE devices, or to a specific device
    when ``device_id`` is provided.

    The ``action`` field must be one of the actions understood by
    ``PartnerFcmService`` in the TPE Android app.
    """
    if body.action not in _VALID_TPE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{body.action}'. Valid: {sorted(_VALID_TPE_ACTIONS)}",
        )

    data = _build_tpe_payload(body)

    if body.device_id:
        return _send_mqtt_to_device(db, body.device_id, data)

    return _send_mqtt_to_all(db, data)


@admin_router.post("/ai/execute-device-command")
def ai_execute_device_command(
    body: ExecuteDeviceCommandBridgeRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Execution bridge for autonomous LLM tool calls to dispatch MQTT device commands."""
    return execute_device_command(
        db=db,
        tool_call=ExecuteDeviceCommandToolCall(
            device_id=body.device_id,
            action=body.action,
            parameters=body.parameters,
        ),
        prompt_context=body.prompt_context,
    )


# ---------------------------------------------------------------------------
# AI tool: post_social_update (draft queue – requires handler approval)
# ---------------------------------------------------------------------------

_VALID_SOCIAL_PLATFORMS = {"twitter", "bluesky", "both"}


class DraftSocialPostRequest(BaseModel):
    platform: str
    content: str
    prompt_context: Optional[str] = None


@admin_router.post("/ai/draft-social-post", status_code=201)
def ai_draft_social_post(
    body: DraftSocialPostRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Queue a social media post draft proposed by the AI Warden for handler review.

    The draft is stored with status 'pending' and is NOT published.  A handler
    must approve it via ``POST /api/handler/social-post-drafts/{draft_id}/approve``
    before any content goes live.
    """
    platform = (body.platform or "").strip().lower()
    if platform not in _VALID_SOCIAL_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid platform '{platform}'. Must be one of: {sorted(_VALID_SOCIAL_PLATFORMS)}",
        )

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty.")

    cursor = db.execute(
        """
        INSERT INTO tpe_ai_social_drafts (platform, content, status, prompt_context, created_at)
        VALUES (?, ?, 'pending', ?, datetime('now'))
        """,
        (platform, content, body.prompt_context),
    )
    db.commit()
    draft_id = cursor.lastrowid
    return {
        "status": "pending_approval",
        "draft_id": draft_id,
        "platform": platform,
        "message": (
            "Draft queued for handler review.  No content has been posted. "
            "A handler must approve this draft before it goes live."
        ),
    }


@admin_router.get("/events")
def tpe_list_events(
    limit: int = 100,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List the most recent TPE device events (all event types)."""
    rows = db.execute(
        "SELECT id, event, reason, session_ts, payload_json, received_at "
        "FROM tpe_events ORDER BY id DESC LIMIT ?",
        (min(limit, 500),),
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.get("/audits")
def tpe_list_audits(
    limit: int = 50,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List the most recent adherence audit records."""
    rows = db.execute(
        "SELECT id, detection_ratio, last_label, last_score, session_ts, video_filename, received_at "
        "FROM tpe_audit_logs ORDER BY id DESC LIMIT ?",
        (min(limit, 200),),
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.get("/uploads")
def tpe_list_uploads(
    limit: int = 50,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List the most recent device screenshot / recording uploads."""
    rows = db.execute(
        "SELECT id, filename, content_type, size_bytes, received_at "
        "FROM tpe_uploads ORDER BY id DESC LIMIT ?",
        (min(limit, 200),),
    ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Task Assignment & Verification
# ===========================================================================

class TpeTaskCreate(BaseModel):
    title: str
    description: str = ""
    deadline_ms: int


class TpeTaskPatch(BaseModel):
    status: str  # pending | completed | failed | overdue


@admin_router.post("/tasks", status_code=201)
def tpe_create_task(
    body: TpeTaskCreate,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Create a task and push a ``TASK_ASSIGNED`` FCM to all paired devices.

    The FCM payload matches the fields expected by ``PartnerFcmService.handleTaskAssigned()``.
    """
    task_id = str(uuid.uuid4())
    now = _now_iso()

    db.execute(
        """
        INSERT INTO tpe_tasks (id, title, description, deadline_ms, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (task_id, body.title, body.description, body.deadline_ms, now),
    )
    db.commit()

    # Push FCM – best-effort; task row already saved even if FCM unavailable.
    try:
        _send_mqtt_to_all(db, {
            "action":      "TASK_ASSIGNED",
            "task_id":     task_id,
            "task_title":  body.title,
            "task_desc":   body.description,
            "deadline_ms": str(body.deadline_ms),
        })
    except HTTPException as exc:
        logger.warning("TPE task command push skipped: %s", exc.detail)

    return {"id": task_id, "status": "created"}


@admin_router.get("/tasks")
def tpe_list_tasks(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all tasks."""
    rows = db.execute(
        "SELECT id, title, description, deadline_ms, status, proof_note, proof_photo, created_at, completed_at "
        "FROM tpe_tasks ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.get("/tasks/{task_id}")
def tpe_get_task(
    task_id: str,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Get a single task."""
    row = db.execute(
        "SELECT id, title, description, deadline_ms, status, proof_note, proof_photo, created_at, completed_at "
        "FROM tpe_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return dict(row)


@admin_router.patch("/tasks/{task_id}")
def tpe_update_task(
    task_id: str,
    body: TpeTaskPatch,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Manually update a task's status."""
    valid = {"pending", "completed", "failed", "overdue"}
    if body.status not in valid:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(valid)}")
    completed_at = _now_iso() if body.status in ("completed", "failed") else None
    cur = db.execute(
        "UPDATE tpe_tasks SET status = ?, completed_at = ? WHERE id = ?",
        (body.status, completed_at, task_id),
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "updated"}


@admin_router.delete("/tasks/{task_id}")
def tpe_delete_task(
    task_id: str,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a task."""
    cur = db.execute("DELETE FROM tpe_tasks WHERE id = ?", (task_id,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted"}


@device_router.post("/api/tpe/task/status")
async def tpe_task_status(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive a task completion or failure report from the Android app.

    Accepts **either** a JSON body or multipart form data (when the device
    includes photo proof).

    JSON body:
      ``{"task_id": "...", "status": "completed"|"failed", "proof_note": "..."}``

    Multipart fields:
      ``task_id``   — (text field)
      ``status``    — ``"COMPLETED"`` or ``"FAILED"`` (text field)
      ``proof_note``— optional note (text field)
      ``photo``     — optional image file
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    ct = request.headers.get("content-type", "")
    task_id: str = ""
    status_val: str = ""
    proof_note: Optional[str] = None
    proof_photo: Optional[str] = None

    if "multipart/form-data" in ct:
        form = await request.form()
        task_id = (form.get("task_id") or "").strip()
        status_val = (form.get("status") or "").strip().lower()
        proof_note = form.get("proof_note") or None
        photo_file = form.get("photo")

        if photo_file is not None and hasattr(photo_file, "read"):
            _TPE_UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
            # Derive extension from content-type only — never from the client filename —
            # so the resulting path contains no user-controlled data.
            file_ct_str = getattr(photo_file, "content_type", None) or "image/jpeg"
            ext = _ext_from_content_type(file_ct_str, fallback=".jpg")
            # Use a timestamp+UUID for the filename — zero user-controlled data in the path.
            photo_name = f"task_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex}{ext}"
            dest = _TPE_UPLOAD_PATH / photo_name
            try:
                total = 0
                with open(dest, "wb") as fh:
                    while chunk := await photo_file.read(_CHUNK_SIZE_BYTES):
                        total += len(chunk)
                        if total > _MAX_UPLOAD_BYTES:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise HTTPException(status_code=413, detail="Photo exceeds 50 MB limit.")
                        fh.write(chunk)
                proof_photo = photo_name
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("TPE task photo save failed: %s", exc)
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        task_id = (body.get("task_id") or "").strip()
        status_val = (body.get("status") or "").strip().lower()
        proof_note = body.get("proof_note")

    valid = {"completed", "failed"}
    if status_val not in valid:
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'failed'")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id is required")

    row = db.execute("SELECT id FROM tpe_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    db.execute(
        "UPDATE tpe_tasks SET status = ?, proof_note = ?, proof_photo = ?, completed_at = ? WHERE id = ?",
        (status_val, proof_note, proof_photo, _now_iso(), task_id),
    )
    db.commit()
    logger.info("TPE task %s → %s (photo=%s)", task_id, status_val, proof_photo)
    return {"status": "received"}


@device_router.post("/api/tpe/upload")
async def tpe_upload(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive a screenshot or screen recording uploaded by the Android app's
    ``DeviceCommandManager`` (``TAKE_SCREENSHOT`` / ``RECORD_SCREEN`` FCM
    commands).

    The file may be sent as:
      - a multipart upload with a ``file`` or ``image`` field, OR
      - a raw binary body (``Content-Type: image/*`` or ``video/*``).

    Auth: ``Authorization: Bearer <webhook_secret>`` (optional when secret
    is unconfigured, matching the tpeapp behaviour).
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    _TPE_UPLOAD_PATH.mkdir(parents=True, exist_ok=True)

    ct = request.headers.get("content-type", "")
    filename: str = ""
    size_bytes: int = 0
    file_ct: str = ct

    if "multipart/form-data" in ct:
        form = await request.form()
        upload_file = form.get("file") or form.get("image")
        if upload_file is None:
            raise HTTPException(status_code=400, detail="Missing file or image field")
        # Derive extension from content-type, not the client filename, to avoid path injection.
        file_ct = getattr(upload_file, "content_type", None) or ct or "application/octet-stream"
        ext = _ext_from_content_type(file_ct, fallback=".bin")
        filename = f"upload_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex}{ext}"
        dest = _TPE_UPLOAD_PATH / filename
        try:
            with open(dest, "wb") as fh:
                while chunk := await upload_file.read(_CHUNK_SIZE_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > _MAX_UPLOAD_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit.")
                    fh.write(chunk)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("TPE upload save failed: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to save upload.")
    else:
        # Raw binary body — extension comes entirely from Content-Type (no user input).
        ext = _ext_from_content_type(ct, fallback=".bin")
        filename = f"upload_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex}{ext}"
        dest = _TPE_UPLOAD_PATH / filename
        try:
            with open(dest, "wb") as fh:
                async for chunk in request.stream():
                    size_bytes += len(chunk)
                    if size_bytes > _MAX_UPLOAD_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit.")
                    fh.write(chunk)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("TPE raw upload save failed: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to save upload.")

    db.execute(
        "INSERT INTO tpe_uploads (filename, content_type, size_bytes, received_at) VALUES (?, ?, ?, ?)",
        (filename, file_ct, size_bytes, _now_iso()),
    )
    db.commit()
    logger.info("TPE upload received: file=%s size=%d ct=%s", filename, size_bytes, file_ct)
    return {"status": "received", "file": filename, "size_bytes": size_bytes}


# ===========================================================================
# Hot-mic WebSocket  (/ws)
# ===========================================================================
#
# The tpeapp's WebSocketService (flutter_app/lib/services/websocket_service.dart)
# connects to ``{endpoint}/ws`` and:
#   • Receives JSON text frames: ``{"command": "START_HOT_MIC"}`` /
#     ``{"command": "STOP_HOT_MIC"}`` — triggers the device mic.
#   • Sends binary frames: raw 16 kHz mono 16-bit PCM audio chunks.
#
# This endpoint relays those audio chunks to all connected Handler Panel
# WebSockets and allows the Handler Panel to push START/STOP commands via
# ``handler_ws.send_mic_command()``.
# ===========================================================================


@device_router.websocket("/ws")
async def tpe_hot_mic_ws(
    websocket: WebSocket,
    secret: str = "",
    device_id: str = "",
) -> None:
    """
    Hot-mic WebSocket relay for TPE devices.

    The tpeapp's ``WebSocketService`` connects here.  Audio chunks sent as
    binary frames are broadcast to all connected Handler Panel clients so the
    partner can listen in real-time.

    Query parameters (all optional):
      ``secret``    – webhook secret for authentication (omit when unconfigured)
      ``device_id`` – stable device UUID (used for assignment lookup)

    If ``device_id`` is not provided via query param the endpoint falls back to
    the ``X-Device-ID`` header sent by the flutter http package, then generates
    an ephemeral UUID for this session.
    """
    db = get_db_connection()
    try:
        expected = _effective_webhook_secret(db)
        if expected and not secrets.compare_digest(secret, expected):
            await websocket.close(code=4001)
            return

        # Resolve device_id: query param → X-Device-ID header → ephemeral UUID.
        effective_device_id = (device_id or "").strip()
        if not effective_device_id:
            for hdr_name, hdr_val in websocket.headers.items():
                if hdr_name.lower() == "x-device-id":
                    effective_device_id = hdr_val.strip()
                    break
        if not effective_device_id:
            effective_device_id = str(uuid.uuid4())

        await websocket.accept()
        _handler_ws.connect_device(effective_device_id, websocket)
        logger.info("TPE hot-mic connected: device=%s", effective_device_id)

        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")

                if msg_type == "websocket.disconnect":
                    break

                if msg_type == "websocket.receive":
                    chunk = msg.get("bytes")
                    if chunk:
                        # Relay binary PCM audio to all handler panel clients.
                        # Fall back to broadcast since assignment lookup would
                        # require a handler assignment for this device.
                        relayed = await _handler_ws.relay_audio(effective_device_id, chunk, db)
                        if not relayed:
                            await _handler_ws.relay_audio_broadcast(chunk)
                    # Text frames (if any) are informational — log but ignore.
                    text = msg.get("text")
                    if text:
                        logger.debug("TPE hot-mic text from device %s: %s", effective_device_id, text[:200])
        except WebSocketDisconnect:
            pass
        finally:
            _handler_ws.disconnect_device(effective_device_id, websocket)
            logger.info("TPE hot-mic disconnected: device=%s", effective_device_id)
    finally:
        db.close()


# ===========================================================================
# Daily Check-ins
# ===========================================================================


class TpeCheckinReport(BaseModel):
    mood_score: Optional[int] = None   # 1–10
    note: Optional[str] = None


@device_router.post("/api/tpe/checkin")
async def tpe_device_checkin(
    body: TpeCheckinReport,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive a daily mood/compliance check-in from the Android app.

    ``mood_score`` is an optional integer 1–10 (1 = very bad, 10 = excellent).
    ``note`` is an optional free-text note from the user.
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    score = body.mood_score
    if score is not None and not (1 <= score <= 10):
        raise HTTPException(status_code=400, detail="mood_score must be 1–10")

    db.execute(
        "INSERT INTO tpe_checkins (mood_score, note, checked_in_at) VALUES (?, ?, ?)",
        (score, body.note, _now_iso()),
    )
    db.commit()
    logger.info("TPE check-in received: mood=%s", score)
    return {"status": "received"}


@admin_router.get("/checkins")
def tpe_list_checkins(
    limit: int = 100,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List check-in history (most recent first)."""
    rows = db.execute(
        "SELECT id, mood_score, note, checked_in_at FROM tpe_checkins ORDER BY id DESC LIMIT ?",
        (min(limit, 500),),
    ).fetchall()
    return [dict(r) for r in rows]


class TpeCheckinRequestBody(BaseModel):
    device_id: Optional[str] = None


@admin_router.post("/checkins/request")
def tpe_request_checkin(
    body: TpeCheckinRequestBody = TpeCheckinRequestBody(),
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Push a ``REQUEST_CHECKIN`` FCM to a specific device (when ``device_id`` is
    given) or to all paired devices."""
    if body.device_id:
        return _send_mqtt_to_device(db, body.device_id, {"action": "REQUEST_CHECKIN"})
    return _send_mqtt_to_all(db, {"action": "REQUEST_CHECKIN"})


# ===========================================================================
# Rule Reminders
# ===========================================================================


class TpeRuleCreate(BaseModel):
    rule_text: str


@admin_router.post("/rules", status_code=201)
def tpe_create_rule(
    body: TpeRuleCreate,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new rule."""
    if not body.rule_text.strip():
        raise HTTPException(status_code=400, detail="rule_text must not be empty")
    cur = db.execute(
        "INSERT INTO tpe_rules (rule_text, active, created_at) VALUES (?, 1, ?)",
        (body.rule_text.strip(), _now_iso()),
    )
    db.commit()
    return {"id": cur.lastrowid, "status": "created"}


@admin_router.get("/rules")
def tpe_list_rules(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all active rules."""
    rows = db.execute(
        "SELECT id, rule_text, active, created_at FROM tpe_rules WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.delete("/rules/{rule_id}")
def tpe_delete_rule(
    rule_id: int,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete (deactivate) a rule."""
    cur = db.execute("UPDATE tpe_rules SET active = 0 WHERE id = ?", (rule_id,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "deleted"}


@admin_router.post("/rules/{rule_id}/remind")
def tpe_remind_rule(
    rule_id: int,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Push a ``RULE_REMINDER`` FCM for the given rule to all paired devices."""
    row = db.execute(
        "SELECT id, rule_text FROM tpe_rules WHERE id = ? AND active = 1", (rule_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _send_mqtt_to_all(db, {
        "action":    "RULE_REMINDER",
        "rule_id":   str(row["id"]),
        "rule_text": row["rule_text"],
    })


# ===========================================================================
# Screen Control — WebRTC signaling (review sessions)
# ===========================================================================
#
# The Android app's StreamCoordinator uses Socket.IO to exchange WebRTC
# signaling messages (offer / answer / ICE candidates) with the partner's
# browser.  Since Socket.IO wire protocol is not natively supported by
# FastAPI, we implement an equivalent relay using plain WebSockets at
# /api/tpe/signal/{session_id}.  The app will be updated to connect here
# directly; the partner's browser dashboard does the same.
#
# Message envelope (JSON):
#   { "type": "offer"|"answer"|"ice-candidate"|"join"|"leave", ...payload }
#
# All messages are broadcast to every OTHER peer in the same session room.
# ===========================================================================

# In-memory signaling rooms: session_id → set of connected WebSockets.
_signal_rooms: dict[str, set[WebSocket]] = {}


@device_router.websocket("/api/tpe/signal/{session_id}")
async def tpe_signal_ws(
    websocket: WebSocket,
    session_id: str,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    WebRTC signaling relay for a TPE screen-control review session.

    Both the Android device and the partner's browser connect here.
    Every JSON message received from one peer is forwarded verbatim
    to all other peers in the same session room.

    The session must have been created via ``POST /api/admin/tpe/review/start``
    before the device can join.
    """
    # Validate session exists
    row = db.execute(
        "SELECT id FROM tpe_review_sessions WHERE id = ? AND ended_at IS NULL",
        (session_id,),
    ).fetchone()
    if not row:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    room = _signal_rooms.setdefault(session_id, set())
    room.add(websocket)
    logger.info("TPE signal: peer joined session %s (room size %d)", session_id, len(room))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") in {"join", "offer", "answer", "ice-candidate", "leave"}:
                    _mqtt_client.publish_json(
                        _mqtt_client.topic_for_session_signaling(session_id),
                        {
                            "session_id": session_id,
                            "transport": "websocket",
                            **msg,
                        },
                        qos=1,
                    )
            except Exception as exc:
                logger.debug("TPE signaling MQTT fallback publish failed: %s", exc)
            # Relay to all other peers in the room
            dead: list[WebSocket] = []
            for peer in room:
                if peer is websocket:
                    continue
                try:
                    await peer.send_text(raw)
                except Exception:
                    dead.append(peer)
            for d in dead:
                room.discard(d)
    except WebSocketDisconnect:
        pass
    finally:
        room.discard(websocket)
        if not room:
            _signal_rooms.pop(session_id, None)
        logger.info("TPE signal: peer left session %s (room size %d)", session_id, len(room))


@admin_router.post("/review/start", status_code=201)
def tpe_start_review(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Create a new screen-control review session and push a ``START_REVIEW``
    FCM to all paired devices.

    The FCM includes the ``session_id`` and ``signaling_url`` the device
    should use to connect to the WebRTC signaling WebSocket.

    The ``signaling_url`` is built from the ``base_url`` setting in the
    settings table (or the ``BASE_URL`` env var).
    """
    session_id = str(uuid.uuid4())
    now = _now_iso()

    db.execute(
        "INSERT INTO tpe_review_sessions (id, created_at) VALUES (?, ?)",
        (session_id, now),
    )
    db.commit()

    # Build the signaling URL from the configured base URL.
    base_url = os.environ.get("BASE_URL", "")
    row = db.execute("SELECT value FROM settings WHERE key = 'base_url'").fetchone()
    if row and row["value"]:
        base_url = row["value"].rstrip("/")
    # Convert http(s):// → ws(s):// for the WebSocket URL.
    if base_url.startswith("https://"):
        ws_base = "wss://" + base_url[len("https://"):]
    elif base_url.startswith("http://"):
        ws_base = "ws://" + base_url[len("http://"):]
    else:
        ws_base = base_url
    signaling_url = f"{ws_base}/api/tpe/signal/{session_id}" if ws_base else ""

    try:
        _send_mqtt_to_all(db, {
            "action":        "START_REVIEW",
            "session_id":    session_id,
            "signaling_url": signaling_url,
            "mqtt_signaling_topic": _mqtt_client.topic_for_session_signaling(session_id),
        })
    except HTTPException as exc:
        logger.warning("TPE review command push skipped: %s", exc.detail)

    return {
        "session_id": session_id,
        "signaling_url": signaling_url,
        "mqtt_signaling_topic": _mqtt_client.topic_for_session_signaling(session_id),
    }


@admin_router.get("/review/sessions")
def tpe_list_review_sessions(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all review sessions (most recent first)."""
    rows = db.execute(
        "SELECT id, created_at, ended_at, device_fcm_token "
        "FROM tpe_review_sessions ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@admin_router.delete("/review/sessions/{session_id}")
def tpe_end_review_session(
    session_id: str,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """End a review session (closes the signaling room)."""
    cur = db.execute(
        "UPDATE tpe_review_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (_now_iso(), session_id),
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Session not found or already ended")
    # Drop any live WebSocket peers still in the room.
    room = _signal_rooms.pop(session_id, set())
    for ws in room:
        try:
            import asyncio
            asyncio.get_event_loop().create_task(ws.close(code=4410))
        except Exception:
            pass
    return {"status": "ended"}


# ===========================================================================
# QR Code — partner pairing helper
# ===========================================================================


@admin_router.get("/qr")
def tpe_pairing_qr(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Generate a PNG QR code containing the pairing payload the Android app scans.

    QR content (matches ``PairingScreen._handleBarcode()`` in the Flutter app):
    ``{
        "endpoint": "<BASE_URL>",
        "pairing_token": "<token>",
        "webhook_secret": "<secret>",
        "signaling_url": "<wss://…/api/tpe/signal/<session_id>>"
      }``

    ``webhook_secret`` is included so the device can authenticate its outbound
    webhook calls (``POST /api/tpe/webhook``, etc.) without a separate
    configuration step.  ``signaling_url`` is the WebRTC signaling WebSocket
    URL and is populated when a live review session exists; it is omitted
    otherwise.

    Returns a PNG image (``Content-Type: image/png``).
    """
    return _render_tpe_pairing_qr_png(db)
