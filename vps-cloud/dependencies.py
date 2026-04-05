"""
dependencies.py – Shared FastAPI dependencies.

Centralises JWT configuration, token creation, and the ``get_current_user``
dependency so that both ``main.py`` and sub-routers can import them without
creating circular imports.

Also provides the ``get_admin_user`` HTTP Basic Auth dependency that guards
all ``/api/admin`` endpoints, and the ``get_current_creator`` JWT dependency
that guards all ``/api/creator`` endpoints.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

# ---------------------------------------------------------------------------
# Auth configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
_mock_auth_raw = os.environ.get("MOCK_AUTH", "")
_MOCK_AUTH: bool = _mock_auth_raw.lower() == "true"

_DEFAULT_KEY = "changeme-replace-in-production!!"
_DEMO_FALLBACK_KEY = "demo-mode-insecure-do-not-use-in-production"

# Prefer JWT_SECRET, then SECRET_KEY.
# If neither is configured:
#   - MOCK_AUTH demo mode → use the recognisable insecure demo key (convenient
#     for demos, startup guard already blocks this in production).
#   - Otherwise → generate a cryptographically random key per process lifetime.
#     This is far safer than a well-known hardcoded default: an attacker cannot
#     pre-compute tokens, and existing tokens are invalidated on each restart
#     (acceptable because the hardcoded default would be equally unreliable in
#     a real deployment).
SECRET_KEY: str = (
    os.environ.get("JWT_SECRET")
    or os.environ.get("SECRET_KEY")
    or (_DEMO_FALLBACK_KEY if _MOCK_AUTH else secrets.token_hex(32))
)
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Return a signed JWT encoding *data* with an expiry claim."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of raising 401 when unauthenticated."""
    if not credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        fanvue_id: Optional[str] = payload.get("sub")
        access_level: int = int(payload.get("access_level", 0))
        if fanvue_id is None:
            return None
        return {"fanvue_id": fanvue_id, "access_level": access_level}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """Decode the Fanvue/native JWT and return ``{"fanvue_id": ..., "access_level": ...}``."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise credentials_exception
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        fanvue_id: Optional[str] = payload.get("sub")
        access_level: int = int(payload.get("access_level", 0))
        if fanvue_id is None:
            raise credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception
    return {"fanvue_id": fanvue_id, "access_level": access_level}


# ---------------------------------------------------------------------------
# Creator authentication (JWT-based, separate from subscriber + admin auth)
# ---------------------------------------------------------------------------

_creator_credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing creator token.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_creator(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    """Decode a creator JWT and return the creator's handle.

    The token must carry ``role: creator`` to prevent subscriber or admin
    tokens from being used as creator tokens (no privilege escalation).
    """
    if not credentials:
        raise _creator_credentials_exception
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        handle: Optional[str] = payload.get("sub")
        role: str = payload.get("role", "")
        if not handle or role != "creator":
            raise _creator_credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise _creator_credentials_exception
    return handle


# ---------------------------------------------------------------------------
# Admin authentication (HTTP Basic Auth)
# ---------------------------------------------------------------------------

# Override via environment variables in docker-compose / .env.
# Both ADMIN_USERNAME and ADMIN_PASSWORD must be set (non-empty) or all admin
# endpoints return 503. This prevents any insecure open-access defaults.
ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")

_http_basic = HTTPBasic(auto_error=False)

_ADMIN_AUTH_REQUIRED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Admin credentials required.",
    headers={"WWW-Authenticate": 'Basic realm="Alpha Kennel"'},
)


def get_admin_user(
    credentials: Optional[HTTPBasicCredentials] = Depends(_http_basic),
) -> str:
    """
    Authenticate an admin request via HTTP Basic Auth.

    Uses ``secrets.compare_digest`` for timing-safe credential comparisons.

    Returns the authenticated username on success.
    Raises 401 if credentials are missing or invalid.
    Raises 503 if admin auth is not configured.
    """
    # ── HTTP Basic Auth ────────────────────────────────────────────────────
    if not ADMIN_PASSWORD or not ADMIN_USERNAME:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured on this server.",
        )
    if not credentials:
        raise _ADMIN_AUTH_REQUIRED
    valid_username = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        ADMIN_USERNAME.encode("utf-8"),
    )
    valid_password = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        ADMIN_PASSWORD.encode("utf-8"),
    )
    if not (valid_username and valid_password):
        raise _ADMIN_AUTH_REQUIRED
    return credentials.username

