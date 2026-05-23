"""
Public SMS control + WATCH signaling routes.

Endpoints:
  POST /sms                       Twilio inbound SMS webhook (public)
  POST /api/public/leak           Device leak callback → send MMS to SECRET requester
  GET  /api/public/leak/media/{id} Serve uploaded leak image for Twilio MediaUrl
  WS   /ws/public/watch/{id}      WebRTC signaling relay for 60-second WATCH sessions
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response

from db import get_db, get_db_connection
from routers.tpe import _send_mqtt_to_device

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-control"])

_DEFAULT_UNIT_084_DEVICE_ID = "unit-084"
_UNIT_084_DEVICE_ID = os.environ.get("UNIT_084_DEVICE_ID", _DEFAULT_UNIT_084_DEVICE_ID).strip() or _DEFAULT_UNIT_084_DEVICE_ID
_WATCH_LINK_BASE = os.environ.get("WATCH_LINK_BASE", "https://mochii.live/watch").rstrip("/")
_LEAK_UPLOAD_PATH = Path(os.environ.get("PUBLIC_LEAK_UPLOAD_PATH", "/app/data/public_leaks"))

_TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
_TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
_TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
_TWILIO_VALIDATE_SIGNATURE = os.environ.get("TWILIO_VALIDATE_SIGNATURE", "false").strip().lower() == "true"

_IMAGE_URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")
_UPLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_LEAK_BYTES = 10 * 1024 * 1024
_MAX_BAN_WORD_LEN = 128
_MAX_WS_MESSAGE_LEN = 64 * 1024
_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_UUID_HEX_RE = re.compile(r"[0-9a-f]{32}")
_TWILIO_API_TIMEOUT_SECONDS = 12.0
_WS_SEND_TIMEOUT_SECONDS = 5.0
_WS_CLOSE_SESSION_NOT_FOUND = 4404

# session_id -> connected peers
_watch_rooms: dict[str, set[WebSocket]] = {}
# WATCH links are intentionally short-lived (60 seconds) for public safety.
_WATCH_TTL_SECONDS = 60


def migrate_public_control(conn: sqlite3.Connection) -> None:
    """Create public control tables if they do not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public_watch_sessions (
            id              TEXT PRIMARY KEY,
            requester_phone TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public_secret_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_phone TEXT NOT NULL,
            requested_at    TEXT NOT NULL,
            fulfilled_at    TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public_leak_media (
            id            TEXT PRIMARY KEY,
            filename      TEXT NOT NULL,
            content_type  TEXT NOT NULL,
            received_at   TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _xml_response(message: str) -> Response:
    escaped = html.escape(message)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Message>{escaped}</Message>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


def _extract_image_url(text: str) -> Optional[str]:
    if not text:
        return None
    for match in _IMAGE_URL_RE.findall(text):
        candidate = match.strip().lstrip("(").rstrip(".,!?)]")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(_IMAGE_EXTS):
            return candidate
    return None


def _dispatch_to_unit_084(db: sqlite3.Connection, payload: dict[str, str]) -> None:
    _send_mqtt_to_device(db, _UNIT_084_DEVICE_ID, payload)


def _store_watch_session(db: sqlite3.Connection, session_id: str, requester_phone: str) -> None:
    created = _now()
    expires = created + timedelta(seconds=_WATCH_TTL_SECONDS)
    db.execute(
        """
        INSERT INTO public_watch_sessions (id, requester_phone, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, requester_phone, created.isoformat(), expires.isoformat()),
    )
    db.commit()


def _record_secret_request(db: sqlite3.Connection, requester_phone: str) -> None:
    db.execute(
        """
        INSERT INTO public_secret_requests (requester_phone, requested_at, fulfilled_at)
        VALUES (?, ?, NULL)
        """,
        (requester_phone, _now_iso()),
    )
    db.commit()


def _twilio_credentials_available() -> bool:
    return bool(_TWILIO_ACCOUNT_SID and _TWILIO_AUTH_TOKEN and _TWILIO_FROM_NUMBER)


def _valid_phone(value: str) -> bool:
    return bool(_PHONE_RE.fullmatch(value.strip()))


def _is_safe_public_image_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(_IMAGE_EXTS)


def _validate_twilio_signature(
    request: Request,
    signature: str,
    fields: dict[str, str],
) -> bool:
    if not signature or not _TWILIO_AUTH_TOKEN:
        return False
    url = str(request.url).split("?", 1)[0]
    s = url
    for key in sorted(fields.keys()):
        s += key + (fields[key] or "")
    digest = hmac.new(
        _TWILIO_AUTH_TOKEN.encode("utf-8"),
        s.encode("utf-8"),
        hashlib.sha1,  # Twilio webhook signature spec mandates HMAC-SHA1.
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return secrets.compare_digest(expected, signature)


def _base_url_for_public(request: Request, db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = 'base_url'").fetchone()
    if row and row["value"]:
        return row["value"].rstrip("/")
    env_base = os.environ.get("BASE_URL", "").rstrip("/")
    if env_base:
        return env_base
    return str(request.base_url).rstrip("/")


async def _send_twilio_mms(to_number: str, body_text: str, media_url: str) -> None:
    if not _twilio_credentials_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twilio MMS is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER.",
        )
    if not _valid_phone(to_number):
        raise HTTPException(status_code=400, detail="Invalid destination phone number.")
    if not _is_safe_public_image_url(media_url):
        raise HTTPException(status_code=400, detail="Invalid media URL.")
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{_TWILIO_ACCOUNT_SID}/Messages.json"
    form = {
        "To": to_number,
        "From": _TWILIO_FROM_NUMBER,
        "Body": body_text,
        "MediaUrl": media_url,
    }
    async with httpx.AsyncClient(timeout=_TWILIO_API_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            endpoint,
            content=urlencode(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(_TWILIO_ACCOUNT_SID, _TWILIO_AUTH_TOKEN),
        )
    if not resp.is_success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Twilio MMS send failed: status={resp.status_code}",
        )


def _has_mms_attachment(num_media: str, media_url_0: str) -> bool:
    if not media_url_0.strip():
        return False
    if not num_media:
        return False
    try:
        return int(num_media) > 0
    except ValueError:
        return False


@router.post("/sms")
async def twilio_sms_webhook(
    request: Request,
    body: str = Form(default="", alias="Body"),
    from_number: str = Form(default="", alias="From"),
    num_media: str = Form(default="0", alias="NumMedia"),
    media_url_0: str = Form(default="", alias="MediaUrl0"),
    db: sqlite3.Connection = Depends(get_db),
):
    text = (body or "").strip()
    text_upper = text.upper()
    from_number = from_number.strip()
    signature = request.headers.get("X-Twilio-Signature", "").strip()

    if _TWILIO_VALIDATE_SIGNATURE:
        fields = {
            "Body": body,
            "From": from_number,
            "NumMedia": num_media,
            "MediaUrl0": media_url_0,
        }
        if not _validate_twilio_signature(request, signature, fields):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    image_url = _extract_image_url(text)
    if not image_url and _has_mms_attachment(num_media, media_url_0):
        image_url = media_url_0.strip()
    if image_url and not _is_safe_public_image_url(image_url):
        image_url = None

    try:
        if text_upper in {"SHOCK", "VIBRATE"}:
            _dispatch_to_unit_084(db, {"action": "ble_trigger", "type": text_upper.lower()})
            return _xml_response("Physical stimulus delivered to Unit 084.")

        if text_upper == "WATCH":
            session_id = uuid.uuid4().hex
            _store_watch_session(db, session_id, from_number)
            _dispatch_to_unit_084(
                db,
                {
                    "action": "start_spectacle",
                    "session_id": session_id,
                    "ttl_seconds": str(_WATCH_TTL_SECONDS),
                },
            )
            return _xml_response(f"{_WATCH_LINK_BASE}/{session_id}")

        if text_upper == "SECRET":
            if not _valid_phone(from_number):
                return _xml_response("Command failed. Please try again shortly.")
            _record_secret_request(db, from_number)
            _dispatch_to_unit_084(db, {"action": "roulette_leak"})
            return _xml_response("Accessing Unit 084's gallery. Stand by.")

        if image_url:
            _dispatch_to_unit_084(db, {"action": "set_wallpaper", "image_url": image_url})
            return _xml_response("Unit's wallpaper overwritten.")

        ban_word = text[:_MAX_BAN_WORD_LEN] if text else "<empty>"
        _dispatch_to_unit_084(db, {"action": "ban_word", "word": ban_word})
        return _xml_response("Word stolen. The Unit can no longer type this.")
    except HTTPException as exc:
        logger.warning("Public SMS command failed: %s", exc.detail)
        return _xml_response("Command failed. Please try again shortly.")
    except Exception as exc:
        logger.exception("Unexpected SMS webhook error: %s", exc)
        return _xml_response("Command failed. Please try again shortly.")


def _leak_ext_from_content_type(content_type: str) -> str:
    c = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    return mapping.get(c, ".jpg")


def _store_uploaded_leak_media(db: sqlite3.Connection, upload: UploadFile) -> str:
    _LEAK_UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    media_id = uuid.uuid4().hex
    ext = _leak_ext_from_content_type(upload.content_type or "")
    filename = f"{media_id}{ext}"
    path = _LEAK_UPLOAD_PATH / filename

    with path.open("wb") as out:
        total = 0
        while True:
            chunk = upload.file.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_LEAK_BYTES:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail="Leak file too large.")
            out.write(chunk)

    db.execute(
        """
        INSERT INTO public_leak_media (id, filename, content_type, received_at)
        VALUES (?, ?, ?, ?)
        """,
        (media_id, filename, upload.content_type or "application/octet-stream", _now_iso()),
    )
    db.commit()
    return media_id


def _store_raw_leak_media(
    db: sqlite3.Connection,
    data: bytes,
    content_type: str = "image/jpeg",
) -> str:
    if len(data) > _MAX_LEAK_BYTES:
        raise HTTPException(status_code=413, detail="Leak payload too large.")
    _LEAK_UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    media_id = uuid.uuid4().hex
    ext = _leak_ext_from_content_type(content_type)
    filename = f"{media_id}{ext}"
    path = _LEAK_UPLOAD_PATH / filename
    path.write_bytes(data)

    db.execute(
        """
        INSERT INTO public_leak_media (id, filename, content_type, received_at)
        VALUES (?, ?, ?, ?)
        """,
        (media_id, filename, content_type, _now_iso()),
    )
    db.commit()
    return media_id


def _next_secret_recipient(db: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT id, requester_phone
        FROM public_secret_requests
        WHERE fulfilled_at IS NULL
        ORDER BY requested_at ASC
        LIMIT 1
        """
    ).fetchone()


@router.post("/api/public/leak")
async def public_leak_callback(
    request: Request,
    image_url: Optional[str] = Form(default=None),
    image_base64: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
    recipient = _next_secret_recipient(db)
    if not recipient:
        raise HTTPException(status_code=404, detail="No pending SECRET requester.")

    resolved_image_url = (image_url or "").strip()

    if not resolved_image_url and image_base64:
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image_base64 payload.")
        media_id = _store_raw_leak_media(db, data, content_type="image/jpeg")
        base_url = _base_url_for_public(request, db)
        resolved_image_url = f"{base_url}/api/public/leak/media/{media_id}"

    if not resolved_image_url and file is not None:
        media_id = _store_uploaded_leak_media(db, file)
        base_url = _base_url_for_public(request, db)
        resolved_image_url = f"{base_url}/api/public/leak/media/{media_id}"

    if not resolved_image_url:
        raise HTTPException(status_code=400, detail="Provide image_url, image_base64, or file.")

    await _send_twilio_mms(
        to_number=recipient["requester_phone"],
        body_text="Unit 084 leak received.",
        media_url=resolved_image_url,
    )

    db.execute(
        "UPDATE public_secret_requests SET fulfilled_at = ? WHERE id = ?",
        (_now_iso(), recipient["id"]),
    )
    db.commit()
    return {"status": "sent", "to": recipient["requester_phone"]}


@router.get("/api/public/leak/media/{media_id}")
def get_public_leak_media(
    media_id: str,
    db: sqlite3.Connection = Depends(get_db),
):
    if not _UUID_HEX_RE.fullmatch(media_id):
        raise HTTPException(status_code=400, detail="Invalid media id.")
    row = db.execute(
        "SELECT filename, content_type FROM public_leak_media WHERE id = ?",
        (media_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media not found.")

    path = _LEAK_UPLOAD_PATH / row["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media file missing.")
    return Response(content=path.read_bytes(), media_type=row["content_type"])


def _watch_session_active(db: sqlite3.Connection, session_id: str) -> bool:
    row = db.execute(
        "SELECT expires_at FROM public_watch_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return False
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except Exception:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > _now()


async def _relay_watch_payload(room: set[WebSocket], source: WebSocket, raw: str) -> None:
    if len(raw.encode("utf-8")) > _MAX_WS_MESSAGE_LEN:
        raise HTTPException(status_code=413, detail="Signaling payload too large.")
    try:
        parsed = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Signaling payload must be valid JSON.")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Signaling payload must be a JSON object.")
    payload_text = json.dumps(parsed, separators=(",", ":"))

    peers = [peer for peer in room if peer is not source]
    if not peers:
        return
    tasks = [asyncio.wait_for(peer.send_text(payload_text), timeout=_WS_SEND_TIMEOUT_SECONDS) for peer in peers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for peer, result in zip(peers, results):
        if isinstance(result, Exception):
            room.discard(peer)


@router.websocket("/ws/public/watch/{session_id}")
async def public_watch_signal_ws(
    websocket: WebSocket,
    session_id: str,
):
    db = get_db_connection()
    try:
        if not _watch_session_active(db, session_id):
            await websocket.close(code=_WS_CLOSE_SESSION_NOT_FOUND)
            return

        await websocket.accept()
        room = _watch_rooms.setdefault(session_id, set())
        room.add(websocket)
        logger.info("WATCH signaling peer joined: session=%s size=%d", session_id, len(room))

        try:
            while True:
                raw = await websocket.receive_text()
                await _relay_watch_payload(room, websocket, raw)
        except WebSocketDisconnect:
            pass
        finally:
            room.discard(websocket)
            if not room:
                _watch_rooms.pop(session_id, None)
            logger.info("WATCH signaling peer left: session=%s size=%d", session_id, len(room))
    finally:
        db.close()
