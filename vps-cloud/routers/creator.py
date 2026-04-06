"""
routers/creator.py – Creator self-serve API endpoints.

Provides login for creator accounts and a full set of self-serve endpoints
so that each creator can manage their own profile, Q&A, and Drool Log
without touching any other creator's data or owner-only infrastructure.

All endpoints under ``/api/creator/`` (except login) are protected by
``get_current_creator`` which validates a JWT with ``role: creator``.

Endpoints
---------
  POST   /api/creator/login                     – authenticate; returns creator JWT
  GET    /api/creator/me                        – own profile (includes forwarding_email)
  PATCH  /api/creator/me                        – update bio / avatar / accent colour / forwarding_email
  POST   /api/creator/email/send                – send email FROM handle@domain via SMTP
  GET    /api/creator/stream-info               – stream keys, RTMP server URL, live status per camera
  GET    /api/creator/subscribers/search        – search users to gift (by email/username)
  GET    /api/creator/subscribers/gifted        – list all gifts given by this creator
  POST   /api/creator/subscribers/gift          – gift a subscription to a user
  DELETE /api/creator/subscribers/gifted/{id}   – revoke a gifted subscription
  GET    /api/creator/questions                 – unanswered questions (own only)
  GET    /api/creator/questions/answered        – answered questions (own only)
  POST   /api/creator/questions/{id}/answer     – answer a question
  DELETE /api/creator/questions/{id}            – delete a question
  GET    /api/creator/drool                     – Drool Log entries (own only)
  DELETE /api/creator/drool/{id}                – remove a Drool Log entry
  GET    /api/creator/stats                     – subscriber count, recent tips, Q count

Public (no auth required)
--------------------------
  GET    /api/creators/{handle}                 – public creator profile (includes public_email)
"""

import logging
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from db import get_db
from dependencies import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    create_access_token,
    get_current_creator,
)
from routers.auth import _hash_password, _verify_password  # reuse stdlib hashing
from routers.moderation import check_image_nsfw, is_nsfw
from stream_utils import is_producer_live as _is_producer_live

# Handle of the creator account linked to the admin user.  When set, the
# admin's HTTP Basic Auth credentials are accepted by POST /api/creator/login
# so the admin can access the creator panel without a separate password.
_ADMIN_CREATOR_HANDLE: str = os.environ.get("ADMIN_CREATOR_HANDLE", "").lower().strip()

router = APIRouter(prefix="/api/creator", tags=["creator"])

logger = logging.getLogger(__name__)

_creator_limiter = Limiter(key_func=get_remote_address)


def _alerts_dispatch(creator_handle: str, event_type: str, data: dict, db) -> None:
    """Fire a stream-overlay alert (lazy import to avoid circular deps)."""
    try:
        from routers.alerts import dispatch_alert
        dispatch_alert(creator_handle, event_type, data, db)
    except Exception as _exc:
        logger.debug("Alert dispatch failed (%s/%s): %s", event_type, creator_handle, _exc)

