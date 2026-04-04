"""routers/spotify.py – Spotify Now Playing & Queue integration.

Endpoints
---------
GET  /auth/spotify/login       Admin-initiated OAuth authorization redirect
GET  /auth/spotify/callback    OAuth callback – exchanges code and stores tokens
GET  /api/spotify/now-playing  Current playback state (no viewer auth required)
GET  /api/spotify/search       Search for tracks (access_level >= 2)
POST /api/spotify/queue        Add a track to the creator's queue (access_level >= 2)
"""

import logging
import os
import secrets
import sqlite3
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from db import get_db, get_setting, set_setting
from dependencies import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

_SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# Scopes needed: read current playback + modify queue
_SCOPES = (
    "user-read-currently-playing "
    "user-read-playback-state "
    "user-modify-playback-state"
)

# Short-lived CSRF state tokens: {state: expiry_timestamp}
_pending_states: dict[str, float] = {}
_STATE_TTL = 600  # 10 minutes

# Simple in-memory cache for now-playing to reduce Spotify API load
# when multiple viewers are watching simultaneously.
_np_cache: tuple[float, dict] | None = None  # (expires_at, data)
_NP_CACHE_TTL = 3.0  # seconds


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_creds() -> tuple[str, str]:
    return (
        os.environ.get("SPOTIFY_CLIENT_ID", ""),
        os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
    )


def _redirect_uri() -> str:
    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    return (
        f"{base_url}/auth/spotify/callback"
        if base_url
        else "/auth/spotify/callback"
    )


async def _get_valid_access_token(db: sqlite3.Connection) -> Optional[str]:
    """Return a valid Spotify access token, refreshing if expired."""
    access_token = get_setting(db, "spotify_access_token")
    refresh_token = get_setting(db, "spotify_refresh_token")
    expires_at_str = get_setting(db, "spotify_token_expires_at")

    if not refresh_token:
        return None

    # Use existing token if still fresh (60-second buffer)
    if access_token and expires_at_str:
        try:
            if float(expires_at_str) > time.time() + 60:
                return access_token
        except ValueError:
            pass

    # Need to refresh
    client_id, client_secret = _get_client_creds()
    if not client_id or not client_secret:
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
        )

    if not resp.is_success:
        logger.error("Spotify token refresh failed: %s", resp.text)
        return None

    data = resp.json()
    new_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))

    set_setting(db, "spotify_access_token", new_token)
    set_setting(db, "spotify_token_expires_at", str(time.time() + expires_in))
    if "refresh_token" in data:
        set_setting(db, "spotify_refresh_token", data["refresh_token"])

    return new_token


# ── Admin OAuth flow ──────────────────────────────────────────────────────────

@router.get("/auth/spotify/login", include_in_schema=False)
async def spotify_login():
    """Redirect admin browser to Spotify's authorization page."""
    client_id, _ = _get_client_creds()
    if not client_id:
        return RedirectResponse(
            url="/admin.html?error=spotify_not_configured", status_code=302
        )

    state = secrets.token_urlsafe(16)
    _pending_states[state] = time.time() + _STATE_TTL

    # Prune expired states to prevent unbounded memory growth
    now = time.time()
    expired = [k for k, v in _pending_states.items() if v < now]
    for k in expired:
        del _pending_states[k]

    params = httpx.QueryParams(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
            "scope": _SCOPES,
            "state": state,
        }
    )
    return RedirectResponse(url=f"{_SPOTIFY_AUTH_URL}?{params}", status_code=302)


@router.get("/auth/spotify/callback", include_in_schema=False)
async def spotify_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    """Handle Spotify OAuth callback and store tokens."""
    if error or not code:
        return RedirectResponse(
            url="/admin.html?error=spotify_cancelled", status_code=302
        )

    # Validate CSRF state
    expiry = _pending_states.pop(state, None) if state else None
    if expiry is None or time.time() > expiry:
        return RedirectResponse(
            url="/admin.html?error=spotify_invalid_state", status_code=302
        )

    client_id, client_secret = _get_client_creds()

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
            auth=(client_id, client_secret),
        )

    if not resp.is_success:
        logger.error("Spotify token exchange failed: %s", resp.text)
        return RedirectResponse(
            url="/admin.html?error=spotify_token_failed", status_code=302
        )

    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))

    set_setting(db, "spotify_access_token", data["access_token"])
    set_setting(db, "spotify_refresh_token", data["refresh_token"])
    set_setting(db, "spotify_token_expires_at", str(time.time() + expires_in))

    logger.info("Spotify OAuth connected successfully")
    return RedirectResponse(url="/admin.html?spotify=connected", status_code=302)


