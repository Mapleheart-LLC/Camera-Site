"""
routers/twitter_auth.py – Twitter/X OAuth credential authorization.

Allows the site owner to authorize the Drool Log scraper to access their
Twitter/X likes and bookmarks via two separate flows:

OAuth 1.0a flow (likes)
-----------------------
1. Admin clicks "Connect Twitter/X" in the admin panel, Drool Log → Twitter/X
   settings, which hits ``GET /auth/twitter/login``.
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

OAuth 2.0 PKCE flow (bookmarks)
--------------------------------
Twitter's ``GET /2/users/:id/bookmarks`` endpoint requires OAuth 2.0
Authorization Code with PKCE – bearer tokens and OAuth 1.0a both return 403.

1. Admin clicks "Connect Twitter/X Bookmarks" in the admin panel, which hits
   ``GET /auth/twitter2/login``.
2. The backend generates a PKCE ``code_verifier``/``code_challenge``, builds
   the authorization URL with ``bookmark.read tweet.read users.read`` scopes,
   stores the verifier keyed by ``state``, and redirects to Twitter.
3. Twitter redirects back to ``GET /auth/twitter2/callback`` with ``code``
   and ``state``.
4. The backend exchanges the code+verifier for an access+refresh token pair,
   saves both to the settings database
   (``drool_twitter_oauth2_access_token``,
   ``drool_twitter_oauth2_refresh_token``).
5. On success the browser is redirected to
   ``/admin.html?twitter2_connected=1``.

Required env vars (shared with the Drool Log scraper)
------------------------------------------------------
- ``TWITTER_API_KEY``    – OAuth 1.0a consumer key
- ``TWITTER_API_SECRET`` – OAuth 1.0a consumer secret

Required env vars for OAuth 2.0 PKCE
--------------------------------------
- ``TWITTER_CLIENT_ID``     – OAuth 2.0 client ID
- ``TWITTER_CLIENT_SECRET`` – OAuth 2.0 client secret

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
# Routes – OAuth 1.0a (liked tweets)
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


# ---------------------------------------------------------------------------
# Routes – OAuth 2.0 PKCE (bookmarks)
# ---------------------------------------------------------------------------

_PKCE_SCOPES = "bookmark.read tweet.read users.read offline.access"


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
        auth_url = oauth2_handler.get_authorization_url()
        state = oauth2_handler.state
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

    try:
        import tweepy  # type: ignore[import-untyped]
    except ImportError:
        return RedirectResponse(
            url="/admin.html?error=tweepy_missing", status_code=302
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
        oauth2_handler = tweepy.OAuth2UserHandler(
            client_id=client_id,
            redirect_uri=callback_url,
            scope=_PKCE_SCOPES.split(),
            client_secret=client_secret,
        )
        # Restore the code verifier and state so tweepy can complete the exchange.
        oauth2_handler._client.code_verifier = code_verifier
        oauth2_handler.state = state
        authorization_response = f"{callback_url}?code={code}&state={state}"
        token_data = oauth2_handler.fetch_token(authorization_response=authorization_response)
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

    try:
        conn = get_db_connection()
        set_setting(conn, "drool_twitter_oauth2_access_token", access_token)
        if refresh_token:
            set_setting(conn, "drool_twitter_oauth2_refresh_token", refresh_token)
        conn.close()
        logger.info("Twitter/X OAuth 2.0 tokens saved for bookmark scraping.")
    except Exception as exc:
        logger.error("Failed to save Twitter OAuth 2.0 tokens to DB: %s", exc)
        return RedirectResponse(
            url="/admin.html?error=db_save_failed", status_code=302
        )

    return RedirectResponse(url="/admin.html?twitter2_connected=1", status_code=302)
