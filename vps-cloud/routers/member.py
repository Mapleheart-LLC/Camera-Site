"""
routers/member.py – Subscriber member portal API.

Provides account self-service, subscription history, content feed, creator
browsing, and follow-management endpoints for subscribers and non-creator users.

Endpoints
---------
  GET    /api/member/me                    – own profile
  PATCH  /api/member/me                    – update display name (≤ 2×/year)
  POST   /api/member/me/password           – change password
  GET    /api/member/me/subscriptions      – billing history
  GET    /api/member/me/follows            – list followed creators
  POST   /api/member/me/follows/{handle}   – follow a creator
  DELETE /api/member/me/follows/{handle}   – unfollow a creator
  GET    /api/member/creators              – browse active creators (public)
  GET    /api/member/me/content-filter     – get content-rating filter preference
  PATCH  /api/member/me/content-filter     – update content-rating filter preference
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_current_user, get_optional_user
from routers.auth import _verify_password, _hash_password
from routers.alerts import dispatch_alert as _dispatch_alert

_GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "localhost")
_GO2RTC_PORT = os.environ.get("GO2RTC_PORT", "1984")

router = APIRouter(prefix="/api/member", tags=["member"])

logger = logging.getLogger(__name__)

_DISPLAY_NAME_MAX_CHANGES_PER_YEAR = 2


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class UpdateDisplayNameRequest(BaseModel):
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Public display name shown to other members.",
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_year() -> str:
    return str(datetime.now(timezone.utc).year)


def _get_site_user(db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = db.execute(
        """
        SELECT id, username, email, hashed_password, access_level, created_at,
               display_name, display_name_changed_count, display_name_last_reset
          FROM site_users
         WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return row


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------


