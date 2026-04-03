"""
routers/twitter_auth.py – Twitter/X OAuth 1.0a credential authorization.

Allows the site owner to authorize the Drool Log scraper to access their
Twitter/X likes and bookmarks.  The OAuth 1.0a flow obtains user-level
access tokens and saves them to the settings database so the scraper can
use them immediately without manual credential entry.

Flow
----
1. Admin (already logged in) clicks "Connect Twitter/X" in the admin panel
   Drool Log → Twitter/X settings, which hits ``GET /auth/twitter/login``.
2. The backend obtains a request token from Twitter and redirects to the
   Twitter authorization page.
3. Twitter redirects back to ``GET /auth/twitter/callback`` with an
   ``oauth_verifier``.
4. The backend exchanges the verifier for an access token, fetches the
   authenticated user's numeric Twitter ID, and saves the access token,
   access token secret, and user ID to the settings database
   (``drool_twitter_access_token``, ``drool_twitter_access_secret``,
   ``drool_twitter_user_id``).
5. On success the browser is redirected to
   ``/admin.html?twitter_connected=1``.

Required env vars (shared with the Drool Log scraper)
------------------------------------------------------
- ``TWITTER_API_KEY``    – OAuth 1.0a consumer key
- ``TWITTER_API_SECRET`` – OAuth 1.0a consumer secret

Optional env vars
-----------------
- ``BASE_URL`` – Used to build the callback URL.  Must match the callback
                 URL registered in the Twitter Developer Portal.
"""

import logging
import os
import sqlite3 as _sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from db import get_db_connection, set_setting

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Pending-state store: oauth_token → (oauth_token_secret, expiry)
# Keys are cleaned up after 10 minutes or on first use, whichever comes first.
# ---------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600  # 10 minutes

_pending: dict[str, tuple[str, datetime]] = {}


def _store_pending(oauth_token: str, oauth_token_secret: str) -> None:
    _prune_pending()
    expiry = datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS)
    _pending[oauth_token] = (oauth_token_secret, expiry)


def _pop_pending(oauth_token: str) -> Optional[str]:
    """Return and remove the stored token secret, or None if missing/expired."""
    _prune_pending()
    entry = _pending.pop(oauth_token, None)
    if entry is None:
        return None
    secret, expiry = entry
    if datetime.now(timezone.utc) > expiry:
        return None
    return secret


def _prune_pending() -> None:
    now = datetime.now(timezone.utc)
    expired = [k for k, (_, exp) in _pending.items() if exp <= now]
    for k in expired:
        del _pending[k]


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
# Routes
# ---------------------------------------------------------------------------


@router.get("/auth/twitter/login", include_in_schema=False)
def twitter_login():
    """Redirect the admin browser to Twitter's OAuth 1.0a authorization page."""
    try:
        import tweepy  # type: ignore[import-untyped]
    except ImportError:
        return RedirectResponse(
            url="/admin.html?error=tweepy_missing", status_code=302
        )

    api_key = _load_cred("drool_twitter_api_key", "TWITTER_API_KEY")
    api_secret = _load_cred("drool_twitter_api_secret", "TWITTER_API_SECRET")
    if not api_key or not api_secret:
        return RedirectResponse(
            url="/admin.html?error=not_configured", status_code=302
        )

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    callback_url = (
        f"{base_url}/auth/twitter/callback"
        if base_url
        else "/auth/twitter/callback"
    )

    try:
        auth = tweepy.OAuth1UserHandler(
            consumer_key=api_key,
            consumer_secret=api_secret,
            callback=callback_url,
        )
        redirect_url = auth.get_authorization_url()
        oauth_token = auth.request_token["oauth_token"]
        oauth_token_secret = auth.request_token["oauth_token_secret"]
    except Exception as exc:
        logger.error("Failed to obtain Twitter request token: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=oauth_init_failed", status_code=302
        )

    _store_pending(oauth_token, oauth_token_secret)
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/auth/twitter/callback", include_in_schema=False)
def twitter_callback(
    oauth_token: Optional[str] = None,
    oauth_verifier: Optional[str] = None,
    denied: Optional[str] = None,
):
    """Handle the OAuth 1.0a callback redirect from Twitter."""
    # User cancelled the authorization on Twitter's page
    if denied or not oauth_token or not oauth_verifier:
        return RedirectResponse(
            url="/admin.html?error=twitter_cancelled", status_code=302
        )

    oauth_token_secret = _pop_pending(oauth_token)
    if not oauth_token_secret:
        return RedirectResponse(
            url="/admin.html?error=invalid_state", status_code=302
        )

    try:
        import tweepy  # type: ignore[import-untyped]
    except ImportError:
        return RedirectResponse(
            url="/admin.html?error=tweepy_missing", status_code=302
        )

    api_key = _load_cred("drool_twitter_api_key", "TWITTER_API_KEY")
    api_secret = _load_cred("drool_twitter_api_secret", "TWITTER_API_SECRET")

    # Exchange verifier for an access token
    try:
        auth = tweepy.OAuth1UserHandler(
            consumer_key=api_key, consumer_secret=api_secret
        )
        auth.request_token = {
            "oauth_token": oauth_token,
            "oauth_token_secret": oauth_token_secret,
        }
        access_token, access_token_secret = auth.get_access_token(oauth_verifier)
    except Exception as exc:
        logger.error("Twitter access-token exchange failed: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=token_exchange_failed", status_code=302
        )

    # Fetch the authenticated user's numeric Twitter ID
    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        me = client.get_me()
        if not me or not me.data:
            raise ValueError("Empty response from Twitter get_me()")
        twitter_user_id = str(me.data.id)
    except Exception as exc:
        logger.error("Failed to fetch Twitter user info: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=profile_fetch_failed", status_code=302
        )

    # Save the obtained credentials to the settings database so the Drool Log
    # scraper can use them immediately without any manual credential entry.
    try:
        conn = get_db_connection()
        set_setting(conn, "drool_twitter_access_token", access_token)
        set_setting(conn, "drool_twitter_access_secret", access_token_secret)
        set_setting(conn, "drool_twitter_user_id", twitter_user_id)
        conn.close()
        logger.info(
            "Twitter/X scraper credentials saved for user ID %s", twitter_user_id
        )
    except Exception as exc:
        logger.error("Failed to save Twitter credentials to DB: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=db_save_failed", status_code=302
        )

    return RedirectResponse(url="/admin.html?twitter_connected=1", status_code=302)
