import io
import asyncio
import logging
import os
import re as _re
import shutil
import sys
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, quote as _url_quote

import httpx
import html as _html_lib

from PIL import Image, ImageDraw, ImageFont
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import DATABASE_PATH, get_db, get_db_connection, get_setting
from stream_utils import is_producer_live as _is_producer_live
from dependencies import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY,
    create_access_token,
    get_current_user,
)
from routers.interactive import router as interactive_router
from routers.admin import router as admin_router
from routers.auth import router as auth_router
from routers.creator import router as creator_router, public_router as creator_public_router
from routers.questions import router as questions_router
from routers.links import router as links_router
from routers.store import router as store_router
from routers.subscriptions import router as subscriptions_router
from routers.discord_interactions import router as discord_interactions_router
from routers.drool import router as drool_router, limiter as drool_limiter
from drool_scraper import start_drool_scheduler, stop_drool_scheduler
from routers.discord_oauth import register_metadata_schema, router as discord_oauth_router
from routers.twitter_auth import router as twitter_auth_router
from routers.spotify import router as spotify_router
from routers.cloudflare import router as cloudflare_router
from routers.member import router as member_router
from routers.compliance import router as compliance_router
from routers.notifications import router as notifications_router
from routers.community import router as community_router
from routers.monetization import router as monetization_router
from routers.analytics import router as analytics_router
from routers.discovery import router as discovery_router
from routers.alerts import router as alerts_router
from routers.moderation import router as moderation_router, public_router as moderation_public_router
from redis_client import close_redis
from slowapi.errors import RateLimitExceeded
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")
GO2RTC_TIMEOUT: float = 15.0

# Filesystem path where go2rtc writes HLS segments (hls.dir in go2rtc.yaml).
# When set, the backend uses this directory to serve DVR playlists and archive
# completed streams as on-demand VODs.  Leave empty to disable DVR/VOD.
RECORDINGS_PATH: str = os.environ.get("RECORDINGS_PATH", "").rstrip("/")

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

# Default creator account seeding.
# When both CREATOR_HANDLE and CREATOR_PASSWORD are set, init_db() creates
# the creator account on first startup (INSERT OR IGNORE – safe on restarts).
# Use ADMIN_CREATOR_HANDLE in routers/admin.py to link admin ↔ creator.
_SEED_CREATOR_HANDLE: str = os.environ.get("CREATOR_HANDLE", "").lower().strip()
_SEED_CREATOR_PASSWORD: str = os.environ.get("CREATOR_PASSWORD", "")

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

# Platform primary creator handle – used as the default owner for content
# not explicitly associated with an invited creator.
_PRIMARY_CREATOR = "mochii"

# Subdomain prefixes that still exist as real subdomains (used for CORS).
# member / creator / anon / drool have been migrated to path slugs.
_SUBDOMAIN_PREFIXES = ("links", "shop", "mochii", "www")

# Root hostname derived from BASE_URL (e.g. "mochii.live" from "https://mochii.live").
# Used by the subdomain middleware to serve the platform landing page at the bare root.
_ROOT_HOSTNAME: str = urlparse(BASE_URL).hostname.lower() if BASE_URL else ""


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

