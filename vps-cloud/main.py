import io
import logging
import os
import sys
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse, quote as _url_quote

import httpx
import html as _html_lib
from passlib.context import CryptContext

from PIL import Image, ImageDraw, ImageFont
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import DATABASE_PATH, get_db, get_db_connection, get_setting, set_setting
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
from routers.twitter_auth import router as twitter_auth_router
from routers.spotify import router as spotify_router
from routers.age_gate import router as age_gate_router
from routers.tpe import (
    device_router as tpe_device_router,
    admin_router as tpe_admin_router,
    migrate_tpe,
)
from redis_client import close_redis
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

# ---------------------------------------------------------------------------
# Configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")
GO2RTC_TIMEOUT: float = 15.0

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# Discord role → access-level configuration
#
# Set DISCORD_GUILD_ID plus one or more role-ID variables so that the login
# endpoint can look up the authenticated user's server roles and map them to
# the correct access level.  Roles are evaluated highest-first; the first
# match wins.
#
#   DISCORD_GUILD_ID       – Discord server (guild) to query for member roles.
#   DISCORD_ROLE_LEVEL_3   – Role ID that grants access_level 3 (tier 2+).
#   DISCORD_ROLE_LEVEL_2   – Role ID that grants access_level 2 (tier 1).
#   DISCORD_ROLE_LEVEL_1   – Role ID that grants access_level 1 (free follower).
#
# If DISCORD_GUILD_ID is not set, or the user has no linked Discord account,
# the stored access_level is returned unchanged.
# ---------------------------------------------------------------------------
DISCORD_GUILD_ID: str = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_ROLE_LEVEL_3: str = os.environ.get("DISCORD_ROLE_LEVEL_3", "")
DISCORD_ROLE_LEVEL_2: str = os.environ.get("DISCORD_ROLE_LEVEL_2", "")
DISCORD_ROLE_LEVEL_1: str = os.environ.get("DISCORD_ROLE_LEVEL_1", "")

# Set MOCK_AUTH=true to bypass password verification and issue a fake token.
# Useful for demos when Discord is not yet configured.
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
# Age gate configuration
#
# AGE_GATE_ENABLED – set to "true" to require age verification before any
#   page is served.  Defaults to false so existing deployments are unaffected
#   until the operator explicitly opts in.
# ---------------------------------------------------------------------------
_age_gate_raw = os.environ.get("AGE_GATE_ENABLED", "false")
AGE_GATE_ENABLED: bool = _age_gate_raw.lower() == "true"

