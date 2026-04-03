"""
routers/discord_oauth.py – Discord OAuth2 account linking and Linked Roles.

Provides two complementary Discord integration features:

1. **Account linking** – Authenticated site users can link their Discord
   account to their Fanvue profile so that subscriber metadata is visible
   inside Discord servers.

2. **Linked Roles Verification URL** – The ``GET /discord/verify-user``
   endpoint is set as the *Linked Roles Verification URL* in the Discord
   Developer Portal.  When a server member clicks a role that requires this
   app, Discord redirects them here, triggering a Discord OAuth2 flow that
   ultimately pushes their subscriber tier to Discord so the role can be
   automatically granted.

Endpoints
---------
GET  /discord/verify-user          Linked-Roles entry point (set in Discord Portal).
GET  /discord/link                 Start account-linking flow (pass ?token=<site JWT>).
GET  /discord/callback             Shared OAuth2 callback for both flows.
GET  /api/discord/status           Return link status for the current site user.
DELETE /api/discord/unlink         Remove the Discord link for the current site user.

Configuration
-------------
DISCORD_CLIENT_ID      Discord application client ID.
DISCORD_CLIENT_SECRET  Discord application client secret.
DISCORD_REDIRECT_URI   OAuth2 redirect URI; defaults to {BASE_URL}/discord/callback.
DISCORD_BOT_TOKEN      (optional) Bot token for registering the metadata schema on
                       startup.  Without this the schema must be registered manually.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt as _jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse

from db import get_db_connection
from dependencies import ALGORITHM, SECRET_KEY, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["discord-oauth"])

# ── Discord API constants ─────────────────────────────────────────────────────

_DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_DISCORD_TOKEN_URL     = "https://discord.com/api/oauth2/token"
_DISCORD_API_BASE      = "https://discord.com/api/v10"

# ── OAuth state store ─────────────────────────────────────────────────────────

_STATE_TTL_SECONDS = 600
_discord_oauth_states: dict[str, dict] = {}


def _generate_state(flow_type: str, fanvue_id: Optional[str] = None) -> str:
    """Create a CSRF state token with embedded flow metadata."""
    _prune_states()
    nonce = secrets.token_urlsafe(32)
    _discord_oauth_states[nonce] = {
        "type": flow_type,
        "fanvue_id": fanvue_id,
        "created_at": datetime.now(timezone.utc),
    }
    return nonce


def _consume_state(nonce: str) -> Optional[dict]:
    """Remove and return the state metadata if valid and unexpired, else None."""
    _prune_states()
    entry = _discord_oauth_states.pop(nonce, None)
    if entry is None:
        return None
    age = (datetime.now(timezone.utc) - entry["created_at"]).total_seconds()
    if age > _STATE_TTL_SECONDS:
        return None
    return entry


def _prune_states() -> None:
    """Evict expired state tokens to prevent unbounded growth."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_STATE_TTL_SECONDS)
    expired = [k for k, v in _discord_oauth_states.items() if v["created_at"] < cutoff]
    for k in expired:
        del _discord_oauth_states[k]


# ── Discord OAuth2 helpers ────────────────────────────────────────────────────

def _discord_redirect_uri() -> str:
    base = os.environ.get("BASE_URL", "").rstrip("/")
    return os.environ.get("DISCORD_REDIRECT_URI", f"{base}/discord/callback")


def _build_discord_auth_url(state: str, scopes: list[str]) -> str:
    client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": _discord_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "prompt": "consent",
    })
    return f"{_DISCORD_AUTHORIZE_URL}?{params}"


# ── Linked Roles metadata schema ──────────────────────────────────────────────

# Registered once at startup via PUT /applications/{id}/role-connections/metadata.
# Discord server admins can then create roles that require these metadata values.
_METADATA_SCHEMA = [
    {
        "key": "access_level",
        "name": "Subscriber Tier",
        "description": "Fanvue subscription access level (0–3)",
        "type": 2,  # integer_greater_than_or_equal
    },
    {
        "key": "is_subscriber",
        "name": "Active Subscriber",
        "description": "Has an active Fanvue subscription",
        "type": 7,  # boolean_equal
    },
]


async def register_metadata_schema() -> None:
    """Register the Linked Roles metadata schema with Discord.

    Called once at startup.  Requires ``DISCORD_BOT_TOKEN`` and
    ``DISCORD_CLIENT_ID`` to be set; silently skips otherwise.
    The PUT is idempotent so repeated calls are safe.
    """
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    if not bot_token or not client_id:
        logger.info(
            "Discord Linked Roles metadata schema not registered "
            "(DISCORD_BOT_TOKEN or DISCORD_CLIENT_ID not set)"
        )
        return

    url = f"{_DISCORD_API_BASE}/applications/{client_id}/role-connections/metadata"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                url,
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "Content-Type": "application/json",
                },
                json=_METADATA_SCHEMA,
            )
        if resp.status_code == 200:
            logger.info("Discord Linked Roles metadata schema registered successfully")
        else:
            logger.warning(
                "Failed to register Discord metadata schema: %s %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Exception registering Discord metadata schema: %s", exc)


