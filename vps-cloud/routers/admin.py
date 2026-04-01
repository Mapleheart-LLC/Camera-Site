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

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from db import get_db
from dependencies import get_admin_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_DEVICES = {"pishock", "lovense"}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CameraCreate(BaseModel):
    display_name: str
    stream_slug: str
    minimum_access_level: int = 1


class CameraUpdate(BaseModel):
    display_name: Optional[str] = None
    stream_slug: Optional[str] = None
    minimum_access_level: Optional[int] = None


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
        "SELECT id, display_name, stream_slug, minimum_access_level FROM cameras ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
def admin_add_camera(
    payload: CameraCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Insert a new camera record. Returns the created camera with its assigned id."""
    try:
        cursor = db.execute(
            """
            INSERT INTO cameras (display_name, stream_slug, minimum_access_level)
            VALUES (?, ?, ?)
            """,
            (payload.display_name, payload.stream_slug, payload.minimum_access_level),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not add camera: {exc}",
        ) from exc
    row = db.execute(
        "SELECT id, display_name, stream_slug, minimum_access_level FROM cameras WHERE id = ?",
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
    new_name = payload.display_name if payload.display_name is not None else row["display_name"]
    new_slug = payload.stream_slug if payload.stream_slug is not None else row["stream_slug"]
    new_level = (
        payload.minimum_access_level
        if payload.minimum_access_level is not None
        else row["minimum_access_level"]
    )
    try:
        db.execute(
            """
            UPDATE cameras
            SET display_name = ?, stream_slug = ?, minimum_access_level = ?
            WHERE id = ?
            """,
            (new_name, new_slug, new_level, cam_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not update camera: {exc}",
        ) from exc
    updated = db.execute(
        "SELECT id, display_name, stream_slug, minimum_access_level FROM cameras WHERE id = ?",
        (cam_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/cameras/{cam_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_camera(
    cam_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a camera from the database."""
    row = db.execute("SELECT id FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found.",
        )
    db.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
    db.commit()


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
        SELECT device, fanvue_id, activated_at
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
        "INSERT INTO activations (device, fanvue_id, activated_at) VALUES (?, ?, ?)",
        (device, f"admin:{admin_user}", datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return {
        "status": "ok",
        "device": device,
        "message": "Admin command accepted (mock response).",
        "triggered_by": admin_user,
    }
