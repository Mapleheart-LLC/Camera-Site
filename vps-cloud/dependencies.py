"""
dependencies.py – Shared FastAPI dependencies.

Centralises JWT configuration, token creation, and the ``get_current_user``
dependency so that both ``main.py`` and sub-routers can import them without
creating circular imports.

Also provides the ``get_admin_user`` HTTP Basic Auth dependency that guards
all ``/api/admin`` endpoints, and the ``role_required`` factory that protects
handler-panel endpoints by JWT user role ('admin' or 'handler').
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

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

# Prefer JWT_SECRET, then SECRET_KEY.  If neither is set and MOCK_AUTH is
# enabled, fall back to an insecure demo key so the app stays up during demos.
SECRET_KEY: str = (
    os.environ.get("JWT_SECRET")
    or os.environ.get("SECRET_KEY")
    or (_DEMO_FALLBACK_KEY if _MOCK_AUTH else _DEFAULT_KEY)
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


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """Decode the site JWT and return ``{"user_id": ..., "access_level": ..., "role": ...}``."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise credentials_exception
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        access_level: int = int(payload.get("access_level", 0))
        role: str = payload.get("role", "handler")
        if user_id is None:
            raise credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception
    return {"user_id": user_id, "access_level": access_level, "role": role}


def role_required(*allowed_roles: str) -> Callable:
    """Return a FastAPI dependency that enforces the caller has one of *allowed_roles*.

    Usage::

        @router.get("/api/handler/devices")
        def list_devices(current_user: dict = Depends(role_required("admin", "handler"))):
            ...
    """
    def _check_role(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        if current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role privileges.",
            )
        return current_user

    return _check_role


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

