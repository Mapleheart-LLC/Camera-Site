import logging
import os
import sys
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dependencies import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY,
    create_access_token,
    get_current_user,
)
from routers.interactive import router as interactive_router
from redis_client import close_redis

# ---------------------------------------------------------------------------
# Configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "camera_site.db")
GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")

# Fanvue OAuth 2.0 settings – set these in your environment / docker-compose.yml
FANVUE_CLIENT_ID: str = os.environ.get("FANVUE_CLIENT_ID", "")
FANVUE_CLIENT_SECRET: str = os.environ.get("FANVUE_CLIENT_SECRET", "")
FANVUE_REDIRECT_URI: str = os.environ.get(
    "FANVUE_REDIRECT_URI", "http://localhost:8000/auth/callback"
)
FANVUE_AUTH_URL: str = os.environ.get(
    "FANVUE_AUTH_URL", "https://app.fanvue.com/oauth/authorize"
)
FANVUE_TOKEN_URL: str = os.environ.get(
    "FANVUE_TOKEN_URL", "https://api.fanvue.com/oauth/token"
)
FANVUE_PROFILE_URL: str = os.environ.get(
    "FANVUE_PROFILE_URL", "https://api.fanvue.com/profile"
)
# Optional: restrict access checks to a specific creator's subscriber list.
FANVUE_CREATOR_ID: str = os.environ.get("FANVUE_CREATOR_ID", "")

# Set MOCK_AUTH=true to bypass Fanvue OAuth and issue a fake token.
# Useful for demos when the Fanvue API keys are not yet available.
# NEVER enable this in production.
_mock_auth_raw = os.environ.get("MOCK_AUTH", "")
MOCK_AUTH: bool = _mock_auth_raw == True or (  # noqa: E712 – intentional bool/str check
    isinstance(_mock_auth_raw, str) and _mock_auth_raw.lower() == "true"
)

# OAuth CSRF state tokens live in memory; entries expire after STATE_TTL seconds.
STATE_TTL: int = 600
_oauth_states: dict[str, datetime] = {}

_DEFAULT_KEY = "changeme-replace-in-production!!"

logger = logging.getLogger(__name__)

# Route all log output to stderr so messages appear in Komodo / container logs.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the users and cameras tables if they do not already exist."""
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT    PRIMARY KEY,
            fanvue_id    TEXT    NOT NULL UNIQUE,
            access_level INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name         TEXT    NOT NULL,
            stream_slug          TEXT    NOT NULL UNIQUE,
            minimum_access_level INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    conn.close()


def get_db():
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fanvue OAuth helpers
# ---------------------------------------------------------------------------


def _generate_state() -> str:
    """Create a cryptographically random CSRF state token and store it."""
    _prune_expired_states()
    token = secrets.token_urlsafe(32)
    _oauth_states[token] = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL)
    return token


def _consume_state(token: str) -> bool:
    """Return True and remove the state if it is valid and not expired."""
    _prune_expired_states()
    expiry = _oauth_states.pop(token, None)
    return expiry is not None and datetime.now(timezone.utc) < expiry


def _prune_expired_states() -> None:
    now = datetime.now(timezone.utc)
    expired = [k for k, v in _oauth_states.items() if v <= now]
    for k in expired:
        del _oauth_states[k]


def determine_access_level(profile: dict) -> int:
    """
    Map a Fanvue profile API response to an integer access level.

      0 – not a follower / no relationship
      1 – free follower
      2 – active Tier 1 subscriber
      3 – active Tier 2+ subscriber

    Adjust the field names below to match the actual Fanvue API response.
    See https://api.fanvue.com/docs for the current profile response format.
    """
    # Subscription block (may be nested or flat depending on API version)
    subscription = profile.get("subscription") or {}
    tier: int = int(subscription.get("tier", 0))
    is_active: bool = bool(subscription.get("is_active", False))

    if is_active and tier >= 2:
        return 3
    if is_active and tier == 1:
        return 2

    # Free follower
    if profile.get("is_following") or profile.get("following"):
        return 1

    return 0


# ---------------------------------------------------------------------------
# FastAPI app lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SECRET_KEY == _DEFAULT_KEY:
        logger.warning(
            "SECRET_KEY is set to the default development value. "
            "Set a strong SECRET_KEY environment variable before deploying to production."
        )
    if MOCK_AUTH:
        print("MOCK MODE IS ENABLED")
        logger.warning(
            "MOCK_AUTH is enabled. /auth/login will redirect to /#token=FAKE_TOKEN "
            "without any Fanvue authentication. Do NOT use this in production."
        )
    elif not FANVUE_CLIENT_ID or not FANVUE_CLIENT_SECRET:
        logger.warning(
            "FANVUE_CLIENT_ID and/or FANVUE_CLIENT_SECRET are not set. "
            "OAuth login will not work until these are configured."
        )
    init_db()
    yield
    # Close the Redis connection pool on shutdown to release resources.
    await close_redis()