@router.get("/me")
def get_profile(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the authenticated member's profile."""
    row = _get_site_user(db, current_user["fanvue_id"])
    year = _current_year()
    count = row["display_name_changed_count"] or 0
    last_reset = row["display_name_last_reset"] or ""
    if last_reset != year:
        count = 0
    return {
        "user_id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "display_name": row["display_name"] or row["username"],
        "access_level": row["access_level"],
        "created_at": row["created_at"],
        "display_name_changed_count": count,
        "display_name_changes_remaining": max(0, _DISPLAY_NAME_MAX_CHANGES_PER_YEAR - count),
    }


@router.patch("/me")
def update_display_name(
    payload: UpdateDisplayNameRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update the member's display name (limited to 2 changes per calendar year)."""
    row = _get_site_user(db, current_user["fanvue_id"])
    user_id = row["id"]
    year = _current_year()

    count = row["display_name_changed_count"] or 0
    last_reset = row["display_name_last_reset"] or ""

    # Reset counter when the year rolls over.
    if last_reset != year:
        count = 0

    if count >= _DISPLAY_NAME_MAX_CHANGES_PER_YEAR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Display name can only be changed {_DISPLAY_NAME_MAX_CHANGES_PER_YEAR} "
                   f"times per calendar year. The limit resets on January 1st.",
        )

    old_name = row["display_name"] or row["username"]
    new_name = payload.display_name.strip()
    now = datetime.now(timezone.utc).isoformat()

    # Record history entry.
    db.execute(
        """
        INSERT INTO display_name_history (user_id, old_name, new_name, changed_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, old_name, new_name, now),
    )

    new_count = count + 1
    db.execute(
        """
        UPDATE site_users
           SET display_name = ?,
               display_name_changed_count = ?,
               display_name_last_reset = ?
         WHERE id = ?
        """,
        (new_name, new_count, year, user_id),
    )
    db.commit()

    logger.info("Member %s changed display name: %r → %r", user_id, old_name, new_name)
    return {
        "display_name": new_name,
        "display_name_changed_count": new_count,
        "display_name_changes_remaining": max(0, _DISPLAY_NAME_MAX_CHANGES_PER_YEAR - new_count),
    }


@router.post("/me/password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Change the member's password after verifying the current password."""
    row = _get_site_user(db, current_user["fanvue_id"])

    if not _verify_password(payload.current_password, row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    new_hash = _hash_password(payload.new_password)
    db.execute(
        "UPDATE site_users SET hashed_password = ? WHERE id = ?",
        (new_hash, row["id"]),
    )
    db.commit()

    logger.info("Member %s changed their password.", row["id"])
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Subscription / billing history
# ---------------------------------------------------------------------------


@router.get("/me/subscriptions")
def get_subscription_history(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the member's Segpay billing history and current subscription status."""
    user_id = current_user["fanvue_id"]

    row = db.execute(
        "SELECT access_level FROM site_users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    events = db.execute(
        """
        SELECT id, segpay_subscription_id, trans_type, status,
               access_level_granted, email, created_at
          FROM segpay_subscriptions
         WHERE user_id = ?
         ORDER BY created_at DESC
         LIMIT 100
        """,
        (user_id,),
    ).fetchall()

    return {
        "access_level": row["access_level"],
        "is_subscribed": row["access_level"] > 0,
        "events": [dict(e) for e in events],
    }


# ---------------------------------------------------------------------------
# Creator follow management
# ---------------------------------------------------------------------------


@router.get("/me/follows")
def list_follows(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the list of creators the authenticated member follows."""
    user_id = current_user["fanvue_id"]
    rows = db.execute(
        """
        SELECT mf.creator_handle, mf.followed_at,
               ca.display_name, ca.avatar_url, ca.bio, ca.accent_color,
               ca.allow_free_content
          FROM member_follows mf
          LEFT JOIN creator_accounts ca ON ca.handle = mf.creator_handle
         WHERE mf.user_id = ?
         ORDER BY mf.followed_at DESC
        """,
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/me/follows/{handle}", status_code=status.HTTP_201_CREATED)
def follow_creator(
    handle: str,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Follow a creator.  Silently succeeds if already following."""
    user_id = current_user["fanvue_id"]
    handle = handle.lower().strip()

    creator = db.execute(
        "SELECT handle FROM creator_accounts WHERE handle = ? AND is_active = 1",
        (handle,),
    ).fetchone()
    if not creator:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")

    now = datetime.now(timezone.utc).isoformat()
    try:
        db.execute(
            "INSERT INTO member_follows (user_id, creator_handle, followed_at) VALUES (?, ?, ?)",
            (user_id, handle, now),
        )
        db.commit()
    except Exception:
        db.rollback()  # UNIQUE constraint hit → already following, that's fine
        return {"status": "following", "creator_handle": handle}

    # Fire stream-overlay alert (best-effort; failure must not affect the response).
    try:
        follower = db.execute(
            "SELECT COALESCE(display_name, username) AS display FROM site_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        _dispatch_alert(
            handle,
            "follow",
            {"username": follower["display"] if follower else "Someone"},
            db,
        )
    except Exception as _exc:
        logger.debug("Alert dispatch failed for follow: %s", _exc)

    return {"status": "following", "creator_handle": handle}


@router.delete("/me/follows/{handle}")
def unfollow_creator(
    handle: str,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Unfollow a creator."""
    user_id = current_user["fanvue_id"]
    handle = handle.lower().strip()

    db.execute(
        "DELETE FROM member_follows WHERE user_id = ? AND creator_handle = ?",
        (user_id, handle),
    )
    db.commit()
    return {"status": "unfollowed", "creator_handle": handle}


# ---------------------------------------------------------------------------
# Creator browser (public)
# ---------------------------------------------------------------------------


@router.get("/creators")
async def browse_creators(
    current_user: Optional[dict] = Depends(get_optional_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all active creator profiles with public information.

    When authenticated the response includes a ``is_following`` flag per creator.
    Each creator also carries an ``is_live`` flag derived from go2rtc (best-effort).
    """
    rows = db.execute(
        """
        SELECT handle, display_name, bio, avatar_url, accent_color, allow_free_content,
               content_rating, created_at
          FROM creator_accounts
         WHERE is_active = 1
         ORDER BY display_name ASC
        """,
    ).fetchall()

    user_id = current_user["fanvue_id"] if current_user else None

    follows: set = set()
    if user_id:
        follow_rows = db.execute(
            "SELECT creator_handle FROM member_follows WHERE user_id = ?", (user_id,)
        ).fetchall()
        follows = {r["creator_handle"] for r in follow_rows}

    result = []
    for r in rows:
        creator = dict(r)
        creator["is_following"] = creator["handle"] in follows
        creator["allow_free_content"] = bool(creator.get("allow_free_content"))
        creator["content_rating"] = creator.get("content_rating") or "unrated"
        creator["is_live"] = False
        result.append(creator)

    # Enrich with live status from go2rtc (best-effort, never blocks the response).
    try:
        cam_rows = db.execute(
            "SELECT stream_slug, rtmp_key FROM cameras"
        ).fetchall()
        if cam_rows:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"http://{_GO2RTC_HOST}:{_GO2RTC_PORT}/api/streams")
            if r.is_success:
                streams_data = r.json()
                live_handles: set = set()
                for cam in cam_rows:
                    go2rtc_name = cam["rtmp_key"] if cam["rtmp_key"] else cam["stream_slug"]
                    stream_info = streams_data.get(go2rtc_name) or {}
                    producers = stream_info.get("producers") or []
                    if any(p.get("url") for p in producers):
                        owner = db.execute(
                            """
                            SELECT ca.handle FROM cameras c
                              JOIN creator_accounts ca ON c.stream_slug LIKE ca.handle || '%'
                             WHERE c.stream_slug = ?
                             LIMIT 1
                            """,
                            (cam["stream_slug"],),
                        ).fetchone()
                        if owner:
                            live_handles.add(owner["handle"])
                for c in result:
                    c["is_live"] = c["handle"] in live_handles
    except Exception as exc:
        logger.debug("Could not fetch live status in browse_creators: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Content-filter preferences
# ---------------------------------------------------------------------------

class ContentFilterUpdate(BaseModel):
    content_filter: str = Field(..., pattern=r"^(all|sfw)$")


@router.get("/me/content-filter")
def get_content_filter(
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the authenticated member's content-filter preference."""
    user_id = user["fanvue_id"]
    row = db.execute(
        "SELECT content_filter FROM site_users WHERE id = ?", (user_id,)
    ).fetchone()
    return {"content_filter": (row["content_filter"] if row else None) or "all"}


@router.patch("/me/content-filter")
def update_content_filter(
    payload: ContentFilterUpdate,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update the authenticated member's content-filter preference ('all' or 'sfw')."""
    user_id = user["fanvue_id"]
    db.execute(
        "UPDATE site_users SET content_filter = ? WHERE id = ?",
        (payload.content_filter, user_id),
    )
    db.commit()
    return {"content_filter": payload.content_filter}