# ── Public now-playing ────────────────────────────────────────────────────────

@router.get("/api/spotify/now-playing")
async def now_playing(db: sqlite3.Connection = Depends(get_db)):
    """Return the currently playing Spotify track. No viewer auth required."""
    global _np_cache

    now = time.time()
    if _np_cache and now < _np_cache[0]:
        return _np_cache[1]

    access_token = await _get_valid_access_token(db)
    if not access_token:
        result: dict = {"is_playing": False, "configured": False}
        _np_cache = (now + _NP_CACHE_TTL, result)
        return result

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{_SPOTIFY_API_BASE}/me/player/currently-playing",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 204:
        result = {"is_playing": False, "configured": True}
        _np_cache = (now + _NP_CACHE_TTL, result)
        return result

    if not resp.is_success:
        logger.warning("Spotify now-playing error: %s", resp.status_code)
        result = {"is_playing": False, "configured": True}
        _np_cache = (now + _NP_CACHE_TTL, result)
        return result

    data = resp.json()
    item = data.get("item")
    if not item or data.get("currently_playing_type") != "track":
        result = {"is_playing": data.get("is_playing", False), "configured": True}
        _np_cache = (now + _NP_CACHE_TTL, result)
        return result

    images = item.get("album", {}).get("images", [])
    result = {
        "is_playing": data.get("is_playing", False),
        "configured": True,
        "track": {
            "name": item["name"],
            "artists": [a["name"] for a in item.get("artists", [])],
            "album": item["album"]["name"],
            "album_art": images[0]["url"] if images else None,
            "duration_ms": item["duration_ms"],
            "progress_ms": data.get("progress_ms", 0),
            "uri": item["uri"],
            "external_url": item.get("external_urls", {}).get("spotify"),
        },
    }
    _np_cache = (now + _NP_CACHE_TTL, result)
    return result


# ── Authenticated search & queue (access_level >= 2) ─────────────────────────

@router.get("/api/spotify/search")
async def search_tracks(
    q: str = Query(..., min_length=1, max_length=200),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Search Spotify for tracks. Requires access_level >= 2 (Tier 1+)."""
    if (user.get("access_level") or 0) < 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tier 1 subscription required.",
        )

    access_token = await _get_valid_access_token(db)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify not configured.",
        )

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{_SPOTIFY_API_BASE}/search",
            params={"q": q, "type": "track", "limit": 5},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if not resp.is_success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Spotify search failed.",
        )

    tracks = []
    for item in resp.json().get("tracks", {}).get("items", []):
        images = item.get("album", {}).get("images", [])
        tracks.append(
            {
                "name": item["name"],
                "artists": [a["name"] for a in item.get("artists", [])],
                "album": item["album"]["name"],
                # Smallest image (last) for compact thumbnails
                "album_art": images[-1]["url"] if images else None,
                "uri": item["uri"],
            }
        )

    return {"tracks": tracks}


class QueueRequest(BaseModel):
    uri: str


@router.post("/api/spotify/queue", status_code=status.HTTP_200_OK)
async def add_to_queue(
    body: QueueRequest,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Add a track to the creator's Spotify queue. Requires access_level >= 2 (Tier 1+)."""
    if (user.get("access_level") or 0) < 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tier 1 subscription required.",
        )

    if not body.uri.startswith("spotify:track:"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Spotify track URI.",
        )

    access_token = await _get_valid_access_token(db)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify not configured.",
        )

    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            f"{_SPOTIFY_API_BASE}/me/player/queue",
            params={"uri": body.uri},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 204:
        return {"ok": True}

    if resp.status_code == 403:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify Premium required for queue.",
        )

    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No active Spotify playback device found.",
        )

    try:
        err_msg = resp.json().get("error", {}).get("message", "Failed to add to queue.")
    except Exception:
        err_msg = "Failed to add to queue."

    logger.error("Spotify add-to-queue failed (%d): %s", resp.status_code, resp.text)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err_msg)
