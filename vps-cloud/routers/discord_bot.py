"""
routers/discord_bot.py – Private REST API consumed by the standalone Discord bot.

All endpoints are authenticated with the shared ``DISCORD_BOT_TOKEN`` passed as:

    Authorization: Bot <DISCORD_BOT_TOKEN>

These endpoints expose data that would otherwise require Fanvue JWT auth or
admin Basic-Auth, but are safe to expose to the bot because the bot token is a
shared secret known only to the ``discord-bot`` container.

Endpoints
---------
GET    /api/discord/bot/member                 Single member access-level lookup
GET    /api/discord/bot/members                All linked members with access levels
POST   /api/discord/bot/control/{device}       IoT device trigger (cooldown-aware)
GET    /api/discord/bot/stats                  Aggregated site statistics
GET    /api/discord/bot/spotify/search         Proxy Spotify track search (level ≥ 2)
POST   /api/discord/bot/spotify/queue          Add track to Spotify queue (level ≥ 2)
GET    /api/discord/bot/links                  List all links on the site
POST   /api/discord/bot/links                  Create a new link
PUT    /api/discord/bot/links/{link_id}        Update an existing link
DELETE /api/discord/bot/links/{link_id}        Delete a link
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from db import get_db_connection, get_setting
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discord-bot"])

# Devices the bot is allowed to trigger.
_ALLOWED_DEVICES = {"pishock", "lovense"}
_COOLDOWN_SECONDS = 3600
_TEASER_DURATION  = 5
_PREMIUM_DURATION = 15
_PREMIUM_LEVEL    = 3
_MIN_DEVICE_LEVEL = 1

_SPOTIFY_API_BASE = "https://api.spotify.com/v1"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_bot_auth(request: Request) -> None:
    """Raise 401/503 if the request doesn't carry a valid bot token."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot token not configured on the server.",
        )
    if request.headers.get("Authorization", "") != f"Bot {bot_token}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bot token.",
        )


# ── Member lookup ─────────────────────────────────────────────────────────────

@router.get("/api/discord/bot/member")
def bot_get_member(discord_id: str, request: Request):
    """Return Fanvue ``access_level`` for a single linked Discord user."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        row = db.execute(
            """
            SELECT da.discord_id, u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()
    if not row:
        return {"discord_id": discord_id, "is_linked": False, "access_level": 0}
    return {"discord_id": discord_id, "is_linked": True, "access_level": row["access_level"]}


