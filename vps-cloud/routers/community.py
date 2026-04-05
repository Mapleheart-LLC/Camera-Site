"""
routers/community.py – Community & Social features.

Phase 3: fan leaderboard, badges, community posts (creator blog).

Endpoints
---------
  GET  /api/leaderboard                       – top fans by creator/month (public)
  GET  /api/member/badges                     – authenticated user's badges
  GET  /api/creators/{handle}/badges/top      – top badge holders for a creator (public)

  GET  /api/posts                             – list community posts (public / gated)
  GET  /api/posts/{id}                        – single community post (public / gated)

  POST   /api/creator/posts                   – create a community post (creator)
  PATCH  /api/creator/posts/{id}              – update a community post (creator)
  DELETE /api/creator/posts/{id}              – delete a community post (creator)
  POST   /api/creator/posts/{id}/publish      – publish a post (creator)
  POST   /api/creator/posts/{id}/unpublish    – unpublish a post (creator)
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import markdown2
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_current_creator, get_current_user, get_optional_user

router = APIRouter(tags=["community"])

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Badge definitions
# ---------------------------------------------------------------------------

BADGE_DEFINITIONS = {
    "first_reaction":  "First Reaction",
    "first_comment":   "First Comment",
    "first_question":  "First Question",
    "10_reactions":    "10 Reactions",
    "50_reactions":    "50 Reactions",
    "10_comments":     "10 Comments",
    "100_comments":    "100 Comments",
    "top_fan":         "Top Fan",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _award_badge_if_new(db: sqlite3.Connection, user_id: str, badge_slug: str) -> None:
    """Award a badge to a user if they don't already have it."""
    if badge_slug not in BADGE_DEFINITIONS:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.execute(
            "INSERT INTO user_badges (user_id, badge_slug, awarded_at) VALUES (?, ?, ?)",
            (user_id, badge_slug, now),
        )
        db.commit()
    except Exception:
        pass  # UNIQUE constraint – already has badge