# Whether the JWT secret was explicitly configured in the environment.
# Imported from dependencies where the actual key is resolved.
from dependencies import SECRET_KEY as _IMPORTED_SECRET_KEY
_SECRET_KEY_IS_CONFIGURED: bool = bool(
    os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY")
)

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
    # ── Native site users (username/password auth, no Fanvue dependency) ──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_users (
            id              TEXT    PRIMARY KEY,
            username        TEXT    NOT NULL UNIQUE,
            email           TEXT    NOT NULL UNIQUE,
            hashed_password TEXT    NOT NULL,
            access_level    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL
        )
        """
    )
    # ── Creator accounts (separate from site_users – one per subdomain) ───
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_accounts (
            id              TEXT    PRIMARY KEY,
            handle          TEXT    NOT NULL UNIQUE,
            display_name    TEXT    NOT NULL,
            bio             TEXT,
            avatar_url      TEXT,
            accent_color    TEXT,
            hashed_password TEXT    NOT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT    NOT NULL
        )
        """
    )
    # Seed the default creator account from env vars (idempotent).
    if _SEED_CREATOR_HANDLE and _SEED_CREATOR_PASSWORD:
        from routers.auth import _hash_password as _auth_hash
        import uuid as _uuid
        from datetime import datetime as _dt, timezone as _tz
        _existing = conn.execute(
            "SELECT id FROM creator_accounts WHERE handle = ?",
            (_SEED_CREATOR_HANDLE,),
        ).fetchone()
        if not _existing:
            conn.execute(
                """
                INSERT INTO creator_accounts
                    (id, handle, display_name, hashed_password, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    str(_uuid.uuid4()),
                    _SEED_CREATOR_HANDLE,
                    _SEED_CREATOR_HANDLE.capitalize(),
                    _auth_hash(_SEED_CREATOR_PASSWORD),
                    _dt.now(_tz.utc).isoformat(),
                ),
            )

    # ── Segpay subscription audit log ─────────────────────────────────────
    # Each Segpay postback is recorded here.  The current access_level lives
    # on site_users; this table is for history and manual reconciliation.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segpay_subscriptions (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                TEXT    REFERENCES site_users(id),
            segpay_subscription_id TEXT,
            trans_type             TEXT    NOT NULL,
            status                 TEXT    NOT NULL,
            access_level_granted   INTEGER,
            email                  TEXT,
            created_at             TEXT    NOT NULL
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
    # Migration: add creator_handle column to questions and drool_archive so
    # queries can be scoped per creator.  Existing rows default to 'mochii'
    # (the platform primary creator — see _PRIMARY_CREATOR in each router).
    for _table in ("questions", "drool_archive"):
        try:
            conn.execute(
                f"ALTER TABLE {_table} ADD COLUMN creator_handle TEXT NOT NULL DEFAULT 'mochii'"
            )
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise  # re-raise unexpected errors; swallow only duplicate-column
    # Migration: add creator attribution columns to products for site-wide store.
    # The default 'mochii' is the platform primary creator; invited creators are
    # set explicitly when a product is created via the admin API.
    for _col, _defn in [
        ("creator_handle",      "TEXT NOT NULL DEFAULT 'mochii'"),
        ("creator_revenue_pct", "REAL NOT NULL DEFAULT 0.0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE products ADD COLUMN {_col} {_defn}")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # Migration: add per-creator email columns to creator_accounts.
    #   forwarding_email – creator's private inbox (inbound CF routing destination)
    #   agent_email      – optional agent inbox that also receives a copy
    for _col in ("forwarding_email", "agent_email"):
        try:
            conn.execute(f"ALTER TABLE creator_accounts ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # Migration: add member portal columns to site_users.
    #   display_name               – public name shown in the member portal
    #   display_name_changed_count – number of changes made in the current year
    #   display_name_last_reset    – calendar year (YYYY) when the counter was last reset
    for _col, _defn in [
        ("display_name",               "TEXT"),
        ("display_name_changed_count", "INTEGER NOT NULL DEFAULT 0"),
        ("display_name_last_reset",    "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE site_users ADD COLUMN {_col} {_defn}")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # Migration: allow_free_content flag on creator_accounts.
    #   When 1, non-subscribed members can browse this creator's feed.
    try:
        conn.execute(
            "ALTER TABLE creator_accounts ADD COLUMN allow_free_content INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # Migration: per-creator drool auto-sync settings.
    #   twitter_user_id  – numeric Twitter/X user ID whose liked tweets are scraped
    #   bsky_handle      – Bluesky handle (e.g. yourname.bsky.social) for likes scraping
    #   bsky_app_password – Bluesky app password (stored at rest; creator self-managed)
    # Column names are hard-coded string literals (not user input); the f-string
    # interpolation here is safe and necessary for DDL ALTER TABLE statements.
    for _col in ("twitter_user_id", "bsky_handle", "bsky_app_password"):
        try:
            conn.execute(f"ALTER TABLE creator_accounts ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # Member portal tables.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS display_name_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL REFERENCES site_users(id),
            old_name   TEXT,
            new_name   TEXT    NOT NULL,
            changed_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_follows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle TEXT    NOT NULL,
            followed_at    TEXT    NOT NULL,
            UNIQUE(user_id, creator_handle)
        )
        """
    )
    # ── Phase 1: Trust, Safety & Compliance ──────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_reports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_user_id TEXT    NOT NULL REFERENCES site_users(id),
            content_type     TEXT    NOT NULL CHECK(content_type IN ('drool','question','comment','post')),
            content_id       TEXT    NOT NULL,
            reason           TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','reviewed','actioned')),
            created_at       TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dmca_requests (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            complainant_name TEXT    NOT NULL,
            complainant_email TEXT   NOT NULL,
            content_url      TEXT    NOT NULL,
            description      TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','reviewed','actioned')),
            created_at       TEXT    NOT NULL,
            resolved_at      TEXT
        )
        """
    )
    # Idempotent migrations on creator_accounts: 2FA columns + age gate flag.
    for _col, _defn in [
        ("totp_secret",       "TEXT"),
        ("totp_enabled",      "INTEGER NOT NULL DEFAULT 0"),
        ("require_age_gate",  "INTEGER NOT NULL DEFAULT 1"),
    ]:
        try:
            conn.execute(f"ALTER TABLE creator_accounts ADD COLUMN {_col} {_defn}")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # ── Phase 2: Engagement & Retention ──────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL REFERENCES site_users(id),
            type       TEXT    NOT NULL,
            content_id TEXT,
            read       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_prefs (
            user_id          TEXT PRIMARY KEY REFERENCES site_users(id),
            email_on_answer  INTEGER NOT NULL DEFAULT 1,
            email_on_drool   INTEGER NOT NULL DEFAULT 1,
            email_on_merch   INTEGER NOT NULL DEFAULT 1,
            email_on_post    INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL REFERENCES site_users(id),
            endpoint   TEXT    NOT NULL,
            p256dh     TEXT    NOT NULL,
            auth       TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            UNIQUE(user_id, endpoint)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookmarks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT    NOT NULL REFERENCES site_users(id),
            content_type TEXT    NOT NULL,
            content_id   TEXT    NOT NULL,
            created_at   TEXT    NOT NULL,
            UNIQUE(user_id, content_type, content_id)
        )
        """
    )
    # ── Phase 3: Community & Social ───────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_activity_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle TEXT    NOT NULL,
            action_type    TEXT    NOT NULL CHECK(action_type IN ('reaction','comment','question')),
            month          TEXT    NOT NULL,
            count          INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, creator_handle, action_type, month)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_badges (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL REFERENCES site_users(id),
            badge_slug TEXT    NOT NULL,
            awarded_at TEXT    NOT NULL,
            UNIQUE(user_id, badge_slug)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS community_posts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_handle    TEXT    NOT NULL,
            title             TEXT    NOT NULL,
            body_md           TEXT    NOT NULL,
            is_subscriber_only INTEGER NOT NULL DEFAULT 0,
            is_published      INTEGER NOT NULL DEFAULT 0,
            published_at      TEXT,
            created_at        TEXT    NOT NULL,
            view_count        INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # ── Phase 4: Deeper Monetization ─────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tips (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id   TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle TEXT    NOT NULL,
            amount_cents   INTEGER NOT NULL,
            message        TEXT,
            provider_ref   TEXT,
            created_at     TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_tiers (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_handle        TEXT    NOT NULL,
            name                  TEXT    NOT NULL,
            description           TEXT,
            price_cents           INTEGER NOT NULL,
            access_level          INTEGER NOT NULL DEFAULT 2,
            is_active             INTEGER NOT NULL DEFAULT 1,
            segpay_package_id     TEXT,
            segpay_price_point_id TEXT,
            created_at            TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_subscriptions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle TEXT    NOT NULL,
            tier_id        INTEGER REFERENCES subscription_tiers(id),
            status         TEXT    NOT NULL DEFAULT 'active'
                               CHECK(status IN ('active','cancelled','expired')),
            started_at     TEXT    NOT NULL,
            expires_at     TEXT,
            UNIQUE(user_id, creator_handle)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle TEXT    NOT NULL,
            tier_id        INTEGER,
            event_type     TEXT    NOT NULL CHECK(event_type IN ('subscribe','cancel','rebill')),
            created_at     TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bundles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            description TEXT,
            price_cents INTEGER NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_creators (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            bundle_id            INTEGER NOT NULL REFERENCES bundles(id),
            creator_handle       TEXT    NOT NULL,
            access_level_granted INTEGER NOT NULL DEFAULT 2,
            UNIQUE(bundle_id, creator_handle)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_purchases (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT    NOT NULL REFERENCES site_users(id),
            bundle_id    INTEGER NOT NULL REFERENCES bundles(id),
            provider_ref TEXT,
            purchased_at TEXT    NOT NULL,
            UNIQUE(user_id, bundle_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ppv_purchases (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT    NOT NULL REFERENCES site_users(id),
            camera_id    INTEGER NOT NULL REFERENCES cameras(id),
            provider_ref TEXT,
            expires_at   TEXT,
            created_at   TEXT    NOT NULL,
            UNIQUE(user_id, camera_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digital_products (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_handle    TEXT    NOT NULL,
            name              TEXT    NOT NULL,
            description       TEXT,
            price_cents       INTEGER NOT NULL,
            file_key          TEXT,
            is_subscriber_only INTEGER NOT NULL DEFAULT 0,
            is_active         INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digital_purchases (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT    NOT NULL REFERENCES site_users(id),
            product_id     INTEGER NOT NULL REFERENCES digital_products(id),
            provider_ref   TEXT,
            purchased_at   TEXT    NOT NULL,
            UNIQUE(user_id, product_id)
        )
        """
    )
    # Migration: ppv_price_cents column on cameras.
    try:
        conn.execute("ALTER TABLE cameras ADD COLUMN ppv_price_cents INTEGER")
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # Migration: rtmp_key column on cameras (stream key for OBS/RTMP ingest).
    # SQLite does not support ADD COLUMN ... UNIQUE, so add the column first
    # then ensure the unique index exists separately.
    try:
        conn.execute("ALTER TABLE cameras ADD COLUMN rtmp_key TEXT")
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_rtmp_key ON cameras(rtmp_key)"
    )
    # Migration: stream_title column on cameras (live title shown to viewers).
    try:
        conn.execute("ALTER TABLE cameras ADD COLUMN stream_title TEXT")
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # ── Gifted subscriptions ───────────────────────────────────────────────
    # Tracks subscriptions manually granted by an admin or creator (not via
    # payment processor).  A gift sets the user's access_level and optionally
    # expires at a given date.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gifted_subscriptions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              TEXT    NOT NULL REFERENCES site_users(id),
            creator_handle       TEXT    NOT NULL,
            granted_by           TEXT    NOT NULL,
            access_level_granted INTEGER NOT NULL DEFAULT 2,
            tier_id              INTEGER REFERENCES subscription_tiers(id),
            expires_at           TEXT,
            note                 TEXT,
            is_active            INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT    NOT NULL
        )
        """
    )
    # ── Stream-alert system ────────────────────────────────────────────────
    # creator_alert_settings: per-creator, per-event configuration.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_alert_settings (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_handle    TEXT    NOT NULL,
            event_type        TEXT    NOT NULL CHECK(event_type IN ('tip','subscribe','follow')),
            enabled           INTEGER NOT NULL DEFAULT 1,
            message_template  TEXT    NOT NULL DEFAULT '',
            min_amount_cents  INTEGER NOT NULL DEFAULT 0,
            duration_ms       INTEGER NOT NULL DEFAULT 5000,
            created_at        TEXT    NOT NULL,
            updated_at        TEXT    NOT NULL,
            UNIQUE(creator_handle, event_type)
        )
        """
    )
    # stream_alerts: ring-buffer of dispatched alert events (kept for WS poll).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stream_alerts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_handle TEXT    NOT NULL,
            event_type     TEXT    NOT NULL,
            payload        TEXT    NOT NULL,
            created_at     TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stream_alerts_handle_id "
        "ON stream_alerts (creator_handle, id)"
    )
    # Migration: is_hidden column on drool_archive for compliance/reports.
    try:
        conn.execute(
            "ALTER TABLE drool_archive ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # Migration: nsfw_score column on drool_archive for AI content moderation.
    try:
        conn.execute(
            "ALTER TABLE drool_archive ADD COLUMN nsfw_score REAL"
        )
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # ── Phase 6: Discovery & SEO ──────────────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            slug  TEXT    NOT NULL UNIQUE,
            label TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_tags (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT    NOT NULL,
            content_id   TEXT    NOT NULL,
            tag_id       INTEGER NOT NULL REFERENCES tags(id),
            UNIQUE(content_type, content_id, tag_id)
        )
        """
    )
    # FTS5 virtual tables for global search (drool, questions, creators, products).
    # Each FTS table shadows the source table; kept in sync by the search router.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_drool
        USING fts5(caption, content='drool_archive', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_questions
        USING fts5(text, content='questions', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_creators
        USING fts5(display_name, bio, content='creator_accounts', content_rowid='id')
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_products
        USING fts5(name, description, content='products', content_rowid='id')
        """
    )
    # ── Phase 7: Featured Creator Queue ───────────────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS featured_creator_queue (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            position       INTEGER NOT NULL UNIQUE,
            creator_handle TEXT    NOT NULL,
            added_at       TEXT    NOT NULL
        )
        """
    )
    # ── VOD archive ───────────────────────────────────────────────────────
    # Created automatically when a stream ends (if RECORDINGS_PATH is set).
    # hls_path points to the directory on disk containing stream.m3u8 + *.ts.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vods (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id   INTEGER NOT NULL REFERENCES cameras(id),
            stream_slug TEXT    NOT NULL,
            title       TEXT,
            started_at  TEXT    NOT NULL,
            ended_at    TEXT,
            hls_path    TEXT,
            is_ready    INTEGER NOT NULL DEFAULT 0,
            is_public   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # ── Phase 8: Mixed-audience SFW + NSFW support ───────────────────────
    # content_rating on creator_accounts: 'sfw' | 'mixed' | 'nsfw' | 'unrated'
    for _col, _defn in [
        ("content_rating", "TEXT NOT NULL DEFAULT 'unrated'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE creator_accounts ADD COLUMN {_col} {_defn}")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # content_filter on site_users: 'all' | 'sfw'
    for _col, _defn in [
        ("content_filter", "TEXT NOT NULL DEFAULT 'all'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE site_users ADD COLUMN {_col} {_defn}")
        except sqlite3.OperationalError as _e:
            if "duplicate column" not in str(_e).lower():
                raise
    # pixelate_media on creator_accounts: creator forces pixelation for their archived items
    try:
        conn.execute(
            "ALTER TABLE creator_accounts ADD COLUMN pixelate_media INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # pixelate_media on site_users: member opts into pixelating NSFW-scored archive media
    try:
        conn.execute(
            "ALTER TABLE site_users ADD COLUMN pixelate_media INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise
    # is_mature on tags: 0 = safe, 1 = adult/explicit
    try:
        conn.execute("ALTER TABLE tags ADD COLUMN is_mature INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as _e:
        if "duplicate column" not in str(_e).lower():
            raise

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# go2rtc helpers
# ---------------------------------------------------------------------------


async def _sync_cameras_to_go2rtc() -> None:
    """On startup, register every RTSP/Tapo camera with go2rtc.

    RTMP cameras (those with an rtmp_key) are push-based: OBS/streaming software
    publishes directly to go2rtc on port 1935 using the rtmp_key as the stream path.
    No pre-registration is needed for RTMP – go2rtc creates the stream automatically
    when the publisher connects.
    """
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
            # RTMP cameras are push-based – skip pre-registration.
            if row["rtmp_key"]:
                continue

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
# DVR / VOD helpers
# ---------------------------------------------------------------------------

_DVR_WINDOW_SECS: float = 1800.0  # 30-minute DVR look-back window

# Tracks when each stream_slug went live (populated by the VOD watcher task).
_live_since: dict[str, datetime] = {}
_vod_watcher_task: asyncio.Task | None = None


def _build_hls_playlist(hls_dir: Path, segment_base_url: str, max_secs: float | None = None) -> str | None:
    """Build an HLS playlist from TS segments in *hls_dir*, rewriting segment
    URLs to *segment_base_url*/<filename>.

    When *max_secs* is set only the trailing window of that length is kept
    (used for the 30-minute DVR endpoint).  Pass ``None`` for full VOD output.

    Returns ``None`` when the directory contains no TS segments.

    The function is path-traversal-safe: *hls_dir* is resolved and checked
    against RECORDINGS_PATH before any filesystem access.  Only ``.ts`` files
    are included and they are referenced by basename only.
    """
    # Resolve and re-check the directory even if the caller already validated,
    # so that this function is safe when used independently.
    if not RECORDINGS_PATH:
        return None
    try:
        recordings_root = Path(RECORDINGS_PATH).resolve()
        hls_dir = hls_dir.resolve()
        hls_dir.relative_to(recordings_root)
    except (ValueError, OSError):
        return None

    if not hls_dir.is_dir():
        return None

    # Extract per-segment durations and target-duration from any existing
    # playlist written by go2rtc.  Fall back to the go2rtc default of 1 s.
    target_dur: float = 1.0
    seg_durations: dict[str, float] = {}
    m3u8_path = hls_dir / "stream.m3u8"
    has_endlist = False
    if m3u8_path.is_file():
        raw = m3u8_path.read_text(encoding="utf-8", errors="replace")
        has_endlist = "#EXT-X-ENDLIST" in raw
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    target_dur = float(line.split(":", 1)[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("#EXTINF:"):
                m = _re.match(r"#EXTINF:([\d.]+)", line)
                if m and i + 1 < len(lines):
                    try:
                        dur = float(m.group(1))
                        seg_name = Path(lines[i + 1]).name
                        if seg_name.endswith(".ts"):
                            seg_durations[seg_name] = dur
                    except (ValueError, IndexError):
                        pass

    # Gather all TS files sorted by name (go2rtc names them sequentially).
    ts_files = sorted(
        (f for f in hls_dir.iterdir() if f.suffix == ".ts" and f.is_file()),
        key=lambda p: p.name,
    )
    if not ts_files:
        return None

    # Trim to the trailing max_secs window when requested.
    if max_secs is not None:
        total = sum(seg_durations.get(f.name, target_dur) for f in ts_files)
        while ts_files and total > max_secs:
            total -= seg_durations.get(ts_files[0].name, target_dur)
            ts_files = ts_files[1:]
        if not ts_files:
            return None

    lines_out = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(target_dur) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for ts in ts_files:
        dur = seg_durations.get(ts.name, target_dur)
        lines_out.append(f"#EXTINF:{dur:.6f},")
        lines_out.append(f"{segment_base_url}/{ts.name}")
    if has_endlist:
        lines_out.append("#EXT-X-ENDLIST")
    return "\n".join(lines_out) + "\n"


def _safe_ts_filename(filename: str) -> str | None:
    """Return *filename* if it is a safe TS segment name, else ``None``.

    Rejects anything containing path separators or not ending in ``.ts`` to
    prevent path-traversal attacks when serving files from the recordings dir.
    """
    name = Path(filename).name  # strip any directory components
    if name != filename:
        return None
    if not name.endswith(".ts"):
        return None
    return name


def _resolve_within_recordings(candidate: Path) -> Path:
    """Resolve *candidate* and verify it stays inside RECORDINGS_PATH.

    Raises HTTPException 403 if the resolved path escapes the recordings root
    (path-traversal guard).  The recordings root must be configured.
    """
    if not RECORDINGS_PATH:
        raise HTTPException(status_code=404, detail="DVR/VOD is not enabled on this server")
    root = Path(RECORDINGS_PATH).resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return resolved


async def _poll_vod_transitions() -> None:
    """Query go2rtc and detect streams that have just gone live or offline.

    When a stream goes offline, snapshot its HLS directory and create a VOD
    record in the database.
    """
    conn = get_db_connection()
    try:
        cameras = conn.execute(
            "SELECT id, stream_slug, rtmp_key, stream_title FROM cameras"
        ).fetchall()
    finally:
        conn.close()

    if not cameras:
        return

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams")
        if not resp.is_success:
            return
        go2rtc_data: dict = resp.json()
    except Exception:
        return

    now = datetime.now(timezone.utc)
    for cam in cameras:
        slug: str = cam["stream_slug"]
        go2rtc_name: str = cam["rtmp_key"] or slug
        stream_info = go2rtc_data.get(go2rtc_name) or {}
        producers = stream_info.get("producers") or []
        is_live = _is_producer_live(producers)

        if is_live and slug not in _live_since:
            _live_since[slug] = now
            logger.info("Stream '%s' went live – recording start noted", slug)
        elif not is_live and slug in _live_since:
            started_at = _live_since.pop(slug)
            logger.info(
                "Stream '%s' went offline – finalizing VOD (started %s)",
                slug,
                started_at.isoformat(),
            )
            await _finalize_vod(dict(cam), started_at, now, go2rtc_name)


async def _finalize_vod(
    camera: dict,
    started_at: datetime,
    ended_at: datetime,
    go2rtc_name: str,
) -> None:
    """Copy the HLS recording for *go2rtc_name* to a permanent VOD directory
    and create a row in the ``vods`` table."""
    if not RECORDINGS_PATH:
        return

    src_dir = Path(RECORDINGS_PATH) / go2rtc_name
    if not src_dir.is_dir():
        logger.warning(
            "VOD finalisation skipped: recording dir '%s' not found", src_dir
        )
        return

    # Insert a placeholder row to obtain the auto-increment ID first so we can
    # name the destination directory after it.
    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO vods (camera_id, stream_slug, title, started_at, ended_at, is_ready)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                camera["id"],
                camera["stream_slug"],
                camera.get("stream_title"),
                started_at.isoformat(),
                ended_at.isoformat(),
            ),
        )
        vod_id: int = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    vod_dir = Path(RECORDINGS_PATH) / "vods" / str(vod_id)
    try:
        shutil.copytree(str(src_dir), str(vod_dir))
    except Exception as exc:
        logger.error(
            "Failed to copy VOD files for vod %d (DB record is_ready=0 – "
            "manual cleanup may be needed): %s",
            vod_id,
            exc,
        )
        return

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE vods SET hls_path = ?, is_ready = 1 WHERE id = ?",
            (str(vod_dir), vod_id),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "VOD %d created for stream '%s' (%s → %s)",
        vod_id,
        camera["stream_slug"],
        started_at.isoformat(),
        ended_at.isoformat(),
    )


async def _vod_watcher_loop() -> None:
    """Background task: poll every 30 s for stream live/offline transitions."""
    while True:
        await asyncio.sleep(30)
        try:
            await _poll_vod_transitions()
        except Exception as exc:
            logger.warning("VOD watcher encountered an error: %s", exc)


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
# Nightly DB backup (APScheduler)
# ---------------------------------------------------------------------------

_backup_scheduler = None


def _run_db_backup() -> None:
    """Dump the SQLite database to a gzip file in BACKUPS_DIR."""
    import gzip
    backup_dir = os.environ.get("BACKUPS_DIR", "/backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"camera_site_{ts}.db.gz")
    try:
        conn = get_db_connection()
        with gzip.open(dest, "wb") as gz_out:
            for line in conn.iterdump():
                gz_out.write((line + "\n").encode("utf-8"))
        conn.close()
        # Keep last 30 backups.
        existing = sorted(
            f for f in os.listdir(backup_dir) if f.endswith(".db.gz")
        )
        for old in existing[:-30]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass
        logger.info("DB backup written to %s", dest)
        # Record last backup time in settings table.
        conn2 = get_db_connection()
        from db import set_setting
        set_setting(conn2, "last_backup_at", datetime.now(timezone.utc).isoformat())
        conn2.close()
    except Exception as exc:
        logger.error("DB backup failed: %s", exc)


def _start_backup_scheduler() -> None:
    """Start the APScheduler cron job for nightly backups."""
    global _backup_scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        _backup_scheduler = BackgroundScheduler()
        _backup_scheduler.add_job(
            _run_db_backup,
            trigger="cron",
            hour=3,
            minute=0,
            id="nightly_db_backup",
            replace_existing=True,
        )
        _backup_scheduler.start()
        logger.info("Nightly DB backup scheduler started (03:00 UTC).")
    except Exception as exc:
        logger.warning("Failed to start backup scheduler: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI app lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    _is_production = BASE_URL.startswith("https")

    # ── Hard production safety guards ─────────────────────────────────────
    # These intentionally abort startup so a misconfigured production deploy
    # fails loudly rather than silently exposing the site with broken auth.
    if _is_production and MOCK_AUTH:
        raise RuntimeError(
            "CRITICAL: MOCK_AUTH=true is set while BASE_URL is an HTTPS production URL. "
            "This grants free premium access to all visitors. "
            "Disable MOCK_AUTH before running in production."
        )
    if _is_production and not _SECRET_KEY_IS_CONFIGURED:
        raise RuntimeError(
            "CRITICAL: No JWT_SECRET or SECRET_KEY environment variable is set while "
            "BASE_URL is an HTTPS production URL. "
            "Set a strong random secret (e.g. `openssl rand -hex 32`) before deploying."
        )

    if not _SECRET_KEY_IS_CONFIGURED:
        logger.warning(
            "Neither JWT_SECRET nor SECRET_KEY is set. A random key was generated for "
            "this process lifetime — all sessions will be invalidated on container "
            "restart. Set a persistent JWT_SECRET before deploying to production."
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
    if _ROOT_HOSTNAME:
        from routers.cloudflare import ensure_platform_subdomains
        ensure_platform_subdomains(_ROOT_HOSTNAME)
    await _sync_cameras_to_go2rtc()
    start_drool_scheduler()
    await register_metadata_schema()
    _start_backup_scheduler()
    # Start the background task that detects stream live/offline transitions
    # and automatically archives completed streams as VODs.
    global _vod_watcher_task
    if RECORDINGS_PATH:
        _vod_watcher_task = asyncio.create_task(_vod_watcher_loop())
        logger.info("VOD watcher started (recordings path: %s)", RECORDINGS_PATH)
    yield
    if _vod_watcher_task is not None:
        _vod_watcher_task.cancel()
    stop_drool_scheduler()
    # Close the Redis connection pool on shutdown to release resources.
    await close_redis()


app = FastAPI(title="mochii.live API", lifespan=lifespan)

# General-purpose rate limiter for protected API endpoints (shared key: remote IP).
_api_limiter = Limiter(key_func=get_remote_address)

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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
)

# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class CameraResponse(BaseModel):
    display_name: str
    stream_slug: str
    ppv_price_cents: Optional[int] = None
    stream_title: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.get("/auth/login")
@_api_limiter.limit("30/hour")
def auth_login(request: Request, db: sqlite3.Connection = Depends(get_db)):
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
@_api_limiter.limit("20/hour")
async def auth_callback(
    request: Request,
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
@_api_limiter.limit("60/minute")
def get_my_cameras(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return cameras the authenticated user is permitted to view."""
    access_level: int = current_user["access_level"]
    user_id: str = current_user["fanvue_id"]

    rows = db.execute(
            """
            SELECT display_name, stream_slug, ppv_price_cents, stream_title
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
        [
            {
                "display_name": row["display_name"],
                "stream_slug": row["stream_slug"],
                "ppv_price_cents": row["ppv_price_cents"],
                "stream_title": row["stream_title"],
            }
            for row in rows
        ]
    )


@app.post("/api/webrtc")
@_api_limiter.limit("30/minute")
async def proxy_webrtc(
    request: Request,
    src: str,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Proxy the WebRTC SDP exchange to go2rtc, enforcing stream-level access control.

    Access is granted when EITHER:
      (a) the user's access_level meets the camera's minimum_access_level, OR
      (b) the user has a valid (non-expired) PPV purchase for the camera.

    For RTMP cameras (those with an rtmp_key), the go2rtc stream name is the
    rtmp_key rather than the stream_slug, since OBS publishes to that path.
    """
    access_level: int = current_user["access_level"]
    user_id: str = current_user["fanvue_id"]

    camera = db.execute(
        "SELECT id, minimum_access_level, ppv_price_cents, rtmp_key FROM cameras WHERE stream_slug = ?",
        (src,),
    ).fetchone()

    if not camera:
        raise HTTPException(status_code=403, detail="Access denied to this stream")

    # Check subscription-level access.
    has_sub_access = access_level >= camera["minimum_access_level"]

    # Check PPV access.
    has_ppv_access = False
    if camera["ppv_price_cents"]:
        now_iso = datetime.now(timezone.utc).isoformat()
        ppv_row = db.execute(
            """
            SELECT id FROM ppv_purchases
             WHERE user_id = ? AND camera_id = ?
               AND (expires_at IS NULL OR expires_at > ?)
            """,
            (user_id, camera["id"], now_iso),
        ).fetchone()
        has_ppv_access = ppv_row is not None

    if not has_sub_access and not has_ppv_access:
        raise HTTPException(status_code=403, detail="Access denied to this stream")

    # RTMP cameras use rtmp_key as the go2rtc stream name; RTSP/Tapo cameras
    # are pre-registered under stream_slug.
    go2rtc_src = camera["rtmp_key"] if camera["rtmp_key"] else src

    body = await request.body()
    go2rtc_url = (
        f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/webrtc"
        f"?src={_url_quote(go2rtc_src, safe='')}"
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


@app.get("/api/stream-status")
@_api_limiter.limit("60/minute")
async def get_stream_status(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return live status and viewer count for each accessible camera.

    Queries go2rtc's /api/streams endpoint and maps the result back to
    user-facing stream_slugs.  A stream is considered live when go2rtc
    reports at least one active producer (RTSP connection or RTMP publisher).
    Viewer count is derived from the number of active go2rtc consumers.
    """
    access_level: int = current_user["access_level"]

    cameras = db.execute(
        """
        SELECT stream_slug, rtmp_key
        FROM cameras
        WHERE minimum_access_level <= ?
        ORDER BY id
        """,
        (access_level,),
    ).fetchall()

    # Default: all cameras offline
    status: dict = {
        row["stream_slug"]: {"is_live": False, "viewer_count": 0}
        for row in cameras
    }

    if not cameras:
        return JSONResponse(status)

    # Build slug → go2rtc_name mapping
    slug_to_go2rtc = {
        row["stream_slug"]: (row["rtmp_key"] if row["rtmp_key"] else row["stream_slug"])
        for row in cameras
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams")
        if resp.is_success:
            go2rtc_data: dict = resp.json()
            for slug, go2rtc_name in slug_to_go2rtc.items():
                stream_info = go2rtc_data.get(go2rtc_name) or {}
                producers = stream_info.get("producers") or []
                consumers = stream_info.get("consumers") or []
                status[slug] = {
                    "is_live": _is_producer_live(producers),
                    "viewer_count": len(consumers),
                }
    except Exception as exc:
        logger.warning("Could not fetch stream status from go2rtc: %s", exc)
        # Return all-offline rather than propagating the error

    return JSONResponse(status)


# ---------------------------------------------------------------------------
# DVR endpoints  (30-minute rolling look-back window)
# ---------------------------------------------------------------------------


def _check_stream_access(
    stream_slug: str,
    current_user: dict,
    db: sqlite3.Connection,
) -> sqlite3.Row:
    """Return the camera row for *stream_slug* or raise 403.

    Access is granted when the user's subscription level meets the camera's
    minimum OR the user has a valid PPV purchase (matching the live-stream
    access logic in ``/api/webrtc``).
    """
    camera = db.execute(
        "SELECT id, minimum_access_level, ppv_price_cents, rtmp_key FROM cameras WHERE stream_slug = ?",
        (stream_slug,),
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=403, detail="Access denied to this stream")

    access_level: int = current_user["access_level"]
    has_sub_access = access_level >= camera["minimum_access_level"]

    has_ppv_access = False
    if camera["ppv_price_cents"]:
        now_iso = datetime.now(timezone.utc).isoformat()
        ppv_row = db.execute(
            """
            SELECT id FROM ppv_purchases
             WHERE user_id = ? AND camera_id = ?
               AND (expires_at IS NULL OR expires_at > ?)
            """,
            (current_user["fanvue_id"], camera["id"], now_iso),
        ).fetchone()
        has_ppv_access = ppv_row is not None

    if not has_sub_access and not has_ppv_access:
        raise HTTPException(status_code=403, detail="Access denied to this stream")

    return camera


@app.get("/api/dvr/{stream_slug}/stream.m3u8")
@_api_limiter.limit("60/minute")
async def dvr_playlist(
    stream_slug: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return a 30-minute DVR HLS playlist for the given live stream.

    Requires the same subscription / PPV access as the live WebRTC feed.
    Returns 404 when RECORDINGS_PATH is not configured or no segments have
    been written yet for this stream.
    """
    if not RECORDINGS_PATH:
        raise HTTPException(status_code=404, detail="DVR is not enabled on this server")

    camera = _check_stream_access(stream_slug, current_user, db)
    go2rtc_name: str = camera["rtmp_key"] or stream_slug
    # Resolve and validate path to prevent traversal via rtmp_key/stream_slug
    hls_dir = _resolve_within_recordings(Path(RECORDINGS_PATH) / go2rtc_name)

    segment_base = f"/api/dvr/{_url_quote(stream_slug, safe='')}/segments"
    playlist = _build_hls_playlist(hls_dir, segment_base, max_secs=_DVR_WINDOW_SECS)
    if playlist is None:
        raise HTTPException(status_code=404, detail="No DVR data available yet for this stream")

    return Response(
        content=playlist,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/dvr/{stream_slug}/segments/{filename}")
@_api_limiter.limit("300/minute")
async def dvr_segment(
    stream_slug: str,
    filename: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Serve a single TS segment for the DVR player."""
    if not RECORDINGS_PATH:
        raise HTTPException(status_code=404, detail="DVR is not enabled on this server")

    safe_name = _safe_ts_filename(filename)
    if safe_name is None:
        raise HTTPException(status_code=400, detail="Invalid segment filename")

    camera = _check_stream_access(stream_slug, current_user, db)
    go2rtc_name: str = camera["rtmp_key"] or stream_slug
    # Resolve and validate path to prevent traversal via rtmp_key/stream_slug
    seg_path = _resolve_within_recordings(Path(RECORDINGS_PATH) / go2rtc_name / safe_name)

    if not seg_path.is_file():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(str(seg_path), media_type="video/MP2T")


# ---------------------------------------------------------------------------
# VOD endpoints  (archived recordings)
# ---------------------------------------------------------------------------


@app.get("/api/vods")
@_api_limiter.limit("60/minute")
async def list_vods(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all ready, public VODs accessible to the authenticated user.

    A VOD is accessible when the underlying camera's access requirements match
    the user's subscription/PPV status (same rules as live streams).
    """
    access_level: int = current_user["access_level"]
    user_id: str = current_user["fanvue_id"]
    now_iso = datetime.now(timezone.utc).isoformat()

    rows = db.execute(
        """
        SELECT v.id, v.stream_slug, v.title, v.started_at, v.ended_at, v.created_at,
               c.display_name AS camera_display_name,
               c.minimum_access_level, c.ppv_price_cents, c.id AS camera_id
          FROM vods v
          JOIN cameras c ON c.id = v.camera_id
         WHERE v.is_ready = 1 AND v.is_public = 1
         ORDER BY v.started_at DESC
        """
    ).fetchall()

    result = []
    for row in rows:
        has_sub = access_level >= row["minimum_access_level"]
        has_ppv = False
        if not has_sub and row["ppv_price_cents"]:
            ppv = db.execute(
                """
                SELECT id FROM ppv_purchases
                 WHERE user_id = ? AND camera_id = ?
                   AND (expires_at IS NULL OR expires_at > ?)
                """,
                (user_id, row["camera_id"], now_iso),
            ).fetchone()
            has_ppv = ppv is not None
        if not has_sub and not has_ppv:
            continue
        result.append(
            {
                "id": row["id"],
                "stream_slug": row["stream_slug"],
                "title": row["title"],
                "camera_display_name": row["camera_display_name"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "created_at": row["created_at"],
            }
        )

    return JSONResponse(result)


@app.get("/api/vods/{vod_id}")
@_api_limiter.limit("60/minute")
async def get_vod(
    vod_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return metadata for a single VOD."""
    row = db.execute(
        """
        SELECT v.id, v.stream_slug, v.title, v.started_at, v.ended_at,
               v.is_ready, v.is_public, v.created_at,
               c.display_name AS camera_display_name,
               c.minimum_access_level, c.ppv_price_cents, c.id AS camera_id
          FROM vods v
          JOIN cameras c ON c.id = v.camera_id
         WHERE v.id = ? AND v.is_ready = 1 AND v.is_public = 1
        """,
        (vod_id,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="VOD not found")

    _check_stream_access(row["stream_slug"], current_user, db)

    return JSONResponse(
        {
            "id": row["id"],
            "stream_slug": row["stream_slug"],
            "title": row["title"],
            "camera_display_name": row["camera_display_name"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "created_at": row["created_at"],
        }
    )


@app.get("/api/vods/{vod_id}/stream.m3u8")
@_api_limiter.limit("60/minute")
async def vod_playlist(
    vod_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the HLS playlist for a VOD archive."""
    row = db.execute(
        """
        SELECT v.hls_path, v.stream_slug, v.is_ready, v.is_public,
               c.minimum_access_level, c.ppv_price_cents, c.id AS camera_id
          FROM vods v
          JOIN cameras c ON c.id = v.camera_id
         WHERE v.id = ? AND v.is_ready = 1 AND v.is_public = 1
        """,
        (vod_id,),
    ).fetchone()

    if not row or not row["hls_path"]:
        raise HTTPException(status_code=404, detail="VOD not found")

    _check_stream_access(row["stream_slug"], current_user, db)

    # Validate hls_path is within RECORDINGS_PATH before reading it
    hls_dir = _resolve_within_recordings(Path(row["hls_path"]))
    segment_base = f"/api/vods/{vod_id}/segments"
    playlist = _build_hls_playlist(hls_dir, segment_base)
    if playlist is None:
        raise HTTPException(status_code=404, detail="VOD data not available")

    return Response(
        content=playlist,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/api/vods/{vod_id}/segments/{filename}")
@_api_limiter.limit("300/minute")
async def vod_segment(
    vod_id: int,
    filename: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Serve a single TS segment from a VOD archive."""
    safe_name = _safe_ts_filename(filename)
    if safe_name is None:
        raise HTTPException(status_code=400, detail="Invalid segment filename")

    row = db.execute(
        """
        SELECT v.hls_path, v.stream_slug, v.is_ready, v.is_public,
               c.minimum_access_level, c.ppv_price_cents, c.id AS camera_id
          FROM vods v
          JOIN cameras c ON c.id = v.camera_id
         WHERE v.id = ? AND v.is_ready = 1 AND v.is_public = 1
        """,
        (vod_id,),
    ).fetchone()

    if not row or not row["hls_path"]:
        raise HTTPException(status_code=404, detail="VOD not found")

    _check_stream_access(row["stream_slug"], current_user, db)

    # Validate hls_path is within RECORDINGS_PATH before accessing it
    seg_path = _resolve_within_recordings(Path(row["hls_path"]) / safe_name)
    if not seg_path.is_file():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(str(seg_path), media_type="video/MP2T")


# ---------------------------------------------------------------------------
# Routers and static files (registered last so API routes take priority)
# ---------------------------------------------------------------------------

app.include_router(interactive_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(creator_router)
app.include_router(creator_public_router)
app.include_router(questions_router)
app.include_router(links_router)
app.include_router(store_router)
app.include_router(subscriptions_router)
app.include_router(discord_interactions_router)
app.include_router(drool_router)
app.include_router(discord_oauth_router)
app.include_router(twitter_auth_router)
app.include_router(spotify_router)
app.include_router(cloudflare_router)
app.include_router(member_router)
app.include_router(compliance_router)
app.include_router(notifications_router)
app.include_router(community_router)
app.include_router(monetization_router)
app.include_router(analytics_router)
app.include_router(discovery_router)
app.include_router(alerts_router)
app.include_router(moderation_router)
app.include_router(moderation_public_router)

# Attach the slowapi rate-limiter state and exception handler to the app so
# that @limiter.limit decorators in all rate-limited routers function correctly.
# All limiters share the same key function (remote IP) and exception handler.
from routers.auth import _auth_limiter
from routers.creator import _creator_limiter as _c_limiter
from routers.compliance import _compliance_limiter

app.state.limiter = drool_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Re-attach all limiters to the app so their shared state is managed.
_auth_limiter.app = app  # type: ignore[attr-defined]
_c_limiter.app = app  # type: ignore[attr-defined]
_api_limiter.app = app  # type: ignore[attr-defined]
_compliance_limiter.app = app  # type: ignore[attr-defined]

# Maps well-known subdomain prefixes → the path that should be served.
# Creator dens (e.g. mochii., someother.) are handled by the dynamic
# fallback in subdomain_routing so they are NOT listed here.
# NOTE: member / creator / anon / drool no longer have subdomains – they
# are served via path slugs (/member, /creator, /{handle}/anon, /{handle}/drool).
_SUBDOMAIN_MAP: dict[str, str] = {
    "links.":   "/links",
    "shop.":    "/store.html",
    "www.":     "/landing.html",
}

# Old subdomains that have been retired: redirect their root (/) to the
# canonical slug path so any existing bookmarks / links keep working.
_SUBDOMAIN_REDIRECT_MAP: dict[str, str] = {
    "member.":  "/member",
    "creator.": "/creator",
    "anon.":    f"/{_PRIMARY_CREATOR}/anon",
    "drool.":   f"/{_PRIMARY_CREATOR}/drool",
}


@app.middleware("http")
async def subdomain_routing(request: Request, call_next):
    """Transparently serve subdomain roots by rewriting the ASGI path in-place.

    {handle}.mochii.live/ → serves /index.html   (creator's subscriber portal)
    links.mochii.live/    → serves /links content
    shop.mochii.live/     → serves /store.html
    mochii.live/ or www.mochii.live/ → serves /landing.html (platform home)

    Retired subdomains are 301-redirected to their canonical slug paths:
      member.mochii.live/  → /member
      creator.mochii.live/ → /creator
      anon.mochii.live/    → /{_PRIMARY_CREATOR}/anon
      drool.mochii.live/   → /{_PRIMARY_CREATOR}/drool

    Creator dens are matched dynamically — any subdomain that is not one of the
    well-known prefixes above and belongs to the root domain is treated as a
    creator handle.  The index.html page detects the handle from
    ``window.location.hostname`` and fetches ``GET /api/creators/{handle}``
    to populate itself at runtime.

    Only GET requests to exactly "/" are rewritten so that the correct HTML
    page is returned.  All other paths (API calls, static assets, …) pass
    through untouched — they work identically on every subdomain.
    """
    if request.method == "GET" and request.url.path == "/":
        host = request.headers.get("host", "").lower().split(":")[0]
        matched = False
        # Retired subdomains: 301-redirect to the new slug path.
        for prefix, redirect_path in _SUBDOMAIN_REDIRECT_MAP.items():
            if host.startswith(prefix):
                base = BASE_URL or f"{request.url.scheme}://{_ROOT_HOSTNAME}"
                return RedirectResponse(url=f"{base}{redirect_path}", status_code=301)
        for prefix, target_path in _SUBDOMAIN_MAP.items():
            if host.startswith(prefix):
                request.scope["path"] = target_path
                matched = True
                break
        # Bare root domain (e.g. mochii.live) → platform landing page.
        if not matched and _ROOT_HOSTNAME and host == _ROOT_HOSTNAME:
            request.scope["path"] = "/landing.html"
            matched = True
        # Any other subdomain of the root domain is treated as a creator den.
        # This covers mochii., someothercreator., and any future handles
        # without needing to hard-code them here.
        # Handle validation is intentionally delegated to the client: index.html
        # calls GET /api/creators/{handle} and redirects to the platform landing
        # page when the handle does not match an active creator account.
        if not matched and _ROOT_HOSTNAME and host.endswith(f".{_ROOT_HOSTNAME}"):
            request.scope["path"] = "/index.html"
    return await call_next(request)


# ---------------------------------------------------------------------------
# Age Gate middleware
# ---------------------------------------------------------------------------

# Paths that are always exempt from the age gate (static assets, API calls,
# the gate confirmation endpoint itself, health checks, etc.)
_AGE_GATE_EXEMPT_PREFIXES = (
    "/api/",
    "/auth/",
    "/static/",
    "/favicon",
    "/manifest",
    "/sw.js",
    "/offline",
    "/explore",
    "/landing",
    "/age-gate",
    "/terms",
    "/privacy",
    "/dmca",
    "/2257",
)
_AGE_GATE_EXEMPT_EXTENSIONS = (".css", ".js", ".ico", ".png", ".jpg", ".svg", ".woff", ".woff2")

_AGE_GATE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Age Verification</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#1a1a1a;color:#f0e6e8;font-family:system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}
  .card{background:#242424;border:1px solid #3d2a2e;border-radius:16px;
        padding:2.5rem;max-width:420px;width:100%;text-align:center}
  h1{font-size:1.6rem;margin-bottom:.75rem;color:#c49a9f}
  p{color:#9e7e82;margin-bottom:1.5rem;line-height:1.6}
  .btn{display:inline-block;background:#c49a9f;color:#1a1a1a;border:none;
       border-radius:10px;padding:.85rem 2rem;font-size:1rem;font-weight:700;
       cursor:pointer;text-decoration:none;width:100%;margin-bottom:.75rem}
  .btn:hover{background:#d4b0b5}
  .btn-exit{background:#3d2a2e;color:#c49a9f}
  .btn-exit:hover{background:#4d3a3e}
  small{color:#6a4a4e;font-size:.8rem}
</style>
</head>
<body>
<div class="card">
  <h1>&#127283; Age Verification</h1>
  <p>This site contains adult content intended for viewers aged <strong>18 years or older</strong>.
     By entering you confirm you are at least 18 years of age and it is legal to view adult
     content in your jurisdiction.</p>
  <form method="POST" action="/age-gate/confirm">
    <input type="hidden" name="next" value="{next_url}">
    <button type="submit" class="btn">I am 18 or older &mdash; Enter</button>
  </form>
  <a href="https://www.google.com" class="btn btn-exit">Exit</a>
  <small>By entering you agree to our <a href="/terms" style="color:#c49a9f">Terms of Service</a> and <a href="/privacy" style="color:#c49a9f">Privacy Policy</a>.</small>
</div>
</body>
</html>"""


@app.post("/age-gate/confirm", include_in_schema=False)
async def age_gate_confirm(request: Request):
    """Set the age-verified cookie and redirect to the intended destination."""
    form = await request.form()
    raw_next = str(form.get("next", "/"))
    # Sanitise the redirect target strictly: only allow paths starting with /
    # and strip any scheme/host to prevent open-redirect exploits.
    parsed = urlparse(raw_next)
    if parsed.scheme or parsed.netloc or not raw_next.startswith("/") or raw_next.startswith("//"):
        next_url = "/"
    else:
        # Reconstruct from path+query only — drop scheme/netloc/fragment.
        next_url = parsed.path
        if parsed.query:
            next_url += f"?{parsed.query}"
        if not next_url.startswith("/"):
            next_url = "/"

    response = RedirectResponse(url=next_url, status_code=303)
    cookie_domain = COOKIE_DOMAIN or None
    response.set_cookie(
        key="age_verified",
        value="1",
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="strict",
        domain=cookie_domain,
        secure=BASE_URL.startswith("https"),
    )
    return response


def _requires_age_gate(request: Request) -> bool:
    """Return True if this request should be intercepted by the age gate."""
    # Only GET requests to HTML pages trigger the gate.
    if request.method != "GET":
        return False
    path = request.url.path
    # Exempt API paths, static assets, the gate itself.
    for prefix in _AGE_GATE_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return False
    for ext in _AGE_GATE_EXEMPT_EXTENSIONS:
        if path.endswith(ext):
            return False
    # Cookie already set → no gate.
    if request.cookies.get("age_verified") == "1":
        return False
    return True


@app.middleware("http")
async def age_gate_middleware(request: Request, call_next):
    """Intercept HTML page requests and show the age gate when not yet verified."""
    if _requires_age_gate(request):
        next_url = str(request.url.path)
        if request.url.query:
            next_url += f"?{request.url.query}"
        gate_html = _AGE_GATE_HTML.replace("{next_url}", _html_lib.escape(next_url))
        return HTMLResponse(content=gate_html, status_code=200)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------

# Paths that serve dynamic API data – we want no-store cache control there.
_API_PATH_PREFIX = "/api/"

# Spoofed server identifier used in responses (security through obscurity:
# makes automated scanners misidentify the server software).
_SPOOFED_SERVER = "nginx/1.18.0"


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Inject security-hardening HTTP headers on every outgoing response.

    Layers of defense:
    - Structural (X-Frame-Options, X-Content-Type-Options) – prevent well-known
      browser-level attacks regardless of content.
    - Transport (HSTS) – enforce HTTPS on supporting browsers when running with
      a production HTTPS BASE_URL.
    - Policy (Referrer-Policy, Permissions-Policy) – limit information leakage
      and browser feature access.
    - Obscurity (Server header) – replace the default server banner with a
      misleading value to slow down automated scanner fingerprinting.
    """
    response = await call_next(request)

    # Prevent the page from being embedded in an iframe (clickjacking).
    response.headers["X-Frame-Options"] = "DENY"

    # Block MIME-type sniffing; browser must respect Content-Type.
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Control how much referrer information is passed in cross-origin requests.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Restrict access to browser features that the site does not need.
    # Camera is allowed (self-origin only) because the subscriber portal
    # may need webcam access for content.
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), payment=(), usb=(), camera=(self)"
    )

    # Enforce HTTPS-only connections for one year (only meaningful under TLS).
    if BASE_URL.startswith("https"):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains; preload"
        )

    # Prevent caching of API responses that may contain personal data.
    if request.url.path.startswith(_API_PATH_PREFIX):
        response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")

    # Security through obscurity: replace the real server banner so that
    # automated scanners cannot trivially fingerprint the framework version.
    response.headers["Server"] = _SPOOFED_SERVER

    # Drop any header that leaks implementation details, if present.
    if "x-powered-by" in response.headers:
        del response.headers["x-powered-by"]

    return response

_APP_START_TIME = datetime.now(timezone.utc)


@app.get("/api/health", tags=["system"])
async def health_check():
    """Structured health endpoint.  Returns 503 if DB or go2rtc is unreachable."""
    uptime_s = int((datetime.now(timezone.utc) - _APP_START_TIME).total_seconds())
    db_ok = False
    try:
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        pass

    go2rtc_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams")
            go2rtc_ok = resp.is_success
    except Exception:
        pass

    # Last backup timestamp.
    last_backup: Optional[str] = None
    try:
        conn = get_db_connection()
        last_backup = get_setting(conn, "last_backup_at")
        conn.close()
    except Exception:
        pass

    all_ok = db_ok  # go2rtc optional
    status_code = 200 if all_ok else 503
    return JSONResponse(
        content={
            "status": "ok" if all_ok else "degraded",
            "db_ok": db_ok,
            "go2rtc_ok": go2rtc_ok,
            "uptime_s": uptime_s,
            "last_backup_at": last_backup,
        },
        status_code=status_code,
    )


@app.get("/api/featured-creator", tags=["discovery"])
def get_featured_creator(db: sqlite3.Connection = Depends(get_db)):
    """Return the currently featured creator (position 1 in the queue).

    Returns ``null`` when the queue is empty so the landing page can hide the
    card gracefully.
    """
    row = db.execute(
        """
        SELECT fq.creator_handle, ca.display_name, ca.bio, ca.avatar_url, ca.accent_color
          FROM featured_creator_queue fq
          JOIN creator_accounts ca ON ca.handle = fq.creator_handle
         WHERE ca.is_active = 1
         ORDER BY fq.position ASC
         LIMIT 1
        """
    ).fetchone()
    if not row:
        return JSONResponse(content=None)
    return dict(row)


@app.get("/api/site-mode", tags=["discovery"])
def get_site_mode(db: sqlite3.Connection = Depends(get_db)):
    """Return whether the platform is in single-creator or multi-creator mode.

    When exactly one creator account is active the UI can present the site as
    dedicated to that creator so visitors cannot tell it is a multi-creator
    platform.  The response includes the creator's public profile fields so the
    frontend only needs this one call.
    """
    rows = db.execute(
        """
        SELECT handle, display_name, bio, avatar_url, accent_color
          FROM creator_accounts
         WHERE is_active = 1
        """
    ).fetchall()
    if len(rows) == 1:
        return {"mode": "single", **dict(rows[0])}
    return {"mode": "multi"}


@app.get("/admin", include_in_schema=False)
def admin_page_redirect(request: Request):
    """Redirect /admin (and /admin?q=...) to the static admin.html page."""
    qs = request.url.query
    target = f"/admin.html?{qs}" if qs else "/admin.html"
    return RedirectResponse(url=target, status_code=301)


@app.get("/drool", include_in_schema=False)
def drool_page_redirect():
    """Redirect /drool to the primary creator's per-creator Shame Gallery."""
    return RedirectResponse(url=f"/{_PRIMARY_CREATOR}/drool", status_code=301)


@app.get("/explore", include_in_schema=False)
def explore_page():
    """Serve the public explore page."""
    return RedirectResponse(url="/explore.html", status_code=301)


# ---------------------------------------------------------------------------
# robots.txt & security.txt
# ---------------------------------------------------------------------------


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    """Disallow automated scraping of sensitive paths."""
    content = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /age-gate/\n"
        "Disallow: /admin\n"
        "Disallow: /admin.html\n"
        "Disallow: /creator-dash.html\n"
        "\n"
        "User-agent: *\n"
        "Allow: /api/health\n"
    )
    return Response(content=content, media_type="text/plain")


@app.get("/.well-known/security.txt", include_in_schema=False)
def security_txt():
    """RFC 9116 security contact disclosure."""
    expiry = (datetime.now(timezone.utc) + timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    content = (
        f"Contact: mailto:security@mochii.live\n"
        f"Expires: {expiry}\n"
        "Preferred-Languages: en\n"
        "Policy: https://mochii.live/security-policy\n"
    )
    return Response(content=content, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Honeypot endpoints
# ---------------------------------------------------------------------------
# Paths commonly probed by vulnerability scanners, bots, and opportunistic
# attackers.  Returning a generic 404 gives nothing away while logging the
# attempt.  Any legitimate client should never hit these routes.

_HONEYPOT_PATHS = [
    "/.env",
    "/.env.local",
    "/.env.production",
    "/.git/HEAD",
    "/.git/config",
    "/wp-admin",
    "/wp-login.php",
    "/wp-config.php",
    "/xmlrpc.php",
    "/phpmyadmin",
    "/pma",
    "/admin/config.php",
    "/config.php",
    "/backup.sql",
    "/dump.sql",
    "/database.sql",
    "/shell.php",
    "/cmd.php",
    "/server-status",
    "/server-info",
    "/.DS_Store",
    "/web.config",
    "/composer.json",
    "/package.json",
    "/yarn.lock",
]

_honeypot_logger = logging.getLogger("honeypot")


def _register_honeypot(path: str) -> None:
    """Register a GET+POST route that logs the probe and returns a generic 404."""

    async def _trap(request: Request) -> Response:
        _honeypot_logger.warning(
            "Honeypot triggered: method=%s path=%s ip=%s ua=%r",
            request.method,
            request.url.path,
            request.client.host if request.client else "unknown",
            request.headers.get("user-agent", ""),
        )
        # Return a generic response that reveals nothing about the stack.
        return Response(
            content="Not Found",
            status_code=404,
            media_type="text/plain",
        )

    # Register for GET, POST, HEAD, PUT, DELETE, PATCH so that scanners using
    # any common HTTP method are caught and logged.
    app.add_api_route(
        path,
        _trap,
        methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )


for _hp_path in _HONEYPOT_PATHS:
    _register_honeypot(_hp_path)


# ---------------------------------------------------------------------------
# Custom error handlers (hide framework fingerprints in error responses)
# ---------------------------------------------------------------------------


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Generic 404 – does not reveal the server framework or route structure."""
    return JSONResponse({"detail": "Not found."}, status_code=404)


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Generic 405 – does not leak allowed-method lists to scanners."""
    return JSONResponse({"detail": "Method not allowed."}, status_code=405)


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Generic 500 – never returns tracebacks or internal paths to clients."""
    logger.exception("Unhandled internal error for %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "An unexpected error occurred."}, status_code=500)

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
# Anonymous Q&A page  –  /anon  (global view) and /{handle}/anon (per-creator)
# ---------------------------------------------------------------------------

@app.get("/anon", response_class=None)
def anon_page(request: Request):
    """Standalone Puppy Pouch page: submit a question + browse all answered Q&A."""
    canonical = BASE_URL or str(request.base_url).rstrip("/")
    return HTMLResponse(content=_build_anon_html(canonical))


@app.get("/{creator_handle}/anon", response_class=None)
def anon_page_creator(creator_handle: str, request: Request):
    """Per-creator Puppy Pouch page scoped to a specific creator's Q&A."""
    canonical = BASE_URL or str(request.base_url).rstrip("/")
    return HTMLResponse(content=_build_anon_html(canonical, creator_handle=creator_handle))


def _build_anon_html(canonical: str, creator_handle: Optional[str] = None) -> str:
    """Generate the Puppy Pouch HTML page.

    When *creator_handle* is supplied the page is scoped to that creator:
    submissions are tagged with the handle and the feed only shows that
    creator's answered questions.  Without it the global (all-creator) view
    is shown.
    """
    if creator_handle:
        safe_handle = _html_escape(creator_handle)
        page_url   = _html_escape(f"{canonical}/{creator_handle}/anon")
        og_title   = f"Puppy Pouch 🐾 – Ask {safe_handle} Anything"
        og_desc    = _html_escape(
            f"Drop an anonymous note for {safe_handle} and browse their answered Q&A."
        )
        if _ROOT_HOSTNAME:
            _parsed = urlparse(canonical)
            back_href = _html_escape(f"{_parsed.scheme}://{creator_handle}.{_ROOT_HOSTNAME}")
        else:
            back_href = _html_escape(canonical)
        back_label = f"← Back to {safe_handle}"
    else:
        page_url   = _html_escape(f"{canonical}/anon")
        og_title   = "Puppy Pouch 🐾 – Ask mochii.live Anything"
        og_desc    = _html_escape(
            "Drop an anonymous question into the Puppy Pouch and browse every answered note."
        )
        back_href  = _html_escape(canonical)
        back_label = "← Back to mochii.live"

    return f"""<!DOCTYPE html>
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
    <a class="back-link" href="{back_href}">{back_label}</a>
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
    // Detect per-creator context from URL path (e.g. /mochii/anon).
    const CREATOR_HANDLE = (function() {{
      const parts = window.location.pathname.split('/');
      return (parts.length >= 3 && parts[1] && parts[2] === 'anon') ? parts[1] : null;
    }})();
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
        const payload = CREATOR_HANDLE ? {{ text, creator_handle: CREATOR_HANDLE }} : {{ text }};
        const resp = await fetch('/api/questions', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
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
        const feedUrl = CREATOR_HANDLE
          ? '/api/questions/public?creator_handle=' + encodeURIComponent(CREATOR_HANDLE)
          : '/api/questions/public';
        const resp = await fetch(feedUrl);
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
          // Show creator badge only on the global feed where multiple creators mix.
          const creatorTag = (!CREATOR_HANDLE && q.creator_handle && q.creator_handle !== '{_PRIMARY_CREATOR}')
            ? `<span style="font-size:.7rem;font-weight:800;color:#6a4a4e;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:.35rem;">🐾 ${{esc(q.creator_handle)}}</span>`
            : '';
          div.innerHTML =
            `<div class="bubble bubble-q">${{creatorTag}}${{esc(q.text)}}</div>` +
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
# Member portal  –  /member  (slug; member.mochii.live/ redirects here)
# ---------------------------------------------------------------------------

@app.get("/member", response_class=HTMLResponse, include_in_schema=False)
def member_portal():
    """Serve the subscriber member portal SPA."""
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), "static", "member.html")
    with open(_path, encoding="utf-8") as _f:
        return HTMLResponse(content=_f.read())


# ---------------------------------------------------------------------------
# Creator pitch page  –  /creator  (slug; creator.mochii.live/ redirects here)
# ---------------------------------------------------------------------------

@app.get("/creator", response_class=HTMLResponse, include_in_schema=False)
def creator_pitch_page():
    """Serve the creator pitch/info page."""
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), "static", "creator.html")
    with open(_path, encoding="utf-8") as _f:
        return HTMLResponse(content=_f.read())


# ---------------------------------------------------------------------------
# Per-creator Shame Gallery  –  /{handle}/drool
# ---------------------------------------------------------------------------

@app.get("/{creator_handle}/drool", response_class=HTMLResponse, include_in_schema=False)
def drool_page_creator(creator_handle: str):
    """Serve the Shame Gallery filtered to a specific creator."""
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), "static", "drool.html")
    with open(_path, encoding="utf-8") as _f:
        return HTMLResponse(content=_f.read())


app.mount("/", StaticFiles(directory="static", html=True), name="static")