async def push_discord_metadata(discord_id: str, access_token: str, access_level: int) -> None:
    """Push the user's subscriber metadata to Discord for Linked Roles evaluation.

    Uses the *user's* OAuth2 access token (requires ``role_connections.write``
    scope).  Logs and swallows all failures so a Discord outage never blocks
    the account-linking flow.
    """
    client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    if not client_id:
        return

    url = f"{_DISCORD_API_BASE}/users/@me/applications/{client_id}/role-connection"
    payload = {
        "platform_name": "mochii.live",
        "metadata": {
            "access_level": str(access_level),
            "is_subscriber": "1" if access_level >= 1 else "0",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code == 200:
            logger.info("Discord metadata pushed for discord_id=%s", discord_id)
        else:
            logger.warning(
                "Failed to push Discord metadata for %s: %s %s",
                discord_id,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Exception pushing Discord metadata for %s: %s", discord_id, exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/discord/verify-user")
def discord_verify_user():
    """Linked Roles Verification URL.

    Set this URL in the Discord Developer Portal under:
        Application → OAuth2 → Linked Roles Verification URL

    When a Discord server member tries to earn a role that requires this app,
    Discord redirects them here.  We start a Discord OAuth2 flow with the
    ``role_connections.write`` scope so we can push their subscriber metadata
    back to Discord.
    """
    client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Discord OAuth is not configured on this server.",
        )
    state = _generate_state("linked_roles")
    return RedirectResponse(
        url=_build_discord_auth_url(state, ["identify", "role_connections.write"]),
        status_code=302,
    )


@router.get("/discord/link")
def discord_link(token: Optional[str] = None):
    """Initiate Discord OAuth2 to link a Discord account to an existing site account.

    Pass the site JWT as the ``token`` query parameter.  The JWT is verified
    server-side before issuing the Discord OAuth redirect; it is *not* forwarded
    to Discord.

    Example: redirect the browser to ``/discord/link?token={jwt}``
    """
    client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Discord OAuth is not configured on this server.",
        )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid site token is required to link your Discord account.",
        )

    try:
        payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        fanvue_id: Optional[str] = payload.get("sub")
    except (_jwt.ExpiredSignatureError, _jwt.InvalidTokenError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired site token.",
        ) from exc

    if not fanvue_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid site token payload.",
        )

    state = _generate_state("link_account", fanvue_id=fanvue_id)
    # Request role_connections.write so we can push metadata immediately on link.
    return RedirectResponse(
        url=_build_discord_auth_url(state, ["identify", "role_connections.write"]),
        status_code=302,
    )


