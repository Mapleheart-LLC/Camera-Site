"""
routers/discovery.py – Discovery, SEO & Search.

Phase 6: tags/categories, global FTS search, /explore page data.
Phase 8: SFW/NSFW content-rating support on explore + search.

Endpoints
---------
  GET  /api/tags                             – list all tags (public)
  POST /api/admin/tags                       – create a tag (admin)
  POST /api/admin/content-tags               – tag a piece of content (admin)
  DELETE /api/admin/content-tags             – untag content (admin)
  POST /api/creator/content-tags             – tag own content (creator)

  GET  /api/search                           – full-text search across all content types
  GET  /api/explore                          – public explore feed (featured, trending, newest)
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone


from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_admin_user, get_current_creator

router = APIRouter(tags=["discovery"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TagCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    label: str = Field(..., min_length=1, max_length=100)
    is_mature: bool = False


class ContentTagRequest(BaseModel):
    content_type: str = Field(..., pattern=r"^(drool|question|post|product)$")
    content_id: str = Field(..., min_length=1, max_length=64)
    tag_id: int


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@router.get("/api/tags")
def list_tags(db: sqlite3.Connection = Depends(get_db)):
    """Return all available tags."""
    rows = db.execute("SELECT id, slug, label, is_mature FROM tags ORDER BY label").fetchall()
    return [dict(r) for r in rows]


@router.post("/api/admin/tags", status_code=status.HTTP_201_CREATED)
def create_tag(
    payload: TagCreate,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new tag."""
    existing = db.execute("SELECT id FROM tags WHERE slug = ?", (payload.slug,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Tag with this slug already exists.")
    cursor = db.execute(
        "INSERT INTO tags (slug, label, is_mature) VALUES (?, ?, ?)",
        (payload.slug, payload.label, 1 if payload.is_mature else 0),
    )
    db.commit()
    return {"id": cursor.lastrowid, "slug": payload.slug, "label": payload.label, "is_mature": payload.is_mature}


@router.post("/api/admin/content-tags", status_code=status.HTTP_201_CREATED)
def admin_tag_content(
    payload: ContentTagRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Tag a piece of content (admin)."""
    try:
        db.execute(
            "INSERT INTO content_tags (content_type, content_id, tag_id) VALUES (?,?,?)",
            (payload.content_type, payload.content_id, payload.tag_id),
        )
        db.commit()
    except Exception:
        raise HTTPException(status_code=409, detail="Content already has this tag.")
    return {"detail": "Tag applied."}


@router.delete("/api/admin/content-tags")
def admin_untag_content(
    payload: ContentTagRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a tag from a piece of content (admin)."""
    result = db.execute(
        "DELETE FROM content_tags WHERE content_type = ? AND content_id = ? AND tag_id = ?",
        (payload.content_type, payload.content_id, payload.tag_id),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tag assignment not found.")
    return {"detail": "Tag removed."}


@router.post("/api/creator/content-tags", status_code=status.HTTP_201_CREATED)
def creator_tag_content(
    payload: ContentTagRequest,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Tag the creator's own content."""
    # Verify ownership for supported types.
    if payload.content_type == "drool":
        row = db.execute(
            "SELECT id FROM drool_archive WHERE id = ? AND creator_handle = ?",
            (payload.content_id, handle),
        ).fetchone()
    elif payload.content_type == "post":
        row = db.execute(
            "SELECT id FROM community_posts WHERE id = ? AND creator_handle = ?",
            (payload.content_id, handle),
        ).fetchone()
    else:
        row = True  # questions etc. – let admin handle
    if not row:
        raise HTTPException(status_code=403, detail="Content not found or not owned by you.")

    try:
        db.execute(
            "INSERT INTO content_tags (content_type, content_id, tag_id) VALUES (?,?,?)",
            (payload.content_type, payload.content_id, payload.tag_id),
        )
        db.commit()
    except Exception:
        raise HTTPException(status_code=409, detail="Content already has this tag.")
    return {"detail": "Tag applied."}


# ---------------------------------------------------------------------------
# Global Search (FTS5)
# ---------------------------------------------------------------------------

_VALID_TYPES = frozenset({"all", "drool", "questions", "creators", "products", "posts"})
_VALID_FILTERS = frozenset({"all", "sfw"})


@router.get("/api/search")
def global_search(
    q: str = Query(..., min_length=1, max_length=200),
    type: str = Query("all"),
    filter: str = Query("all", description="Content-rating filter: 'all' or 'sfw'"),
    limit: int = Query(20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
):
    """Full-text search across drool, Q&A, creators, products, and posts.

    Pass ``filter=sfw`` to restrict creator results to SFW-rated profiles only.
    """
    if type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {_VALID_TYPES}")
    if filter not in _VALID_FILTERS:
        raise HTTPException(status_code=400, detail=f"filter must be one of {_VALID_FILTERS}")

    results: dict = {}
    safe_q = q.replace('"', '""')  # escape double-quotes for FTS5 query
    sfw_only = filter == "sfw"

    if type in ("all", "drool"):
        try:
            rows = db.execute(
                """
                SELECT da.id, da.text_content AS title, da.creator_handle, da.timestamp,
                       da.view_count, 'drool' AS result_type
                  FROM fts_drool fd
                  JOIN drool_archive da ON da.id = fd.rowid
                 WHERE fts_drool MATCH ?
                 LIMIT ?
                """,
                (safe_q, limit),
            ).fetchall()
            results["drool"] = [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("FTS drool error: %s", exc)
            results["drool"] = []

    if type in ("all", "questions"):
        try:
            rows = db.execute(
                """
                SELECT q.id, q.text AS title, q.creator_handle, q.created_at,
                       'question' AS result_type
                  FROM fts_questions fq
                  JOIN questions q ON q.id = fq.rowid
                 WHERE fts_questions MATCH ? AND q.is_public = 1
                 LIMIT ?
                """,
                (safe_q, limit),
            ).fetchall()
            results["questions"] = [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("FTS questions error: %s", exc)
            results["questions"] = []

    if type in ("all", "creators"):
        try:
            if sfw_only:
                rows = db.execute(
                    """
                    SELECT ca.id, ca.handle, ca.display_name, ca.bio, ca.avatar_url,
                           ca.content_rating, 'creator' AS result_type
                      FROM fts_creators fc
                      JOIN creator_accounts ca ON ca.id = fc.rowid
                     WHERE fts_creators MATCH ? AND ca.is_active = 1
                           AND ca.content_rating = 'sfw'
                     LIMIT ?
                    """,
                    (safe_q, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT ca.id, ca.handle, ca.display_name, ca.bio, ca.avatar_url,
                           ca.content_rating, 'creator' AS result_type
                      FROM fts_creators fc
                      JOIN creator_accounts ca ON ca.id = fc.rowid
                     WHERE fts_creators MATCH ? AND ca.is_active = 1
                     LIMIT ?
                    """,
                    (safe_q, limit),
                ).fetchall()
            results["creators"] = [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("FTS creators error: %s", exc)
            results["creators"] = []

    if type in ("all", "products"):
        try:
            rows = db.execute(
                """
                SELECT p.id, p.name AS title, p.description, p.price, p.creator_handle,
                       'product' AS result_type
                  FROM fts_products fp
                  JOIN products p ON p.id = fp.rowid
                 WHERE fts_products MATCH ?
                 LIMIT ?
                """,
                (safe_q, limit),
            ).fetchall()
            results["products"] = [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("FTS products error: %s", exc)
            results["products"] = []

    if type in ("all", "posts"):
        try:
            rows = db.execute(
                """
                SELECT id, title, creator_handle, published_at, view_count,
                       'post' AS result_type
                  FROM community_posts
                 WHERE (title LIKE ? OR body_md LIKE ?) AND is_published = 1
                 LIMIT ?
                """,
                (f"%{q}%", f"%{q}%", limit),
            ).fetchall()
            results["posts"] = [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("posts search error: %s", exc)
            results["posts"] = []

    return {"query": q, "results": results}


# ---------------------------------------------------------------------------
# Explore page data
# ---------------------------------------------------------------------------

@router.get("/api/explore")
def explore_feed(
    filter: str = Query("all", description="Content-rating filter: 'all', 'sfw', or 'nsfw'"),
    db: sqlite3.Connection = Depends(get_db),
):
    """Public explore feed: featured creators, trending drool, newest posts, top products.

    Use ``filter=sfw`` to show only SFW-rated creators, or ``filter=nsfw`` for adult creators only.
    """
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    valid_explore_filters = {"all", "sfw", "nsfw"}
    if filter not in valid_explore_filters:
        raise HTTPException(status_code=400, detail=f"filter must be one of {valid_explore_filters}")

    # Featured creators (most recent active ones, up to 8).
    if filter == "sfw":
        featured_creators = db.execute(
            """
            SELECT handle, display_name, bio, avatar_url, accent_color, content_rating
              FROM creator_accounts
             WHERE is_active = 1 AND content_rating = 'sfw'
             ORDER BY created_at DESC
             LIMIT 8
            """
        ).fetchall()
    elif filter == "nsfw":
        featured_creators = db.execute(
            """
            SELECT handle, display_name, bio, avatar_url, accent_color, content_rating
              FROM creator_accounts
             WHERE is_active = 1 AND content_rating IN ('nsfw', 'mixed')
             ORDER BY created_at DESC
             LIMIT 8
            """
        ).fetchall()
    else:
        featured_creators = db.execute(
            """
            SELECT handle, display_name, bio, avatar_url, accent_color, content_rating
              FROM creator_accounts
             WHERE is_active = 1
             ORDER BY created_at DESC
             LIMIT 8
            """
        ).fetchall()

    # Trending drool (top engagement last 7 days).
    trending_drool = db.execute(
        """
        SELECT d.id, d.text_content AS caption, d.media_url, d.view_count,
               d.creator_handle, d.timestamp,
               COUNT(DISTINCT r.id)*2 + COUNT(DISTINCT c.id)*3 + d.view_count AS score
          FROM drool_archive d
          LEFT JOIN drool_reactions r ON r.drool_id = d.id
          LEFT JOIN drool_comments c ON c.drool_id = d.id
         WHERE d.timestamp >= ?
         GROUP BY d.id
         ORDER BY score DESC
         LIMIT 12
        """,
        (seven_days_ago,),
    ).fetchall()

    # Newest community posts (published, non-subscriber-only).
    newest_posts = db.execute(
        """
        SELECT id, creator_handle, title, published_at, view_count
          FROM community_posts
         WHERE is_published = 1 AND is_subscriber_only = 0
         ORDER BY published_at DESC
         LIMIT 8
        """
    ).fetchall()

    # Top products by name (simple listing).
    top_products = db.execute(
        """
        SELECT id, name, price, image_url, creator_handle
          FROM products
         ORDER BY id DESC
         LIMIT 8
        """
    ).fetchall()

    return {
        "featured_creators": [dict(r) for r in featured_creators],
        "trending_drool": [dict(r) for r in trending_drool],
        "newest_posts": [dict(r) for r in newest_posts],
        "top_products": [dict(r) for r in top_products],
    }
