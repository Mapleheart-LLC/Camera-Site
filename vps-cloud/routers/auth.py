"""
routers/auth.py – Native site authentication endpoints.

Provides username/email + password based registration and login that are
completely independent of Fanvue OAuth.  Issues JWTs in the same format
as the existing Fanvue flow (``{sub, access_level}``) so ``get_current_user``
works unchanged throughout the rest of the application.

Also exposes the helper endpoint for building a Segpay subscription URL.

Endpoints
---------
  POST /api/auth/register    – create a new account; returns a JWT
  POST /api/auth/login       – authenticate; returns a JWT
  GET  /api/subscriptions/url – build a Segpay subscription redirect URL
"""

import base64
import hashlib
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token

router = APIRouter(tags=["auth"])

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000
_SEGPAY_PURCHASE_BASE = "https://purchase.segpay.com/hosted/index.asp"


# ---------------------------------------------------------------------------
# Password hashing (Python stdlib only – no extra dependencies)
#
# Each stored hash encodes a 32-byte random salt prepended to the derived key,
# then base64-encoded, so the salt is always recovered alongside the hash.
# ---------------------------------------------------------------------------


def _hash_password(password: str) -> str:
    """Return a PBKDF2-HMAC-SHA256 hash of *password* with a random salt.

    Format: base64(salt[32] || derived_key[32])
    """
    salt = secrets.token_bytes(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return base64.b64encode(salt + dk).decode("ascii")


def _verify_password(password: str, stored_hash: str) -> bool:
    """Return True when *password* matches *stored_hash*."""
    try:
        decoded = base64.b64decode(stored_hash.encode("ascii"))
        salt, dk_stored = decoded[:32], decoded[32:]
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
        return secrets.compare_digest(dk, dk_stored)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Letters, numbers, underscores, and hyphens only.",
    )
    email: str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username_or_email: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Register a new account.

    Returns a JWT so the user is immediately signed in without a second
    round-trip.  The new account starts at ``access_level=0``; they must
    subscribe via Segpay to unlock streamed content.
    """
    username = payload.username.lower()
    email = payload.email.lower().strip()

    conflict = db.execute(
        "SELECT id FROM site_users WHERE username = ? OR email = ?",
        (username, email),
    ).fetchone()
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email is already registered.",
        )

    user_id = str(uuid.uuid4())
    hashed = _hash_password(payload.password)
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        """
        INSERT INTO site_users (id, username, email, hashed_password, access_level, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (user_id, username, email, hashed, now),
    )
    db.commit()

    token = create_access_token(
        {"sub": user_id, "access_level": 0},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    logger.info("New site_user registered: %s (%s)", username, user_id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": username,
        "access_level": 0,
    }


@router.post("/api/auth/login")
def login(
    payload: LoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate with username/email + password.  Returns a JWT."""
    identifier = payload.username_or_email.lower().strip()

    row = db.execute(
        """
        SELECT id, username, email, hashed_password, access_level
          FROM site_users
         WHERE username = ? OR email = ?
        """,
        (identifier, identifier),
    ).fetchone()

    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username/email or password.",
    )

    if not row:
        # Always run the hash so response time doesn't reveal whether the
        # username/email exists (timing-safe user-enumeration prevention).
        _hash_password("__timing_guard__")
        raise _invalid

    if not _verify_password(payload.password, row["hashed_password"]):
        raise _invalid

    token = create_access_token(
        {"sub": row["id"], "access_level": row["access_level"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": row["username"],
        "access_level": row["access_level"],
    }


@router.get("/api/subscriptions/url")
def get_subscription_url(email: Optional[str] = None):
    """Build and return the Segpay subscription checkout URL.

    Requires ``SEGPAY_SUB_PACKAGE_ID`` and ``SEGPAY_SUB_PRICE_POINT_ID`` to
    be set.  When *email* is supplied it is pre-filled in the Segpay form.
    """
    package_id = os.environ.get("SEGPAY_SUB_PACKAGE_ID", "")
    price_point_id = os.environ.get("SEGPAY_SUB_PRICE_POINT_ID", "")
    base_url = os.environ.get("BASE_URL", "").rstrip("/")

    if not package_id or not price_point_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Subscriptions are not yet configured on this server.",
        )

    params: dict = {
        "x-eticketid": f"{package_id}:{price_point_id}",
        "x-postbackurl": f"{base_url}/api/webhooks/subscriptions/segpay",
        "x-successurl": f"{base_url}/?subscribed=1",
        "x-cancelurl": f"{base_url}/?subscribed=0",
    }
    if email:
        params["x-billemail"] = email.lower().strip()

    return {"url": f"{_SEGPAY_PURCHASE_BASE}?{urlencode(params)}"}