@router.get("/api/discord/bot/members")
def bot_get_all_members(request: Request):
    """Return all Discord-linked members with their Fanvue access levels."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        rows = db.execute(
            """
            SELECT da.discord_id, u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.user_id IS NOT NULL
            """,
        ).fetchall()
    return [{"discord_id": r["discord_id"], "access_level": r["access_level"]} for r in rows]


# ── IoT control ───────────────────────────────────────────────────────────────

class DeviceControlBody(BaseModel):
    discord_id: str


@router.post("/api/discord/bot/control/{device}")
async def bot_control_device(device: str, body: DeviceControlBody, request: Request):
    """
    Trigger an IoT device on behalf of a Discord user.

    Access rules mirror ``routers/interactive.py``:
    - Not linked / level 0 → error message returned (no exception)
    - Level 1–2 → 5-second activation + 1-hour Redis cooldown
    - Level 3+  → 15-second activation, no cooldown
    """
    _check_bot_auth(request)
    if device not in _ALLOWED_DEVICES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown device.")

    # ── Resolve user ──────────────────────────────────────────────────────────
    with get_db_connection() as db:
        row = db.execute(
            """
            SELECT u.id, u.fanvue_id, u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (body.discord_id,),
        ).fetchone()

    if not row:
        return {
            "success": False,
            "message": (
                "Your Discord account isn't linked to a Fanvue account yet. "
                "Use `/verify` to get started! 🐾"
            ),
        }

    access_level: int = row["access_level"]
    if access_level < _MIN_DEVICE_LEVEL:
        return {
            "success": False,
            "message": "You need to at least follow on Fanvue to activate devices. 🐾",
        }

    user_id: str = row["id"]
    is_premium: bool = access_level >= _PREMIUM_LEVEL

    # ── Cooldown check ────────────────────────────────────────────────────────
    if not is_premium:
        redis = await get_redis()
        if redis is not None:
            cooldown_key = f"teaser:cooldown:{device}:{user_id}"
            ttl: int = await redis.ttl(cooldown_key)
            if ttl > 0 or ttl == -1:
                remaining = ttl if ttl > 0 else _COOLDOWN_SECONDS
                mins, secs = divmod(remaining, 60)
                return {
                    "success": False,
                    "message": f"⏳ Cooldown active — try again in **{mins}m {secs}s**.",
                    "cooldown_remaining": remaining,
                }

    # ── Activate (mock — real implementation via Tailscale edge agent) ────────
    duration = _PREMIUM_DURATION if is_premium else _TEASER_DURATION

    # ── Set cooldown for non-premium ──────────────────────────────────────────
    if not is_premium:
        redis = await get_redis()
        if redis is not None:
            cooldown_key = f"teaser:cooldown:{device}:{user_id}"
            await redis.set(cooldown_key, "1", ex=_COOLDOWN_SECONDS)

    # ── Log activation ────────────────────────────────────────────────────────
    with get_db_connection() as db:
        db.execute(
            "INSERT INTO activations (device, actor, activated_at) VALUES (?, ?, ?)",
            (device, f"discord:{body.discord_id}", datetime.now(timezone.utc).isoformat()),
        )
        db.commit()

    emoji = "⚡" if device == "pishock" else "💜"
    qualifier = "" if is_premium else f" ({duration}s teaser)"
    return {
        "success": True,
        "message": f"{emoji} **{device.capitalize()}** activated{qualifier}!",
        "duration": duration,
        "is_premium": is_premium,
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/api/discord/bot/stats")
def bot_get_stats(request: Request):
    """Return aggregated site statistics for the `/server-info` command."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        user_count      = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        linked_count    = db.execute(
            "SELECT COUNT(*) FROM discord_accounts WHERE user_id IS NOT NULL"
        ).fetchone()[0]
        tier2_count     = db.execute("SELECT COUNT(*) FROM users WHERE access_level >= 3").fetchone()[0]  # Tier 2 = level 3+
        sub_count       = db.execute("SELECT COUNT(*) FROM users WHERE access_level = 2").fetchone()[0]
        follower_count  = db.execute("SELECT COUNT(*) FROM users WHERE access_level = 1").fetchone()[0]
        answered_q      = db.execute("SELECT COUNT(*) FROM questions WHERE answer IS NOT NULL").fetchone()[0]
        pending_q       = db.execute("SELECT COUNT(*) FROM questions WHERE answer IS NULL").fetchone()[0]
        drool_count     = db.execute("SELECT COUNT(*) FROM drool_archive").fetchone()[0]
        order_count     = db.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'").fetchone()[0]
        activation_count = db.execute("SELECT COUNT(*) FROM activations").fetchone()[0]
    return {
        "user_count":       user_count,
        "linked_count":     linked_count,
        "tier2_count":      tier2_count,
        "sub_count":        sub_count,
        "follower_count":   follower_count,
        "answered_questions": answered_q,
        "pending_questions":  pending_q,
        "drool_count":      drool_count,
        "order_count":      order_count,
        "activation_count": activation_count,
    }


# ── Spotify proxy (access-level gated) ───────────────────────────────────────
# These endpoints wrap the Spotify API using the creator's stored tokens so
# that Discord slash commands can search / queue tracks without needing a
# Fanvue JWT — they use the discord_id → access_level check instead.

async def _get_spotify_token() -> Optional[str]:
    """Return a valid Spotify access token from the settings table, or None."""
    import time

    with get_db_connection() as db:
        access_token    = get_setting(db, "spotify_access_token")
        refresh_token   = get_setting(db, "spotify_refresh_token")
        expires_at_str  = get_setting(db, "spotify_token_expires_at")

    if not refresh_token:
        return None

    if access_token and expires_at_str:
        try:
            if float(expires_at_str) > time.time() + 60:
                return access_token
        except ValueError:
            pass

    # Refresh
    client_id     = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(client_id, client_secret),
            )
        if not resp.is_success:
            return None
        data = resp.json()
        new_token = data["access_token"]
        with get_db_connection() as db:
            from db import set_setting as _set
            _set(db, "spotify_access_token", new_token)
            _set(db, "spotify_token_expires_at", str(time.time() + int(data.get("expires_in", 3600))))
            if "refresh_token" in data:
                _set(db, "spotify_refresh_token", data["refresh_token"])
        return new_token
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spotify token refresh failed: %s", exc)
        return None


def _check_spotify_access(discord_id: str) -> int:
    """Return access_level or raise HTTP 403 if level < 2."""
    with get_db_connection() as db:
        row = db.execute(
            """
            SELECT u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()
    level = row["access_level"] if row else 0
    if level < 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Spotify features require an active Fanvue subscription (Tier 1+).",
        )
    return level


