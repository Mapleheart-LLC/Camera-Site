"""
routers/notifications.py – In-app notifications, email prefs, Web Push, bookmarks.

Phase 2: Engagement & Retention.

Endpoints
---------
  GET    /api/notifications                  – list unread notifications (authenticated)
  POST   /api/notifications/read             – mark notifications read (authenticated)
  PATCH  /api/member/notification-prefs      – update email notification preferences
  POST   /api/notifications/push/subscribe   – save a Web Push subscription (authenticated)
  DELETE /api/notifications/push/unsubscribe – remove a Web Push subscription (authenticated)

  POST   /api/bookmarks                      – bookmark a piece of content (authenticated)
  DELETE /api/bookmarks                      – remove a bookmark (authenticated)
  GET    /api/bookmarks                      – list bookmarks (authenticated)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_current_user

router = APIRouter(tags=["notifications"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class MarkReadRequest(BaseModel):
    ids: Optional[list[int]] = Field(
        None,
        description="List of notification IDs to mark read.  Omit to mark all read.",
    )


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=10)
    p256dh: str = Field(..., min_length=10)
    auth: str = Field(..., min_length=4)


class BookmarkToggleRequest(BaseModel):
    content_type: str = Field(..., pattern=r"^(drool|question|post|product)$")
    content_id: str = Field(..., min_length=1, max_length=64)


class NotificationPrefsUpdate(BaseModel):
    email_on_answer: Optional[bool] = None
    email_on_drool: Optional[bool] = None
    email_on_merch: Optional[bool] = None
    email_on_post: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_prefs(db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        db.execute(
            "INSERT OR IGNORE INTO notification_prefs (user_id) VALUES (?)", (user_id,)
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row


def create_notification(
    db: sqlite3.Connection,
    user_id: str,
    notif_type: str,
    content_id: Optional[str] = None,
) -> None:
    """Insert a notification row.  Called from other routers as a write-path hook."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.execute(
            """
            INSERT INTO notifications (user_id, type, content_id, read, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (user_id, notif_type, content_id, now),
        )
        db.commit()
    except Exception as exc:
        logger.warning("Failed to create notification for %s: %s", user_id, exc)


def dispatch_web_push(
    db: sqlite3.Connection,
    user_id: str,
    title: str,
    body: str,
    url: str = "/",
) -> None:
    """Send a Web Push notification to all subscriptions for a user.

    Requires VAPID_PRIVATE_KEY and VAPID_CLAIMS_EMAIL env vars.
    Silently skips if pywebpush or VAPID keys are not configured.
    """
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "")
    vapid_email = os.environ.get("VAPID_CLAIMS_EMAIL", "")
    if not vapid_private or not vapid_email:
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed; skipping push notification")
        return

    rows = db.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    payload = json.dumps({"title": title, "body": body, "url": url})
    for row in rows:
        try:
            webpush(
                subscription_info={
                    "endpoint": row["endpoint"],
                    "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": f"mailto:{vapid_email}"},
            )
        except Exception as exc:
            logger.debug("Push to %s failed: %s", row["endpoint"][:40], exc)


# ---------------------------------------------------------------------------
# In-app notifications
# ---------------------------------------------------------------------------

@router.get("/api/notifications")
def list_notifications(
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return unread notifications for the authenticated user."""
    user_id = current_user["fanvue_id"]
    rows = db.execute(
        """
        SELECT id, type, content_id, read, created_at
          FROM notifications
         WHERE user_id = ?
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    unread_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = ? AND read = 0",
        (user_id,),
    ).fetchone()["cnt"]
    return {"unread_count": unread_count, "notifications": [dict(r) for r in rows]}


@router.post("/api/notifications/read")
def mark_notifications_read(
    payload: MarkReadRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Mark specific notification IDs (or all) as read."""
    user_id = current_user["fanvue_id"]
    if payload.ids:
        placeholders = ",".join("?" * len(payload.ids))
        db.execute(
            f"UPDATE notifications SET read = 1 WHERE user_id = ? AND id IN ({placeholders})",
            [user_id] + payload.ids,
        )
    else:
        db.execute(
            "UPDATE notifications SET read = 1 WHERE user_id = ?", (user_id,)
        )
    db.commit()
    return {"detail": "Notifications marked read."}


# ---------------------------------------------------------------------------
# Email notification preferences
# ---------------------------------------------------------------------------

@router.patch("/api/member/notification-prefs")
def update_notification_prefs(
    payload: NotificationPrefsUpdate,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update email notification preferences for the authenticated user."""
    user_id = current_user["fanvue_id"]
    _get_or_create_prefs(db, user_id)

    updates = {}
    if payload.email_on_answer is not None:
        updates["email_on_answer"] = int(payload.email_on_answer)
    if payload.email_on_drool is not None:
        updates["email_on_drool"] = int(payload.email_on_drool)
    if payload.email_on_merch is not None:
        updates["email_on_merch"] = int(payload.email_on_merch)
    if payload.email_on_post is not None:
        updates["email_on_post"] = int(payload.email_on_post)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE notification_prefs SET {set_clause} WHERE user_id = ?",
            list(updates.values()) + [user_id],
        )
        db.commit()

    prefs = db.execute(
        "SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(prefs)


@router.get("/api/member/notification-prefs")
def get_notification_prefs(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the current notification preferences for the authenticated user."""
    user_id = current_user["fanvue_id"]
    prefs = _get_or_create_prefs(db, user_id)
    return dict(prefs)


# ---------------------------------------------------------------------------
# Web Push subscriptions
# ---------------------------------------------------------------------------

@router.post("/api/notifications/push/subscribe", status_code=status.HTTP_201_CREATED)
def push_subscribe(
    payload: PushSubscribeRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Store a Web Push subscription for the current user."""
    user_id = current_user["fanvue_id"]
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, endpoint) DO UPDATE SET p256dh = excluded.p256dh,
            auth = excluded.auth
        """,
        (user_id, payload.endpoint, payload.p256dh, payload.auth, now),
    )
    db.commit()
    return {"detail": "Push subscription saved."}


@router.delete("/api/notifications/push/unsubscribe")
def push_unsubscribe(
    payload: PushSubscribeRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a Web Push subscription."""
    user_id = current_user["fanvue_id"]
    db.execute(
        "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
        (user_id, payload.endpoint),
    )
    db.commit()
    return {"detail": "Push subscription removed."}


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

@router.post("/api/bookmarks", status_code=status.HTTP_201_CREATED)
def add_bookmark(
    payload: BookmarkToggleRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Bookmark a piece of content."""
    user_id = current_user["fanvue_id"]
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.execute(
            """
            INSERT INTO bookmarks (user_id, content_type, content_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, payload.content_type, payload.content_id, now),
        )
        db.commit()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already bookmarked.",
        )
    return {"detail": "Bookmarked."}


@router.delete("/api/bookmarks")
def remove_bookmark(
    payload: BookmarkToggleRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a bookmark."""
    user_id = current_user["fanvue_id"]
    result = db.execute(
        "DELETE FROM bookmarks WHERE user_id = ? AND content_type = ? AND content_id = ?",
        (user_id, payload.content_type, payload.content_id),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Bookmark not found.")
    return {"detail": "Bookmark removed."}


@router.get("/api/bookmarks")
def list_bookmarks(
    content_type: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List bookmarks for the authenticated user, optionally filtered by content_type."""
    user_id = current_user["fanvue_id"]
    if content_type:
        rows = db.execute(
            "SELECT * FROM bookmarks WHERE user_id = ? AND content_type = ? ORDER BY created_at DESC",
            (user_id, content_type),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]
