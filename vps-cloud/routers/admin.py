"""
routers/admin.py – Admin-only management endpoints for mochii.live.

All endpoints are protected by HTTP Basic Auth via the ``get_admin_user``
dependency (ADMIN_USERNAME / ADMIN_PASSWORD environment variables).
This auth system is entirely separate from the Fanvue OAuth / JWT flow.

Endpoints
---------
  GET    /api/admin/cameras            – list all cameras (full details)
  POST   /api/admin/cameras            – add a new camera
  PUT    /api/admin/cameras/{cam_id}   – update an existing camera
  DELETE /api/admin/cameras/{cam_id}   – remove a camera
  GET    /api/admin/stats              – user/camera counts + recent activations
  POST   /api/admin/control/{device}   – manually trigger an IoT device
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from db import get_db
from dependencies import get_admin_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_DEVICES = {"pishock", "lovense"}

GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# go2rtc helpers
# ---------------------------------------------------------------------------


def _effective_rtsp_url(
    rtsp_url: Optional[str],
    tapo_ip: Optional[str],
    tapo_username: Optional[str],
    tapo_password: Optional[str],
) -> Optional[str]:
    """Return the RTSP URL to use for this camera record."""
    if tapo_ip:
        user = tapo_username or ""
        pwd = tapo_password or ""
        return f"rtsp://{user}:{pwd}@{tapo_ip}/stream1"
    return rtsp_url or None


def _register_stream(slug: str, rtsp_url: Optional[str]) -> None:
    """Add or update a stream in go2rtc. Failures are logged and not re-raised."""
    if not rtsp_url:
        return
    try:
        with httpx.Client(timeout=5.0) as client:
            client.put(
                f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams",
                params={"name": slug, "src": rtsp_url},
            )
    except Exception as exc:
        logger.warning("Could not register stream '%s' with go2rtc: %s", slug, exc)


def _deregister_stream(slug: str) -> None:
    """Remove a stream from go2rtc. Failures are logged and not re-raised."""
    try:
        with httpx.Client(timeout=5.0) as client:
            client.delete(
                f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams",
                params={"name": slug},
            )
    except Exception as exc:
        logger.warning("Could not deregister stream '%s' from go2rtc: %s", slug, exc)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CameraCreate(BaseModel):
    display_name: str
    stream_slug: str
    minimum_access_level: int = 1
    rtsp_url: Optional[str] = None
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None


class CameraUpdate(BaseModel):
    display_name: Optional[str] = None
    stream_slug: Optional[str] = None
    minimum_access_level: Optional[int] = None
    rtsp_url: Optional[str] = None
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None


# ---------------------------------------------------------------------------
# Camera management
# ---------------------------------------------------------------------------


@router.get("/cameras")
def admin_list_cameras(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all cameras with their full database record."""
    rows = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password
        FROM cameras ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
def admin_add_camera(
    payload: CameraCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Insert a new camera record and register its stream with go2rtc."""
    try:
        cursor = db.execute(
            """
            INSERT INTO cameras
                (display_name, stream_slug, minimum_access_level,
                 rtsp_url, tapo_ip, tapo_username, tapo_password)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.display_name,
                payload.stream_slug,
                payload.minimum_access_level,
                payload.rtsp_url or None,
                payload.tapo_ip or None,
                payload.tapo_username or None,
                payload.tapo_password or None,
            ),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not add camera: {exc}",
        ) from exc

    effective_url = _effective_rtsp_url(
        payload.rtsp_url, payload.tapo_ip, payload.tapo_username, payload.tapo_password
    )
    _register_stream(payload.stream_slug, effective_url)

    row = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password
        FROM cameras WHERE id = ?
        """,
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


@router.put("/cameras/{cam_id}")
def admin_update_camera(
    cam_id: int,
    payload: CameraUpdate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update one or more fields on an existing camera record."""
    row = db.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found.",
        )
    old_slug = row["stream_slug"]
    new_name  = payload.display_name        if payload.display_name        is not None else row["display_name"]
    new_slug  = payload.stream_slug         if payload.stream_slug         is not None else row["stream_slug"]
    new_level = payload.minimum_access_level if payload.minimum_access_level is not None else row["minimum_access_level"]
    new_rtsp  = (payload.rtsp_url      or None) if payload.rtsp_url      is not None else row["rtsp_url"]
    new_tapo_ip   = (payload.tapo_ip       or None) if payload.tapo_ip       is not None else row["tapo_ip"]
    new_tapo_user = (payload.tapo_username or None) if payload.tapo_username is not None else row["tapo_username"]
    new_tapo_pass = (payload.tapo_password or None) if payload.tapo_password is not None else row["tapo_password"]
    try:
        db.execute(
            """
            UPDATE cameras
            SET display_name = ?, stream_slug = ?, minimum_access_level = ?,
                rtsp_url = ?, tapo_ip = ?, tapo_username = ?, tapo_password = ?
            WHERE id = ?
            """,
            (new_name, new_slug, new_level, new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass, cam_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not update camera: {exc}",
        ) from exc

    # If the slug changed, remove the old stream name from go2rtc first.
    if new_slug != old_slug:
        _deregister_stream(old_slug)

    effective_url = _effective_rtsp_url(new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass)
    _register_stream(new_slug, effective_url)

    updated = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password
        FROM cameras WHERE id = ?
        """,
        (cam_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/cameras/{cam_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_camera(
    cam_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a camera from the database and deregister its stream from go2rtc."""
    row = db.execute("SELECT stream_slug FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found.",
        )
    slug = row["stream_slug"]
    db.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
    db.commit()
    _deregister_stream(slug)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
def admin_stats(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return a summary of registered users, cameras, and the 20 most recent IoT activations."""
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_cameras = db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    recent = db.execute(
        """
        SELECT device, actor, activated_at
        FROM activations
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    return {
        "total_users": total_users,
        "total_cameras": total_cameras,
        "recent_activations": [dict(r) for r in recent],
    }


# ---------------------------------------------------------------------------
# Manual IoT control (admin-only, no rate limit)
# ---------------------------------------------------------------------------


@router.post("/control/{device}")
def admin_control_device(
    device: str,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Manually trigger an IoT device without rate limiting or a Fanvue JWT.

    Logs the activation to the ``activations`` table with the admin username
    as the actor so it appears in the stats history.
    """
    if device not in _VALID_DEVICES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown device '{device}'. Valid options: {sorted(_VALID_DEVICES)}",
        )
    db.execute(
        "INSERT INTO activations (device, actor, activated_at) VALUES (?, ?, ?)",
        (device, f"admin:{admin_user}", datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return {
        "status": "ok",
        "device": device,
        "message": "Admin command accepted (mock response).",
        "triggered_by": admin_user,
    }


# ---------------------------------------------------------------------------
# Puppy Pouch – admin question management
# ---------------------------------------------------------------------------


class AnswerPayload(BaseModel):
    answer: str


@router.get("/questions")
def admin_list_unanswered_questions(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all questions that have not yet been answered."""
    rows = db.execute(
        """
        SELECT id, text, created_at
        FROM questions
        WHERE answer IS NULL
        ORDER BY created_at ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/questions/{question_id}/answer", status_code=status.HTTP_200_OK)
def admin_answer_question(
    question_id: str,
    payload: AnswerPayload,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Save an answer to the specified question and mark it as public."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute(
        "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ?",
        (payload.answer, question_id),
    )
    db.commit()
    return {"id": question_id, "message": "Answer saved and question is now public 🐾"}