# ---------------------------------------------------------------------------
# Creator JWT lifetime (longer than subscriber tokens — 24 h default)
# ---------------------------------------------------------------------------
_CREATOR_TOKEN_MINUTES = 60 * 24  # 24 hours


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreatorLoginRequest(BaseModel):
    handle: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ProfilePatch(BaseModel):
    display_name: Optional[str] = Field(None, max_length=64)
    bio: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = Field(None, max_length=512)
    accent_color: Optional[str] = Field(None, max_length=16, pattern=r"^#[0-9a-fA-F]{3,8}$")
    forwarding_email: Optional[str] = Field(None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    content_rating: Optional[str] = Field(None, pattern=r"^(sfw|mixed|nsfw|unrated)$")
    pixelate_media: Optional[bool] = None


class AnswerPayload(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


class SendEmailPayload(BaseModel):
    to: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=10000)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post("/login")
@_creator_limiter.limit("10/15minutes")
def creator_login(
    request: Request,
    payload: CreatorLoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate a creator and return a short-lived JWT.

    The JWT carries ``role: creator`` so it cannot be used as a subscriber
    or admin token.
    """
    handle = payload.handle.lower().strip()

    row = db.execute(
        """
        SELECT id, handle, display_name, hashed_password, is_active,
               COALESCE(totp_enabled, 0) AS totp_enabled
          FROM creator_accounts
         WHERE handle = ?
        """,
        (handle,),
    ).fetchone()

    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid handle or password.",
    )

    if not row:
        # ── Admin-credential fallback ───────────────────────────────────────
        # If the submitted handle matches the admin username and the password
        # matches the admin password, issue a creator JWT for the linked
        # creator account (ADMIN_CREATOR_HANDLE) without requiring a separate
        # creator password.  This lets the site owner log into the creator
        # panel using the same credentials they use for the admin panel.
        if (
            _ADMIN_CREATOR_HANDLE
            and ADMIN_USERNAME
            and ADMIN_PASSWORD
            and secrets.compare_digest(handle.encode(), ADMIN_USERNAME.lower().encode())
            and secrets.compare_digest(payload.password.encode(), ADMIN_PASSWORD.encode())
        ):
            linked = db.execute(
                """
                SELECT id, handle, display_name,
                       COALESCE(totp_enabled, 0) AS totp_enabled
                  FROM creator_accounts
                 WHERE handle = ? AND is_active = 1
                """,
                (_ADMIN_CREATOR_HANDLE,),
            ).fetchone()
            if linked:
                totp_enabled = linked["totp_enabled"]
                if totp_enabled:
                    pending_token = create_access_token(
                        {"sub": linked["handle"], "role": "creator_2fa_pending"},
                        expires_delta=timedelta(minutes=5),
                    )
                    return {
                        "requires_2fa": True,
                        "pending_token": pending_token,
                        "handle": linked["handle"],
                    }
                token = create_access_token(
                    {"sub": linked["handle"], "role": "creator"},
                    expires_delta=timedelta(minutes=_CREATOR_TOKEN_MINUTES),
                )
                logger.info(
                    "Admin '%s' logged into creator panel via admin credentials (linked handle: '%s').",
                    ADMIN_USERNAME,
                    linked["handle"],
                )
                return {
                    "access_token": token,
                    "token_type": "bearer",
                    "handle": linked["handle"],
                    "display_name": linked["display_name"],
                }

        _hash_password("__timing_guard__")  # prevent timing-based enumeration
        raise _invalid

    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This creator account is deactivated. Contact the site owner.",
        )

    if not _verify_password(payload.password, row["hashed_password"]):
        raise _invalid

    # ── 2FA check ──────────────────────────────────────────────────────────
    totp_enabled = row["totp_enabled"] if "totp_enabled" in row.keys() else 0
    if totp_enabled:
        # Issue a short-lived (5-minute) "2FA pending" token that only carries
        # role:creator_2fa_pending — not role:creator — so it cannot access
        # any protected endpoints.
        pending_token = create_access_token(
            {"sub": row["handle"], "role": "creator_2fa_pending"},
            expires_delta=timedelta(minutes=5),
        )
        return {
            "requires_2fa": True,
            "pending_token": pending_token,
            "handle": row["handle"],
        }

    token = create_access_token(
        {"sub": row["handle"], "role": "creator"},
        expires_delta=timedelta(minutes=_CREATOR_TOKEN_MINUTES),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "handle": row["handle"],
        "display_name": row["display_name"],
    }


# ---------------------------------------------------------------------------
# 2FA second-step confirmation (exchanges pending token for full JWT)
# ---------------------------------------------------------------------------

class TwoFAConfirmRequest(BaseModel):
    pending_token: str = Field(..., min_length=10)
    otp: str = Field(..., min_length=6, max_length=8)


@router.post("/2fa/confirm")
@_creator_limiter.limit("5/15minutes")
def creator_2fa_confirm(
    request: Request,
    payload: TwoFAConfirmRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Exchange a 2FA-pending token + OTP for a full creator JWT."""
    import jwt
    from dependencies import SECRET_KEY, ALGORITHM

    try:
        decoded = jwt.decode(payload.pending_token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired pending token.",
        )

    if decoded.get("role") != "creator_2fa_pending":
        raise HTTPException(status_code=400, detail="Not a 2FA pending token.")

    handle = decoded.get("sub")
    row = db.execute(
        "SELECT handle, display_name, totp_secret, totp_enabled FROM creator_accounts WHERE handle = ?",
        (handle,),
    ).fetchone()
    if not row or not row["totp_enabled"]:
        raise HTTPException(status_code=400, detail="2FA is not enabled for this account.")

    import pyotp
    totp = pyotp.TOTP(row["totp_secret"])
    if not totp.verify(payload.otp, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid OTP code.")

    token = create_access_token(
        {"sub": handle, "role": "creator"},
        expires_delta=timedelta(minutes=_CREATOR_TOKEN_MINUTES),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "handle": handle,
        "display_name": row["display_name"],
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/me")
def get_my_profile(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the creator's public + editable profile fields."""
    row = db.execute(
        """
        SELECT handle, display_name, bio, avatar_url, accent_color, forwarding_email,
               content_rating, require_age_gate, pixelate_media
          FROM creator_accounts
         WHERE handle = ?
        """,
        (handle,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")
    result = dict(row)
    result["content_rating"] = result.get("content_rating") or "unrated"
    result["require_age_gate"] = bool(result.get("require_age_gate", 1))
    result["pixelate_media"] = bool(result.get("pixelate_media", 0))
    return result


@router.patch("/me")
async def patch_my_profile(
    payload: ProfilePatch,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update the creator's editable profile fields (bio, avatar URL, accent colour, forwarding email)."""
    row = db.execute(
        "SELECT handle, display_name, bio, avatar_url, accent_color, forwarding_email, content_rating, require_age_gate, pixelate_media FROM creator_accounts WHERE handle = ?",
        (handle,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")

    new_display_name    = payload.display_name    if payload.display_name    is not None else row["display_name"]
    new_bio             = payload.bio             if payload.bio             is not None else row["bio"]
    new_avatar_url      = payload.avatar_url      if payload.avatar_url      is not None else row["avatar_url"]
    new_accent_color    = payload.accent_color    if payload.accent_color    is not None else row["accent_color"]
    new_forwarding_email = payload.forwarding_email if payload.forwarding_email is not None else row["forwarding_email"]
    new_content_rating  = payload.content_rating  if payload.content_rating  is not None else (row["content_rating"] or "unrated")
    new_pixelate_media  = payload.pixelate_media  if payload.pixelate_media  is not None else bool(row["pixelate_media"])
    forwarding_email_changed = (
        payload.forwarding_email is not None
        and payload.forwarding_email != row["forwarding_email"]
    )

    # Auto-manage age gate: SFW creators don't need one; NSFW/mixed always do.
    if new_content_rating == "sfw":
        new_require_age_gate = 0
    elif new_content_rating in ("nsfw", "mixed"):
        new_require_age_gate = 1
    else:
        new_require_age_gate = row["require_age_gate"]

    db.execute(
        """
        UPDATE creator_accounts
           SET display_name = ?, bio = ?, avatar_url = ?, accent_color = ?,
               forwarding_email = ?, content_rating = ?, require_age_gate = ?,
               pixelate_media = ?
         WHERE handle = ?
        """,
        (new_display_name, new_bio, new_avatar_url, new_accent_color,
         new_forwarding_email, new_content_rating, new_require_age_gate,
         1 if new_pixelate_media else 0, handle),
    )
    db.commit()

    # Re-provision Cloudflare email routing when the forwarding address changed.
    if forwarding_email_changed:
        base_url = os.environ.get("BASE_URL", "").rstrip("/")
        if base_url:
            from routers.cloudflare import deprovision_creator_subdomain, provision_creator_subdomain
            root_domain = urlparse(base_url).hostname or ""
            if root_domain:
                # Fetch current agent_email (admin-controlled) to preserve it.
                agent_row = db.execute(
                    "SELECT agent_email FROM creator_accounts WHERE handle = ?", (handle,)
                ).fetchone()
                agent_email = agent_row["agent_email"] if agent_row else None
                deprovision_creator_subdomain(handle, root_domain)
                provision_creator_subdomain(
                    handle, root_domain,
                    forwarding_email=new_forwarding_email,
                    agent_email=agent_email,
                )

    # Run NSFW check on the avatar URL when it was changed (best-effort).
    avatar_nsfw_warning: Optional[str] = None
    if payload.avatar_url and payload.avatar_url != row["avatar_url"]:
        score = await check_image_nsfw(new_avatar_url)
        if is_nsfw(score):
            avatar_nsfw_warning = (
                f"⚠️ Your avatar image was flagged by the NSFW detector "
                f"(score {score:.0%}). Please review platform guidelines."
            )
            logger.warning(
                "Creator %s uploaded avatar with NSFW score %.2f: %.120s",
                handle, score, new_avatar_url,
            )

    result = {
        "handle": handle,
        "display_name": new_display_name,
        "bio": new_bio,
        "avatar_url": new_avatar_url,
        "accent_color": new_accent_color,
        "forwarding_email": new_forwarding_email,
        "content_rating": new_content_rating,
        "require_age_gate": bool(new_require_age_gate),
        "pixelate_media": new_pixelate_media,
    }
    if avatar_nsfw_warning:
        result["avatar_nsfw_warning"] = avatar_nsfw_warning
    return result
def _smtp_send(from_addr: str, to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email via SMTP using environment credentials.

    Required env vars: SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD.
    Optional: SMTP_PORT (default 587).
    Raises ``RuntimeError`` when SMTP is not configured or delivery fails.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not host or not username or not password:
        raise RuntimeError("SMTP is not configured (SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD).")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)


@router.post("/email/send", status_code=status.HTTP_200_OK)
def creator_send_email(
    payload: SendEmailPayload,
    handle: str = Depends(get_current_creator),
):
    """Send an email FROM ``handle@domain`` to any address.

    The FROM address is derived automatically from the creator's handle and
    the site's root domain (``BASE_URL``).  Requires the ``SMTP_HOST``,
    ``SMTP_USERNAME``, and ``SMTP_PASSWORD`` environment variables to be set.
    """
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    root_domain = urlparse(base_url).hostname if base_url else ""
    if not root_domain:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BASE_URL is not configured; cannot derive sender address.",
        )

    from_addr = f"{handle}@{root_domain}"
    try:
        _smtp_send(from_addr, payload.to, payload.subject, payload.body)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error("SMTP send failed for creator @%s: %s", handle, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Email delivery failed. Check SMTP configuration.",
        ) from exc

    logger.info("Creator @%s sent email from %s to %s.", handle, from_addr, payload.to)
    return {"from": from_addr, "to": payload.to, "subject": payload.subject}


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------


@router.get("/questions")
def creator_list_unanswered(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all unanswered questions for this creator."""
    rows = db.execute(
        """
        SELECT id, text, created_at
          FROM questions
         WHERE answer IS NULL AND creator_handle = ?
         ORDER BY created_at ASC
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/questions/answered")
def creator_list_answered(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all answered questions for this creator."""
    rows = db.execute(
        """
        SELECT id, text, answer, is_public, created_at
          FROM questions
         WHERE answer IS NOT NULL AND creator_handle = ?
         ORDER BY created_at DESC
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/questions/{question_id}/answer", status_code=status.HTTP_200_OK)
def creator_answer_question(
    question_id: str,
    payload: AnswerPayload,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Save an answer to a question belonging to this creator."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ? AND creator_handle = ?",
        (question_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute(
        "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ?",
        (payload.answer, question_id),
    )
    db.commit()
    return {"id": question_id, "message": "Answer saved 🐾"}


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def creator_delete_question(
    question_id: str,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Permanently delete a question belonging to this creator."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ? AND creator_handle = ?",
        (question_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Drool Log
# ---------------------------------------------------------------------------


@router.get("/drool")
def creator_list_drool(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return Drool Log entries belonging to this creator, newest first."""
    rows = db.execute(
        """
        SELECT id, platform, original_url AS url, media_url,
               text_content AS title, view_count,
               timestamp AS created_at
          FROM drool_archive
         WHERE creator_handle = ?
         ORDER BY id DESC
         LIMIT 200
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.delete("/drool/{entry_id}", status_code=status.HTTP_200_OK)
def creator_delete_drool(
    entry_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a Drool Log entry belonging to this creator."""
    row = db.execute(
        "SELECT id FROM drool_archive WHERE id = ? AND creator_handle = ?",
        (entry_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entry not found.",
        )
    db.execute("DELETE FROM drool_reactions WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_comments WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_archive WHERE id = ?", (entry_id,))
    db.commit()
    return {"deleted": entry_id}


# ---------------------------------------------------------------------------
# Streaming – stream keys and live status
# ---------------------------------------------------------------------------

_GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
_GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")


@router.get("/stream-info")
async def creator_stream_info(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return streaming credentials and live status for all cameras.

    Includes the RTMP ingest URL and stream key (rtmp_key) that the creator
    enters into OBS or other streaming software.  Also returns whether each
    stream is currently live, derived from go2rtc's /api/streams API.
    """
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    hostname = urlparse(base_url).hostname if base_url else "localhost"
    rtmp_server = f"rtmp://{hostname}:1935"

    rows = db.execute(
        "SELECT id, display_name, stream_slug, rtmp_key, stream_title FROM cameras ORDER BY id"
    ).fetchall()

    # Attempt to fetch live status from go2rtc
    go2rtc_data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{_GO2RTC_HOST}:{_GO2RTC_PORT}/api/streams")
        if resp.is_success:
            go2rtc_data = resp.json()
    except Exception as exc:
        logger.warning("Could not fetch go2rtc stream status: %s", exc)

    cameras = []
    for row in rows:
        go2rtc_name = row["rtmp_key"] if row["rtmp_key"] else row["stream_slug"]
        stream_info = go2rtc_data.get(go2rtc_name) or {}
        producers = stream_info.get("producers") or []
        consumers = stream_info.get("consumers") or []
        cameras.append({
            "id": row["id"],
            "display_name": row["display_name"],
            "stream_slug": row["stream_slug"],
            "stream_title": row["stream_title"],
            "rtmp_key": row["rtmp_key"],
            "rtmp_server": rtmp_server if row["rtmp_key"] else None,
            "is_live": _is_producer_live(producers),
            "viewer_count": len(consumers),
        })

    return {"cameras": cameras, "rtmp_server": rtmp_server}


# ---------------------------------------------------------------------------
# Gift subscriptions (creator)
# ---------------------------------------------------------------------------


class CreatorGiftRequest(BaseModel):
    identifier: str = Field(..., min_length=1, max_length=254,
                            description="User email address or username")
    access_level: int = Field(2, ge=1, le=3)
    tier_id: Optional[int] = None
    expires_at: Optional[str] = None   # ISO-8601 datetime or None for permanent
    note: Optional[str] = Field(None, max_length=500)


@router.get("/subscribers/search")
def creator_search_subscribers(
    q: str = "",
    limit: int = 20,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Search for site users by username or email to gift a subscription.

    Returns id, username, email, display_name and their current subscription
    status for this creator.  Searches across all users (not just existing
    subscribers) so new users can be gifted too.
    """
    limit = max(1, min(limit, 100))
    pattern = f"%{q}%"
    rows = db.execute(
        """
        SELECT u.id, u.username, u.email, u.display_name,
               us.status AS sub_status
          FROM site_users u
          LEFT JOIN user_subscriptions us
            ON us.user_id = u.id AND us.creator_handle = ?
         WHERE u.username LIKE ? OR u.email LIKE ?
         ORDER BY u.created_at DESC
         LIMIT ?
        """,
        (handle, pattern, pattern, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/subscribers/gifted")
def creator_list_gifted(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all subscriptions gifted by this creator, newest first."""
    rows = db.execute(
        """
        SELECT g.id, g.user_id, u.username, u.email, u.display_name,
               g.access_level_granted, g.tier_id, g.expires_at,
               g.note, g.is_active, g.created_at
          FROM gifted_subscriptions g
          JOIN site_users u ON u.id = g.user_id
         WHERE g.creator_handle = ?
         ORDER BY g.created_at DESC
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/subscribers/gift", status_code=201)
def creator_gift_subscription(
    payload: CreatorGiftRequest,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Gift a subscription to a user identified by email or username.

    Grants ``access_level`` (never downgrades an existing higher level) and
    records the gift in ``gifted_subscriptions`` scoped to this creator.
    """
    # Resolve identifier → user.
    user = db.execute(
        "SELECT id, username, email, access_level FROM site_users WHERE email = ? OR username = ?",
        (payload.identifier, payload.identifier),
    ).fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No user found with that email or username.",
        )

    if payload.tier_id is not None:
        tier = db.execute(
            "SELECT id FROM subscription_tiers WHERE id = ? AND creator_handle = ?",
            (payload.tier_id, handle),
        ).fetchone()
        if not tier:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tier not found for this creator.",
            )

    now = datetime.now(timezone.utc).isoformat()
    user_id = user["id"]

    # Grant access (never downgrade).
    db.execute(
        "UPDATE site_users SET access_level = MAX(access_level, ?) WHERE id = ?",
        (payload.access_level, user_id),
    )

    cursor = db.execute(
        """
        INSERT INTO gifted_subscriptions
            (user_id, creator_handle, granted_by, access_level_granted, tier_id,
             expires_at, note, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            user_id,
            handle,
            f"creator:{handle}",
            payload.access_level,
            payload.tier_id,
            payload.expires_at or None,
            payload.note or None,
            now,
        ),
    )

    # Upsert user_subscriptions.
    db.execute(
        """
        INSERT INTO user_subscriptions (user_id, creator_handle, tier_id, status, started_at, expires_at)
        VALUES (?, ?, ?, 'active', ?, ?)
        ON CONFLICT(user_id, creator_handle)
        DO UPDATE SET status = 'active', tier_id = excluded.tier_id,
                      expires_at = excluded.expires_at
        """,
        (user_id, handle, payload.tier_id, now, payload.expires_at or None),
    )

    # Log subscribe event.
    db.execute(
        """
        INSERT INTO subscription_events (user_id, creator_handle, tier_id, event_type, created_at)
        VALUES (?, ?, ?, 'subscribe', ?)
        """,
        (user_id, handle, payload.tier_id, now),
    )

    db.commit()

    # Fire stream-overlay alert for gifted subscription.
    _alerts_dispatch(
        handle,
        "subscribe",
        {"username": user["username"], "tier_name": "", "gifted": True},
        db,
    )

    return {
        "gift_id": cursor.lastrowid,
        "user_id": user_id,
        "username": user["username"],
        "email": user["email"],
        "access_level_granted": payload.access_level,
        "expires_at": payload.expires_at,
    }


@router.delete("/subscribers/gifted/{gift_id}", status_code=status.HTTP_200_OK)
def creator_revoke_gift(
    gift_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Revoke a gift previously granted by this creator."""
    gift = db.execute(
        """
        SELECT id, user_id, access_level_granted, is_active
          FROM gifted_subscriptions
         WHERE id = ? AND creator_handle = ?
        """,
        (gift_id, handle),
    ).fetchone()
    if not gift:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift not found.")
    if not gift["is_active"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Gift already revoked.")

    now = datetime.now(timezone.utc).isoformat()
    user_id = gift["user_id"]

    db.execute("UPDATE gifted_subscriptions SET is_active = 0 WHERE id = ?", (gift_id,))

    # Revoke user_subscriptions if no other active gift for this creator.
    other = db.execute(
        "SELECT id FROM gifted_subscriptions WHERE user_id = ? AND creator_handle = ? AND is_active = 1 AND id != ?",
        (user_id, handle, gift_id),
    ).fetchone()
    if not other:
        db.execute(
            "UPDATE user_subscriptions SET status = 'cancelled' WHERE user_id = ? AND creator_handle = ?",
            (user_id, handle),
        )

    # Drop access_level to 0 if no remaining active gifts or Segpay subs.
    active_gifts = db.execute(
        "SELECT id FROM gifted_subscriptions WHERE user_id = ? AND is_active = 1", (user_id,)
    ).fetchone()
    active_subs = db.execute(
        "SELECT id FROM user_subscriptions WHERE user_id = ? AND status = 'active'", (user_id,)
    ).fetchone()
    if not active_gifts and not active_subs:
        db.execute("UPDATE site_users SET access_level = 0 WHERE id = ?", (user_id,))

    db.execute(
        "INSERT INTO subscription_events (user_id, creator_handle, tier_id, event_type, created_at) VALUES (?, ?, NULL, 'cancel', ?)",
        (user_id, handle, now),
    )

    db.commit()
    return {"revoked": gift_id}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
def creator_stats(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return simple statistics scoped to this creator."""
    subscriber_count = db.execute(
        "SELECT COUNT(*) FROM site_users WHERE access_level >= 2"
    ).fetchone()[0]

    unanswered_count = db.execute(
        "SELECT COUNT(*) FROM questions WHERE answer IS NULL AND creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    answered_count = db.execute(
        "SELECT COUNT(*) FROM questions WHERE answer IS NOT NULL AND creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    drool_count = db.execute(
        "SELECT COUNT(*) FROM drool_archive WHERE creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    return {
        "subscriber_count": subscriber_count,
        "unanswered_questions": unanswered_count,
        "answered_questions": answered_count,
        "drool_entries": drool_count,
    }


# ---------------------------------------------------------------------------
# Public creator profile
# ---------------------------------------------------------------------------

# Use a separate router with no prefix so the path is /api/creators/{handle}
# (distinct from the authenticated /api/creator/* namespace).
public_router = APIRouter(prefix="/api/creators", tags=["creator-public"])


@public_router.get("/{handle}")
def public_creator_profile(
    handle: str,
    db: sqlite3.Connection = Depends(get_db),
):
    """Return publicly available information about a creator.

    Includes ``public_email`` — the computed ``handle@domain`` address that
    fans can use to contact the creator.  Never reveals the private
    ``forwarding_email`` or ``agent_email`` stored in the database.
    """
    row = db.execute(
        """
        SELECT handle, display_name, bio, avatar_url, accent_color, allow_free_content, content_rating
          FROM creator_accounts
         WHERE handle = ? AND is_active = 1
        """,
        (handle,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    root_domain = urlparse(base_url).hostname if base_url else ""
    public_email = f"{handle}@{root_domain}" if root_domain else None

    return {
        **dict(row),
        "allow_free_content": bool(row["allow_free_content"]),
        "content_rating": row["content_rating"] or "unrated",
        "public_email": public_email,
    }
