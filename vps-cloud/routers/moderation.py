"""
routers/moderation.py – NSFW image moderation using a local nudenet ONNX model.

Endpoints
---------
  POST /api/admin/moderation/check-url         – manually check a URL
  GET  /api/admin/moderation/flagged           – list auto-flagged drool items
  POST /api/admin/moderation/unflag/{drool_id} – clear flag and unhide an item

Helper
------
  check_image_nsfw(url)  – async, returns float 0-1 or None (model error / no URL)
  is_nsfw(score)         – True when score meets threshold

How the score is computed
-------------------------
nudenet detects body-part regions and assigns each a label + confidence score.
Labels that are inherently explicit (exposed genitalia, exposed breasts, exposed
buttocks, exposed anus) are treated as NSFW.  The returned score is the highest
confidence value among those detections, or 0.0 if none are found.

Environment variables
---------------------
  NSFW_SCORE_THRESHOLD – float 0-1, default 0.75. Items at or above this score
                         are auto-hidden in the drool feed.
"""

import io
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

_NSFW_THRESHOLD: float = float(os.environ.get("NSFW_SCORE_THRESHOLD", "0.75"))

# Labels from nudenet that indicate explicit content.
_NSFW_LABELS: frozenset[str] = frozenset({
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
})

# ---------------------------------------------------------------------------
# Lazy-loaded detector singleton – ONNX model loads once, reused on every call
# ---------------------------------------------------------------------------

_detector = None  # NudeDetector instance, initialised on first use


def _get_detector():
    """Return the shared NudeDetector, initialising it on first call."""
    global _detector
    if _detector is None:
        from nudenet import NudeDetector  # type: ignore[import-untyped]
        _detector = NudeDetector()
        logger.info("nudenet NudeDetector initialised (ONNX model loaded).")
    return _detector


# ---------------------------------------------------------------------------
# Core helper – importable by other routers
# ---------------------------------------------------------------------------


async def check_image_nsfw(url: str) -> Optional[float]:
    """Return the NSFW score (0.0 – 1.0) for *url*, or ``None`` on error.

    Downloads the image into memory then runs the local nudenet ONNX detector.
    Never raises — returns ``None`` on any network or inference failure.
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if not resp.is_success:
            logger.warning("NSFW check: HTTP %s fetching %.120s", resp.status_code, url)
            return None
        image_bytes: bytes = resp.content
    except Exception as exc:
        logger.warning("NSFW check: download failed for %.120s: %s", url, exc)
        return None

    try:
        detector = _get_detector()
        detections = detector.detect(image_bytes)
        # Compute score: highest confidence among NSFW-labelled detections.
        nsfw_scores = [
            d["score"] for d in detections if d.get("class") in _NSFW_LABELS
        ]
        return max(nsfw_scores) if nsfw_scores else 0.0
    except Exception as exc:
        logger.warning("NSFW check: inference failed for %.120s: %s", url, exc)
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
    """Manually run the local NSFW detector against any image URL."""
    score = await check_image_nsfw(body.url)
    if score is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="NSFW check failed — could not download or process the image.",
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
