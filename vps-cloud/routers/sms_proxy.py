"""
routers/sms_proxy.py – Enterprise SMS/MMS Communication Proxy

Implements a centralized SMS proxy over Twilio with:
  - Inbound webhook: receive SMS/MMS from external clients, store threads,
    forward via MQTT (INCOMING_PROXY_SMS) so the field worker's app gets
    the message and any image media context.
  - Moderation pipeline: outgoing agent replies are held PENDING until an
    admin approves or edits them before dispatch.
  - Tone compliance middleware: mandatory vocabulary replacements are applied
    to agent replies before they enter the moderation queue.
  - Admin bypass: admins can send directly to any client at any time.

Environment variables
---------------------
TWILIO_ACCOUNT_SID   Twilio account SID (required for outbound send)
TWILIO_AUTH_TOKEN    Twilio auth token (required for webhook validation + send)
TWILIO_FROM_NUMBER   E.164 number to send from (e.g. +15551234567)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from db import get_db, get_db_connection, get_setting
from dependencies import get_admin_user, require_role
from mqtt_client import mqtt_client as _mqtt_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
_TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
_TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Public-facing webhook (no auth – Twilio signature validated internally)
webhook_router = APIRouter(tags=["sms-proxy-webhook"])

# Agent-facing router (requires handler or admin JWT)
agent_router = APIRouter(prefix="/api/sms", tags=["sms-proxy-agent"])

# Admin-facing router (requires admin JWT)
admin_sms_router = APIRouter(prefix="/api/admin/sms", tags=["sms-proxy-admin"])

# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------


def migrate_sms_proxy(conn: sqlite3.Connection) -> None:
    """Create SMS proxy tables if they do not exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sms_threads (
            thread_id    TEXT PRIMARY KEY,
            from_number  TEXT NOT NULL,
            to_number    TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sms_messages (
            message_id   TEXT PRIMARY KEY,
            thread_id    TEXT NOT NULL REFERENCES sms_threads(thread_id),
            direction    TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            body         TEXT NOT NULL DEFAULT '',
            media_urls   TEXT NOT NULL DEFAULT '[]',
            status       TEXT NOT NULL DEFAULT 'received',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sms_moderation_queue (
            queue_id       TEXT PRIMARY KEY,
            thread_id      TEXT NOT NULL REFERENCES sms_threads(thread_id),
            agent_user_id  TEXT NOT NULL,
            original_body  TEXT NOT NULL,
            rewritten_body TEXT NOT NULL,
            final_body     TEXT,
            queue_status   TEXT NOT NULL DEFAULT 'pending'
                           CHECK(queue_status IN ('pending', 'approved', 'rejected')),
            created_at     TEXT NOT NULL,
            decided_at     TEXT,
            decided_by     TEXT
        );
        """
    )

    # Backfill: ensure sms_tone_rules setting row exists
    conn.execute(
        """
        INSERT OR IGNORE INTO settings (key, value, updated_at)
        VALUES ('sms_tone_rules', ?, ?)
        """,
        (json.dumps(_DEFAULT_TONE_RULES), _now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tone compliance middleware
# ---------------------------------------------------------------------------

# Default vocabulary replacement rules.  Each entry is
# {"pattern": <regex>, "replacement": <string>}.
# Patterns are case-insensitive and matched at word boundaries.
_DEFAULT_TONE_RULES: list[dict] = [
    {"pattern": r"\bpls\b",          "replacement": "please"},
    {"pattern": r"\bplz\b",          "replacement": "please"},
    {"pattern": r"\bgonna\b",        "replacement": "going to"},
    {"pattern": r"\bwanna\b",        "replacement": "want to"},
    {"pattern": r"\bgotta\b",        "replacement": "have to"},
    {"pattern": r"\bcause\b",        "replacement": "because"},
    {"pattern": r"\bcuz\b",          "replacement": "because"},
    {"pattern": r"\bu\b",            "replacement": "you"},
    {"pattern": r"\bur\b",           "replacement": "your"},
    {"pattern": r"\bthx\b",          "replacement": "thank you"},
    {"pattern": r"\bthanks\b",       "replacement": "thank you"},
    {"pattern": r"\bimo\b",          "replacement": "in my opinion"},
    {"pattern": r"\btbh\b",          "replacement": "to be honest"},
    {"pattern": r"\bfyi\b",          "replacement": "for your information"},
    {"pattern": r"\basap\b",         "replacement": "as soon as possible"},
    {"pattern": r"\bnp\b",           "replacement": "no problem"},
    {"pattern": r"\byep\b",          "replacement": "yes"},
    {"pattern": r"\byeah\b",         "replacement": "yes"},
    {"pattern": r"\bnope\b",         "replacement": "no"},
    {"pattern": r"\bgr8\b",          "replacement": "great"},
    {"pattern": r"\blmk\b",          "replacement": "please let me know"},
    {"pattern": r"\bbtw\b",          "replacement": "by the way"},
    {"pattern": r"\bomw\b",          "replacement": "on my way"},
    {"pattern": r"\bbrb\b",          "replacement": "I will be right back"},
    {"pattern": r"\bbbl\b",          "replacement": "I will be back later"},
    {"pattern": r"\bidk\b",          "replacement": "I do not know"},
    {"pattern": r"\bkinda\b",        "replacement": "somewhat"},
    {"pattern": r"\bsorta\b",        "replacement": "somewhat"},
    {"pattern": r"\bdunno\b",        "replacement": "I do not know"},
    {"pattern": r"\blemme\b",        "replacement": "let me"},
    {"pattern": r"\bgimme\b",        "replacement": "give me"},
]


def _load_tone_rules(db: sqlite3.Connection) -> list[dict]:
    """Load tone replacement rules from the settings table."""
    raw = get_setting(db, "sms_tone_rules")
    if not raw:
        return _DEFAULT_TONE_RULES
    try:
        rules = json.loads(raw)
        if isinstance(rules, list):
            return rules
    except Exception:
        pass
    return _DEFAULT_TONE_RULES


def apply_tone_compliance(text: str, rules: list[dict]) -> str:
    """Apply all vocabulary replacement rules to *text* and return the result."""
    result = text
    for rule in rules:
        try:
            pattern = rule.get("pattern", "")
            replacement = rule.get("replacement", "")
            if pattern:
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        except re.error:
            logger.warning("Invalid tone rule pattern %r – skipped.", rule.get("pattern"))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_twilio_signature(request_url: str, params: dict, signature: str) -> bool:
    """Return True if the Twilio X-Twilio-Signature header is valid."""
    auth_token = _effective_auth_token()
    if not auth_token:
        logger.warning("TWILIO_AUTH_TOKEN not set – skipping signature validation.")
        return True

    s = request_url
    for key in sorted(params):
        s += key + str(params[key])

    mac = hmac.new(
        auth_token.encode("utf-8"),
        s.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    expected = b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _effective_account_sid(db: Optional[sqlite3.Connection] = None) -> str:
    if db:
        row = db.execute("SELECT value FROM settings WHERE key = 'twilio_account_sid'").fetchone()
        if row and row["value"]:
            return str(row["value"]).strip()
    return _TWILIO_ACCOUNT_SID


def _effective_auth_token(db: Optional[sqlite3.Connection] = None) -> str:
    if db:
        row = db.execute("SELECT value FROM settings WHERE key = 'twilio_auth_token'").fetchone()
        if row and row["value"]:
            return str(row["value"]).strip()
    return _TWILIO_AUTH_TOKEN


def _effective_from_number(db: Optional[sqlite3.Connection] = None) -> str:
    if db:
        row = db.execute("SELECT value FROM settings WHERE key = 'twilio_from_number'").fetchone()
        if row and row["value"]:
            return str(row["value"]).strip()
    return _TWILIO_FROM_NUMBER


def _get_or_create_thread(
    db: sqlite3.Connection, from_number: str, to_number: str
) -> str:
    """Return existing thread_id or create a new one for this number pair."""
    row = db.execute(
        "SELECT thread_id FROM sms_threads WHERE from_number = ? AND to_number = ?",
        (from_number, to_number),
    ).fetchone()
    if row:
        thread_id = str(row["thread_id"])
        db.execute(
            "UPDATE sms_threads SET updated_at = ? WHERE thread_id = ?",
            (_now_iso(), thread_id),
        )
        db.commit()
        return thread_id

    thread_id = uuid.uuid4().hex
    now = _now_iso()
    db.execute(
        "INSERT INTO sms_threads (thread_id, from_number, to_number, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (thread_id, from_number, to_number, now, now),
    )
    db.commit()
    return thread_id


def _store_inbound_message(
    db: sqlite3.Connection,
    thread_id: str,
    body: str,
    media_urls: list[str],
) -> str:
    message_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO sms_messages (message_id, thread_id, direction, body, media_urls, status, created_at)
        VALUES (?, ?, 'inbound', ?, ?, 'received', ?)
        """,
        (message_id, thread_id, body, json.dumps(media_urls), _now_iso()),
    )
    db.commit()
    return message_id


def _store_outbound_message(
    db: sqlite3.Connection,
    thread_id: str,
    body: str,
    send_status: str = "sent",
) -> str:
    message_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO sms_messages (message_id, thread_id, direction, body, media_urls, status, created_at)
        VALUES (?, ?, 'outbound', ?, '[]', ?, ?)
        """,
        (message_id, thread_id, body, send_status, _now_iso()),
    )
    db.commit()
    return message_id


def _publish_incoming_proxy_sms(
    db: sqlite3.Connection,
    from_number: str,
    to_number: str,
    body: str,
    media_urls: list[str],
    thread_id: str,
    message_id: str,
) -> None:
    """Publish INCOMING_PROXY_SMS via MQTT to all paired devices."""
    try:
        _mqtt_client.start(db)
        if not _mqtt_client.enabled:
            logger.warning("MQTT not enabled – INCOMING_PROXY_SMS not forwarded.")
            return

        from routers.tpe import _known_device_ids
        device_ids = _known_device_ids(db)
        if not device_ids:
            logger.info("No devices registered – INCOMING_PROXY_SMS not forwarded.")
            return

        payload: dict = {
            "action": "INCOMING_PROXY_SMS",
            "from_number": from_number,
            "to_number": to_number,
            "body": body,
            "media_urls": media_urls,
            "thread_id": thread_id,
            "message_id": message_id,
            "received_at": _now_iso(),
        }

        sent = failed = 0
        for device_id in device_ids:
            topic = _mqtt_client.topic_for_device_command(device_id)
            if _mqtt_client.publish_json(topic, payload, qos=1):
                sent += 1
            else:
                failed += 1

        logger.info(
            "INCOMING_PROXY_SMS dispatched: sent=%d failed=%d thread=%s",
            sent,
            failed,
            thread_id,
        )
    except Exception as exc:
        logger.exception("Error publishing INCOMING_PROXY_SMS: %s", exc)


async def _send_via_twilio(
    to_number: str,
    body: str,
    db: sqlite3.Connection,
) -> dict:
    """Send an SMS via the Twilio REST API. Returns the Twilio response dict."""
    account_sid = _effective_account_sid(db)
    auth_token  = _effective_auth_token(db)
    from_number = _effective_from_number(db)

    if not account_sid or not auth_token:
        raise HTTPException(
            status_code=503,
            detail="Twilio credentials not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
        )
    if not from_number:
        raise HTTPException(
            status_code=503,
            detail="TWILIO_FROM_NUMBER not configured.",
        )

    url = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            data={"To": to_number, "From": from_number, "Body": body},
            auth=(account_sid, auth_token),
        )

    if resp.status_code not in (200, 201):
        logger.warning(
            "Twilio send failed: status=%d body=%s",
            resp.status_code,
            resp.text[:200],
        )
        raise HTTPException(
            status_code=502,
            detail=f"Twilio returned {resp.status_code}.",
        )

    return resp.json()


# ---------------------------------------------------------------------------
# Twilio inbound webhook
# ---------------------------------------------------------------------------


@webhook_router.post("/api/sms/twilio/inbound")
async def twilio_inbound_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Twilio inbound SMS/MMS webhook.

    Twilio sends a POST with form-encoded parameters:
      From, To, Body, NumMedia, MediaUrl0..N, MediaContentType0..N, etc.

    Validates the Twilio signature, stores the message (including any media
    URLs) in the sms_threads / sms_messages tables, then publishes an
    INCOMING_PROXY_SMS MQTT payload to all paired field devices.

    Returns an empty TwiML <Response/> so Twilio does not send an auto-reply.
    """
    form = await request.form()
    params = dict(form)

    # ── Twilio signature validation ─────────────────────────────────────────
    sig = request.headers.get("X-Twilio-Signature", "")
    request_url = str(request.url)
    if sig and not _validate_twilio_signature(request_url, params, sig):
        logger.warning("Twilio signature validation failed for inbound webhook.")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    from_number: str = str(params.get("From", "")).strip()
    to_number: str = str(params.get("To", "")).strip()
    body: str = str(params.get("Body", "")).strip()

    if not from_number:
        raise HTTPException(status_code=400, detail="Missing 'From' parameter.")

    # ── Collect MMS media URLs ──────────────────────────────────────────────
    try:
        num_media = int(params.get("NumMedia", "0"))
    except (ValueError, TypeError):
        num_media = 0

    media_urls: list[str] = []
    for i in range(num_media):
        url = str(params.get(f"MediaUrl{i}", "")).strip()
        if url:
            media_urls.append(url)

    # ── Persist thread and message ─────────────────────────────────────────
    thread_id  = _get_or_create_thread(db, from_number, to_number)
    message_id = _store_inbound_message(db, thread_id, body, media_urls)

    # ── Forward to field devices via MQTT (background) ─────────────────────
    captured_db = get_db_connection()
    background_tasks.add_task(
        _publish_incoming_proxy_sms_bg,
        captured_db,
        from_number,
        to_number,
        body,
        media_urls,
        thread_id,
        message_id,
    )

    logger.info(
        "Inbound SMS received: from=%s thread=%s media_count=%d",
        from_number,
        thread_id,
        len(media_urls),
    )

    # Return empty TwiML – no auto-reply
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


def _publish_incoming_proxy_sms_bg(
    db: sqlite3.Connection,
    from_number: str,
    to_number: str,
    body: str,
    media_urls: list[str],
    thread_id: str,
    message_id: str,
) -> None:
    try:
        _publish_incoming_proxy_sms(
            db, from_number, to_number, body, media_urls, thread_id, message_id
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Agent reply endpoint  (read-write for handler+, but replies go to queue)
# ---------------------------------------------------------------------------


class AgentReplyBody(BaseModel):
    thread_id: str
    body: str


@agent_router.post("/reply")
async def agent_reply(
    payload: AgentReplyBody,
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_role(["handler"])),
):
    """
    Submit an outgoing reply from the agent's app.

    The reply is NOT sent immediately.  Instead:
      1. Tone compliance middleware rewrites the body.
      2. The rewritten body is stored in the moderation queue with
         status 'pending'.
      3. An admin must approve (and optionally edit) the entry before
         it is dispatched via Twilio.
    """
    thread_id = payload.thread_id.strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required.")

    thread_row = db.execute(
        "SELECT thread_id FROM sms_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if not thread_row:
        raise HTTPException(status_code=404, detail="Thread not found.")

    original_body = payload.body.strip()
    if not original_body:
        raise HTTPException(status_code=400, detail="Message body cannot be empty.")

    # Apply tone compliance middleware
    rules = _load_tone_rules(db)
    rewritten_body = apply_tone_compliance(original_body, rules)

    queue_id = uuid.uuid4().hex
    uid = current_user.get("user_id")
    agent_user_id = str(uid) if uid is not None else str(current_user.get("username", "unknown"))

    db.execute(
        """
        INSERT INTO sms_moderation_queue
            (queue_id, thread_id, agent_user_id, original_body, rewritten_body, queue_status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (queue_id, thread_id, agent_user_id, original_body, rewritten_body, _now_iso()),
    )
    db.commit()

    logger.info(
        "Agent reply queued for moderation: queue_id=%s thread=%s agent=%s",
        queue_id,
        thread_id,
        agent_user_id,
    )

    return {
        "status": "pending_review",
        "queue_id": queue_id,
        "original_body": original_body,
        "rewritten_body": rewritten_body,
        "message": "Your reply has been queued and is awaiting admin approval.",
    }


# ---------------------------------------------------------------------------
# Admin – moderation queue management
# ---------------------------------------------------------------------------


@admin_sms_router.get("/queue")
def list_moderation_queue(
    queue_status: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """Return moderation queue items, optionally filtered by status."""
    valid_statuses = {"pending", "approved", "rejected"}
    if queue_status and queue_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(valid_statuses)}.")

    if queue_status:
        rows = db.execute(
            """
            SELECT q.*, t.from_number, t.to_number
            FROM sms_moderation_queue q
            JOIN sms_threads t ON t.thread_id = q.thread_id
            WHERE q.queue_status = ?
            ORDER BY q.created_at DESC
            """,
            (queue_status,),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT q.*, t.from_number, t.to_number
            FROM sms_moderation_queue q
            JOIN sms_threads t ON t.thread_id = q.thread_id
            ORDER BY q.created_at DESC
            """,
        ).fetchall()

    return [dict(r) for r in rows]


class ApproveBody(BaseModel):
    final_body: Optional[str] = None  # if provided, overrides rewritten_body


@admin_sms_router.post("/queue/{queue_id}/approve")
async def approve_queue_item(
    queue_id: str,
    payload: ApproveBody,
    background_tasks: BackgroundTasks,
    db: sqlite3.Connection = Depends(get_db),
    admin: str = Depends(get_admin_user),
):
    """
    Approve a pending moderation queue item and dispatch it via Twilio.

    The admin may optionally supply *final_body* to override the rewritten text
    before sending.  If omitted, the rewritten_body is sent as-is.
    """
    row = db.execute(
        """
        SELECT q.*, t.from_number, t.to_number
        FROM sms_moderation_queue q
        JOIN sms_threads t ON t.thread_id = q.thread_id
        WHERE q.queue_id = ?
        """,
        (queue_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if row["queue_status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Queue item is already '{row['queue_status']}'.",
        )

    final_body = (payload.final_body or "").strip() or str(row["rewritten_body"])
    to_number  = str(row["from_number"])  # reply goes back to the original sender
    thread_id  = str(row["thread_id"])

    # Send via Twilio
    await _send_via_twilio(to_number, final_body, db)

    now = _now_iso()
    db.execute(
        """
        UPDATE sms_moderation_queue
        SET queue_status = 'approved', final_body = ?, decided_at = ?, decided_by = ?
        WHERE queue_id = ?
        """,
        (final_body, now, admin, queue_id),
    )
    _store_outbound_message(db, thread_id, final_body, send_status="sent")
    db.commit()

    logger.info(
        "Queue item approved and sent: queue_id=%s thread=%s admin=%s",
        queue_id,
        thread_id,
        admin,
    )

    return {
        "status": "approved",
        "queue_id": queue_id,
        "final_body": final_body,
        "to_number": to_number,
    }


@admin_sms_router.post("/queue/{queue_id}/reject")
def reject_queue_item(
    queue_id: str,
    db: sqlite3.Connection = Depends(get_db),
    admin: str = Depends(get_admin_user),
):
    """Reject a pending moderation queue item without sending."""
    row = db.execute(
        "SELECT queue_id, queue_status FROM sms_moderation_queue WHERE queue_id = ?",
        (queue_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if row["queue_status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Queue item is already '{row['queue_status']}'.",
        )

    db.execute(
        """
        UPDATE sms_moderation_queue
        SET queue_status = 'rejected', decided_at = ?, decided_by = ?
        WHERE queue_id = ?
        """,
        (_now_iso(), admin, queue_id),
    )
    db.commit()

    logger.info("Queue item rejected: queue_id=%s admin=%s", queue_id, admin)
    return {"status": "rejected", "queue_id": queue_id}


# ---------------------------------------------------------------------------
# Admin – direct send (bypasses all filters)
# ---------------------------------------------------------------------------


class AdminDirectSendBody(BaseModel):
    to_number: str
    body: str
    thread_id: Optional[str] = None


@admin_sms_router.post("/send")
async def admin_direct_send(
    payload: AdminDirectSendBody,
    db: sqlite3.Connection = Depends(get_db),
    admin: str = Depends(get_admin_user),
):
    """
    Admin bypass: send an SMS directly to any number via Twilio.

    Skips the moderation queue and tone compliance middleware entirely.
    If *thread_id* is provided and valid, the message is logged to that thread.
    Otherwise a new outbound-only thread record is created.
    """
    to_number = payload.to_number.strip()
    body      = payload.body.strip()

    if not to_number:
        raise HTTPException(status_code=400, detail="to_number is required.")
    if not body:
        raise HTTPException(status_code=400, detail="body is required.")

    await _send_via_twilio(to_number, body, db)

    # Resolve or create thread for logging
    thread_id = (payload.thread_id or "").strip()
    if thread_id:
        row = db.execute(
            "SELECT thread_id FROM sms_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if not row:
            thread_id = ""
    if not thread_id:
        from_number = _effective_from_number(db)
        thread_id = _get_or_create_thread(db, from_number, to_number)

    _store_outbound_message(db, thread_id, body, send_status="sent_by_admin")
    db.commit()

    logger.info(
        "Admin direct send: admin=%s to=%s thread=%s",
        admin,
        to_number,
        thread_id,
    )

    return {
        "status": "sent",
        "to_number": to_number,
        "thread_id": thread_id,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Admin – thread and message inspection
# ---------------------------------------------------------------------------


@admin_sms_router.get("/threads")
def list_threads(
    db: sqlite3.Connection = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """List all SMS threads ordered by most-recently-updated first."""
    rows = db.execute(
        "SELECT * FROM sms_threads ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@admin_sms_router.get("/threads/{thread_id}/messages")
def get_thread_messages(
    thread_id: str,
    db: sqlite3.Connection = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """Return all messages in a thread ordered chronologically."""
    thread = db.execute(
        "SELECT * FROM sms_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found.")

    messages = db.execute(
        """
        SELECT message_id, direction, body, media_urls, status, created_at
        FROM sms_messages
        WHERE thread_id = ?
        ORDER BY created_at ASC
        """,
        (thread_id,),
    ).fetchall()

    result = []
    for m in messages:
        row = dict(m)
        try:
            row["media_urls"] = json.loads(row["media_urls"])
        except Exception:
            row["media_urls"] = []
        result.append(row)

    return {"thread": dict(thread), "messages": result}


# ---------------------------------------------------------------------------
# Admin – tone rule management
# ---------------------------------------------------------------------------


@admin_sms_router.get("/tone-rules")
def get_tone_rules(
    db: sqlite3.Connection = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """Return the active tone compliance rules."""
    return {"rules": _load_tone_rules(db)}


class ToneRulesBody(BaseModel):
    rules: list[dict]


@admin_sms_router.put("/tone-rules")
def update_tone_rules(
    payload: ToneRulesBody,
    db: sqlite3.Connection = Depends(get_db),
    _admin: str = Depends(get_admin_user),
):
    """Replace the full set of tone compliance rules."""
    rules = payload.rules
    for rule in rules:
        if not isinstance(rule.get("pattern"), str) or not isinstance(rule.get("replacement"), str):
            raise HTTPException(
                status_code=400,
                detail="Each rule must have string 'pattern' and 'replacement' fields.",
            )
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid regex pattern {rule['pattern']!r}: {exc}",
            )

    db.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ('sms_tone_rules', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(rules), _now_iso()),
    )
    db.commit()
    return {"status": "updated", "rule_count": len(rules)}
