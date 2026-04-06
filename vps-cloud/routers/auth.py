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
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from db import get_db
from dependencies import ACCESS_TOKEN_EXPIRE_MINUTES, ADMIN_PASSWORD, ADMIN_USERNAME, create_access_token

router = APIRouter(tags=["auth"])

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 600_000
_SEGPAY_PURCHASE_BASE = "https://purchase.segpay.com/hosted/index.asp"

# Rate limiter: max 10 auth attempts per IP per 15 minutes.
_auth_limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Account lockout (in-memory, per identifier)
#
# After _LOCKOUT_THRESHOLD consecutive failures the identifier is locked for
# _LOCKOUT_DURATION.  The state lives in process memory; it resets on restart
# which is acceptable — the rate-limiter provides the persistent layer.
# ---------------------------------------------------------------------------

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_DURATION = timedelta(minutes=15)

# identifier → {"count": int, "locked_until": datetime | None}
_login_attempts: dict[str, dict] = {}


def _record_failed_attempt(identifier: str) -> None:
    entry = _login_attempts.setdefault(identifier, {"count": 0, "locked_until": None})
    entry["count"] += 1
    if entry["count"] >= _LOCKOUT_THRESHOLD:
        entry["locked_until"] = datetime.now(timezone.utc) + _LOCKOUT_DURATION
        logger.warning("Account locked after %d failed attempts: %s", entry["count"], identifier)


def _check_lockout(identifier: str) -> None:
    """Raise 429 if the identifier is currently locked out."""
    entry = _login_attempts.get(identifier)
    if not entry:
        return
    locked_until = entry.get("locked_until")
    if locked_until:
        now = datetime.now(timezone.utc)
        if now < locked_until:
            retry_after = int((locked_until - now).total_seconds())
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Account temporarily locked due to too many failed login attempts. "
                    f"Try again in {retry_after // 60 + 1} minute(s)."
                ),
                headers={"Retry-After": str(retry_after)},
            )


def _clear_lockout(identifier: str) -> None:
    _login_attempts.pop(identifier, None)


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

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """Enforce a minimum password complexity policy."""
        errors = []
        if not re.search(r"[A-Z]", v):
            errors.append("an uppercase letter")
        if not re.search(r"[a-z]", v):
            errors.append("a lowercase letter")
        if not re.search(r"\d", v):
            errors.append("a digit (0-9)")
        if not re.search(r"[^A-Za-z0-9]", v):
            errors.append("a special character (e.g. !@#$%)")
        if errors:
            missing = "; ".join(errors)
            raise ValueError(f"Password does not meet complexity requirements. Missing: {missing}.")
        return v


class LoginRequest(BaseModel):
    username_or_email: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
@_auth_limiter.limit("10/15minutes")
def register(
    request: Request,
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
@_auth_limiter.limit("10/15minutes")
def login(
    request: Request,
    payload: LoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate with username/email + password.  Returns a JWT."""
    identifier = payload.username_or_email.lower().strip()

    # Check lockout before touching the database.
    _check_lockout(identifier)

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
        # ── Admin-credential fallback ───────────────────────────────────────
        # If the submitted identifier matches the admin username and the
        # password matches the admin password, issue a max-access subscriber
        # JWT so the admin can use all subscriber-gated areas of the site.
        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and secrets.compare_digest(identifier.encode(), ADMIN_USERNAME.lower().encode())
            and secrets.compare_digest(payload.password.encode(), ADMIN_PASSWORD.encode())
        ):
            _clear_lockout(identifier)
            token = create_access_token(
                {"sub": f"admin:{ADMIN_USERNAME}", "access_level": 3},
                expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            )
            logger.info("Admin '%s' logged into subscriber portal via admin credentials.", ADMIN_USERNAME)
            return {
                "access_token": token,
                "token_type": "bearer",
                "username": ADMIN_USERNAME,
                "access_level": 3,
            }

        # Always run the hash so response time doesn't reveal whether the
        # username/email exists (timing-safe user-enumeration prevention).
        _hash_password("__timing_guard__")
        _record_failed_attempt(identifier)
        raise _invalid

    if not _verify_password(payload.password, row["hashed_password"]):
        _record_failed_attempt(identifier)
        raise _invalid

    # Successful login — clear any lockout state.
    _clear_lockout(identifier)

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