def _log_activity(
    db: sqlite3.Connection,
    user_id: str,
    creator_handle: str,
    action_type: str,
) -> None:
    """Increment the activity log for the current month and award badges if appropriate."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        db.execute(
            """
            INSERT INTO user_activity_log (user_id, creator_handle, action_type, month, count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id, creator_handle, action_type, month)
            DO UPDATE SET count = count + 1
            """,
            (user_id, creator_handle, action_type, month),
        )
        db.commit()
    except Exception as exc:
        logger.warning("Activity log error: %s", exc)

    # Check cumulative counts for badge awards.
    totals = db.execute(
        """
        SELECT SUM(count) AS total FROM user_activity_log
        WHERE user_id = ? AND action_type = ?
        """,
        (user_id, action_type),
    ).fetchone()
    total = (totals["total"] or 0) if totals else 0

    if action_type == "reaction":
        _award_badge_if_new(db, user_id, "first_reaction")
        if total >= 10:
            _award_badge_if_new(db, user_id, "10_reactions")
        if total >= 50:
            _award_badge_if_new(db, user_id, "50_reactions")
    elif action_type == "comment":
        _award_badge_if_new(db, user_id, "first_comment")
        if total >= 10:
            _award_badge_if_new(db, user_id, "10_comments")
        if total >= 100:
            _award_badge_if_new(db, user_id, "100_comments")
    elif action_type == "question":
        _award_badge_if_new(db, user_id, "first_question")


# ---------------------------------------------------------------------------
# Fan leaderboard
# ---------------------------------------------------------------------------

@router.get("/api/leaderboard")
def get_leaderboard(
    creator_handle: Optional[str] = None,
    month: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the top 10 fans by activity score for the given creator/month."""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    if creator_handle:
        rows = db.execute(
            """
            SELECT ual.user_id,
                   COALESCE(su.display_name, su.username, ual.user_id) AS display_name,
                   SUM(ual.count) AS total_activity
              FROM user_activity_log ual
              LEFT JOIN site_users su ON su.id = ual.user_id
             WHERE ual.creator_handle = ? AND ual.month = ?
             GROUP BY ual.user_id
             ORDER BY total_activity DESC
             LIMIT 10
            """,
            (creator_handle, month),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT ual.user_id,
                   COALESCE(su.display_name, su.username, ual.user_id) AS display_name,
                   SUM(ual.count) AS total_activity
              FROM user_activity_log ual
              LEFT JOIN site_users su ON su.id = ual.user_id
             WHERE ual.month = ?
             GROUP BY ual.user_id
             ORDER BY total_activity DESC
             LIMIT 10
            """,
            (month,),
        ).fetchall()
    return {"month": month, "leaderboard": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------

@router.get("/api/member/badges")
def get_my_badges(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the authenticated user's badges."""
    user_id = current_user["fanvue_id"]
    rows = db.execute(
        "SELECT badge_slug, awarded_at FROM user_badges WHERE user_id = ? ORDER BY awarded_at",
        (user_id,),
    ).fetchall()
    return [
        {
            "slug": r["badge_slug"],
            "label": BADGE_DEFINITIONS.get(r["badge_slug"], r["badge_slug"]),
            "awarded_at": r["awarded_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Community Posts (Creator Blog)
# ---------------------------------------------------------------------------

class PostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body_md: str = Field(..., min_length=1, max_length=50000)
    is_subscriber_only: bool = False


class PostUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    body_md: Optional[str] = Field(None, max_length=50000)
    is_subscriber_only: Optional[bool] = None


def _render_post(row: sqlite3.Row, include_body: bool = True) -> dict:
    d = dict(row)
    if include_body and d.get("body_md"):
        d["body_html"] = markdown2.markdown(
            d["body_md"],
            extras=["fenced-code-blocks", "tables", "strike", "footnotes"],
        )
    return d


@router.get("/api/posts")
def list_posts(
    creator_handle: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    current_user: Optional[dict] = Depends(get_optional_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List published community posts.  Subscriber-only posts are included only for subscribers."""
    is_subscriber = current_user is not None and current_user.get("access_level", 0) >= 2
    offset = (page - 1) * per_page

    if creator_handle:
        if is_subscriber:
            rows = db.execute(
                """
                SELECT id, creator_handle, title, is_subscriber_only, published_at,
                       created_at, view_count
                  FROM community_posts
                 WHERE creator_handle = ? AND is_published = 1
                 ORDER BY published_at DESC LIMIT ? OFFSET ?
                """,
                (creator_handle, per_page, offset),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT id, creator_handle, title, is_subscriber_only, published_at,
                       created_at, view_count
                  FROM community_posts
                 WHERE creator_handle = ? AND is_published = 1 AND is_subscriber_only = 0
                 ORDER BY published_at DESC LIMIT ? OFFSET ?
                """,
                (creator_handle, per_page, offset),
            ).fetchall()
    else:
        if is_subscriber:
            rows = db.execute(
                """
                SELECT id, creator_handle, title, is_subscriber_only, published_at,
                       created_at, view_count
                  FROM community_posts
                 WHERE is_published = 1
                 ORDER BY published_at DESC LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT id, creator_handle, title, is_subscriber_only, published_at,
                       created_at, view_count
                  FROM community_posts
                 WHERE is_published = 1 AND is_subscriber_only = 0
                 ORDER BY published_at DESC LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()

    return [dict(r) for r in rows]


@router.get("/api/posts/{post_id}")
def get_post(
    post_id: int,
    current_user: Optional[dict] = Depends(get_optional_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Get a single published community post with rendered HTML body."""
    row = db.execute(
        "SELECT * FROM community_posts WHERE id = ? AND is_published = 1", (post_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found.")

    is_subscriber = current_user is not None and current_user.get("access_level", 0) >= 2
    if row["is_subscriber_only"] and not is_subscriber:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="This post is for subscribers only.",
        )

    # Increment view count.
    db.execute(
        "UPDATE community_posts SET view_count = view_count + 1 WHERE id = ?", (post_id,)
    )
    db.commit()

    return _render_post(row)


# Creator CRUD

@router.post("/api/creator/posts", status_code=status.HTTP_201_CREATED)
def create_post(
    payload: PostCreate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new community post (draft by default)."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        INSERT INTO community_posts
            (creator_handle, title, body_md, is_subscriber_only, is_published, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (handle, payload.title, payload.body_md, int(payload.is_subscriber_only), now),
    )
    db.commit()
    return {"id": cursor.lastrowid, "detail": "Post created as draft."}


@router.patch("/api/creator/posts/{post_id}")
def update_post(
    post_id: int,
    payload: PostUpdate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update a community post owned by this creator."""
    row = db.execute(
        "SELECT id FROM community_posts WHERE id = ? AND creator_handle = ?",
        (post_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found.")

    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.body_md is not None:
        updates["body_md"] = payload.body_md
    if payload.is_subscriber_only is not None:
        updates["is_subscriber_only"] = int(payload.is_subscriber_only)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE community_posts SET {set_clause} WHERE id = ?",
            list(updates.values()) + [post_id],
        )
        db.commit()
    return {"detail": "Post updated."}


@router.delete("/api/creator/posts/{post_id}")
def delete_post(
    post_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a community post."""
    result = db.execute(
        "DELETE FROM community_posts WHERE id = ? AND creator_handle = ?",
        (post_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Post not found.")
    return {"detail": "Post deleted."}


@router.post("/api/creator/posts/{post_id}/publish")
def publish_post(
    post_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Publish a draft post."""
    now = datetime.now(timezone.utc).isoformat()
    result = db.execute(
        """
        UPDATE community_posts
           SET is_published = 1, published_at = ?
         WHERE id = ? AND creator_handle = ?
        """,
        (now, post_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Post not found.")
    return {"detail": "Post published."}


@router.post("/api/creator/posts/{post_id}/unpublish")
def unpublish_post(
    post_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Unpublish a post (returns it to draft)."""
    result = db.execute(
        "UPDATE community_posts SET is_published = 0 WHERE id = ? AND creator_handle = ?",
        (post_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Post not found.")
    return {"detail": "Post unpublished."}
