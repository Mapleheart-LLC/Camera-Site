"""
routers/moderation.py – NSFW image moderation using a local nudenet ONNX model.

Routers
-------
  router        – admin-only endpoints (prefix /api/admin/moderation)
  public_router – publicly accessible media proxy (no auth)

Admin endpoints
---------------
  POST /api/admin/moderation/check-url         – manually check a URL
  GET  /api/admin/moderation/flagged           – list auto-flagged drool items
  POST /api/admin/moderation/unflag/{drool_id} – clear flag and unhide an item

Public endpoint
---------------
  GET /api/media/pixelated/{drool_id}
      Fetches the original media_url for the drool item, applies a blocky
      pixelation effect with Pillow, and returns the result as JPEG.

      This is intentionally the only way the browser ever receives the image
      when creator-forced pixelation is active.  The original URL is kept
      server-side so any download always produces the pixelated version.

Helpers (importable by other routers)
--------------------------------------
  check_image_nsfw(url)  – async, returns float 0-1 or None on error
  is_nsfw(score)         – True when score meets threshold

How the NSFW score is computed
-------------------------------
nudenet detects body-part regions and assigns each a label + confidence.
Labels that indicate explicit content (exposed genitalia, breasts, buttocks,
anus) are treated as NSFW.  The returned score is the highest confidence
among those detections, or 0.0 if none are found.

Environment variables
---------------------
  NSFW_SCORE_THRESHOLD – float 0-1, default 0.75
  NSFW_PIXEL_SIZE      – integer pixel-block size for pixelation, default 16
"""

import io
import logging
import os
import sqlite3
from urllib.parse import urlparse
from typing import Optional

import httpx
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from db import get_db
from dependencies import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/moderation", tags=["moderation"])
public_router = APIRouter(tags=["media"])

_NSFW_THRESHOLD: float = float(os.environ.get("NSFW_SCORE_THRESHOLD", "0.75"))
_PIXEL_SIZE: int = max(4, int(os.environ.get("NSFW_PIXEL_SIZE", "16")))

# Labels from nudenet that indicate explicit content.
_NSFW_LABELS: frozenset[str] = frozenset({
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
})

# Rate limiter for the public pixelation proxy.
_limiter = Limiter(key_func=get_remote_address)

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
# SSRF guard
# ---------------------------------------------------------------------------

_PRIVATE_PREFIXES = ("10.", "172.", "192.168.", "127.", "0.", "169.254.")


def _is_safe_url(url: str) -> bool:
    """Return True only for http/https URLs pointing to non-private hosts."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "::1"):
        return False
    if any(host.startswith(p) for p in _PRIVATE_PREFIXES):
        return False
    return True


# ---------------------------------------------------------------------------
# Pillow pixelation
# ---------------------------------------------------------------------------


def _pixelate_image(img_bytes: bytes) -> bytes:
    """Downscale then upscale to produce a blocky pixelation effect.

    Returns JPEG bytes.  Any input format supported by Pillow is accepted.
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    small_w = max(1, img.width // _PIXEL_SIZE)
    small_h = max(1, img.height // _PIXEL_SIZE)
    pixelated = img.resize((small_w, small_h), Image.BOX).resize(
        img.size, Image.NEAREST
    )
    out = io.BytesIO()
    pixelated.save(out, format="JPEG", quality=85)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Core NSFW helper – importable by other routers
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
# Public pixelation proxy
# ---------------------------------------------------------------------------


@public_router.get("/api/media/pixelated/{drool_id}")
@_limiter.limit("120/minute")
async def serve_pixelated_media(
    request: Request,
    drool_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Fetch the original media for a drool item and return a pixelated JPEG.

    The original URL is read from the database — it is never taken from the
    request — which prevents SSRF.  The response is cached by the browser for
    24 hours so repeated views don't re-process the same image.
    """
    row = db.execute(
        "SELECT media_url FROM drool_archive WHERE id = ? AND media_url IS NOT NULL",
        (drool_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    original_url: str = row["media_url"]

    # Guard: never proxy our own proxy endpoint (avoid recursive loops).
    if "/api/media/pixelated/" in original_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid source URL."
        )

    if not _is_safe_url(original_url):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Source URL is not eligible for proxying.",
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                original_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CameraSiteBot/1.0)"},
                follow_redirects=True,
            )
        if not resp.is_success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream returned HTTP {resp.status_code}.",
            )
        img_bytes = resp.content
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("pixelation proxy: download failed for drool #%d: %s", drool_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not fetch source image.",
        )

    try:
        pixelated = _pixelate_image(img_bytes)
    except Exception as exc:
        logger.warning("pixelation proxy: processing failed for drool #%d: %s", drool_id, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not process image.",
        )

    return Response(
        content=pixelated,
        media_type="image/jpeg",
        headers={
            # 24-hour browser cache; immutable since the drool item won't change.
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
