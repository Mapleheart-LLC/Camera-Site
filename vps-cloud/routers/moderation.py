"""
routers/moderation.py – NSFW image moderation via DeepAI REST API.

Endpoints
---------
  POST /api/admin/moderation/check-url         – manually check a URL
  GET  /api/admin/moderation/flagged           – list auto-flagged drool items
  POST /api/admin/moderation/unflag/{drool_id} – clear flag and unhide an item

Helper
------
  check_image_nsfw(url)  – async, returns float 0-1 or None (no key / error)
  is_nsfw(score)         – True when score meets threshold

Environment variables
---------------------
  DEEPAI_API_KEY       – DeepAI API key (https://deepai.org). If empty, checks
                         are skipped and all images pass.
  NSFW_SCORE_THRESHOLD – float 0-1, default 0.75. Items at or above this score
                         are auto-hidden in the drool feed.
"""

import logging
import os
import sqlite3
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/moderation", tags=["moderation"])

_DEEPAI_KEY: str = os.environ.get("DEEPAI_API_KEY", "")
_NSFW_THRESHOLD: float = float(os.environ.get("NSFW_SCORE_THRESHOLD", "0.75"))

# ---------------------------------------------------------------------------
# Core helper – importable by other routers
# ---------------------------------------------------------------------------


async def check_image_nsfw(url: str) -> Optional[float]:
    """Return the NSFW score (0.0 – 1.0) for *url*, or ``None`` if the check
    could not be performed (no API key configured, network error, etc.).

    Uses the DeepAI NSFW Detector:
    https://deepai.org/machine-learning-model/nsfw-detector
    """
    if not _DEEPAI_KEY or not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.deepai.org/api/nsfw-detector",
                headers={"api-key": _DEEPAI_KEY},
                data={"image": url},
            )
        if not resp.is_success:
            logger.warning(
                "NSFW check HTTP %s for %.120s", resp.status_code, url
            )
            return None
        output = (resp.json().get("output") or {})
        score = output.get("nsfw_score")
        return float(score) if score is not None else None
    except Exception as exc:
        logger.warning("NSFW check failed for %.120s: %s", url, exc)
        return None


def is_nsfw(score: Optional[float]) -> bool:
    """Return True when *score* meets or exceeds the configured threshold."""
    return score is not None and score >= _NSFW_THRESHOLD


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


class CheckUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)


@router.post("/check-url")
async def admin_check_url(
    body: CheckUrlRequest,
    _admin: dict = Depends(get_admin_user),
):
    """Manually run the NSFW detector against any URL."""
    if not _DEEPAI_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DEEPAI_API_KEY is not configured on this server.",
        )
    score = await check_image_nsfw(body.url)
    if score is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="NSFW check failed — see server logs for details.",
        )
    return {
        "url": body.url,
        "nsfw_score": round(score, 4),
        "flagged": is_nsfw(score),
        "threshold": _NSFW_THRESHOLD,
    }


@router.get("/flagged")
def admin_list_flagged(
    _admin: dict = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return drool items that were auto-flagged by the NSFW detector."""
    rows = db.execute(
        """
        SELECT id, platform, original_url, media_url, text_content,
               nsfw_score, is_hidden, timestamp, creator_handle
          FROM drool_archive
         WHERE nsfw_score IS NOT NULL
         ORDER BY nsfw_score DESC, timestamp DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/unflag/{drool_id}", status_code=status.HTTP_200_OK)
def admin_unflag(
    drool_id: int,
    _admin: dict = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Clear the NSFW flag and unhide a drool item."""
    row = db.execute(
        "SELECT id FROM drool_archive WHERE id = ?", (drool_id,)
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found."
        )
    db.execute(
        "UPDATE drool_archive SET nsfw_score = NULL, is_hidden = 0 WHERE id = ?",
        (drool_id,),
    )
    db.commit()
    return {"message": "Unflagged and restored.", "id": drool_id}
