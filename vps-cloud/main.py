import io
import logging
import os
import sys
import secrets
import sqlite3
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, quote as _url_quote

import httpx
import html as _html_lib
from PIL import Image, ImageDraw, ImageFont
from fastapi import Depends, FastAPI, HTTPException, Request, status
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
from redis_client import close_redis

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
    yield
    # Close the Redis connection pool on shutdown to release resources.
    await close_redis()


app = FastAPI(title="mochii.live API", lifespan=lifespan)

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


@app.get("/admin", include_in_schema=False)
def admin_page_redirect(request: Request):
    """Redirect /admin (and /admin?q=...) to the static admin.html page."""
    qs = request.url.query
    target = f"/admin.html?{qs}" if qs else "/admin.html"
    return RedirectResponse(url=target, status_code=301)


# ---------------------------------------------------------------------------
# Puppy Pouch share page
# ---------------------------------------------------------------------------

# OG image dimensions (Twitter / Open Graph recommended: 1200×630)
_OG_IMG_W = 1200
_OG_IMG_H = 630

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


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return the best available font at the requested size.

    Falls back to Pillow's built-in bitmap font when no TrueType font is found;
    that font ignores *size* and renders at a fixed small size.
    """
    candidates = [
        # Common sans-serif fonts that are typically installed on Linux/Debian
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
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
    """Render a 1200×630 PNG card matching the site's dark-pink aesthetic."""
    img = Image.new("RGB", (_OG_IMG_W, _OG_IMG_H), _BG_OUTER)
    draw = ImageDraw.Draw(img)

    # Card bounds
    margin = 60
    card_x1, card_y1 = margin, margin
    card_x2, card_y2 = _OG_IMG_W - margin, _OG_IMG_H - margin
    card_w = card_x2 - card_x1
    pad = 36  # inner padding

    # Draw card background + border
    draw.rounded_rectangle(
        [card_x1, card_y1, card_x2, card_y2],
        radius=24,
        fill=_BG_CARD,
        outline=_BORDER_CARD,
        width=2,
    )

    # Fonts
    font_label  = _load_font(20)
    font_body   = _load_font(30)
    font_footer = _load_font(22)
    font_title  = _load_font(22)

    cur_y = card_y1 + pad

    # ── Card title ──────────────────────────────────────────────────────────
    title_text = "PUPPY POUCH 🐾 ANONYMOUS Q&A"
    draw.text((card_x1 + pad, cur_y), title_text, font=font_title, fill=_FG_TITLE)
    cur_y += 30 + 20  # title height + gap

    # ── Question bubble ─────────────────────────────────────────────────────
    inner_w = card_w - pad * 2

    # Measure label
    label_q = "🐾 QUESTION"
    lq_bbox = draw.textbbox((0, 0), label_q, font=font_label)
    lq_h = lq_bbox[3] - lq_bbox[1]

    # Wrap question text
    q_lines = _wrap_text(q_text, font_body, inner_w - 24, draw)
    # Cap at 3 lines to avoid overflow; pixel-accurate ellipsis on last line
    if len(q_lines) > 3:
        q_lines = q_lines[:3]
        q_lines[-1] = _truncate_line(q_lines[-1], font_body, inner_w - 24, draw)
    body_q_bbox = draw.textbbox((0, 0), q_lines[0], font=font_body)
    line_h = body_q_bbox[3] - body_q_bbox[1]
    bubble_q_h = pad // 2 + lq_h + 8 + line_h * len(q_lines) + (len(q_lines) - 1) * 6 + pad // 2

    bx1, by1 = card_x1 + pad, cur_y
    bx2, by2 = card_x2 - pad, cur_y + bubble_q_h
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=14, fill=_BG_Q, outline=_BORDER_Q, width=1)
    ty = by1 + pad // 2
    draw.text((bx1 + 14, ty), label_q, font=font_label, fill=_FG_Q_LABEL)
    ty += lq_h + 8
    for line in q_lines:
        draw.text((bx1 + 14, ty), line, font=font_body, fill=_FG_MAIN)
        ty += line_h + 6

    cur_y = by2 + 16  # gap between bubbles

    # ── Answer bubble ────────────────────────────────────────────────────────
    label_a = "💬 ANSWER"
    la_bbox = draw.textbbox((0, 0), label_a, font=font_label)
    la_h = la_bbox[3] - la_bbox[1]

    remaining_h = (card_y2 - pad - 40) - cur_y  # leave room for footer
    a_lines = _wrap_text(a_text, font_body, inner_w - 24, draw)
    max_a_lines = max(1, (remaining_h - la_h - 8 - pad) // (line_h + 6))
    if len(a_lines) > max_a_lines:
        a_lines = a_lines[:max_a_lines]
        a_lines[-1] = _truncate_line(a_lines[-1], font_body, inner_w - 24, draw)

    bubble_a_h = pad // 2 + la_h + 8 + line_h * len(a_lines) + (len(a_lines) - 1) * 6 + pad // 2
    ax1, ay1 = card_x1 + pad, cur_y
    ax2, ay2 = card_x2 - pad, cur_y + bubble_a_h
    draw.rounded_rectangle([ax1, ay1, ax2, ay2], radius=14, fill=_BG_A, outline=_BORDER_A, width=1)
    ty = ay1 + pad // 2
    draw.text((ax1 + 14, ty), label_a, font=font_label, fill=_FG_A_LABEL)
    ty += la_h + 8
    for line in a_lines:
        draw.text((ax1 + 14, ty), line, font=font_body, fill=_FG_MAIN)
        ty += line_h + 6

    # ── Footer ───────────────────────────────────────────────────────────────
    footer_text = "Ask me anything at mochii.live 🐾"
    ft_bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
    ft_w = ft_bbox[2] - ft_bbox[0]
    draw.text(
        (card_x1 + (card_w - ft_w) // 2, card_y2 - pad - (ft_bbox[3] - ft_bbox[1])),
        footer_text,
        font=font_footer,
        fill=_FG_FOOTER,
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
    base_url = str(request.base_url).rstrip("/")
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
  <meta property="og:type"        content="website" />
  <meta property="og:url"         content="{page_url}" />
  <meta property="og:title"       content="{og_title}" />
  <meta property="og:description" content="{og_description}" />
  <meta property="og:image"       content="{og_image_url}" />
  <meta property="og:image:width"  content="1200" />
  <meta property="og:image:height" content="630" />
  <meta property="og:site_name"   content="mochii.live" />
  <meta name="twitter:card"        content="summary_large_image" />
  <meta name="twitter:title"       content="{og_title}" />
  <meta name="twitter:description" content="{og_description}" />
  <meta name="twitter:image"       content="{og_image_url}" />

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
    <p class="card-footer">Ask me anything at <a href="https://mochii.live">mochii.live</a> 🐾</p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)



app.mount("/", StaticFiles(directory="static", html=True), name="static")
