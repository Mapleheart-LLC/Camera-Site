"""
dependencies.py – Shared FastAPI dependencies.

Centralises JWT configuration, token creation, and the ``get_current_user``
dependency so that both ``main.py`` and sub-routers can import them without
creating circular imports.

Also provides:
  - ``get_admin_user``   – HTTP Basic Auth guard for all ``/api/admin`` endpoints.
  - ``get_optional_user`` – Returns the authenticated user dict, or ``None`` for
                            unauthenticated requests (for public endpoints).
  - ``role_required``    – Legacy factory; protects endpoints by exact role match.
  - ``require_role``     – Preferred factory; enforces the RBAC role hierarchy so
                            higher-ranked roles automatically satisfy lower-ranked
                            requirements.

Role hierarchy (lowest → highest):
  user < paid_user < handler < admin

Defined roles and their permissions:
  user       – Access to SFW live stream endpoints.
  paid_user  – Access to SFW + NSFW live stream endpoints and uploaded VOD content.
  handler    – Full control over device interaction (tpeapp hardware) plus the
               ability to flag devices/streams as public for user/paid_user accounts.
  admin      – Unrestricted access to all endpoints.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

import bcrypt
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
# Override via JWT_ACCESS_TOKEN_EXPIRE_MINUTES env var (default: 30 minutes).
# Increase to e.g. 43200 (30 days) for long-lived mobile sessions.
try:
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
        os.environ.get("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    )
except ValueError:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES is not a valid integer; falling back to 30 minutes."
    )
    ACCESS_TOKEN_EXPIRE_MINUTES = 30

bearer_scheme = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# RBAC role definitions
# ---------------------------------------------------------------------------

# Canonical set of valid site roles.
VALID_ROLES: frozenset[str] = frozenset({"user", "paid_user", "handler", "admin"})

# Numeric rank for each role.  A higher rank means more privileges.
# Used by ``require_role`` to implement hierarchical access checks so that
# higher-ranked roles automatically satisfy lower-ranked requirements.
ROLE_HIERARCHY: dict[str, int] = {
    "user":      1,
    "paid_user": 2,
    "handler":   3,
    "admin":     4,
}

# Maps a user's role to the integer access_level used by the cameras table
# (minimum_access_level column).  Admins and handlers have full camera access.
ROLE_ACCESS_LEVEL: dict[str, int] = {
    "user":      1,
    "paid_user": 2,
    "handler":   3,
    "admin":     3,
}


#
# bcrypt only uses the first 72 bytes of a password. We reject longer values
# explicitly so hashing/verification behavior is predictable.
#
BCRYPT_MAX_PASSWORD_BYTES: int = 72


def is_bcrypt_password_compatible(password: str) -> bool:
    return len(password.encode("utf-8")) <= BCRYPT_MAX_PASSWORD_BYTES


def enforce_bcrypt_password_limit(password: str) -> None:
    if not is_bcrypt_password_compatible(password):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Password must be {BCRYPT_MAX_PASSWORD_BYTES} bytes or fewer "
                "for bcrypt compatibility."
            ),
        )


def hash_password(password: str) -> str:
    enforce_bcrypt_password_limit(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not is_bcrypt_password_compatible(password):
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


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
        role: str = payload.get("role", "user")
        if user_id is None:
            raise credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception
    return {"user_id": user_id, "access_level": access_level, "role": role}


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """Return the decoded JWT payload dict, or ``None`` for unauthenticated requests.

    Use this dependency on public endpoints that optionally enrich their
    response for logged-in users without blocking anonymous visitors::

        @router.get("/api/store/products")
        def list_products(current_user: Optional[dict] = Depends(get_optional_user)):
            ...
    """
    if not credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            return None
        access_level: int = int(payload.get("access_level", 0))
        role: str = payload.get("role", "user")
        return {"user_id": user_id, "access_level": access_level, "role": role}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def role_required(*allowed_roles: str) -> Callable:
    """Return a FastAPI dependency that enforces the caller has one of *allowed_roles*.

    Performs exact role matching.  Prefer ``require_role`` for new endpoints
    because it applies the RBAC hierarchy so higher-ranked roles automatically
    satisfy lower-ranked requirements.

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


def require_role(allowed_roles: List[str]) -> Callable:
    """Return a FastAPI dependency that enforces a minimum role level via the RBAC hierarchy.

    The *allowed_roles* list specifies the lowest-ranked role(s) that may access
    the endpoint.  Any role ranked *at or above* the minimum required rank is also
    permitted.  This means, for example, that ``require_role(["handler"])``
    automatically allows both ``handler`` and ``admin`` users.

    Role hierarchy (lowest → highest)::

        user (1) < paid_user (2) < handler (3) < admin (4)

    Usage::

        # Requires at least 'user' role (paid_user, handler, and admin also pass)
        @router.get("/api/stream/sfw")
        def sfw_stream(current_user: dict = Depends(require_role(["user"]))):
            ...

        # Requires at least 'paid_user' role
        @router.get("/api/stream/nsfw")
        def nsfw_stream(current_user: dict = Depends(require_role(["paid_user"]))):
            ...

        # Requires at least 'handler' role (admin also passes)
        @router.post("/api/handler/lock")
        def lock_device(current_user: dict = Depends(require_role(["handler"]))):
            ...

        # Requires admin only
        @router.delete("/api/admin/users/{user_id}")
        def delete_user(current_user: dict = Depends(require_role(["admin"]))):
            ...
    """
    # Validate all requested roles at definition time to catch configuration bugs early.
    unknown = [r for r in allowed_roles if r not in VALID_ROLES]
    if unknown:
        raise ValueError(
            f"require_role() received unknown role(s): {unknown}. "
            f"Valid roles are: {sorted(VALID_ROLES)}"
        )

    # Compute the minimum rank required once at definition time.
    min_rank: int = min(ROLE_HIERARCHY[r] for r in allowed_roles)

    def _check_role(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        user_role = current_user.get("role", "")
        if user_role not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user role in token. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_rank = ROLE_HIERARCHY[user_role]
        if user_rank < min_rank:
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
