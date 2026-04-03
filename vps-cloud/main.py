import io
import logging
import os
import sys
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, quote as _url_quote

import httpx
import html as _html_lib

from PIL import Image, ImageDraw, ImageFont
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import DATABASE_PATH, get_db, get_db_connection, get_setting
from dependencies import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY,
    create_access_token,
    get_current_user,
)
from routers.interactive import router as interactive_router
from routers.admin import router as admin_router
from routers.questions import router as questions_router
from routers.links import router as links_router
from routers.store import router as store_router
from routers.discord_interactions import router as discord_interactions_router
from routers.drool import router as drool_router, limiter as drool_limiter
from drool_scraper import start_drool_scheduler, stop_drool_scheduler
from routers.discord_oauth import register_metadata_schema, router as discord_oauth_router
from redis_client import close_redis
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

# ---------------------------------------------------------------------------
# Configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
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

# Canonical public root URL of the site (e.g. https://mochii.live).
# Used for OG image URLs and share-page links so they are always absolute
# HTTPS URLs even when the backend is behind a reverse proxy or tunnel.
# Falls back to the request base_url when not set.
BASE_URL: str = os.environ.get("BASE_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# CORS / cookie-domain configuration
#
# ALLOWED_ORIGINS — comma-separated list of origins that browsers are allowed
#   to make cross-origin requests from (e.g. "https://mochii.live,https://shop.mochii.live").
#   When empty, origins are auto-derived from BASE_URL (root + all known subdomains).
#   Include "http://localhost:8000" here for local development.
#
# COOKIE_DOMAIN — value passed as the "domain" attribute on any Set-Cookie header.
#   When empty, falls back to the public root hostname derived from BASE_URL
#   (e.g. ".mochii.live" — note the leading dot which enables sub-domain sharing).
# ---------------------------------------------------------------------------

_SUBDOMAIN_PREFIXES = ("anon", "links", "shop", "drool")


def _build_allowed_origins() -> list[str]:
    """Derive the CORS allowed-origins list from ALLOWED_ORIGINS env var or BASE_URL."""
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    if not BASE_URL:
        # Fallback for local development when no BASE_URL is set
        return ["http://localhost:8000", "http://localhost:3000"]
    origins = [BASE_URL]  # e.g. https://mochii.live
    parsed = urlparse(BASE_URL)
    for prefix in _SUBDOMAIN_PREFIXES:
        origins.append(f"{parsed.scheme}://{prefix}.{parsed.hostname}")
    return origins


ALLOWED_ORIGINS: list[str] = _build_allowed_origins()


def _cookie_domain() -> str:
    """Return the cookie domain (leading-dot form for subdomain sharing)."""
    raw = os.environ.get("COOKIE_DOMAIN", "").strip()
    if raw:
        return raw
    if BASE_URL:
        hostname = urlparse(BASE_URL).hostname or ""
        # Leading dot lets the cookie be read by all subdomains
        return f".{hostname}" if hostname and not hostname.startswith(".") else hostname
    return ""


COOKIE_DOMAIN: str = _cookie_domain()
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


def init_db() -> None:
    """Create the users, cameras, activations, and questions tables if they do not already exist."""
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device       TEXT    NOT NULL,
            actor        TEXT    NOT NULL,
            activated_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id         TEXT    PRIMARY KEY,
            text       TEXT    NOT NULL,
            answer     TEXT,
            is_public  INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS camera_service_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT    NOT NULL,
            access_level INTEGER NOT NULL,
            camera_count INTEGER NOT NULL,
            accessed_at  TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            url         TEXT    NOT NULL,
            emoji       TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            name                 TEXT    NOT NULL,
            description          TEXT,
            price                REAL    NOT NULL,
            image_url            TEXT,
            is_printful          INTEGER NOT NULL DEFAULT 0,
            printful_variant_id  TEXT,
            stock_count          INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id                      TEXT    PRIMARY KEY,
            external_transaction_id TEXT,
            provider_name           TEXT    NOT NULL DEFAULT 'segpay',
            status                  TEXT    NOT NULL DEFAULT 'pending',
            customer_email          TEXT    NOT NULL,
            total_amount            REAL    NOT NULL,
            shipping_address        TEXT    NOT NULL DEFAULT '{}',
            created_at              TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_accounts (
            discord_id               TEXT PRIMARY KEY,
            user_id                  TEXT REFERENCES users(id),
            discord_username         TEXT NOT NULL,
            discord_avatar           TEXT,
            discord_access_token     TEXT NOT NULL,
            discord_refresh_token    TEXT,
            discord_token_expires_at TEXT,
            linked_at                TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    TEXT    NOT NULL REFERENCES orders(id),
            product_id  INTEGER NOT NULL REFERENCES products(id),
            quantity    INTEGER NOT NULL,
            unit_price  REAL    NOT NULL
        )
        """
    )
    # ── Drool Log tables ──────────────────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drool_archive (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            platform     TEXT    NOT NULL CHECK(platform IN ('reddit', 'twitter')),
            original_url TEXT    NOT NULL UNIQUE,
            media_url    TEXT,
            text_content TEXT,
            view_count   INTEGER NOT NULL DEFAULT 0,
            timestamp    TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drool_comments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            drool_id       INTEGER NOT NULL REFERENCES drool_archive(id),
            comment_text   TEXT    NOT NULL,
            pack_member_id TEXT    NOT NULL,
            created_at     TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drool_reactions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            drool_id       INTEGER NOT NULL REFERENCES drool_archive(id),
            reaction_type  TEXT    NOT NULL
                               CHECK(reaction_type IN ('Good Girl','Bad Puppy','Dumb Thing','Pretty Toy')),
            pack_member_id TEXT    NOT NULL,
            UNIQUE(drool_id, pack_member_id)
        )
        """
    )
    # Idempotent migrations: add stream-source columns to existing databases.
    # Column names and types are hardcoded literals (not user input), so
    # string interpolation here is safe and necessary for DDL statements.
    for _col, _defn in [
        ("rtsp_url",      "TEXT"),
        ("tapo_ip",       "TEXT"),
        ("tapo_username", "TEXT"),
        ("tapo_password", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE cameras ADD COLUMN {_col} {_defn}")
        except Exception:
            pass  # column already exists
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# go2rtc helpers
# ---------------------------------------------------------------------------


async def _sync_cameras_to_go2rtc() -> None:
    """On startup, register every camera that has connection info with go2rtc."""
    go2rtc_base = f"http://{GO2RTC_HOST}:{GO2RTC_PORT}"
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT stream_slug, rtsp_url, tapo_ip, tapo_username, tapo_password FROM cameras"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    async with httpx.AsyncClient(timeout=5.0) as client:
        for row in rows:
            tapo_ip = row["tapo_ip"]
            if tapo_ip:
                user = _url_quote(row["tapo_username"] or "", safe="")
                pwd  = _url_quote(row["tapo_password"]  or "", safe="")
                effective_url = f"rtsp://{user}:{pwd}@{tapo_ip}/stream1"
            else:
                effective_url = row["rtsp_url"]

            if not effective_url:
                continue

            slug = row["stream_slug"]
            try:
                await client.put(
                    f"{go2rtc_base}/api/streams",
                    params={"name": slug, "src": effective_url},
                )
                logger.info("Registered camera stream '%s' with go2rtc", slug)
            except Exception as exc:
                logger.warning(
                    "Could not register stream '%s' with go2rtc on startup: %s", slug, exc
                )


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
    logger.info("Startup config: MOCK_AUTH=%s  DATABASE_PATH=%s", MOCK_AUTH, DATABASE_PATH)
    logger.info("CORS allowed origins: %s", ALLOWED_ORIGINS)
    logger.info("Cookie domain: %r", COOKIE_DOMAIN or "(not set – browser default)")
    if MOCK_AUTH:
        print("MOCK MODE IS ENABLED")
        logger.warning(
            "MOCK_AUTH is enabled. /auth/login will issue a fake access_level=3 token "
            "without any Fanvue authentication. Do NOT use this in production."
        )
    elif not FANVUE_CLIENT_ID or not FANVUE_CLIENT_SECRET:
        logger.warning(
            "FANVUE_CLIENT_ID and/or FANVUE_CLIENT_SECRET are not set. "
            "OAuth login will not work until these are configured."
        )
    init_db()
    await _sync_cameras_to_go2rtc()
    start_drool_scheduler()
    await register_metadata_schema()
    yield
    stop_drool_scheduler()
    # Close the Redis connection pool on shutdown to release resources.
    await close_redis()


app = FastAPI(title="mochii.live API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# CORSMiddleware must be registered before the subdomain path-rewriting
# middleware so that preflight OPTIONS requests are answered immediately and
# CORS headers are injected on all responses, including those from subdomains.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
def auth_login(db: sqlite3.Connection = Depends(get_db)):
    """Redirect the browser to Fanvue's OAuth 2.0 authorization page.

    If MOCK_AUTH is enabled (via env var or the admin Danger Zone DB override),
    skip the Fanvue redirect entirely and issue a fake JWT with access_level=3
    so the site's features can be demoed without real Fanvue API credentials.
    """
    # DB setting takes precedence over the env var so the admin can toggle
    # mock-auth at runtime without restarting the container.
    db_mock_auth = get_setting(db, "mock_auth")
    effective_mock_auth = (
        db_mock_auth.lower() == "true" if db_mock_auth is not None else MOCK_AUTH
    )

    if effective_mock_auth:
        mock_token = create_access_token(
            {"sub": "mock-user", "access_level": 3},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        return RedirectResponse(url=f"/#token={mock_token}", status_code=302)

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
    user_id: str = current_user["sub"]

    rows = db.execute(
            """
            SELECT display_name, stream_slug
            FROM cameras
            WHERE minimum_access_level <= ?
            ORDER BY minimum_access_level, id
            """,
            (access_level,),
        ).fetchall()

    db.execute(
        "INSERT INTO camera_service_logs (user_id, access_level, camera_count, accessed_at) VALUES (?, ?, ?, ?)",
        (user_id, access_level, len(rows), datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    return JSONResponse(
        [{"display_name": row["display_name"], "stream_slug": row["stream_slug"]} for row in rows]
    )


# ---------------------------------------------------------------------------
# Routers and static files (registered last so API routes take priority)
# ---------------------------------------------------------------------------

app.include_router(interactive_router)
app.include_router(admin_router)
app.include_router(questions_router)
app.include_router(links_router)
app.include_router(store_router)
app.include_router(discord_interactions_router)
app.include_router(drool_router)
app.include_router(discord_oauth_router)

# Attach the slowapi rate-limiter state and exception handler to the app so
# that @limiter.limit decorators in the drool router function correctly.
app.state.limiter = drool_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def subdomain_routing(request: Request, call_next):
    """Transparently serve subdomain roots by rewriting the ASGI path in-place.

    anon.mochii.live/   → serves /anon content    (URL in browser unchanged)
    links.mochii.live/  → serves /links content   (URL in browser unchanged)
    shop.mochii.live/   → serves /store.html      (URL in browser unchanged)
    drool.mochii.live/  → serves /drool.html      (URL in browser unchanged)

    Only GET requests to exactly "/" are rewritten so that the correct HTML
    page is returned.  All other paths (API calls, static assets, …) pass
    through untouched — they work identically on every subdomain.
    """
    if request.method == "GET" and request.url.path == "/":
        host = request.headers.get("host", "").lower().split(":")[0]
        # Maps subdomain prefix → the path that should be served for that root.
        _subdomain_map = {
            "anon.":   "/anon",
            "links.":  "/links",
            "shop.":   "/store.html",
            "drool.":  "/drool.html",
        }
        for prefix, target_path in _subdomain_map.items():
            if host.startswith(prefix):
                request.scope["path"] = target_path
                break
    return await call_next(request)


@app.get("/admin", include_in_schema=False)
def admin_page_redirect(request: Request):
    """Redirect /admin (and /admin?q=...) to the static admin.html page."""
    qs = request.url.query
    target = f"/admin.html?{qs}" if qs else "/admin.html"
    return RedirectResponse(url=target, status_code=301)


@app.get("/drool", include_in_schema=False)
def drool_page_redirect():
    """Redirect /drool to the static drool.html page (Shame Gallery)."""
    return RedirectResponse(url="/drool.html", status_code=301)


# ---------------------------------------------------------------------------
# Puppy Pouch share page
# ---------------------------------------------------------------------------

# OG image dimensions (Twitter / Open Graph recommended: 1200×630)
_OG_IMG_W = 1200
_OG_IMG_H = 630

# Vertical spacing constants used when laying out text inside bubbles
_LABEL_TEXT_GAP = 8   # pixels between a label line and the first body line
_LINE_SPACING   = 6   # pixels between consecutive body text lines

# Adaptive body font sizes tried in descending order; the first that fits is used
_ADAPTIVE_FONT_SIZES = [46, 40, 36, 32, 28]

# Fraction of the content area height allocated to the question bubble
_Q_BUBBLE_HEIGHT_RATIO = 0.56

# Brand colours matching the HTML card
_BG_OUTER   = (26,  26,  26)   # #1a1a1a – page background
_BG_CARD    = (36,  36,  36)   # #242424 – card background
_BG_Q       = (61,  32,  40)   # #3d2028 – question bubble
_BORDER_Q   = (196, 154, 159)  # #c49a9f
_BORDER_CARD= (61,  42,  46)   # #3d2a2e
_BG_A       = (44,  44,  44)   # #2c2c2c – answer bubble
_BORDER_A   = (74,  74,  74)   # #4a4a4a
_FG_MAIN    = (240, 230, 232)  # #f0e6e8
_FG_Q_LABEL = (196, 154, 159)  # #c49a9f
_FG_A_LABEL = (158, 126, 130)  # #9e7e82
_FG_FOOTER  = (106,  74,  78)  # #6a4a4e
_FG_TITLE   = (158, 126, 130)  # #9e7e82

# Pre-built 1×1 fallback PNG returned when a question isn't public/answered
def _make_fallback_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), _BG_OUTER).save(buf, format="PNG")
    return buf.getvalue()

_FALLBACK_PNG = _make_fallback_png()


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return the best available font at the requested size.

    Falls back to Pillow's built-in bitmap font when no TrueType font is found;
    that font ignores *size* and renders at a fixed small size.
    """
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    logging.warning(
        "No TrueType font found; falling back to Pillow default bitmap font "
        "(size parameter %d ignored – text may render very small).", size
    )
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap *text* so that each rendered line fits within *max_w* pixels.

    Words wider than *max_w* are split character-by-character to prevent overflow.
    """
    def _text_w(s: str) -> int:
        bb = draw.textbbox((0, 0), s, font=font)
        return bb[2] - bb[0]

    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        # Split overlong single words character-by-character
        if _text_w(word) > max_w:
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for ch in word:
                if _text_w(chunk + ch) <= max_w:
                    chunk += ch
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            if chunk:
                current = chunk
            continue

        test = (current + " " + word).strip()
        if _text_w(test) <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _truncate_line(line: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_w: int, draw: ImageDraw.ImageDraw) -> str:
    """Truncate *line* with an ellipsis so it fits within *max_w* pixels."""
    ellipsis = "…"

    def _text_w(s: str) -> int:
        bb = draw.textbbox((0, 0), s, font=font)
        return bb[2] - bb[0]

    if _text_w(line) <= max_w:
        return line
    # Binary-search for the longest prefix that fits with the ellipsis appended
    lo, hi = 0, len(line)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(line[:mid] + ellipsis) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return line[:lo] + ellipsis


def _generate_og_image(q_text: str, a_text: str) -> bytes:
    """Render a 1200×630 OG image with large text that fills the canvas."""
    img  = Image.new("RGB", (_OG_IMG_W, _OG_IMG_H), (16, 8, 12))
    draw = ImageDraw.Draw(img)

    # ── Gradient background (top dark → bottom slightly warmer) ─────────────
    for y in range(_OG_IMG_H):
        t = y / _OG_IMG_H
        draw.line([(0, y), (_OG_IMG_W, y)], fill=(
            int(16 + t * 9), int(8 + t * 5), int(12 + t * 9),
        ))

    # ── Layout constants ─────────────────────────────────────────────────────
    HEADER_H   = 74    # branded top bar
    FOOTER_H   = 46    # footer strip at bottom
    PAD_X      = 52    # left/right canvas margin
    PAD_V      = 18    # bubble top/bottom inner padding
    ACCENT_W   = 6     # left-edge accent bar width
    PAD_BX     = 20    # text left padding (after accent bar)
    PAD_BR     = 22    # text right padding
    BUBBLE_R   = 16    # corner radius
    LABEL_SIZE = 20
    LABEL_GAP  = 10    # gap between label row and body text
    LINE_GAP   = 8     # extra pixels between wrapped body lines
    BUBBLE_GAP = 18    # gap between Q and A bubbles

    bubble_w   = _OG_IMG_W - PAD_X * 2
    inner_w    = bubble_w - ACCENT_W - PAD_BX - PAD_BR

    content_top = HEADER_H + 14
    content_bot = _OG_IMG_H - FOOTER_H - 10
    content_h   = content_bot - content_top

    # ── Fixed fonts ──────────────────────────────────────────────────────────
    font_label  = _load_font(LABEL_SIZE, bold=True)
    font_brand  = _load_font(27, bold=True)
    font_domain = _load_font(20)
    font_footer = _load_font(21)

    def _fh(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
        bb = draw.textbbox((0, 0), "Ag", font=font)
        return bb[3] - bb[1]

    lq_h = _fh(font_label)
    la_h = _fh(font_label)

    # ── Adaptive body font: try largest size where both bubbles fit ──────────
    font_body = _load_font(28)
    for size in _ADAPTIVE_FONT_SIZES:
        f    = _load_font(size)
        lh   = _fh(f)
        step = lh + LINE_GAP
        q_ls = _wrap_text(q_text, f, inner_w, draw)
        a_ls = _wrap_text(a_text, f, inner_w, draw)
        q_bh = 2 * PAD_V + lq_h + LABEL_GAP + len(q_ls) * step
        a_bh = 2 * PAD_V + la_h + LABEL_GAP + len(a_ls) * step
        if q_bh + BUBBLE_GAP + a_bh <= content_h:
            font_body = f
            break

    lh   = _fh(font_body)
    step = lh + LINE_GAP

    q_lines = _wrap_text(q_text, font_body, inner_w, draw)
    a_lines = _wrap_text(a_text, font_body, inner_w, draw)

    # Truncate if even size-28 font overflows (very long text)
    q_bh_max = int(content_h * _Q_BUBBLE_HEIGHT_RATIO) - BUBBLE_GAP // 2
    a_bh_max = content_h - q_bh_max - BUBBLE_GAP
    max_q = max(1, (q_bh_max - 2 * PAD_V - lq_h - LABEL_GAP) // step)
    max_a = max(1, (a_bh_max - 2 * PAD_V - la_h - LABEL_GAP) // step)
    if len(q_lines) > max_q:
        q_lines = q_lines[:max_q]
        q_lines[-1] = _truncate_line(q_lines[-1], font_body, inner_w, draw)
    if len(a_lines) > max_a:
        a_lines = a_lines[:max_a]
        a_lines[-1] = _truncate_line(a_lines[-1], font_body, inner_w, draw)

    q_bh = 2 * PAD_V + lq_h + LABEL_GAP + len(q_lines) * step
    a_bh = 2 * PAD_V + la_h + LABEL_GAP + len(a_lines) * step

    # Vertically centre both bubbles in the content area
    total_bh = q_bh + BUBBLE_GAP + a_bh
    start_y  = content_top + max(0, (content_h - total_bh) // 2)

    # ── Header bar ───────────────────────────────────────────────────────────
    draw.rectangle([0, 0, _OG_IMG_W, HEADER_H], fill=(24, 10, 16))
    draw.line([(0, HEADER_H), (_OG_IMG_W, HEADER_H)], fill=(72, 28, 42), width=2)

    # Paw-dot accent
    dot_cx, dot_cy = 42, HEADER_H // 2
    draw.ellipse([dot_cx - 18, dot_cy - 18, dot_cx + 18, dot_cy + 18], fill=(82, 30, 46))
    draw.ellipse([dot_cx - 10, dot_cy - 10, dot_cx + 10, dot_cy + 10], fill=(212, 140, 162))

    draw.text((72, HEADER_H // 2 - 16), "PUPPY POUCH",   font=font_brand,  fill=(220, 158, 178))
    draw.text((72, HEADER_H // 2 +  8), "Anonymous Q&A", font=font_domain, fill=(128, 85, 100))

    site_text = "mochii.live"
    st_bb = draw.textbbox((0, 0), site_text, font=font_domain)
    draw.text(
        (_OG_IMG_W - PAD_X - (st_bb[2] - st_bb[0]), HEADER_H // 2 - 10),
        site_text, font=font_domain, fill=(152, 106, 122),
    )

    # ── Question bubble ──────────────────────────────────────────────────────
    qx1, qy1 = PAD_X, start_y
    qx2, qy2 = _OG_IMG_W - PAD_X, start_y + q_bh
    draw.rounded_rectangle([qx1, qy1, qx2, qy2], radius=BUBBLE_R,
                            fill=(58, 20, 30), outline=(200, 132, 154), width=2)
    draw.rounded_rectangle([qx1, qy1, qx1 + ACCENT_W, qy2], radius=BUBBLE_R,
                            fill=(220, 128, 154))

    ty = qy1 + PAD_V
    draw.text((qx1 + ACCENT_W + PAD_BX, ty), "QUESTION", font=font_label, fill=(200, 132, 154))
    ty += lq_h + LABEL_GAP
    for line in q_lines:
        draw.text((qx1 + ACCENT_W + PAD_BX, ty), line, font=font_body, fill=(248, 220, 228))
        ty += step

    # ── Answer bubble ────────────────────────────────────────────────────────
    ax1, ay1 = PAD_X, start_y + q_bh + BUBBLE_GAP
    ax2, ay2 = _OG_IMG_W - PAD_X, start_y + q_bh + BUBBLE_GAP + a_bh
    draw.rounded_rectangle([ax1, ay1, ax2, ay2], radius=BUBBLE_R,
                            fill=(30, 27, 33), outline=(70, 60, 70), width=1)
    draw.rounded_rectangle([ax1, ay1, ax1 + ACCENT_W, ay2], radius=BUBBLE_R,
                            fill=(100, 72, 88))

    ty = ay1 + PAD_V
    draw.text((ax1 + ACCENT_W + PAD_BX, ty), "ANSWER", font=font_label, fill=(144, 108, 124))
    ty += la_h + LABEL_GAP
    for line in a_lines:
        draw.text((ax1 + ACCENT_W + PAD_BX, ty), line, font=font_body, fill=(228, 215, 220))
        ty += step

    # ── Footer ───────────────────────────────────────────────────────────────
    footer_text = "Ask me anything at mochii.live"
    ft_bb = draw.textbbox((0, 0), footer_text, font=font_footer)
    ft_w  = ft_bb[2] - ft_bb[0]
    draw.text(
        ((_OG_IMG_W - ft_w) // 2, _OG_IMG_H - FOOTER_H + 12),
        footer_text, font=font_footer, fill=(96, 58, 72),
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def _html_escape(text: str) -> str:
    """Escape HTML special characters to prevent XSS in the share page."""
    return _html_lib.escape(text, quote=True)


def _render_404_html(heading: str, message: str) -> str:
    """Return a styled 404 HTML page using the site's dark pink theme."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>404 – Not Found 🐾 mochii.live</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Nunito', system-ui, sans-serif; background: #1a1a1a; color: #f0e6e8;
           min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 2rem 1rem; }}
    .card {{ width: 100%; max-width: 420px; background: #242424; border: 1px solid #3d2a2e;
            border-radius: 20px; padding: 2.5rem 2rem 2rem; box-shadow: 0 8px 40px rgba(232,174,183,0.14);
            text-align: center; }}
    .paw {{ font-size: 3rem; margin-bottom: 1rem; display: block; }}
    .error-code {{ font-size: .72rem; font-weight: 800; text-transform: uppercase; letter-spacing: .12em;
                  color: #9e7e82; margin-bottom: .6rem; }}
    h1 {{ font-size: 1.5rem; font-weight: 800; color: #e8aeb7; margin-bottom: .75rem; }}
    p {{ font-size: .92rem; color: #9e7e82; line-height: 1.55; margin-bottom: 1.75rem; }}
    a.btn {{ display: inline-block; padding: .65rem 1.5rem; background: #3d2028; border: 1px solid #e8aeb7;
            border-radius: 10px; color: #e8aeb7; font-family: inherit; font-size: .95rem; font-weight: 700;
            text-decoration: none; transition: background .15s, color .15s; }}
    a.btn:hover {{ background: #e8aeb7; color: #1a1a1a; }}
  </style>
</head>
<body>
  <div class="card" role="main">
    <span class="paw" aria-hidden="true">🐾</span>
    <p class="error-code">404 – Not Found</p>
    <h1>{_html_escape(heading)}</h1>
    <p>{_html_escape(message)}</p>
    <a class="btn" href="/">Back to mochii.live 🐾</a>
  </div>
</body>
</html>"""


@app.get("/q/{question_id}/og-image.png", response_class=None)
def question_og_image(
    question_id: str,
    db: sqlite3.Connection = Depends(get_db),
):
    """Return a dynamically generated PNG suitable for og:image / twitter:image."""
    row = db.execute(
        "SELECT text, answer, is_public FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()

    if not row or not row["is_public"] or not row["answer"]:
        # Return a minimal 1×1 PNG rather than an error status so scrapers
        # don't cache a 404 against the image URL.
        return Response(content=_FALLBACK_PNG, media_type="image/png")

    png_bytes = _generate_og_image(row["text"], row["answer"])
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/q/{question_id}", response_class=None)
def question_share_page(
    question_id: str,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Render a standalone pretty HTML card for a public answered question.

    Includes OpenGraph / Twitter Card meta tags so sharing on social media
    generates a rich preview.
    """
    row = db.execute(
        "SELECT id, text, answer, is_public FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()

    if not row or not row["is_public"] or not row["answer"]:
        return HTMLResponse(
            content=_render_404_html(
                "This note isn't here.",
                "That question hasn't been answered yet, doesn't exist, or isn't public.",
            ),
            status_code=404,
        )

    q_text = _html_escape(row["text"])
    a_text = _html_escape(row["answer"])
    base_url = BASE_URL or str(request.base_url).rstrip("/")
    # URL-encode the question_id for the share URL, then HTML-escape the full URL
    # to safely embed it in HTML attributes.
    page_url = _html_escape(f"{base_url}/q/{_url_quote(question_id, safe='')}")
    og_image_url = _html_escape(f"{base_url}/q/{_url_quote(question_id, safe='')}/og-image.png")
    og_title = "Puppy Pouch 🐾 – mochii.live"
    # Truncate for OG description (keep within typical 155-char limit after joining)
    _OG_PREVIEW_LEN = 120
    og_q = row["text"][:_OG_PREVIEW_LEN] + ("…" if len(row["text"]) > _OG_PREVIEW_LEN else "")
    og_a = row["answer"][:_OG_PREVIEW_LEN] + ("…" if len(row["answer"]) > _OG_PREVIEW_LEN else "")
    og_description = _html_escape(f"Q: {og_q}  A: {og_a}")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Puppy Pouch 🐾 – mochii.live</title>

  <!-- OpenGraph / Twitter Card -->
  <meta property="og:type"         content="website" />
  <meta property="og:url"          content="{page_url}" />
  <meta property="og:site_name"    content="mochii.live" />
  <meta property="og:locale"       content="en_US" />
  <meta property="og:title"        content="{og_title}" />
  <meta property="og:description"  content="{og_description}" />
  <meta property="og:image"        content="{og_image_url}" />
  <meta property="og:image:width"  content="{_OG_IMG_W}" />
  <meta property="og:image:height" content="{_OG_IMG_H}" />
  <meta name="twitter:card"        content="summary_large_image" />
  <meta name="twitter:title"       content="{og_title}" />
  <meta name="twitter:description" content="{og_description}" />
  <meta name="twitter:image"       content="{og_image_url}" />
  <link rel="canonical"            href="{page_url}" />

  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Nunito', system-ui, sans-serif;
      background: #1a1a1a;
      color: #f0e6e8;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }}

    .card {{
      width: 100%;
      max-width: 560px;
      background: #242424;
      border: 1px solid #3d2a2e;
      border-radius: 20px;
      padding: 2rem 1.75rem 1.5rem;
      box-shadow: 0 8px 40px rgba(232,174,183,0.14);
    }}

    .card-title {{
      font-size: .72rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #9e7e82;
      margin-bottom: 1.25rem;
    }}

    .bubble {{
      border-radius: 14px;
      padding: 1rem 1.25rem;
      font-size: 1rem;
      line-height: 1.55;
      margin-bottom: 1rem;
      word-break: break-word;
    }}

    .bubble-q {{
      background: #3d2028;
      border: 1px solid #c49a9f;
      color: #f5d5da;
    }}

    .bubble-q::before {{
      content: "🐾 Question";
      display: block;
      font-size: .7rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #c49a9f;
      margin-bottom: .5rem;
    }}

    .bubble-a {{
      background: #2c2c2c;
      border: 1px solid #4a4a4a;
      color: #e0d4d6;
    }}

    .bubble-a::before {{
      content: "💬 Answer";
      display: block;
      font-size: .7rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #9e7e82;
      margin-bottom: .5rem;
    }}

    .card-footer {{
      margin-top: .5rem;
      text-align: center;
      font-size: .8rem;
      color: #6a4a4e;
      font-weight: 700;
    }}

    .card-footer a {{
      color: #c49a9f;
      text-decoration: none;
    }}

    .card-footer a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="card" role="main">
    <p class="card-title">Puppy Pouch 🐾 Anonymous Q&amp;A</p>
    <div class="bubble bubble-q">{q_text}</div>
    <div class="bubble bubble-a">{a_text}</div>
    <p class="card-footer">Ask me anything at <a href="{_html_escape(base_url)}/anon">the Puppy Pouch</a> 🐾</p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Anonymous Q&A page  –  /anon  (also reached via anon.mochii.live/)
# ---------------------------------------------------------------------------

@app.get("/anon", response_class=None)
def anon_page(request: Request):
    """Standalone Puppy Pouch page: submit a question + browse all answered Q&A."""
    canonical = BASE_URL or str(request.base_url).rstrip("/")
    page_url  = _html_escape(f"{canonical}/anon")
    og_title  = "Puppy Pouch 🐾 – Ask mochii.live Anything"
    og_desc   = _html_escape(
        "Drop an anonymous question into the Puppy Pouch and browse every answered note."
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{og_title}</title>

  <!-- Primary meta -->
  <meta name="description" content="{og_desc}" />
  <link rel="canonical" href="{page_url}" />

  <!-- OpenGraph -->
  <meta property="og:type"        content="website" />
  <meta property="og:url"         content="{page_url}" />
  <meta property="og:site_name"   content="mochii.live" />
  <meta property="og:locale"      content="en_US" />
  <meta property="og:title"       content="{og_title}" />
  <meta property="og:description" content="{og_desc}" />

  <!-- Twitter / X Card -->
  <meta name="twitter:card"        content="summary" />
  <meta name="twitter:title"       content="{og_title}" />
  <meta name="twitter:description" content="{og_desc}" />

  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Nunito', system-ui, sans-serif;
      background: #1a1a1a;
      color: #f0e6e8;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2.5rem 1rem 3rem;
    }}

    .page-header {{
      width: 100%;
      max-width: 560px;
      margin-bottom: 1.75rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: .5rem;
    }}

    .page-header h1 {{
      font-size: 1.4rem;
      font-weight: 800;
      color: #e8aeb7;
    }}

    .back-link {{
      font-size: .82rem;
      color: #9e7e82;
      text-decoration: none;
      font-weight: 700;
      border: 1px solid #3d2a2e;
      border-radius: 8px;
      padding: .3rem .75rem;
      transition: border-color .2s, color .2s;
    }}
    .back-link:hover {{ border-color: #c49a9f; color: #e8aeb7; }}

    .card {{
      width: 100%;
      max-width: 560px;
      background: #242424;
      border: 1px solid #3d2a2e;
      border-radius: 20px;
      padding: 1.75rem 1.75rem 1.5rem;
      box-shadow: 0 8px 40px rgba(232,174,183,0.10);
      margin-bottom: 1.5rem;
    }}

    .card h2 {{
      font-size: 1rem;
      font-weight: 800;
      color: #e8aeb7;
      margin-bottom: .3rem;
    }}

    .card p.tagline {{
      font-size: .82rem;
      color: #9e7e82;
      margin-bottom: 1rem;
    }}

    textarea {{
      width: 100%;
      background: #1a1a1a;
      border: 1px solid #3d2a2e;
      border-radius: 8px;
      color: #f0e6e8;
      font-family: inherit;
      font-size: .92rem;
      padding: .65rem .85rem;
      resize: vertical;
      min-height: 90px;
      outline: none;
      transition: border-color .2s;
    }}
    textarea:focus {{ border-color: #c49a9f; }}

    .note-footer-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: .5rem;
      gap: .75rem;
    }}

    #char-count {{
      font-size: .75rem;
      color: #6a4a4e;
      white-space: nowrap;
    }}
    #char-count.warn {{ color: #f0b040; }}
    #char-count.over {{ color: #f87171; }}

    #send-btn {{
      padding: .5rem 1.2rem;
      background: #3d2028;
      border: 1px solid #e8aeb7;
      border-radius: 8px;
      color: #e8aeb7;
      font-family: inherit;
      font-size: .88rem;
      font-weight: 700;
      cursor: pointer;
      transition: background .15s, color .15s;
      white-space: nowrap;
    }}
    #send-btn:hover:not(:disabled) {{ background: #e8aeb7; color: #1a1a1a; }}
    #send-btn:disabled {{ opacity: .5; cursor: not-allowed; }}

    #send-msg {{
      font-size: .8rem;
      min-height: 1em;
      margin-top: .4rem;
    }}
    #send-msg.success {{ color: #67d399; }}
    #send-msg.error   {{ color: #f87171; }}

    /* Q&A feed */
    .feed-header {{
      font-size: .72rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .09em;
      color: #9e7e82;
      margin-bottom: 1rem;
    }}

    .qa-item {{
      margin-bottom: 1rem;
    }}

    .bubble {{
      border-radius: 12px;
      padding: .85rem 1rem;
      font-size: .92rem;
      line-height: 1.55;
      margin-bottom: .35rem;
      word-break: break-word;
    }}

    .bubble-q {{
      background: #3d2028;
      border: 1px solid #c49a9f;
      color: #f5d5da;
    }}
    .bubble-q::before {{
      content: "🐾 Question";
      display: block;
      font-size: .68rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #c49a9f;
      margin-bottom: .4rem;
    }}

    .bubble-a {{
      background: #2c2c2c;
      border: 1px solid #4a4a4a;
      color: #e0d4d6;
    }}
    .bubble-a::before {{
      content: "💬 Answer";
      display: block;
      font-size: .68rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #9e7e82;
      margin-bottom: .4rem;
    }}

    .share-link {{
      display: inline-block;
      margin-top: .3rem;
      font-size: .75rem;
      color: #9e7e82;
      text-decoration: none;
      font-weight: 700;
      transition: color .2s;
    }}
    .share-link:hover {{ color: #c49a9f; }}

    #feed-loading {{
      font-size: .88rem;
      color: #9e7e82;
      text-align: center;
      padding: 1rem 0;
    }}

    #empty-feed {{
      display: none;
      font-size: .88rem;
      color: #5a3a3e;
      text-align: center;
      padding: 1rem 0;
    }}
  </style>
</head>
<body>
  <div class="page-header">
    <h1>🐾 Puppy Pouch</h1>
    <a class="back-link" href="/">← Back to mochii.live</a>
  </div>

  <!-- Submit form -->
  <div class="card">
    <h2>Drop a Note 🐾</h2>
    <p class="tagline">Ask anything anonymously – no sign-in needed.</p>
    <textarea id="note-textarea" maxlength="280" placeholder="What's on your mind? 🐾" aria-label="Your question"></textarea>
    <div class="note-footer-row">
      <span id="char-count">0 / 280</span>
      <button id="send-btn" disabled>Send 🐾</button>
    </div>
    <p id="send-msg" role="alert" aria-live="polite"></p>
  </div>

  <!-- Answered Q&A feed -->
  <div class="card">
    <p class="feed-header">Answered Notes 🐾</p>
    <p id="feed-loading">Loading… 🐾</p>
    <p id="empty-feed">No answered notes yet – be the first to ask! 🐾</p>
    <div id="qa-list"></div>
  </div>

  <script>
    const MAX = 280;
    const textarea  = document.getElementById('note-textarea');
    const charCount = document.getElementById('char-count');
    const sendBtn   = document.getElementById('send-btn');
    const sendMsg   = document.getElementById('send-msg');

    textarea.addEventListener('input', () => {{
      const len = textarea.value.length;
      charCount.textContent = `${{len}} / ${{MAX}}`;
      charCount.className = len >= MAX ? 'over' : len >= MAX * 0.85 ? 'warn' : '';
      sendBtn.disabled = len === 0 || len > MAX;
    }});

    sendBtn.addEventListener('click', async () => {{
      const text = textarea.value.trim();
      if (!text || text.length > MAX) return;
      sendBtn.disabled = true;
      sendMsg.textContent = '';
      sendMsg.className = '';
      try {{
        const resp = await fetch('/api/questions', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ text }}),
        }});
        if (resp.ok) {{
          textarea.value = '';
          charCount.textContent = `0 / ${{MAX}}`;
          charCount.className = '';
          sendMsg.textContent = '🐾 Your note was sent!';
          sendMsg.className = 'success';
          setTimeout(() => {{ sendMsg.textContent = ''; sendMsg.className = ''; }}, 3500);
        }} else {{
          const data = await resp.json().catch(() => ({{}}));
          sendMsg.textContent = data.detail || 'Something went wrong. Please try again.';
          sendMsg.className = 'error';
          sendBtn.disabled = false;
        }}
      }} catch {{
        sendMsg.textContent = 'Could not send. Please try again.';
        sendMsg.className = 'error';
        sendBtn.disabled = textarea.value.trim().length === 0;
      }}
    }});

    function esc(str) {{
      return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    (async function loadFeed() {{
      try {{
        const resp = await fetch('/api/questions/public');
        document.getElementById('feed-loading').style.display = 'none';
        if (!resp.ok) return;
        const qs = await resp.json();
        if (!qs.length) {{
          document.getElementById('empty-feed').style.display = 'block';
          return;
        }}
        const list = document.getElementById('qa-list');
        qs.forEach(q => {{
          const div = document.createElement('div');
          div.className = 'qa-item';
          div.innerHTML =
            `<div class="bubble bubble-q">${{esc(q.text)}}</div>` +
            `<div class="bubble bubble-a">${{esc(q.answer)}}</div>` +
            `<a class="share-link" href="/q/${{encodeURIComponent(q.id)}}" target="_blank" rel="noopener">🔗 Share this note</a>`;
          list.appendChild(div);
        }});
      }} catch {{
        document.getElementById('feed-loading').textContent = 'Could not load notes.';
      }}
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Links page  –  /links  (also reached via links.mochii.live/)
# ---------------------------------------------------------------------------

@app.get("/links", response_class=None)
def links_page(request: Request, db: sqlite3.Connection = Depends(get_db)):
    """Render a Linktree-style page of all active links from the database."""
    canonical = BASE_URL or str(request.base_url).rstrip("/")
    page_url  = _html_escape(f"{canonical}/links")
    og_title  = "mochii.live 🐾 – Links"
    og_desc   = _html_escape("All the links you need in one place.")

    rows = db.execute(
        """
        SELECT title, url, emoji
        FROM links
        WHERE is_active = 1
        ORDER BY sort_order ASC, id ASC
        """
    ).fetchall()

    link_items_html = ""
    for row in rows:
        emoji = _html_escape(row["emoji"] or "")
        title = _html_escape(row["title"])
        url   = _html_escape(row["url"])
        label = f"{emoji} {title}".strip() if emoji else title
        link_items_html += (
            f'<a class="link-btn" href="{url}" target="_blank" rel="noopener noreferrer">'
            f'{label}</a>\n'
        )

    if not link_items_html:
        link_items_html = '<p class="empty-msg">No links yet – check back soon 🐾</p>\n'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{og_title}</title>

  <!-- Primary meta -->
  <meta name="description" content="{og_desc}" />
  <link rel="canonical" href="{page_url}" />

  <!-- OpenGraph -->
  <meta property="og:type"        content="website" />
  <meta property="og:url"         content="{page_url}" />
  <meta property="og:site_name"   content="mochii.live" />
  <meta property="og:locale"      content="en_US" />
  <meta property="og:title"       content="{og_title}" />
  <meta property="og:description" content="{og_desc}" />

  <!-- Twitter / X Card -->
  <meta name="twitter:card"        content="summary" />
  <meta name="twitter:title"       content="{og_title}" />
  <meta name="twitter:description" content="{og_desc}" />

  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Nunito', system-ui, sans-serif;
      background: #1a1a1a;
      color: #f0e6e8;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2.5rem 1rem;
    }}

    .container {{
      width: 100%;
      max-width: 480px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: .9rem;
    }}

    .site-name {{
      font-size: 1.8rem;
      font-weight: 800;
      color: #e8aeb7;
      text-align: center;
    }}

    .tagline {{
      font-size: .88rem;
      color: #9e7e82;
      text-align: center;
      margin-top: -.25rem;
      margin-bottom: .5rem;
    }}

    .link-btn {{
      display: block;
      width: 100%;
      padding: .85rem 1.25rem;
      background: #242424;
      border: 1px solid #3d2a2e;
      border-radius: 14px;
      color: #f0e6e8;
      font-family: inherit;
      font-size: 1rem;
      font-weight: 700;
      text-align: center;
      text-decoration: none;
      transition: background .15s, border-color .15s, color .15s, box-shadow .15s;
      box-shadow: 0 2px 12px rgba(232,174,183,0.06);
    }}
    .link-btn:hover {{
      background: #3d2028;
      border-color: #e8aeb7;
      color: #e8aeb7;
      box-shadow: 0 4px 20px rgba(232,174,183,0.18);
    }}

    .empty-msg {{
      font-size: .9rem;
      color: #5a3a3e;
      text-align: center;
    }}

    .page-footer {{
      margin-top: 1.5rem;
      font-size: .75rem;
      color: #4a3234;
      text-align: center;
    }}
    .page-footer a {{
      color: #6a4a4e;
      text-decoration: none;
    }}
    .page-footer a:hover {{ color: #c49a9f; }}
  </style>
</head>
<body>
  <div class="container" role="main">
    <p class="site-name">🐾 mochii.live</p>
    <p class="tagline">All the links in one place.</p>
    {link_items_html}
    <p class="page-footer"><a href="/">← Back to mochii.live</a></p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