# Paths that are always accessible without age verification.
# Anything starting with one of these prefixes is exempt.
_AGE_GATE_EXEMPT_PREFIXES = (
    "/api/",
    "/ws/",
    "/age-gate",
    "/static/",
    "/favicon.ico",
    "/sw.js",
    "/manifest.json",
    "/offline.html",
)

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
    # Enable WAL journal mode for better concurrent read/write performance.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT    PRIMARY KEY,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL DEFAULT '',
            access_level  INTEGER NOT NULL DEFAULT 0
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
    # New installs: platform column has no CHECK constraint so new platforms
    # (e.g. 'bluesky') can be added without schema changes.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drool_archive (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            platform     TEXT    NOT NULL,
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_pending (
            token      TEXT PRIMARY KEY,
            secret     TEXT NOT NULL,
            expires_at TEXT NOT NULL
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
    # Migration: existing drool_archive tables may carry a CHECK constraint
    # that rejects platforms other than 'reddit' and 'twitter'.  SQLite does
    # not support ALTER COLUMN, so we recreate the table when needed.
    try:
        conn.execute(
            "INSERT INTO drool_archive (platform, original_url, timestamp)"
            " VALUES ('bluesky', '__bsky_probe__', 'x')"
        )
        conn.execute("DELETE FROM drool_archive WHERE original_url = '__bsky_probe__'")
    except Exception:
        # CHECK constraint violation – rebuild without the constraint.
        conn.execute("DROP TABLE IF EXISTS drool_archive_v2")
        conn.execute(
            """
            CREATE TABLE drool_archive_v2 (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                platform     TEXT    NOT NULL,
                original_url TEXT    NOT NULL UNIQUE,
                media_url    TEXT,
                text_content TEXT,
                view_count   INTEGER NOT NULL DEFAULT 0,
                timestamp    TEXT    NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO drool_archive_v2 SELECT * FROM drool_archive")
        conn.execute("DROP TABLE drool_archive")
        conn.execute("ALTER TABLE drool_archive_v2 RENAME TO drool_archive")
    # Idempotent migration: add media_urls column for multi-image/video support.
    try:
        conn.execute("ALTER TABLE drool_archive ADD COLUMN media_urls TEXT")
    except Exception:
        pass  # column already exists
    # Idempotent migration: add username/password_hash columns.
    # Column names and type definitions are hardcoded string literals (not
    # derived from user input), so the f-string interpolation is safe here.
    for _col, _defn in [
        ("username",      "TEXT NOT NULL DEFAULT ''"),
        ("password_hash", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_defn}")
        except Exception:
            pass  # column already exists
    # Indexes for analytics date-range queries (idempotent – safe to re-run)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_camera_service_logs_accessed_at ON camera_service_logs(accessed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activations_activated_at ON activations(activated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_questions_created_at ON questions(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drool_archive_timestamp ON drool_archive(timestamp)"
    )
    # ── Age verification table ────────────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS age_verifications (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_id  TEXT    NOT NULL UNIQUE,
            session_token    TEXT    NOT NULL UNIQUE,
            idswyft_user_id  TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'pending',
            created_at       TEXT    NOT NULL,
            verified_at      TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_age_verifications_session ON age_verifications(session_token)"
    )
    # Idempotent migration: add provider column for multi-provider age-gate support.
    try:
        conn.execute("ALTER TABLE age_verifications ADD COLUMN provider TEXT NOT NULL DEFAULT 'idswyft'")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Idempotent migration: add rtmp_key column for OBS/RTMP streaming.
    try:
        conn.execute("ALTER TABLE cameras ADD COLUMN rtmp_key TEXT")
    except Exception:
        pass  # column already exists
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_rtmp_key ON cameras(rtmp_key) WHERE rtmp_key IS NOT NULL"
    )
    # ── Chat messages table ───────────────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL,
            username   TEXT    NOT NULL,
            message    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(created_at)"
    )
    # ── Content drops (file vault) table ─────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_drops (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            title                TEXT    NOT NULL,
            description          TEXT,
            file_url             TEXT    NOT NULL,
            minimum_access_level INTEGER NOT NULL DEFAULT 1,
            sort_order           INTEGER NOT NULL DEFAULT 0,
            is_active            INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT    NOT NULL
        )
        """
    )
    # ── VODs table ────────────────────────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vods (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            title                TEXT    NOT NULL,
            description          TEXT,
            file_url             TEXT    NOT NULL,
            thumbnail_url        TEXT,
            minimum_access_level INTEGER NOT NULL DEFAULT 1,
            duration_seconds     INTEGER,
            is_active            INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT    NOT NULL
        )
        """
    )
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
            "SELECT stream_slug, rtsp_url, tapo_ip, tapo_username, tapo_password, rtmp_key FROM cameras"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    async with httpx.AsyncClient(timeout=5.0) as client:
        for row in rows:
            tapo_ip = row["tapo_ip"]
            rtmp_key = row["rtmp_key"]
            if tapo_ip:
                user = _url_quote(row["tapo_username"] or "", safe="")
                pwd  = _url_quote(row["tapo_password"]  or "", safe="")
                effective_url = f"rtsp://{user}:{pwd}@{tapo_ip}/stream1"
            elif rtmp_key:
                effective_url = f"rtmp://localhost:1935/{rtmp_key}"
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
# Discord role → access-level helper
# ---------------------------------------------------------------------------


async def _fetch_discord_access_level(discord_id: str) -> Optional[int]:
    """Query the Discord Bot API for the user's guild roles and map to access_level.

    Returns the highest matching access level (0–3), or None if the guild or
    bot token is not configured, or the member is not in the guild.
    """
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token or not DISCORD_GUILD_ID:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{discord_id}",
                headers={"Authorization": f"Bot {bot_token}"},
            )
    except httpx.RequestError as exc:
        logger.warning("Discord guild member fetch failed for %s: %s", discord_id, exc)
        return None

    if resp.status_code == 404:
        # User is not in the guild → no access
        return 0

    if resp.status_code != 200:
        logger.warning(
            "Discord guild member lookup returned %s for discord_id=%s",
            resp.status_code,
            discord_id,
        )
        return None

    member_roles: list[str] = resp.json().get("roles", [])

    # Evaluate highest matching role, tier 3 first
    for level, role_id in [
        (3, DISCORD_ROLE_LEVEL_3),
        (2, DISCORD_ROLE_LEVEL_2),
        (1, DISCORD_ROLE_LEVEL_1),
    ]:
        if role_id and role_id in member_roles:
            return level

    return 0


# ---------------------------------------------------------------------------
# Idswyft webhook auto-registration
# ---------------------------------------------------------------------------


async def _register_idswyft_webhook() -> None:
    """Register our age-gate webhook with idswyft on startup (idempotent).

    Requires IDSWYFT_API_KEY and BASE_URL to be set.  If either is missing the
    step is skipped and the operator must register the webhook manually via the
    idswyft developer portal at http://localhost:8090.
    """
    from routers.age_gate import IDSWYFT_API_URL, IDSWYFT_API_KEY, AGE_GATE_ENABLED
    if not AGE_GATE_ENABLED:
        return
    if not IDSWYFT_API_KEY:
        logger.info(
            "IDSWYFT_API_KEY not set – skipping automatic webhook registration. "
            "Create an API key via the idswyft portal (http://localhost:8090) and set it "
            "in the IDSWYFT_API_KEY environment variable."
        )
        return
    if not BASE_URL:
        logger.info(
            "BASE_URL not set – skipping automatic idswyft webhook registration. "
            "Set BASE_URL (e.g. http://backend:8000) for automatic registration."
        )
        return

    webhook_url = f"{BASE_URL.rstrip('/')}/api/age-gate/webhook"
    from routers.age_gate import IDSWYFT_WEBHOOK_SECRET
    payload: dict = {"url": webhook_url, "is_sandbox": False}
    if IDSWYFT_WEBHOOK_SECRET:
        payload["secret_token"] = IDSWYFT_WEBHOOK_SECRET

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Check if already registered
            list_resp = await client.get(
                f"{IDSWYFT_API_URL}/api/webhooks",
                headers={"X-API-Key": IDSWYFT_API_KEY},
            )
            if list_resp.is_success:
                existing = [w for w in list_resp.json().get("webhooks", []) if w.get("url") == webhook_url]
                if existing:
                    logger.info("Idswyft webhook already registered: %s", webhook_url)
                    return

            resp = await client.post(
                f"{IDSWYFT_API_URL}/api/webhooks/register",
                json=payload,
                headers={"X-API-Key": IDSWYFT_API_KEY, "Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        logger.warning("Could not register idswyft webhook on startup: %s", exc)
        return

    if resp.is_success:
        logger.info("Idswyft webhook registered: %s", webhook_url)
    else:
        logger.warning(
            "Idswyft webhook registration returned %s: %s", resp.status_code, resp.text
        )


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
            "MOCK_AUTH is enabled. /api/auth/login will issue a fake access_level=3 token "
            "without password verification. Do NOT use this in production."
        )
    if not DISCORD_GUILD_ID:
        logger.warning(
            "DISCORD_GUILD_ID is not set. Access levels will not be synced from Discord "
            "roles on login. Set DISCORD_GUILD_ID and DISCORD_ROLE_LEVEL_* to enable "
            "automatic role-based tier assignment."
        )
    init_db()
    migrate_tpe(get_db_connection())
    await _sync_cameras_to_go2rtc()
    start_drool_scheduler()
    await register_metadata_schema()
    await _register_idswyft_webhook()
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
# Auth endpoints – username / password
# ---------------------------------------------------------------------------


class _RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(..., min_length=8, max_length=128)


class _LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register", status_code=201)
def auth_register(body: _RegisterRequest, db: sqlite3.Connection = Depends(get_db)):
    """Create a new site account.

    Usernames are case-insensitive and may only contain letters, numbers,
    underscores, and hyphens (3–32 characters).  New accounts start at
    access_level=0; a Discord role link is required to gain tier access.
    """
    username_lower = body.username.lower()
    existing = db.execute(
        "SELECT id FROM users WHERE username = ?", (username_lower,)
    ).fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken.",
        )

    user_id = secrets.token_hex(16)
    pw_hash = _hash_password(body.password)
    db.execute(
        "INSERT INTO users (id, username, password_hash, access_level) VALUES (?, ?, ?, 0)",
        (user_id, username_lower, pw_hash),
    )
    db.commit()
    logger.info("New user registered: username=%s id=%s", username_lower, user_id)
    return {"message": "Account created. Link your Discord to unlock tier access."}


@app.post("/api/auth/login")
async def auth_login(
    body: _LoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate with username and password.

    If MOCK_AUTH is enabled, any credentials are accepted and access_level=3
    is issued.  Otherwise the password is verified and, if the account has a
    linked Discord, guild roles are queried to refresh the stored access_level
    before issuing the JWT.
    """
    # DB setting takes precedence over the env var for runtime toggle.
    db_mock_auth = get_setting(db, "mock_auth")
    effective_mock_auth = (
        db_mock_auth.lower() == "true" if db_mock_auth is not None else MOCK_AUTH
    )

    if effective_mock_auth:
        mock_token = create_access_token(
            {"sub": "mock-user", "access_level": 3},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        return {"access_token": mock_token, "token_type": "bearer"}

    username_lower = body.username.lower()
    row = db.execute(
        "SELECT id, password_hash, access_level FROM users WHERE username = ?",
        (username_lower,),
    ).fetchone()

    # Use a valid bcrypt hash for the dummy comparison to ensure constant-time
    # behaviour regardless of whether the username exists in the database.
    _DUMMY_HASH = "$2b$12$gJZZEZSwdcoCzIFtb1WvKeXE5vcZdz52cLCR/0nDLQJwukVQwKxx."
    stored_hash = row["password_hash"] if row else _DUMMY_HASH
    password_ok = _verify_password(body.password, stored_hash)

    if not row or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    user_id: str = row["id"]
    access_level: int = row["access_level"]

    # Refresh access_level from Discord guild roles if the account is linked.
    discord_row = db.execute(
        "SELECT discord_id FROM discord_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if discord_row:
        discord_access = await _fetch_discord_access_level(discord_row["discord_id"])
        if discord_access is not None and discord_access != access_level:
            access_level = discord_access
            db.execute(
                "UPDATE users SET access_level = ? WHERE id = ?",
                (access_level, user_id),
            )
            db.commit()
            logger.info(
                "Access level updated from Discord roles: user_id=%s level=%s",
                user_id,
                access_level,
            )

    site_token = create_access_token(
        {"sub": user_id, "access_level": access_level},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": site_token, "token_type": "bearer"}


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
    user_id: str = current_user["user_id"]

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


@app.post("/api/webrtc")
async def proxy_webrtc(
    request: Request,
    src: str,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Proxy the WebRTC SDP exchange to go2rtc, enforcing stream-level access control."""
    access_level: int = current_user["access_level"]

    row = db.execute(
        "SELECT id FROM cameras WHERE stream_slug = ? AND minimum_access_level <= ?",
        (src, access_level),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Access denied to this stream")

    body = await request.body()
    go2rtc_url = (
        f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/webrtc"
        f"?src={_url_quote(src, safe='')}"
    )

    async with httpx.AsyncClient(timeout=GO2RTC_TIMEOUT) as client:
        try:
            resp = await client.post(
                go2rtc_url,
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            logger.error("go2rtc request error for src=%s: %s", src, exc)
            raise HTTPException(status_code=502, detail="Unable to reach stream service")

    if not resp.is_success:
        logger.error("go2rtc returned %s for src=%s", resp.status_code, src)
        raise HTTPException(status_code=502, detail="Stream service returned an error")

    return Response(content=resp.content, media_type="text/plain")


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
app.include_router(twitter_auth_router)
app.include_router(spotify_router)
app.include_router(age_gate_router)
app.include_router(tpe_device_router)
app.include_router(tpe_admin_router)

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


@app.middleware("http")
async def age_gate_middleware(request: Request, call_next):
    """Redirect unverified visitors to the age-gate page.

    Checks the HttpOnly ``age_verified`` cookie.  If it is absent (or not
    equal to "1") *and* the request is for a page-level resource (not an API
    call, static asset, or the age-gate itself), the user is redirected to
    ``/age-gate`` so they can complete identity verification.

    The gate is bypassed entirely when ``AGE_GATE_ENABLED`` is false.
    """
    if not AGE_GATE_ENABLED:
        return await call_next(request)

    path = request.url.path

    # Exempt paths – always let these through
    for prefix in _AGE_GATE_EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return await call_next(request)

    # Check the age_verified cookie
    if request.cookies.get("age_verified") == "1":
        return await call_next(request)

    # Redirect to the age gate, preserving the originally-requested URL as
    # a query parameter so the gate can send the user back after verification.
    from fastapi.responses import RedirectResponse as _RR
    from urllib.parse import quote as _q
    return _RR(url=f"/age-gate?next={_q(str(request.url), safe='')}", status_code=302)


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


@app.get("/spotify", include_in_schema=False)
def spotify_page():
    """Serve the Spotify now-playing page at /spotify (no .html needed)."""
    return FileResponse("static/spotify.html")


@app.get("/age-gate", include_in_schema=False)
def age_gate_page():
    """Serve the age verification landing page."""
    return FileResponse("static/age-gate.html")


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
    home_url = _html_escape(BASE_URL or "/")
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
    <a class="btn" href="{home_url}">Back to mochii.live 🐾</a>
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
    <a class="back-link" href="{_html_escape(canonical)}">← Back to mochii.live</a>
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

    product_count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]

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

    .shop-link {{
      border-color: #6a3040;
      background: #2a1520;
    }}
    .shop-link:hover {{
      background: #3d2028;
      border-color: #e8aeb7;
      color: #e8aeb7;
      box-shadow: 0 4px 20px rgba(232,174,183,0.18);
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
    {'<a class="link-btn shop-link" href="https://shop.mochii.live" target="_blank" rel="noopener noreferrer">🛒 The Pack Shop</a>' if product_count else ''}
    {link_items_html}
    <p class="page-footer"><a href="{_html_escape(canonical)}">← Back to mochii.live</a></p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Stream status (viewer counts)
# ---------------------------------------------------------------------------


@app.get("/api/stream-status")
async def get_stream_status():
    """Return live status and viewer counts for all cameras by querying go2rtc."""
    go2rtc_base = f"http://{GO2RTC_HOST}:{GO2RTC_PORT}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{go2rtc_base}/api/streams")
        if not resp.is_success:
            return JSONResponse({"streams": {}})
        data = resp.json()
    except Exception:
        return JSONResponse({"streams": {}})

    result = {}
    for stream_name, info in data.items():
        producers = info.get("producers", [])
        consumers = info.get("consumers", [])
        is_live = any(p.get("state") == "running" for p in producers)
        viewer_count = len(consumers)
        result[stream_name] = {"is_live": is_live, "viewer_count": viewer_count}
    return JSONResponse({"streams": result})


# ---------------------------------------------------------------------------
# SEO – robots.txt + sitemap.xml
# ---------------------------------------------------------------------------


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    base = BASE_URL.rstrip("/") if BASE_URL else ""
    sitemap_line = f"\nSitemap: {base}/sitemap.xml" if base else ""
    content = f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/{sitemap_line}"
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(db: sqlite3.Connection = Depends(get_db)):
    base = (BASE_URL or "").rstrip("/")
    urls = [base + "/", base + "/links"]
    rows = db.execute(
        "SELECT id, created_at FROM questions WHERE is_public = 1 ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    url_entries = ""
    for u in urls:
        url_entries += f"  <url><loc>{_html_escape(u)}</loc></url>\n"
    for row in rows:
        loc = _html_escape(f"{base}/q/{row['id']}")
        lastmod = row["created_at"][:10] if row["created_at"] else ""
        url_entries += f"  <url><loc>{loc}</loc>"
        if lastmod:
            url_entries += f"<lastmod>{lastmod}</lastmod>"
        url_entries += "</url>\n"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_entries}</urlset>"""
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Stream goal
# ---------------------------------------------------------------------------


@app.get("/api/stream/goal")
def get_stream_goal(db: sqlite3.Connection = Depends(get_db)):
    enabled = get_setting(db, "tip_goal_enabled", "false") == "true"
    label = get_setting(db, "tip_goal_label", "Stream Goal")
    try:
        target = int(get_setting(db, "tip_goal_target_cents", "0") or "0")
        current = int(get_setting(db, "tip_goal_current_cents", "0") or "0")
    except ValueError:
        target, current = 0, 0
    return JSONResponse({"enabled": enabled, "label": label, "target_cents": target, "current_cents": current})


# ---------------------------------------------------------------------------
# Stream schedule
# ---------------------------------------------------------------------------


@app.get("/api/schedule")
def get_schedule(db: sqlite3.Connection = Depends(get_db)):
    import json as _json
    raw = get_setting(db, "stream_schedule", None)
    if raw:
        try:
            return JSONResponse({"schedule": _json.loads(raw)})
        except Exception:
            pass
    return JSONResponse({"schedule": []})


# ---------------------------------------------------------------------------
# File vault
# ---------------------------------------------------------------------------


@app.get("/api/vault")
def get_vault(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    access_level = current_user["access_level"]
    rows = db.execute(
        """SELECT id, title, description, file_url, minimum_access_level, sort_order, created_at
           FROM content_drops
           WHERE is_active = 1 AND minimum_access_level <= ?
           ORDER BY sort_order, id DESC""",
        (access_level,),
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# VOD gallery
# ---------------------------------------------------------------------------


@app.get("/api/vods")
def get_vods(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    access_level = current_user["access_level"]
    rows = db.execute(
        """SELECT id, title, description, file_url, thumbnail_url, minimum_access_level, duration_seconds, created_at
           FROM vods WHERE is_active = 1 AND minimum_access_level <= ?
           ORDER BY id DESC""",
        (access_level,),
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Subscriber chat WebSocket
# ---------------------------------------------------------------------------


class _ChatManager:
    def __init__(self) -> None:
        self._connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, data: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_chat_manager = _ChatManager()
_CHAT_MAX_MESSAGES = 200


@app.get("/api/chat/messages")
def get_chat_messages(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    rows = db.execute(
        "SELECT id, username, message, created_at FROM chat_messages ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return JSONResponse([dict(r) for r in reversed(rows)])


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket, token: str = ""):
    import jwt as _pyjwt
    try:
        payload = _pyjwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
        access_level = payload.get("access_level", 0)
    except Exception:
        await websocket.close(code=4001)
        return

    if not user_id or access_level < 1:
        await websocket.close(code=4001)
        return

    db = get_db_connection()
    try:
        row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            await websocket.close(code=4001)
            return
        username = row["username"]

        await _chat_manager.connect(websocket)
        recent = db.execute(
            "SELECT id, username, message, created_at FROM chat_messages ORDER BY id DESC LIMIT 50"
        ).fetchall()
        await websocket.send_json({"type": "history", "messages": [dict(r) for r in reversed(recent)]})

        try:
            while True:
                data = await websocket.receive_text()
                msg_text = data.strip()[:500]
                if not msg_text:
                    continue
                now = datetime.now(timezone.utc).isoformat()
                cur = db.execute(
                    "INSERT INTO chat_messages (user_id, username, message, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, username, msg_text, now),
                )
                db.commit()
                db.execute(
                    "DELETE FROM chat_messages WHERE id NOT IN (SELECT id FROM chat_messages ORDER BY id DESC LIMIT ?)",
                    (_CHAT_MAX_MESSAGES,),
                )
                db.commit()
                await _chat_manager.broadcast({
                    "type": "message",
                    "id": cur.lastrowid,
                    "username": username,
                    "message": msg_text,
                    "created_at": now,
                })
        except WebSocketDisconnect:
            _chat_manager.disconnect(websocket)
    finally:
        db.close()


app.mount("/", StaticFiles(directory="static", html=True), name="static")
