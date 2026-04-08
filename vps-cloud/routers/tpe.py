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

Admin endpoints (HTTP Basic auth):
  GET    /api/admin/tpe/devices          – List paired devices
  DELETE /api/admin/tpe/devices/{token}  – Unpair a device
  GET    /api/admin/tpe/settings         – Get current filter/remote-control settings
  PATCH  /api/admin/tpe/settings         – Update settings
  POST   /api/admin/tpe/push             – Push an FCM message to all paired devices
  GET    /api/admin/tpe/events           – List consequence events (punishment / reward log)
  GET    /api/admin/tpe/audits           – List audit upload records

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
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import get_db
from dependencies import get_admin_user

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TPE_PAIRING_TOKEN  = os.environ.get("TPE_PAIRING_TOKEN", "")
_TPE_WEBHOOK_SECRET = os.environ.get("TPE_WEBHOOK_SECRET", "")
_TPE_AUDIT_PATH     = Path(os.environ.get("TPE_AUDIT_PATH", "/app/data/tpe_audits"))

_MAX_AUDIT_VIDEO_BYTES = 200 * 1024 * 1024  # 200 MB

# ---------------------------------------------------------------------------
# Firebase / FCM (lazy initialisation)
# ---------------------------------------------------------------------------

_firebase_app = None


def _get_firebase_app(db: sqlite3.Connection):
    """Return (and lazily initialise) the Firebase Admin app."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    try:
        import firebase_admin
        from firebase_admin import credentials as _creds

        cred = None

        # Priority 1: GOOGLE_APPLICATION_CREDENTIALS env var
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if gac and Path(gac).is_file():
            cred = _creds.Certificate(gac)

        # Priority 2: service account JSON stored in the settings table
        if cred is None:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'tpe_fcm_service_account_json'"
            ).fetchone()
            if row and row["value"]:
                try:
                    sa_info = json.loads(row["value"])
                    cred = _creds.Certificate(sa_info)
                except Exception as exc:
                    logger.warning("TPE: invalid tpe_fcm_service_account_json: %s", exc)

        if cred is None:
            return None

        # Avoid re-initialising if something already init'd the default app.
        try:
            _firebase_app = firebase_admin.get_app()
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(cred)

        return _firebase_app

    except ImportError:
        logger.warning("TPE: firebase-admin is not installed – FCM push unavailable")
        return None


def _send_fcm_to_all(db: sqlite3.Connection, data: dict[str, str]) -> dict[str, int]:
    """Send a data-only FCM message to every paired device.

    Returns ``{"sent": n, "failed": n}``.
    """
    app = _get_firebase_app(db)
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "FCM is not configured. "
                "Set GOOGLE_APPLICATION_CREDENTIALS or tpe_fcm_service_account_json."
            ),
        )

    from firebase_admin import messaging

    rows = db.execute("SELECT fcm_token FROM tpe_paired_devices").fetchall()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No paired devices registered.",
        )

    sent = failed = 0
    for row in rows:
        try:
            messaging.send(messaging.Message(token=row["fcm_token"], data=data))
            sent += 1
        except Exception as exc:
            logger.warning("TPE FCM delivery failed for token %s: %s", row["fcm_token"][:16], exc)
            failed += 1

    logger.info("TPE FCM push: sent=%d failed=%d", sent, failed)
    return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _effective_pairing_token(db: sqlite3.Connection) -> str:
    """Return the active pairing token: settings table > env var."""
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'tpe_pairing_token'"
    ).fetchone()
    return (row["value"].strip() if row and row["value"] else "") or _TPE_PAIRING_TOKEN


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
            received_at TEXT NOT NULL
        )
        """
    )
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
    conn.commit()


# ===========================================================================
# Device-facing endpoints
# ===========================================================================


class PairRequest(BaseModel):
    fcm_token: str
    pairing_token: str


