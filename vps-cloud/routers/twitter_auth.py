"""
routers/twitter_auth.py – Twitter/X OAuth 2.0 PKCE credential authorization.

A single OAuth 2.0 Authorization Code + PKCE flow covers all Twitter/X
functionality: liked-tweet scraping, bookmark scraping, and tweeting answers.

Flow
----
1. Admin clicks "Connect Twitter/X" in the admin panel (Drool Log → Twitter/X),
   which hits ``GET /auth/twitter2/login``.
2. The backend generates a PKCE ``code_verifier``/``code_challenge``, builds
   the authorization URL with the required scopes, stores the verifier keyed
   by ``state``, and redirects the browser to Twitter.
3. Twitter redirects back to ``GET /auth/twitter2/callback`` with ``code``
   and ``state``.
4. The backend exchanges the code+verifier for an access+refresh token pair,
   then calls ``GET /2/users/me`` to resolve the authenticated user's numeric
   ID.  All four values are saved to the settings database:
   - ``drool_twitter_oauth2_access_token``
   - ``drool_twitter_oauth2_refresh_token``
   - ``drool_twitter_user_id``
5. On success the browser is redirected to ``/admin.html?twitter2_connected=1``.

Required env vars / DB settings
---------------------------------
- ``TWITTER_CLIENT_ID``     – OAuth 2.0 client ID
- ``TWITTER_CLIENT_SECRET`` – OAuth 2.0 client secret

Optional env vars
-----------------
- ``BASE_URL`` – Used to build the callback URL.  Must match the callback
                 URL registered in the Twitter Developer Portal.
"""

import base64
import logging
import os
import sqlite3 as _sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from db import get_db_connection, set_setting

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Pending-state store: persisted in the SQLite oauth_pending table so that
# state survives container restarts between /login and /callback.
# Keys are cleaned up after 10 minutes or on first use, whichever comes first.
# ---------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600  # 10 minutes


def _store_pending(token: str, secret: str) -> None:
    """Persist an OAuth pending state token → secret mapping to the database."""
    expiry = (
        datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS)
    ).isoformat()
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM oauth_pending WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO oauth_pending (token, secret, expires_at)"
            " VALUES (?, ?, ?)",
            (token, secret, expiry),
        )
        conn.commit()
    except _sqlite3.Error as exc:
        logger.error("Failed to store OAuth pending state: %s", exc)
    finally:
        if conn:
            conn.close()