@router.get("/api/discord/bot/spotify/search")
async def bot_spotify_search(q: str, discord_id: str, request: Request):
    """Search Spotify for tracks. Requires level ≥ 2."""
    _check_bot_auth(request)
    _check_spotify_access(discord_id)

    token = await _get_spotify_token()
    if not token:
        return {"tracks": [], "error": "Spotify is not connected."}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{_SPOTIFY_API_BASE}/search",
                params={"q": q, "type": "track", "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
            )
        if not resp.is_success:
            return {"tracks": [], "error": "Spotify search failed."}

        items = resp.json().get("tracks", {}).get("items", [])
        tracks = [
            {
                "id":       t["id"],
                "uri":      t["uri"],
                "name":     t["name"],
                "artist":   ", ".join(a["name"] for a in t.get("artists", [])),
                "album":    t.get("album", {}).get("name", ""),
                "duration": t.get("duration_ms", 0) // 1000,
                "image":    (t.get("album", {}).get("images") or [{}])[0].get("url"),
            }
            for t in items
        ]
        return {"tracks": tracks}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spotify search error: %s", exc)
        return {"tracks": [], "error": "Spotify search failed."}


class SpotifyQueueBody(BaseModel):
    discord_id: str
    track_uri:  str


@router.post("/api/discord/bot/spotify/queue")
async def bot_spotify_queue(body: SpotifyQueueBody, request: Request):
    """Add a track to the creator's Spotify queue. Requires level ≥ 2."""
    _check_bot_auth(request)
    _check_spotify_access(body.discord_id)

    token = await _get_spotify_token()
    if not token:
        return {"success": False, "message": "Spotify is not connected."}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{_SPOTIFY_API_BASE}/me/player/queue",
                params={"uri": body.track_uri},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 204:
            return {"success": True}
        if resp.status_code == 403:
            return {"success": False, "message": "Spotify Premium is required to modify the queue."}
        if resp.status_code == 404:
            return {"success": False, "message": "No active Spotify playback found."}
        return {"success": False, "message": f"Spotify API error ({resp.status_code})."}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spotify queue error: %s", exc)
        return {"success": False, "message": "Could not add track to queue."}


# ── Links CRUD ────────────────────────────────────────────────────────────────
# The bot manages the site's links page (bio-link directory) via these
# endpoints, which mirror /api/admin/links but use bot-token auth.

class BotLinkCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    url:   str = Field(..., min_length=1, max_length=2000)
    emoji: Optional[str] = Field(None, max_length=8)
    sort_order: int = 0
    is_active:  bool = True


class BotLinkUpdate(BaseModel):
    title:      Optional[str]  = Field(None, min_length=1, max_length=200)
    url:        Optional[str]  = Field(None, min_length=1, max_length=2000)
    emoji:      Optional[str]  = Field(None, max_length=8)
    sort_order: Optional[int]  = None
    is_active:  Optional[bool] = None


@router.get("/api/discord/bot/links")
def bot_list_links(request: Request):
    """Return all links (active and inactive) ordered by sort_order."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        rows = db.execute(
            """
            SELECT id, title, url, emoji, sort_order, is_active, created_at
            FROM links
            ORDER BY sort_order ASC, id ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/discord/bot/links", status_code=status.HTTP_201_CREATED)
def bot_create_link(body: BotLinkCreate, request: Request):
    """Create a new link on the site's links page."""
    _check_bot_auth(request)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as db:
            cursor = db.execute(
                """
                INSERT INTO links (title, url, emoji, sort_order, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (body.title, body.url, body.emoji or None,
                 body.sort_order, 1 if body.is_active else 0, created_at),
            )
            db.commit()
            row = db.execute(
                "SELECT id, title, url, emoji, sort_order, is_active, created_at FROM links WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not create link: {exc}",
        ) from exc
    return dict(row)


@router.put("/api/discord/bot/links/{link_id}")
def bot_update_link(link_id: int, body: BotLinkUpdate, request: Request):
    """Update one or more fields on an existing link."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        row = db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")

        new_title      = body.title      if body.title      is not None else row["title"]
        new_url        = body.url        if body.url        is not None else row["url"]
        new_emoji      = (body.emoji or None) if body.emoji is not None else row["emoji"]
        new_sort_order = body.sort_order if body.sort_order is not None else row["sort_order"]
        new_is_active  = (1 if body.is_active else 0) if body.is_active is not None else row["is_active"]

        try:
            db.execute(
                "UPDATE links SET title=?, url=?, emoji=?, sort_order=?, is_active=? WHERE id=?",
                (new_title, new_url, new_emoji, new_sort_order, new_is_active, link_id),
            )
            db.commit()
            updated = db.execute(
                "SELECT id, title, url, emoji, sort_order, is_active, created_at FROM links WHERE id = ?",
                (link_id,),
            ).fetchone()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Could not update link: {exc}",
            ) from exc
    return dict(updated)


@router.delete("/api/discord/bot/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def bot_delete_link(link_id: int, request: Request):
    """Delete a link from the site's links page."""
    _check_bot_auth(request)
    with get_db_connection() as db:
        row = db.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found.")
        db.execute("DELETE FROM links WHERE id = ?", (link_id,))
        db.commit()