@device_router.post("/api/pair")
def tpe_pair(body: PairRequest, db: sqlite3.Connection = Depends(get_db)):
    """
    Register a TPE device's FCM token.

    The Android app calls this after scanning the partner QR code.
    Body: ``{"fcm_token": "...", "pairing_token": "..."}``
    """
    token = body.fcm_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing or invalid fcm_token")

    expected = _effective_pairing_token(db)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="TPE pairing is not configured. Set TPE_PAIRING_TOKEN.",
        )

    if not secrets.compare_digest(body.pairing_token, expected):
        raise HTTPException(status_code=403, detail="Invalid pairing_token")

    now = _now_iso()
    db.execute(
        """
        INSERT INTO tpe_paired_devices (fcm_token, paired_at, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(fcm_token) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (token, now, now),
    )
    db.commit()
    logger.info("TPE device paired/refreshed: %s…", token[:16])
    return {"status": "paired"}


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

    if event not in ("punishment", "reward"):
        raise HTTPException(
            status_code=400, detail="event must be 'punishment' or 'reward'"
        )

    db.execute(
        "INSERT INTO tpe_events (event, reason, session_ts, received_at) VALUES (?, ?, ?, ?)",
        (event, reason, session_ts, _now_iso()),
    )
    db.commit()
    logger.info("TPE webhook: event=%s reason=%r", event, reason)
    return {"status": "received"}


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
}


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
    for secret_key in ("tpe_pairing_token", "tpe_webhook_secret", "tpe_fcm_service_account_json"):
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
    return {"updated": list(updates.keys())}


class TpePushRequest(BaseModel):
    """
    Flexible FCM push payload.  ``action`` maps to the FCM ``data.action`` field
    understood by ``PartnerFcmService`` in the TPE app.

    Supported actions (and their additional fields):
      UPDATE_SETTINGS              threshold (float str), strict (bool str), blocked_classes (JSON str)
      UPDATE_NOTIFICATION_BLOCKLIST blocklist (JSON str)
      UPDATE_RESTRICTED_VOCABULARY  vocabulary (JSON str)
      UPDATE_TONE_COMPLIANCE        strict_tone_mode (bool str)
      LOVENSE_COMMAND               toy_command, toy_level (int str 0–20)
      PAVLOK_COMMAND                pavlok_cmd, pavlok_intensity (0–255), pavlok_duration_ms
    """

    action: str
    threshold: Optional[str] = None
    strict: Optional[str] = None
    blocked_classes: Optional[str] = None
    blocklist: Optional[str] = None
    vocabulary: Optional[str] = None
    strict_tone_mode: Optional[str] = None
    toy_command: Optional[str] = None
    toy_level: Optional[str] = None
    pavlok_cmd: Optional[str] = None
    pavlok_intensity: Optional[str] = None
    pavlok_duration_ms: Optional[str] = None


_VALID_TPE_ACTIONS = {
    "UPDATE_SETTINGS",
    "UPDATE_NOTIFICATION_BLOCKLIST",
    "UPDATE_RESTRICTED_VOCABULARY",
    "UPDATE_TONE_COMPLIANCE",
    "LOVENSE_COMMAND",
    "PAVLOK_COMMAND",
}


@admin_router.post("/push")
def tpe_push_settings(
    body: TpePushRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Push an FCM data message to all paired TPE devices.

    The ``action`` field must be one of the actions understood by
    ``PartnerFcmService`` in the TPE Android app.
    """
    if body.action not in _VALID_TPE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{body.action}'. Valid: {sorted(_VALID_TPE_ACTIONS)}",
        )

    # Build the FCM data payload (all values must be strings per FCM spec)
    data: dict[str, str] = {"action": body.action}

    field_map = {
        "threshold":        body.threshold,
        "strict":           body.strict,
        "blocked_classes":  body.blocked_classes,
        "blocklist":        body.blocklist,
        "vocabulary":       body.vocabulary,
        "strict_tone_mode": body.strict_tone_mode,
        "toy_command":      body.toy_command,
        "toy_level":        body.toy_level,
        "pavlok_cmd":       body.pavlok_cmd,
        "pavlok_intensity": body.pavlok_intensity,
        "pavlok_duration_ms": body.pavlok_duration_ms,
    }
    for field, val in field_map.items():
        if val is not None:
            data[field] = val

    return _send_fcm_to_all(db, data)


@admin_router.get("/events")
def tpe_list_events(
    limit: int = 100,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List the most recent TPE consequence events (punishment / reward)."""
    rows = db.execute(
        "SELECT id, event, reason, session_ts, received_at "
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
