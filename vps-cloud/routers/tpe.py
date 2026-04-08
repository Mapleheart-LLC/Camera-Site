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
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, Response
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tpe_tasks (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            deadline_ms  INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            proof_note   TEXT,
            created_at   TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
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
      TASK_ASSIGNED                 task_id, task_title, task_desc, deadline_ms (epoch ms str)
      REQUEST_CHECKIN               (no additional fields)
      RULE_REMINDER                 rule_id (str), rule_text
      START_REVIEW                  session_id, signaling_url
    """

    action: str
    # UPDATE_SETTINGS
    threshold: Optional[str] = None
    strict: Optional[str] = None
    blocked_classes: Optional[str] = None
    # UPDATE_NOTIFICATION_BLOCKLIST
    blocklist: Optional[str] = None
    # UPDATE_RESTRICTED_VOCABULARY
    vocabulary: Optional[str] = None
    # UPDATE_TONE_COMPLIANCE
    strict_tone_mode: Optional[str] = None
    # LOVENSE_COMMAND
    toy_command: Optional[str] = None
    toy_level: Optional[str] = None
    # PAVLOK_COMMAND
    pavlok_cmd: Optional[str] = None
    pavlok_intensity: Optional[str] = None
    pavlok_duration_ms: Optional[str] = None
    # TASK_ASSIGNED
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    task_desc: Optional[str] = None
    deadline_ms: Optional[str] = None
    # RULE_REMINDER
    rule_id: Optional[str] = None
    rule_text: Optional[str] = None
    # START_REVIEW
    session_id: Optional[str] = None
    signaling_url: Optional[str] = None


_VALID_TPE_ACTIONS = {
    "UPDATE_SETTINGS",
    "UPDATE_NOTIFICATION_BLOCKLIST",
    "UPDATE_RESTRICTED_VOCABULARY",
    "UPDATE_TONE_COMPLIANCE",
    "LOVENSE_COMMAND",
    "PAVLOK_COMMAND",
    "TASK_ASSIGNED",
    "REQUEST_CHECKIN",
    "RULE_REMINDER",
    "START_REVIEW",
    "NEW_QUESTION",
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
        "task_id":          body.task_id,
        "task_title":       body.task_title,
        "task_desc":        body.task_desc,
        "deadline_ms":      body.deadline_ms,
        "rule_id":          body.rule_id,
        "rule_text":        body.rule_text,
        "session_id":       body.session_id,
        "signaling_url":    body.signaling_url,
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


# ===========================================================================
# Task Assignment & Verification
# ===========================================================================


class TpeTaskCreate(BaseModel):
    title: str
    description: str = ""
    deadline_ms: int


class TpeTaskPatch(BaseModel):
    status: str  # pending | completed | failed | overdue


class TpeTaskStatusReport(BaseModel):
    """Sent by the device when a task is completed or failed."""
    task_id: str
    status: str       # "completed" or "failed"
    proof_note: Optional[str] = None


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
        _send_fcm_to_all(db, {
            "action":      "TASK_ASSIGNED",
            "task_id":     task_id,
            "task_title":  body.title,
            "task_desc":   body.description,
            "deadline_ms": str(body.deadline_ms),
        })
    except HTTPException as exc:
        logger.warning("TPE task FCM push skipped: %s", exc.detail)

    return {"id": task_id, "status": "created"}


@admin_router.get("/tasks")
def tpe_list_tasks(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all tasks."""
    rows = db.execute(
        "SELECT id, title, description, deadline_ms, status, proof_note, created_at, completed_at "
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
        "SELECT id, title, description, deadline_ms, status, proof_note, created_at, completed_at "
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
    body: TpeTaskStatusReport,
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Receive a task completion or failure report from the Android app.

    The app calls this after the user marks a task complete/failed,
    optionally including a short ``proof_note``.
    """
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    valid = {"completed", "failed"}
    if body.status not in valid:
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'failed'")

    row = db.execute("SELECT id FROM tpe_tasks WHERE id = ?", (body.task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    db.execute(
        "UPDATE tpe_tasks SET status = ?, proof_note = ?, completed_at = ? WHERE id = ?",
        (body.status, body.proof_note, _now_iso(), body.task_id),
    )
    db.commit()
    logger.info("TPE task %s → %s", body.task_id, body.status)
    return {"status": "received"}


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


@admin_router.post("/checkins/request")
def tpe_request_checkin(
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Push a ``REQUEST_CHECKIN`` FCM to all paired devices, prompting an immediate check-in."""
    return _send_fcm_to_all(db, {"action": "REQUEST_CHECKIN"})


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
    return _send_fcm_to_all(db, {
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
        _send_fcm_to_all(db, {
            "action":        "START_REVIEW",
            "session_id":    session_id,
            "signaling_url": signaling_url,
        })
    except HTTPException as exc:
        logger.warning("TPE review FCM push skipped: %s", exc.detail)

    return {"session_id": session_id, "signaling_url": signaling_url}


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

    QR content (matches ``PairingActivity.handleQrPayload()``):
    ``{"endpoint": "<BASE_URL>", "pairing_token": "<token>"}``

    The partner prints or displays this QR code; the device owner scans it
    once to complete initial pairing.

    Returns a PNG image (``Content-Type: image/png``).
    """
    try:
        import qrcode
        from qrcode.image.pil import PilImage
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="qrcode library is not installed. Add qrcode[pil] to requirements.",
        )

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

    payload = json.dumps({"endpoint": base_url, "pairing_token": pairing_token})

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
