"""
Public SMS control + WATCH signaling routes.

Endpoints:
  POST /sms                       Twilio inbound SMS webhook (public)
  POST /api/public/leak           Device leak callback → send MMS to SECRET requester
  GET  /api/public/leak/media/{id}Serve uploaded leak image for Twilio MediaUrl
  WS   /ws/public/watch/{id}      WebRTC signaling relay for 60-second WATCH sessions
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response

from db import get_db, get_db_connection
from routers.tpe import _send_fcm_to_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-control"])

_DEFAULT_UNIT_084_DEVICE_ID = "unit-084"
_UNIT_084_DEVICE_ID = os.environ.get("UNIT_084_DEVICE_ID", _DEFAULT_UNIT_084_DEVICE_ID).strip() or _DEFAULT_UNIT_084_DEVICE_ID
_WATCH_LINK_BASE = os.environ.get("WATCH_LINK_BASE", "https://mochii.live/watch").rstrip("/")
_LEAK_UPLOAD_PATH = Path(os.environ.get("PUBLIC_LEAK_UPLOAD_PATH", "/app/data/public_leaks"))

_TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
_TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
_TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()

_IMAGE_URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|heic|heif)(?:[?#].*)?$", re.IGNORECASE)

# session_id -> connected peers
_watch_rooms: dict[str, set[WebSocket]] = {}
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
        candidate = match.strip(".,!?)(").strip()
        if _IMAGE_EXT_RE.search(candidate):
            return candidate
    return None


def _dispatch_to_unit_084(db: sqlite3.Connection, payload: dict[str, str]) -> None:
    row = db.execute(
        "SELECT fcm_token FROM handler_device_status WHERE device_id = ?",
        (_UNIT_084_DEVICE_ID,),
    ).fetchone()
    if not row or not row["fcm_token"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No FCM token found for device_id '{_UNIT_084_DEVICE_ID}'.",
        )
    _send_fcm_to_token(db, row["fcm_token"], payload)


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


def _upsert_secret_request(db: sqlite3.Connection, requester_phone: str) -> None:
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
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{_TWILIO_ACCOUNT_SID}/Messages.json"
    form = {
        "To": to_number,
        "From": _TWILIO_FROM_NUMBER,
        "Body": body_text,
        "MediaUrl": media_url,
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
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


@router.post("/sms")
async def twilio_sms_webhook(
    body: str = Form(default="", alias="Body"),
    from_number: str = Form(default="", alias="From"),
    num_media: str = Form(default="0", alias="NumMedia"),
    media_url_0: str = Form(default="", alias="MediaUrl0"),
    db: sqlite3.Connection = Depends(get_db),
):
    text = (body or "").strip()
    text_upper = text.upper()

    image_url = _extract_image_url(text)
    if not image_url and num_media and num_media.isdigit() and int(num_media) > 0 and media_url_0:
        image_url = media_url_0.strip()

    try:
        if text_upper in {"SHOCK", "VIBRATE"}:
            _dispatch_to_unit_084(db, {"action": "ble_trigger", "mode": text_upper.lower()})
            return _xml_response("Physical stimulus delivered to Unit 084.")

        if text_upper == "WATCH":
            session_id = uuid.uuid4().hex
            _store_watch_session(db, session_id, from_number.strip())
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
            _upsert_secret_request(db, from_number.strip())
            _dispatch_to_unit_084(db, {"action": "roulette_leak"})
            return _xml_response("Accessing Unit 084's gallery. Stand by.")

        if image_url:
            _dispatch_to_unit_084(db, {"action": "set_wallpaper", "image_url": image_url})
            return _xml_response("Unit's wallpaper overwritten.")

        _dispatch_to_unit_084(db, {"action": "ban_word", "word": text})
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
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
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
    return expires_at > _now()


@router.websocket("/ws/public/watch/{session_id}")
async def public_watch_signal_ws(
    websocket: WebSocket,
    session_id: str,
):
    db = get_db_connection()
    try:
        if not _watch_session_active(db, session_id):
            await websocket.close(code=4404)
            return

        await websocket.accept()
        room = _watch_rooms.setdefault(session_id, set())
        room.add(websocket)
        logger.info("WATCH signaling peer joined: session=%s size=%d", session_id, len(room))

        try:
            while True:
                raw = await websocket.receive_text()
                dead: list[WebSocket] = []
                for peer in room:
                    if peer is websocket:
                        continue
                    try:
                        await asyncio.wait_for(peer.send_text(raw), timeout=5.0)
                    except Exception:
                        dead.append(peer)
                for d in dead:
                    room.discard(d)
        except WebSocketDisconnect:
            pass
        finally:
            room.discard(websocket)
            if not room:
                _watch_rooms.pop(session_id, None)
            logger.info("WATCH signaling peer left: session=%s size=%d", session_id, len(room))
    finally:
        db.close()