app = FastAPI(title="Camera Site API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class CameraResponse(BaseModel):
    display_name: str
    stream_slug: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.get("/auth/login")
def auth_login():
    """Redirect the browser to Fanvue's OAuth 2.0 authorization page.

    If MOCK_AUTH is enabled, skip the Fanvue redirect entirely and issue a
    fake JWT with access_level=3 so the site's features can be demoed without
    real Fanvue API credentials.
    """
    if MOCK_AUTH:
        return RedirectResponse(url="/#token=FAKE_TOKEN", status_code=302)

    if not FANVUE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth is not configured on this server.",
        )
    state = _generate_state()
    params = urlencode(
        {
            "client_id": FANVUE_CLIENT_ID,
            "redirect_uri": FANVUE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
        }
    )
    return RedirectResponse(url=f"{FANVUE_AUTH_URL}?{params}", status_code=302)


@app.get("/auth/callback")
async def auth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Handle the OAuth 2.0 authorization code callback from Fanvue.

    On success: exchange the code for a token, fetch the user profile,
    assign an access level, persist the user, issue a short-lived JWT,
    and redirect to the frontend with the token in the URL fragment.
    """
    # If the provider returned an error, log it and redirect to the login page.
    # We intentionally do NOT forward the raw provider error code into the
    # redirect URL to prevent URL injection / open-redirect via the query string.
    if error:
        logger.warning("Fanvue OAuth error received: %s", error)
        return RedirectResponse(url="/?error=oauth_error", status_code=302)

    if not code or not state:
        return RedirectResponse(url="/?error=missing_params", status_code=302)

    if not _consume_state(state):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)

    # Exchange authorization code for an access token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                FANVUE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": FANVUE_REDIRECT_URI,
                    "client_id": FANVUE_CLIENT_ID,
                    "client_secret": FANVUE_CLIENT_SECRET,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        logger.error("Token exchange request failed: %s", exc)
        return RedirectResponse(url="/?error=token_request_failed", status_code=302)

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        return RedirectResponse(url="/?error=token_exchange_failed", status_code=302)

    fanvue_token = token_resp.json().get("access_token")
    if not fanvue_token:
        return RedirectResponse(url="/?error=no_access_token", status_code=302)

    # Fetch the user's Fanvue profile
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            profile_resp = await client.get(
                FANVUE_PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {fanvue_token}",
                    "Accept": "application/json",
                },
            )
    except httpx.RequestError as exc:
        logger.error("Profile request failed: %s", exc)
        return RedirectResponse(url="/?error=profile_request_failed", status_code=302)

    if profile_resp.status_code != 200:
        logger.error("Profile fetch failed: %s %s", profile_resp.status_code, profile_resp.text)
        return RedirectResponse(url="/?error=profile_fetch_failed", status_code=302)

    profile = profile_resp.json()

    # Resolve the Fanvue user ID from the profile response.
    # Common field names: "uuid", "id", "user_id" – adjust to match the API.
    fanvue_id: Optional[str] = (
        profile.get("uuid")
        or profile.get("id")
        or profile.get("user_id")
    )
    if not fanvue_id:
        logger.error("Could not determine fanvue_id from profile: %s", profile)
        return RedirectResponse(url="/?error=no_user_id", status_code=302)

    fanvue_id = str(fanvue_id)
    access_level = determine_access_level(profile)

    # Persist or update the user in the local database.
    # `user_id` is only stored on first insert; ON CONFLICT leaves the existing
    # `id` unchanged and only refreshes the access_level.
    new_user_id = secrets.token_hex(16)
    db.execute(
        """
        INSERT INTO users (id, fanvue_id, access_level)
        VALUES (?, ?, ?)
        ON CONFLICT(fanvue_id) DO UPDATE SET access_level = excluded.access_level
        """,
        (new_user_id, fanvue_id, access_level),
    )
    db.commit()

    # Issue a short-lived JWT for the frontend
    site_token = create_access_token(
        {"sub": fanvue_id, "access_level": access_level},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    # Redirect to the SPA; deliver token via URL fragment so it is never sent
    # to the server in subsequent requests.
    return RedirectResponse(url=f"/#token={site_token}", status_code=302)


# ---------------------------------------------------------------------------
# Protected API endpoints
# ---------------------------------------------------------------------------


@app.get("/api/my-cameras", response_model=list[CameraResponse])
def get_my_cameras(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return cameras the authenticated user is permitted to view."""
    access_level: int = current_user["access_level"]

    rows = db.execute(
            """
            SELECT display_name, stream_slug
            FROM cameras
            WHERE minimum_access_level <= ?
            ORDER BY minimum_access_level, id
            """,
            (access_level,),
        ).fetchall()

    return JSONResponse(
        [{"display_name": row["display_name"], "stream_slug": row["stream_slug"]} for row in rows]
    )


# ---------------------------------------------------------------------------
# Routers and static files (registered last so API routes take priority)
# ---------------------------------------------------------------------------

app.include_router(interactive_router)

# Serve the static frontend (mount last so API routes take priority)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
