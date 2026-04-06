"""
routers/drool.py – The Drool Log: Shame Gallery API.

Public endpoints (no authentication required):
  GET  /api/drool                    – paginated feed; 'Weekly Whimper' pinned first
  POST /api/drool/{id}/comment       – post an anonymous comment (rate-limited 5/min per IP)
  POST /api/drool/{id}/react         – one-tap reaction (rate-limited 20/hour per IP)
  POST /api/drool/ifttt/reddit       – IFTTT webhook receiver for Reddit (secured by ?secret=)
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal, Optional

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
    media_urls: list[str] = []
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


def _parse_media_urls(row: sqlite3.Row) -> list[str]:
    """Parse media_urls JSON from a drool_archive row, falling back to media_url."""
    media_url = row["media_url"]
    try:
        raw_media_urls = row["media_urls"]
    except (IndexError, KeyError):
        raw_media_urls = None
    if raw_media_urls:
        try:
            return [u for u in json.loads(raw_media_urls) if u]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return [media_url] if media_url else []


def _build_feed_items(
    rows: list[sqlite3.Row],
    whimper_id: Optional[int],
    db: sqlite3.Connection,
) -> list[DroolItem]:
    """Build DroolItem objects for a list of rows using batch DB queries.

    Uses two aggregate queries (one for comment counts, one for reaction counts)
    instead of N per-item queries, reducing DB round-trips from O(N) to O(1).
    """
    if not rows:
        return []

    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))

    # Batch: comment counts for all ids
    comment_rows = db.execute(
        f"SELECT drool_id, COUNT(*) AS cnt FROM drool_comments WHERE drool_id IN ({placeholders}) GROUP BY drool_id",  # noqa: S608
        ids,
    ).fetchall()
    comment_map: dict[int, int] = {r["drool_id"]: r["cnt"] for r in comment_rows}

    # Batch: reaction counts for all ids
    reaction_rows = db.execute(
        f"SELECT drool_id, reaction_type, COUNT(*) AS cnt FROM drool_reactions WHERE drool_id IN ({placeholders}) GROUP BY drool_id, reaction_type",  # noqa: S608
        ids,
    ).fetchall()
    reaction_map: dict[int, dict[str, int]] = {}
    for r in reaction_rows:
        reaction_map.setdefault(r["drool_id"], {})[r["reaction_type"]] = r["cnt"]

    # Batch: increment view counts for all ids in a single statement
    db.execute(
        f"UPDATE drool_archive SET view_count = view_count + 1 WHERE id IN ({placeholders})",  # noqa: S608
        ids,
    )

    return [
        DroolItem(
            id=row["id"],
            platform=row["platform"],
            original_url=row["original_url"],
            media_url=row["media_url"],
            media_urls=_parse_media_urls(row),
            text_content=row["text_content"],
            view_count=row["view_count"],
            timestamp=row["timestamp"],
            comment_count=comment_map.get(row["id"], 0),
            reaction_counts=reaction_map.get(row["id"], {}),
            is_weekly_whimper=(row["id"] == whimper_id),
        )
        for row in rows
    ]


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
    if page_size < 1 or page_size > 500:
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

    all_rows: list[sqlite3.Row] = []

    # Pin Weekly Whimper at the top on the first page.
    whimper_row = None
    if page == 1 and whimper_id is not None:
        whimper_row = db.execute(
            "SELECT * FROM drool_archive WHERE id = ?", (whimper_id,)
        ).fetchone()
        if whimper_row:
            all_rows.append(whimper_row)

    all_rows.extend(rows)

    feed = _build_feed_items(all_rows, whimper_id, db)
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


# ---------------------------------------------------------------------------
# IFTTT webhook receiver – Reddit
# ---------------------------------------------------------------------------


def _ifttt_secret() -> str:
    """Return the shared secret used to validate incoming IFTTT webhook requests."""
    from db import get_db_connection as _gdc  # local import avoids circular
    try:
        conn = _gdc()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'drool_reddit_ifttt_secret'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("REDDIT_IFTTT_SECRET", "")


@router.post("/ifttt/reddit", status_code=status.HTTP_201_CREATED)
async def ifttt_reddit_webhook(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Receive an IFTTT Webhooks payload for a Reddit upvote or save.

    Security: the caller must pass ``?secret=<shared_secret>`` in the URL.
    IFTTT lets you embed this in the webhook URL when you set up the applet.

    Expected JSON body (IFTTT Maker Webhooks format)::

        {
            "value1": "<reddit post URL>",
            "value2": "<post title>",
            "value3": "<media/thumbnail URL or empty>"
        }

    Map your IFTTT applet ingredients accordingly:
      - value1 → {{PostURL}}  (or {{Permalink}})
      - value2 → {{Title}}
      - value3 → {{ImageURL}}  (leave blank if not available)
    """
    secret = _ifttt_secret()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IFTTT mode is not configured (no secret set).",
        )

    incoming = request.query_params.get("secret", "")
    if not hmac.compare_digest(incoming, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )

    try:
        body: dict[str, Any] = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON.",
        )

    original_url: str = (body.get("value1") or "").strip()
    text_content: Optional[str] = (body.get("value2") or "").strip() or None
    media_url:    Optional[str] = (body.get("value3") or "").strip() or None

    if not original_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="value1 (post URL) is required.",
        )

    ts = datetime.now(timezone.utc).isoformat()

    existing = db.execute(
        "SELECT id FROM drool_archive WHERE original_url = ?", (original_url,)
    ).fetchone()
    if existing:
        return {"message": "Already archived.", "id": existing["id"]}

    media_urls_json: Optional[str] = json.dumps([media_url]) if media_url else None

    cursor = db.execute(
        """
        INSERT INTO drool_archive (platform, original_url, media_url, media_urls, text_content, timestamp)
        VALUES ('reddit', ?, ?, ?, ?, ?)
        """,
        (original_url, media_url, media_urls_json, text_content, ts),
    )
    db.commit()

    new_id = cursor.lastrowid
    logger.info("IFTTT Reddit webhook: archived item #%d – %s", new_id, original_url)

    await send_discord_notification(
        content=f"🐾 A new Reddit secret has been logged in the Drool Archive! {(text_content or original_url)[:200]}",
        is_embed=False,
    )

    return {"message": "Archived 🐾", "id": new_id}
