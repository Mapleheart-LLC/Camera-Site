"""
routers/links.py – Public and admin-protected link management endpoints.

Public endpoints (no authentication required):
  GET  /api/links                     – list all active links ordered by sort_order

Admin endpoints (HTTP Basic Auth via get_admin_user):
  GET    /api/admin/links             – list all links (active + inactive)
  POST   /api/admin/links             – create a new link
  PUT    /api/admin/links/{link_id}   – update an existing link
  DELETE /api/admin/links/{link_id}   – delete a link
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from db import get_db
from dependencies import get_admin_user

router = APIRouter(tags=["links"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LinkCreate(BaseModel):
    title: str
    url: str
    emoji: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True


class LinkUpdate(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    emoji: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Public endpoint
# ---------------------------------------------------------------------------


@router.get("/api/links")
def list_public_links(db: sqlite3.Connection = Depends(get_db)):
    """Return all active links ordered by sort_order, then id."""
    rows = db.execute(
        """
        SELECT id, title, url, emoji, sort_order
        FROM links
        WHERE is_active = 1
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@router.get("/api/admin/links")
def admin_list_links(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all links (active and inactive) for the admin panel."""
    rows = db.execute(
        """
        SELECT id, title, url, emoji, sort_order, is_active, created_at
        FROM links
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/api/admin/links", status_code=status.HTTP_201_CREATED)
def admin_create_link(
    payload: LinkCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new link and return the full record."""
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        cursor = db.execute(
            """
            INSERT INTO links (title, url, emoji, sort_order, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.title,
                payload.url,
                payload.emoji or None,
                payload.sort_order,
                1 if payload.is_active else 0,
                created_at,
            ),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not create link: {exc}",
        ) from exc

    row = db.execute(
        "SELECT id, title, url, emoji, sort_order, is_active, created_at FROM links WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


@router.put("/api/admin/links/{link_id}")
def admin_update_link(
    link_id: int,
    payload: LinkUpdate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update one or more fields on an existing link."""
    row = db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found.",
        )

    new_title      = payload.title      if payload.title      is not None else row["title"]
    new_url        = payload.url        if payload.url        is not None else row["url"]
    new_emoji      = payload.emoji      if payload.emoji      is not None else row["emoji"]
    new_sort_order = payload.sort_order if payload.sort_order is not None else row["sort_order"]
    new_is_active  = (1 if payload.is_active else 0) if payload.is_active is not None else row["is_active"]

    try:
        db.execute(
            """
            UPDATE links
            SET title = ?, url = ?, emoji = ?, sort_order = ?, is_active = ?
            WHERE id = ?
            """,
            (new_title, new_url, new_emoji, new_sort_order, new_is_active, link_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not update link: {exc}",
        ) from exc

    updated = db.execute(
        "SELECT id, title, url, emoji, sort_order, is_active, created_at FROM links WHERE id = ?",
        (link_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/api/admin/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_link(
    link_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a link permanently."""
    row = db.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found.",
        )
    db.execute("DELETE FROM links WHERE id = ?", (link_id,))
    db.commit()
