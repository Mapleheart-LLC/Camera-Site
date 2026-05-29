"""Public WATCH signaling routes and leak media intake."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from db import get_db, get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-control"])

_LEAK_UPLOAD_PATH = Path(os.environ.get("PUBLIC_LEAK_UPLOAD_PATH", "/app/data/public_leaks"))

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")
_UPLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_LEAK_BYTES = 10 * 1024 * 1024
_MAX_WS_MESSAGE_LEN = 64 * 1024
_UUID_HEX_RE = re.compile(r"[0-9a-f]{32}")
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


def _store_watch_session(db: sqlite3.Connection, session_id: str, requester_phone: str) -> str:
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
    return expires.isoformat()


def _is_safe_public_image_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(_IMAGE_EXTS)


@router.post("/api/public/watch/session")
def create_public_watch_session(
    request: Request,
    phone: Optional[str] = Form(default="anonymous"),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    session_id = uuid.uuid4().hex
    expires_at = _store_watch_session(db, session_id, (phone or "anonymous").strip() or "anonymous")
    base_url = _base_url_for_public(request, db)
    ws_base = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    return {
        "session_id": session_id,
        "expires_at": expires_at,
        "ws_url": f"{ws_base}/ws/public/watch/{session_id}",
    }


def _base_url_for_public(request: Request, db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = 'base_url'").fetchone()
    if row and row["value"]:
        return row["value"].rstrip("/")
    env_base = os.environ.get("BASE_URL", "").rstrip("/")
    if env_base:
        return env_base
    return str(request.base_url).rstrip("/")


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


@router.post("/api/public/leak")
async def public_leak_callback(
    request: Request,
    image_url: Optional[str] = Form(default=None),
    image_base64: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    db: sqlite3.Connection = Depends(get_db),
):
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

    if not _is_safe_public_image_url(resolved_image_url):
        raise HTTPException(status_code=400, detail="Invalid media URL.")

    return {"status": "stored", "media_url": resolved_image_url}


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