def _pop_pending(token: str) -> Optional[str]:
    """Return and remove the stored secret, or None if missing/expired."""
    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT secret, expires_at FROM oauth_pending WHERE token = ?", (token,)
        ).fetchone()
        if row is not None:
            secret, expires_at = row["secret"], row["expires_at"]
            # Always delete the used token (one-time use regardless of expiry).
            conn.execute("DELETE FROM oauth_pending WHERE token = ?", (token,))
        conn.execute(
            "DELETE FROM oauth_pending WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    except _sqlite3.Error as exc:
        logger.error("Failed to pop OAuth pending state: %s", exc)
        return None
    finally:
        if conn:
            conn.close()
    if row is None:
        return None
    if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
        return None
    return secret


# ---------------------------------------------------------------------------
# Credential helper (DB-first, env-var fallback – mirrors drool_scraper.py)
# ---------------------------------------------------------------------------


def _load_cred(db_key: str, env_key: str) -> str:
    """Return a credential from the settings table, falling back to env var."""
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (db_key,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except _sqlite3.Error as exc:
        logger.debug("Could not read credential '%s' from DB: %s", db_key, exc)
    return os.environ.get(env_key, "")


# ---------------------------------------------------------------------------
# Routes – OAuth 2.0 PKCE (likes, bookmarks, and tweeting)
# ---------------------------------------------------------------------------

# Scopes requested during the OAuth 2.0 PKCE flow.  All Twitter/X features
# used by this site are covered by a single connection:
#   like.read      – fetch liked tweets (Drool Log scraper)
#   bookmark.read  – fetch bookmarks   (Drool Log scraper)
#   tweet.read     – required alongside like.read / bookmark.read
#   tweet.write    – post tweets when an answer is published
#   users.read     – resolve the authenticated user's ID
#   offline.access – obtain a refresh token for long-lived access
_PKCE_SCOPES = "like.read bookmark.read tweet.read tweet.write users.read offline.access"


@router.get("/auth/twitter2/login", include_in_schema=False)
def twitter2_login():
    """Redirect the admin browser to Twitter's OAuth 2.0 PKCE authorization page."""
    try:
        import tweepy  # type: ignore[import-untyped]
    except ImportError:
        return RedirectResponse(
            url="/admin.html?error=tweepy_missing", status_code=302
        )

    client_id = _load_cred("drool_twitter_client_id", "TWITTER_CLIENT_ID")
    client_secret = _load_cred("drool_twitter_client_secret", "TWITTER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return RedirectResponse(
            url="/admin.html?error=oauth2_not_configured", status_code=302
        )

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    callback_url = (
        f"{base_url}/auth/twitter2/callback"
        if base_url
        else "/auth/twitter2/callback"
    )

    try:
        oauth2_handler = tweepy.OAuth2UserHandler(
            client_id=client_id,
            redirect_uri=callback_url,
            scope=_PKCE_SCOPES.split(),
            client_secret=client_secret,
        )
        # tweepy generates the PKCE code verifier/challenge internally.
        # The actual state string lives in _state; the public .state attribute
        # is the state-generator callable.  The code verifier is stored on the
        # underlying oauthlib WebApplicationClient (_client.code_verifier).
        auth_url = oauth2_handler.get_authorization_url()
        state = oauth2_handler._state
        code_verifier = oauth2_handler._client.code_verifier
    except Exception as exc:
        logger.error("Failed to build Twitter OAuth 2.0 authorization URL: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=oauth2_init_failed", status_code=302
        )

    _store_pending(state, code_verifier)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/auth/twitter2/callback", include_in_schema=False)
def twitter2_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Handle the OAuth 2.0 PKCE callback redirect from Twitter."""
    if error or not code or not state:
        return RedirectResponse(
            url="/admin.html?error=twitter2_cancelled", status_code=302
        )

    code_verifier = _pop_pending(state)
    if not code_verifier:
        return RedirectResponse(
            url="/admin.html?error=invalid_state", status_code=302
        )

    client_id = _load_cred("drool_twitter_client_id", "TWITTER_CLIENT_ID")
    client_secret = _load_cred("drool_twitter_client_secret", "TWITTER_CLIENT_SECRET")

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    callback_url = (
        f"{base_url}/auth/twitter2/callback"
        if base_url
        else "/auth/twitter2/callback"
    )

    try:
        credentials = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        resp = httpx.post(
            "https://api.twitter.com/2/oauth2/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url,
                "code_verifier": code_verifier,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except Exception as exc:
        logger.error("Twitter OAuth 2.0 token exchange failed: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=oauth2_token_failed", status_code=302
        )

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if not access_token:
        logger.error("Twitter OAuth 2.0 callback: no access_token in response")
        return RedirectResponse(
            url="/admin.html?error=oauth2_token_failed", status_code=302
        )

    # Resolve the authenticated user's numeric ID from the /2/users/me endpoint.
    try:
        me_resp = httpx.get(
            "https://api.twitter.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        me_resp.raise_for_status()
        twitter_user_id = str(me_resp.json()["data"]["id"])
    except Exception as exc:
        logger.error("Twitter OAuth 2.0 callback: failed to fetch user ID: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=profile_fetch_failed", status_code=302
        )

    try:
        conn = get_db_connection()
        set_setting(conn, "drool_twitter_oauth2_access_token", access_token)
        if refresh_token:
            set_setting(conn, "drool_twitter_oauth2_refresh_token", refresh_token)
        set_setting(conn, "drool_twitter_user_id", twitter_user_id)
        conn.close()
        logger.info("Twitter/X OAuth 2.0 tokens saved for user ID %s", twitter_user_id)
    except Exception as exc:
        logger.error("Failed to save Twitter OAuth 2.0 tokens to DB: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=db_save_failed", status_code=302
        )

    return RedirectResponse(url="/admin.html?twitter2_connected=1", status_code=302)