@router.get("/discord/callback")
async def discord_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Shared Discord OAuth2 callback for both Linked Roles and account-linking flows."""
    if error:
        logger.warning("Discord OAuth error: %s", error)
        return RedirectResponse(url="/?error=discord_oauth_error", status_code=302)

    if not code or not state:
        return RedirectResponse(url="/?error=discord_missing_params", status_code=302)

    state_data = _consume_state(state)
    if state_data is None:
        return RedirectResponse(url="/?error=discord_invalid_state", status_code=302)

    flow_type: str = state_data["type"]
    fanvue_id: Optional[str] = state_data.get("fanvue_id")

    client_id     = os.environ.get("DISCORD_CLIENT_ID", "")
    client_secret = os.environ.get("DISCORD_CLIENT_SECRET", "")

    # ── Exchange code for tokens ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                _DISCORD_TOKEN_URL,
                data={
                    "grant_type":   "authorization_code",
                    "code":         code,
                    "redirect_uri": _discord_redirect_uri(),
                    "client_id":    client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        logger.error("Discord token exchange request failed: %s", exc)
        return RedirectResponse(url="/?error=discord_token_request_failed", status_code=302)

    if token_resp.status_code != 200:
        logger.error(
            "Discord token exchange failed: %s %s",
            token_resp.status_code,
            token_resp.text,
        )
        return RedirectResponse(url="/?error=discord_token_exchange_failed", status_code=302)

    token_data            = token_resp.json()
    discord_access_token  = token_data.get("access_token")
    discord_refresh_token = token_data.get("refresh_token")
    expires_in: int       = int(token_data.get("expires_in", 604800))

    if not discord_access_token:
        return RedirectResponse(url="/?error=discord_no_access_token", status_code=302)

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # ── Fetch Discord user info ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            user_resp = await client.get(
                f"{_DISCORD_API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {discord_access_token}"},
            )
    except httpx.RequestError as exc:
        logger.error("Discord user fetch failed: %s", exc)
        return RedirectResponse(url="/?error=discord_profile_request_failed", status_code=302)

    if user_resp.status_code != 200:
        logger.error(
            "Discord user fetch failed: %s %s",
            user_resp.status_code,
            user_resp.text,
        )
        return RedirectResponse(url="/?error=discord_profile_fetch_failed", status_code=302)

    discord_user     = user_resp.json()
    discord_id       = str(discord_user.get("id", ""))
    discord_username = discord_user.get("username", "")
    discord_avatar   = discord_user.get("avatar")

    if not discord_id:
        return RedirectResponse(url="/?error=discord_no_user_id", status_code=302)

    if flow_type == "link_account":
        return await _handle_link_account(
            discord_id, discord_username, discord_avatar,
            discord_access_token, discord_refresh_token, expires_at,
            fanvue_id,
        )

    # flow_type == "linked_roles"
    return await _handle_linked_roles(
        discord_id, discord_username, discord_avatar,
        discord_access_token, discord_refresh_token, expires_at,
    )


async def _handle_link_account(
    discord_id: str,
    discord_username: str,
    discord_avatar: Optional[str],
    access_token: str,
    refresh_token: Optional[str],
    expires_at: str,
    fanvue_id: Optional[str],
):
    """Persist the Discord↔Fanvue link and push subscriber metadata."""
    now = datetime.now(timezone.utc).isoformat()

    # Resolve the site user_id from the fanvue_id carried through state.
    user_id: Optional[str] = None
    access_level: Optional[int] = None
    if fanvue_id:
        with get_db_connection() as db:
            row = db.execute(
                "SELECT id, access_level FROM users WHERE fanvue_id = ?",
                (fanvue_id,),
            ).fetchone()
        if row:
            user_id      = row["id"]
            access_level = row["access_level"]

    with get_db_connection() as db:
        db.execute(
            """
            INSERT INTO discord_accounts
                (discord_id, user_id, discord_username, discord_avatar,
                 discord_access_token, discord_refresh_token,
                 discord_token_expires_at, linked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                user_id                  = excluded.user_id,
                discord_username         = excluded.discord_username,
                discord_avatar           = excluded.discord_avatar,
                discord_access_token     = excluded.discord_access_token,
                discord_refresh_token    = excluded.discord_refresh_token,
                discord_token_expires_at = excluded.discord_token_expires_at
            """,
            (
                discord_id, user_id, discord_username, discord_avatar,
                access_token, refresh_token, expires_at, now,
            ),
        )
        db.commit()

    logger.info(
        "Discord account linked: discord_id=%s user_id=%s", discord_id, user_id
    )

    # Push metadata immediately so the linked role can be assigned right away.
    if access_level is not None:
        await push_discord_metadata(discord_id, access_token, access_level)

    return RedirectResponse(url="/?discord_linked=1", status_code=302)


async def _handle_linked_roles(
    discord_id: str,
    discord_username: str,
    discord_avatar: Optional[str],
    access_token: str,
    refresh_token: Optional[str],
    expires_at: str,
):
    """Handle the Linked Roles verification flow.

    Always upserts the Discord token so it stays fresh.  If the Discord
    account is already linked to a Fanvue user, push their subscriber
    metadata and show a success page.  Otherwise show a prompt to link.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Read any existing link so we can preserve user_id on token refresh.
    existing_user_id: Optional[str] = None
    access_level: Optional[int] = None

    with get_db_connection() as db:
        existing = db.execute(
            """
            SELECT da.user_id, u.access_level
            FROM discord_accounts da
            LEFT JOIN users u ON u.id = da.user_id
            WHERE da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()

        if existing:
            existing_user_id = existing["user_id"]
            access_level     = existing["access_level"]

        # Upsert – keep existing user_id, only refresh tokens and profile.
        db.execute(
            """
            INSERT INTO discord_accounts
                (discord_id, user_id, discord_username, discord_avatar,
                 discord_access_token, discord_refresh_token,
                 discord_token_expires_at, linked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_username         = excluded.discord_username,
                discord_avatar           = excluded.discord_avatar,
                discord_access_token     = excluded.discord_access_token,
                discord_refresh_token    = excluded.discord_refresh_token,
                discord_token_expires_at = excluded.discord_token_expires_at
            """,
            (
                discord_id, existing_user_id, discord_username, discord_avatar,
                access_token, refresh_token, expires_at, now,
            ),
        )
        db.commit()

    if existing_user_id and access_level is not None:
        await push_discord_metadata(discord_id, access_token, access_level)
        return HTMLResponse(
            _LINKED_ROLES_SUCCESS_HTML.format(
                username=discord_username,
                access_level=access_level,
            )
        )

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    return HTMLResponse(
        _LINKED_ROLES_NOT_LINKED_HTML.format(
            username=discord_username,
            site_url=base_url or "/",
        )
    )


@router.get("/api/discord/status")
def discord_status(current_user: dict = Depends(get_current_user)):
    """Return the Discord link status for the authenticated site user."""
    fanvue_id: str = current_user["fanvue_id"]
    with get_db_connection() as db:
        row = db.execute(
            """
            SELECT da.discord_id, da.discord_username, da.discord_avatar, da.linked_at
            FROM discord_accounts da
            JOIN users u ON u.id = da.user_id
            WHERE u.fanvue_id = ?
            """,
            (fanvue_id,),
        ).fetchone()

    if row:
        return {
            "linked":           True,
            "discord_id":       row["discord_id"],
            "discord_username": row["discord_username"],
            "discord_avatar":   row["discord_avatar"],
            "linked_at":        row["linked_at"],
        }
    return {"linked": False}


@router.delete("/api/discord/unlink")
def discord_unlink(current_user: dict = Depends(get_current_user)):
    """Remove the Discord link for the authenticated site user.

    Sets ``user_id = NULL`` on the discord_accounts row rather than deleting
    it, so the Discord token row is preserved for future re-linking.
    """
    fanvue_id: str = current_user["fanvue_id"]
    with get_db_connection() as db:
        user_row = db.execute(
            "SELECT id FROM users WHERE fanvue_id = ?", (fanvue_id,)
        ).fetchone()
        if not user_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found."
            )
        db.execute(
            "UPDATE discord_accounts SET user_id = NULL WHERE user_id = ?",
            (user_row["id"],),
        )
        db.commit()
    return {"unlinked": True}


# ── HTML response pages ───────────────────────────────────────────────────────

_LINKED_ROLES_SUCCESS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Discord Verified ✓</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
         justify-content:center;min-height:100vh;background:#1e1e2e;color:#cdd6f4}}
    .card{{text-align:center;padding:2.5rem 2rem;background:#313244;
           border-radius:16px;max-width:440px;width:90%;box-shadow:0 8px 32px #0006}}
    .icon{{font-size:3.5rem;margin-bottom:.75rem}}
    h1{{font-size:1.6rem;margin-bottom:.5rem}}
    p{{color:#a6adc8;line-height:1.6}}
    .badge{{display:inline-block;background:#45475a;border-radius:99px;
            padding:.3rem .9rem;font-size:.85rem;margin:.75rem 0;color:#cdd6f4}}
    .close{{margin-top:1.25rem;font-size:.9rem;color:#6c7086}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>Account Verified!</h1>
    <p>Welcome, <strong>{username}</strong>!</p>
    <p>Your Fanvue subscription has been confirmed.</p>
    <div class="badge">Subscriber Tier {access_level}</div>
    <p class="close">You can now close this window and return to Discord.</p>
  </div>
</body>
</html>"""

_LINKED_ROLES_NOT_LINKED_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Link Your Account</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
         justify-content:center;min-height:100vh;background:#1e1e2e;color:#cdd6f4}}
    .card{{text-align:center;padding:2.5rem 2rem;background:#313244;
           border-radius:16px;max-width:440px;width:90%;box-shadow:0 8px 32px #0006}}
    .icon{{font-size:3.5rem;margin-bottom:.75rem}}
    h1{{font-size:1.6rem;margin-bottom:.5rem}}
    p{{color:#a6adc8;line-height:1.6;margin:.5rem 0}}
    .btn{{display:inline-block;background:#89b4fa;color:#1e1e2e;font-weight:700;
          padding:.65rem 1.5rem;border-radius:10px;text-decoration:none;
          margin-top:1.25rem;font-size:1rem}}
    .hint{{margin-top:1rem;font-size:.85rem;color:#6c7086}}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔗</div>
    <h1>One More Step</h1>
    <p>Hi <strong>{username}</strong> — your Discord account isn't linked to a
       Fanvue subscription yet.</p>
    <p>Log in to mochii.live with your Fanvue account, then click
       <em>"Link Discord"</em> in your profile to earn your role.</p>
    <a class="btn" href="{site_url}">Go to mochii.live</a>
    <p class="hint">Already linked? Try the verification again after logging in.</p>
  </div>
</body>
</html>"""
