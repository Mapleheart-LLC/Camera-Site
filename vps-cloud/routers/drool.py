"""
routers/drool.py – The Drool Log: Shame Gallery API.

Public endpoints (no authentication required):
  GET  /api/drool                    – paginated feed; 'Weekly Whimper' pinned first
  POST /api/drool/{id}/comment       – post an anonymous comment (rate-limited 5/min per IP)
  POST /api/drool/{id}/react         – one-tap reaction (rate-limited 20/hour per IP)
"""

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from db import get_db
from discord_webhook import send_discord_notification

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter (shared key function: remote IP address)
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/api/drool", tags=["drool"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DROOL_SALT: str = os.environ.get("DROOL_SALT", "drool-default-salt-change-me")
if _DROOL_SALT == "drool-default-salt-change-me":
    logger.warning(
        "DROOL_SALT is not set. Pack Member identities are NOT cryptographically anonymous. "
        "Set a strong DROOL_SALT environment variable before deploying to production."
    )
_MAX_COMMENT_LENGTH = 500

ReactionType = Literal["Good Girl", "Bad Puppy", "Dumb Thing", "Pretty Toy"]


# ---------------------------------------------------------------------------
# Anonymous identity helper
# ---------------------------------------------------------------------------


def get_pack_identity(request: Request) -> str:
    """Hash the requester's IP with a secret salt to produce 'Pack Member #XXXX'."""
    ip = get_remote_address(request) or "unknown"
    digest = hashlib.sha256(f"{_DROOL_SALT}:{ip}".encode()).hexdigest()[:8].upper()
    return f"Pack Member #{digest}"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CommentSubmit(BaseModel):
    comment_text: str = Field(..., min_length=1, max_length=_MAX_COMMENT_LENGTH)


class ReactSubmit(BaseModel):
    reaction_type: ReactionType


class DroolItem(BaseModel):
    id: int
    platform: str
    original_url: str
    media_url: Optional[str]
    text_content: Optional[str]
    view_count: int
    timestamp: str
    comment_count: int
    reaction_counts: dict[str, int]
    is_weekly_whimper: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_item_or_404(item_id: int, db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM drool_archive WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Drool item not found.",
        )
    return row


def _reaction_counts(item_id: int, db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT reaction_type, COUNT(*) AS cnt
        FROM drool_reactions
        WHERE drool_id = ?
        GROUP BY reaction_type
        """,
        (item_id,),
    ).fetchall()
    return {r["reaction_type"]: r["cnt"] for r in rows}


def _comment_count(item_id: int, db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM drool_comments WHERE drool_id = ?",
        (item_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def _weekly_whimper_id(db: sqlite3.Connection) -> Optional[int]:
    """Return the id of the most-interacted item in the last 7 days."""
    row = db.execute(
        """
        SELECT da.id,
               (COALESCE(c.cnt, 0) + COALESCE(r.cnt, 0)) AS score
        FROM drool_archive da
        LEFT JOIN (
            SELECT drool_id, COUNT(*) AS cnt
            FROM drool_comments
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY drool_id
        ) c ON c.drool_id = da.id
        LEFT JOIN (
            SELECT drool_id, COUNT(*) AS cnt
            FROM drool_reactions
            GROUP BY drool_id
        ) r ON r.drool_id = da.id
        ORDER BY score DESC, da.id DESC
        LIMIT 1
        """
    ).fetchone()
    return row["id"] if row else None


def _build_item(row: sqlite3.Row, whimper_id: Optional[int], db: sqlite3.Connection) -> DroolItem:
    item_id = row["id"]
    db.execute(
        "UPDATE drool_archive SET view_count = view_count + 1 WHERE id = ?",
        (item_id,),
    )
    return DroolItem(
        id=item_id,
        platform=row["platform"],
        original_url=row["original_url"],
        media_url=row["media_url"],
        text_content=row["text_content"],
        view_count=row["view_count"],
        timestamp=row["timestamp"],
        comment_count=_comment_count(item_id, db),
        reaction_counts=_reaction_counts(item_id, db),
        is_weekly_whimper=(item_id == whimper_id),
    )


# ---------------------------------------------------------------------------
# Public API endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[DroolItem])
def get_drool_feed(
    page: int = 1,
    page_size: int = 20,
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the shame gallery feed, with the Weekly Whimper pinned first."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    whimper_id = _weekly_whimper_id(db)
    offset = (page - 1) * page_size

    # Fetch one extra to exclude the whimper from the regular list.
    rows = db.execute(
        """
        SELECT * FROM drool_archive
        WHERE id != COALESCE(?, -1)
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
        """,
        (whimper_id, page_size, offset),
    ).fetchall()

    feed: list[DroolItem] = []

    # Pin Weekly Whimper at the top on the first page.
    if page == 1 and whimper_id is not None:
        wrow = db.execute(
            "SELECT * FROM drool_archive WHERE id = ?", (whimper_id,)
        ).fetchone()
        if wrow:
            feed.append(_build_item(wrow, whimper_id, db))

    for row in rows:
        feed.append(_build_item(row, whimper_id, db))

    db.commit()
    return feed


@router.post("/{item_id}/comment", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def post_comment(
    item_id: int,
    payload: CommentSubmit,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Post an anonymous comment. Rate-limited to 5 per minute per IP."""
    _get_item_or_404(item_id, db)

    pack_id = get_pack_identity(request)
    created_at = datetime.now(timezone.utc).isoformat()

    db.execute(
        """
        INSERT INTO drool_comments (drool_id, comment_text, pack_member_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (item_id, payload.comment_text, pack_id, created_at),
    )
    db.commit()

    # Discord ping – fire and forget
    await send_discord_notification(
        content=(
            f"🐾 Someone is barking at our pretty girl! {pack_id} said: "
            f"'{payload.comment_text[:200]}'"
        ),
        is_embed=False,
    )

    return {"message": "Comment posted 🐾", "pack_member_id": pack_id}


@router.post("/{item_id}/react", status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
async def post_reaction(
    item_id: int,
    payload: ReactSubmit,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """One-tap reaction. Each pack member can react once per item (upsert). Rate-limited to 20/hour per IP."""
    _get_item_or_404(item_id, db)

    pack_id = get_pack_identity(request)

    try:
        db.execute(
            """
            INSERT INTO drool_reactions (drool_id, reaction_type, pack_member_id)
            VALUES (?, ?, ?)
            ON CONFLICT(drool_id, pack_member_id)
            DO UPDATE SET reaction_type = excluded.reaction_type
            """,
            (item_id, payload.reaction_type, pack_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not save reaction: {exc}",
        ) from exc

    return {"message": f"Reacted with '{payload.reaction_type}' 🐾", "pack_member_id": pack_id}
