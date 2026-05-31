"""
drool_scraper.py – Permanent Record scraper for The Drool Log.

Fetches liked/saved content from Reddit (via private JSON feed URLs),
Twitter/X (via tweepy), and Bluesky (via atproto) on a 5-minute schedule
using APScheduler, saving only new items to the drool_archive table.

Configuration (environment variables – all also settable via admin panel)
-------------------------------------------------------------------------
REDDIT_JSON_SAVED_URL   – Full private JSON feed URL for saved posts.
                          Obtain from old.reddit.com by appending
                          ?feed=<token>&user=<username> to the saved feed path.
REDDIT_JSON_UPVOTED_URL – Full private JSON feed URL for upvoted posts.
                          Same format as REDDIT_JSON_SAVED_URL.

TWITTER_BEARER_TOKEN   – Twitter/X app-only ****** (optional if user auth is set)
TWITTER_USER_ID        – Numeric Twitter/X user ID to scrape
TWITTER_API_KEY        – Twitter/X API Key (consumer key) – for OAuth 1.0a user auth
TWITTER_API_SECRET     – Twitter/X API Secret
TWITTER_ACCESS_TOKEN   – Twitter/X Access Token (user auth) – obtained via admin OAuth flow
TWITTER_ACCESS_SECRET  – Twitter/X Access Token Secret

TWITTER_CLIENT_ID      – OAuth 2.0 Client ID – required for bookmark scraping
TWITTER_CLIENT_SECRET  – OAuth 2.0 Client Secret – required for bookmark scraping
                         (OAuth 2.0 tokens are stored in the settings DB after the
                          /auth/twitter2/login PKCE flow; they are not set via env var)

BSKY_HANDLE            – Bluesky handle (e.g. yourname.bsky.social)
BSKY_APP_PASSWORD      – Bluesky app password (from Settings → App Passwords)

DISCORD_WEBHOOK_URL    – (shared) Discord webhook for new-item pings
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import get_db_connection

try:
    import tweepy as _tweepy  # type: ignore[import-untyped]
    _TWEEPY_AVAILABLE = True
except ImportError:  # noqa: BLE001
    _tweepy = None  # type: ignore[assignment]
    _TWEEPY_AVAILABLE = False

try:
    from atproto import Client as _AtprotoClient  # type: ignore[import-untyped]
    _ATPROTO_AVAILABLE = True
except ImportError:  # noqa: BLE001
    _AtprotoClient = None  # type: ignore[assignment]
    _ATPROTO_AVAILABLE = False

logger = logging.getLogger(__name__)

_TWITTER_AUTH_BACKOFF_SECONDS = max(60, int(os.environ.get("TWITTER_AUTH_BACKOFF_SECONDS", "1800")))
_TWITTER_AUTH_BACKOFF_UNTIL: dict[str, float] = {
    "likes": 0.0,
    "bookmarks": 0.0,
}

# ---------------------------------------------------------------------------
# Credential helper – DB-first with env-var fallback
# ---------------------------------------------------------------------------


def _load_credential(db_key: str, env_key: str) -> str:
    """Return a scraper credential from the settings table, falling back to env."""
    import sqlite3 as _sqlite3  # noqa: PLC0415 – local import avoids top-level circular risk
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (db_key,)).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except _sqlite3.Error as exc:
        logger.debug("Could not read credential '%s' from DB: %s", db_key, exc)
    return os.environ.get(env_key, "")



# ---------------------------------------------------------------------------
# Scheduler (module-level singleton; started / stopped by main.py lifespan)
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Discord helper (reuse existing webhook)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Reddit scraper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reddit scraper
# ---------------------------------------------------------------------------

_REDDIT_JSON_FEED_HEADERS = {"User-Agent": "CameraSiteDroolFeed/1.0"}


def _scrape_reddit() -> None:
    """Fetch saved and upvoted Reddit posts via private JSON feed URLs.

    No OAuth is required – the URLs embed a personal feed token.  Configure
    the URLs via the settings DB or environment variables:

    - ``drool_reddit_json_saved_url``   / ``REDDIT_JSON_SAVED_URL``
    - ``drool_reddit_json_upvoted_url`` / ``REDDIT_JSON_UPVOTED_URL``
    """
    saved_url   = (
        _load_credential("drool_reddit_json_saved_url",   "REDDIT_JSON_SAVED_URL")

    )
    upvoted_url = (
        _load_credential("drool_reddit_json_upvoted_url", "REDDIT_JSON_UPVOTED_URL")

    )

    feed_urls = [
        (saved_url,   "saved"),
        (upvoted_url, "upvoted"),
    ]

    items: list[tuple] = []

    for feed_url, label in feed_urls:
        try:
            resp = httpx.get(
                feed_url,
                headers=_REDDIT_JSON_FEED_HEADERS,
                follow_redirects=True,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Reddit JSON feed scraper (%s): fetch failed: %s", label, exc)
            continue

        try:
            children = data["data"]["children"]
        except (KeyError, TypeError) as exc:
            logger.warning("Reddit JSON feed scraper (%s): unexpected JSON structure: %s", label, exc)
            continue

        for child in children:
            child_data = child.get("data", {})

            # Filter out comments – only posts have a title field.
            if "title" not in child_data:
                continue

            title     = child_data.get("title", "") or ""
            subreddit = child_data.get("subreddit", "") or ""
            permalink = child_data.get("permalink", "")

            # Use the post's linked URL when present and absolute; otherwise
            # fall back to the Reddit permalink.
            raw_url = child_data.get("url", "") or ""
            if raw_url and raw_url.startswith("http"):
                media_url: Optional[str] = raw_url
            else:
                media_url = None

            orig_url = (
                f"https://reddit.com{permalink}"
                if permalink
                else raw_url or ""
            )
            if not orig_url:
                continue

            created_utc = child_data.get("created_utc")
            if created_utc:
                try:
                    ts = datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    ts = datetime.now(timezone.utc).isoformat()
            else:
                ts = datetime.now(timezone.utc).isoformat()

            text_content = title
            if subreddit:
                text_content = f"[r/{subreddit}] {title}"

            items.append(("reddit", orig_url, media_url, text_content, ts))

    if not items:
        logger.debug("Reddit JSON feed scraper: no posts retrieved.")
        return

    conn = get_db_connection()
    try:
        new_count = 0
        newly_inserted: list[tuple] = []
        for platform, orig_url, media_url, text_content, ts in items:
            existing = conn.execute(
                "SELECT id FROM drool_archive WHERE original_url = ?", (orig_url,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO drool_archive (platform, original_url, media_url, text_content, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (platform, orig_url, media_url or None, text_content or None, ts),
            )
            newly_inserted.append((platform, orig_url, media_url, text_content, ts))
            new_count += 1
        conn.commit()
        if new_count:
            logger.info("Reddit JSON feed scraper: archived %d new item(s).", new_count)
            _notify_new_items(newly_inserted)
        else:
            logger.debug("Reddit JSON feed scraper: no new items.")
    except Exception as exc:
        logger.error("Reddit JSON feed scraper error: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Twitter / X scraper
# ---------------------------------------------------------------------------


def _get_tweepy_client() -> Optional[object]:
    """Return an authenticated tweepy.Client or None if credentials are missing."""
    if not _TWEEPY_AVAILABLE:
        logger.debug("tweepy is not installed; Twitter scraper disabled.")
        return None

    bearer        = _load_credential("drool_twitter_bearer_token",  "TWITTER_BEARER_TOKEN")
    api_key       = _load_credential("drool_twitter_api_key",       "TWITTER_API_KEY")
    api_secret    = _load_credential("drool_twitter_api_secret",    "TWITTER_API_SECRET")
    access_token  = _load_credential("drool_twitter_access_token",  "TWITTER_ACCESS_TOKEN")
    access_secret = _load_credential("drool_twitter_access_secret", "TWITTER_ACCESS_SECRET")

    # Require at least a bearer token OR a full user-auth token pair so the
    # client can make authenticated calls (liked tweets work with either).
    if not bearer and not (access_token and access_secret):
        return None

    try:
        return _tweepy.Client(
            bearer_token=bearer or None,
            consumer_key=api_key or None,
            consumer_secret=api_secret or None,
            access_token=access_token or None,
            access_token_secret=access_secret or None,
            wait_on_rate_limit=False,
        )
    except Exception as exc:
        logger.warning("Could not initialise tweepy Client: %s", exc)
        return None


def _refresh_oauth2_token() -> Optional[str]:
    """Refresh the OAuth 2.0 access token using the stored refresh token.

    Saves the new access and refresh tokens to the DB and returns the new
    access token, or None if refresh is not possible.
    """
    import sqlite3 as _sqlite3  # noqa: PLC0415

    try:
        import requests as _requests  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("requests library not available; cannot refresh OAuth 2.0 token.")
        return None

    client_id     = _load_credential("drool_twitter_client_id",            "TWITTER_CLIENT_ID")
    client_secret = _load_credential("drool_twitter_client_secret",        "TWITTER_CLIENT_SECRET")
    refresh_token = _load_credential("drool_twitter_oauth2_refresh_token", "")

    if not client_id or not client_secret or not refresh_token:
        return None

    try:
        resp = _requests.post(
            "https://api.twitter.com/2/oauth2/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "client_id":     client_id,
            },
            auth=(client_id, client_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Twitter OAuth 2.0 token refresh failed: %s", exc)
        return None

    new_access  = data.get("access_token", "")
    new_refresh = data.get("refresh_token", "")

    if not new_access:
        logger.warning("Twitter OAuth 2.0 token refresh returned no access_token.")
        return None

    try:
        from db import set_setting as _set_setting  # noqa: PLC0415
        conn = get_db_connection()
        _set_setting(conn, "drool_twitter_oauth2_access_token", new_access)
        if new_refresh:
            _set_setting(conn, "drool_twitter_oauth2_refresh_token", new_refresh)
        conn.close()
        logger.info("Twitter/X OAuth 2.0 access token refreshed successfully.")
    except _sqlite3.Error as exc:
        logger.warning("Could not persist refreshed OAuth 2.0 tokens: %s", exc)

    return new_access


def _get_oauth2_client() -> Optional[object]:
    """Return a tweepy.Client authenticated with the OAuth 2.0 user access token.

    If the stored access token is missing, attempts a refresh.  Returns None
    if no usable token is available (bookmarks scraping will be skipped).
    """
    if not _TWEEPY_AVAILABLE:
        return None

    oauth2_token = _load_credential("drool_twitter_oauth2_access_token", "")
    if not oauth2_token:
        oauth2_token = _refresh_oauth2_token() or ""
    if not oauth2_token:
        return None

    try:
        return _tweepy.Client(
            bearer_token=oauth2_token,
            wait_on_rate_limit=False,
        )
    except Exception as exc:
        logger.warning("Could not initialise OAuth 2.0 tweepy Client: %s", exc)
        return None


def _twitter_exc_status(exc: Exception) -> Optional[int]:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    if response is not None:
        response_code = getattr(response, "status_code", None)
        if isinstance(response_code, int):
            return response_code
    text = str(exc).lower()
    if "401" in text or "unauthorized" in text:
        return 401
    if "403" in text or "forbidden" in text:
        return 403
    return None


def _twitter_is_auth_error(exc: Exception) -> bool:
    status = _twitter_exc_status(exc)
    return status in {401, 403}


def _twitter_auth_message(exc: Exception) -> str:
    # Tweepy exceptions can include duplicate/newline payload text.
    return str(exc).strip().splitlines()[0][:220]


def _twitter_backoff_active(endpoint: str) -> bool:
    until = float(_TWITTER_AUTH_BACKOFF_UNTIL.get(endpoint, 0.0) or 0.0)
    now = time.time()
    if now >= until:
        return False
    remaining = int(until - now)
    logger.debug(
        "Twitter scraper: %s auth backoff active (%ss remaining); skipping endpoint.",
        endpoint,
        max(1, remaining),
    )
    return True


def _twitter_set_auth_backoff(endpoint: str, exc: Exception) -> None:
    until = time.time() + _TWITTER_AUTH_BACKOFF_SECONDS
    _TWITTER_AUTH_BACKOFF_UNTIL[endpoint] = until
    retry_at = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    logger.warning(
        "Twitter scraper: %s auth failed (%s). Backing off for %ss (until %s UTC). "
        "Reauthorize Twitter tokens in admin panel if this persists.",
        endpoint,
        _twitter_auth_message(exc),
        _TWITTER_AUTH_BACKOFF_SECONDS,
        retry_at,
    )


def _scrape_twitter() -> None:
    """Fetch liked and bookmarked tweets and store new ones in drool_archive."""
    user_id = _load_credential("drool_twitter_user_id", "TWITTER_USER_ID")
    if not user_id:
        logger.debug("Twitter scraper: TWITTER_USER_ID not set, skipping.")
        return

    # Prefer OAuth 2.0 user context (single connection covers both liked tweets
    # and bookmarks).  Fall back to the legacy OAuth 1.0a / bearer-token client
    # if OAuth 2.0 credentials have not been configured yet.
    oauth2_client = _get_oauth2_client()
    legacy_client = None
    if oauth2_client is None:
        legacy_client = _get_tweepy_client()
        if legacy_client is None:
            logger.debug("Twitter scraper: credentials not configured, skipping.")
            return

    conn = get_db_connection()
    try:
        items: list[tuple] = []

        # Liked tweets – use OAuth 2.0 when available; otherwise fall back to
        # OAuth 1.0a / bearer token.  When using the OAuth 2.0 client tweepy
        # sends the user-context access token as the Bearer header, which
        # satisfies the like.read scope requirement.
        likes_client = oauth2_client if oauth2_client is not None else legacy_client
        # user_auth=True is only meaningful for OAuth 1.0a; always False here.
        use_user_auth = oauth2_client is None and bool(
            _load_credential("drool_twitter_access_token",  "TWITTER_ACCESS_TOKEN")
            and _load_credential("drool_twitter_access_secret", "TWITTER_ACCESS_SECRET")
        )

        if not _twitter_backoff_active("likes"):
            # Liked tweets
            try:
                resp = likes_client.get_liked_tweets(
                    id=user_id,
                    user_auth=use_user_auth,
                    max_results=50,
                    tweet_fields=["created_at", "text", "attachments"],
                    expansions=["attachments.media_keys"],
                    media_fields=["url", "preview_image_url", "type"],
                )
                if resp and resp.data:
                    media_map: dict = {}
                    if resp.includes and "media" in resp.includes:
                        for m in resp.includes["media"]:
                            media_map[m.media_key] = getattr(m, "url", None) or getattr(
                                m, "preview_image_url", None
                            )
                    for tweet in resp.data:
                        url = f"https://fxtwitter.com/i/web/status/{tweet.id}"
                        att = getattr(tweet, "attachments", None) or {}
                        mks = att.get("media_keys") or []
                        tweet_media_urls = []
                        for mk in mks:
                            resolved = media_map.get(mk)
                            if resolved:
                                tweet_media_urls.append(resolved)
                        media_url: Optional[str] = tweet_media_urls[0] if tweet_media_urls else None
                        media_urls_json: Optional[str] = json.dumps(tweet_media_urls) if tweet_media_urls else None
                        ts = (
                            tweet.created_at.isoformat()
                            if tweet.created_at
                            else datetime.now(timezone.utc).isoformat()
                        )
                        items.append(("twitter", url, media_url, tweet.text, ts, media_urls_json))
            except Exception as exc:
                # OAuth 2.0 token can expire; refresh once and retry likes call.
                if oauth2_client is not None and _twitter_exc_status(exc) == 401:
                    refreshed = _refresh_oauth2_token()
                    if refreshed:
                        retry_client = _get_oauth2_client()
                        if retry_client is not None:
                            try:
                                resp = retry_client.get_liked_tweets(
                                    id=user_id,
                                    user_auth=False,
                                    max_results=50,
                                    tweet_fields=["created_at", "text", "attachments"],
                                    expansions=["attachments.media_keys"],
                                    media_fields=["url", "preview_image_url", "type"],
                                )
                                if resp and resp.data:
                                    media_map: dict = {}
                                    if resp.includes and "media" in resp.includes:
                                        for m in resp.includes["media"]:
                                            media_map[m.media_key] = getattr(m, "url", None) or getattr(
                                                m, "preview_image_url", None
                                            )
                                    for tweet in resp.data:
                                        url = f"https://fxtwitter.com/i/web/status/{tweet.id}"
                                        att = getattr(tweet, "attachments", None) or {}
                                        mks = att.get("media_keys") or []
                                        tweet_media_urls = []
                                        for mk in mks:
                                            resolved = media_map.get(mk)
                                            if resolved:
                                                tweet_media_urls.append(resolved)
                                        media_url: Optional[str] = tweet_media_urls[0] if tweet_media_urls else None
                                        media_urls_json: Optional[str] = json.dumps(tweet_media_urls) if tweet_media_urls else None
                                        ts = (
                                            tweet.created_at.isoformat()
                                            if tweet.created_at
                                            else datetime.now(timezone.utc).isoformat()
                                        )
                                        items.append(("twitter", url, media_url, tweet.text, ts, media_urls_json))
                            except Exception as retry_exc:
                                if _twitter_is_auth_error(retry_exc):
                                    _twitter_set_auth_backoff("likes", retry_exc)
                                else:
                                    logger.warning(
                                        "Twitter scraper: liked tweets fetch failed after token refresh: %s",
                                        _twitter_auth_message(retry_exc),
                                    )
                        else:
                            _twitter_set_auth_backoff("likes", exc)
                    else:
                        _twitter_set_auth_backoff("likes", exc)
                elif _twitter_is_auth_error(exc):
                    _twitter_set_auth_backoff("likes", exc)
                else:
                    logger.warning("Twitter scraper: liked tweets fetch failed: %s", _twitter_auth_message(exc))

        # Bookmarks – always uses OAuth 2.0 PKCE user context (the bookmarks
        # endpoint returns 403 for bearer tokens and OAuth 1.0a).
        if oauth2_client is not None:
            if _twitter_backoff_active("bookmarks"):
                pass
            else:
                try:
                    bk_resp = oauth2_client.get_bookmarks(
                        max_results=50,
                        tweet_fields=["created_at", "text", "attachments"],
                        expansions=["attachments.media_keys"],
                        media_fields=["url", "preview_image_url", "type"],
                    )
                    if bk_resp and bk_resp.data:
                        bk_media_map: dict = {}
                        if bk_resp.includes and "media" in bk_resp.includes:
                            for m in bk_resp.includes["media"]:
                                bk_media_map[m.media_key] = getattr(m, "url", None) or getattr(
                                    m, "preview_image_url", None
                                )
                        for tweet in bk_resp.data:
                            url = f"https://fxtwitter.com/i/web/status/{tweet.id}"
                            att = getattr(tweet, "attachments", None) or {}
                            mks = att.get("media_keys") or []
                            bk_tweet_media_urls = []
                            for mk in mks:
                                resolved = bk_media_map.get(mk)
                                if resolved:
                                    bk_tweet_media_urls.append(resolved)
                            bk_media_url: Optional[str] = bk_tweet_media_urls[0] if bk_tweet_media_urls else None
                            bk_media_urls_json: Optional[str] = json.dumps(bk_tweet_media_urls) if bk_tweet_media_urls else None
                            ts = (
                                tweet.created_at.isoformat()
                                if tweet.created_at
                                else datetime.now(timezone.utc).isoformat()
                            )
                            items.append(("twitter", url, bk_media_url, tweet.text, ts, bk_media_urls_json))
                except Exception as exc:
                    if _twitter_exc_status(exc) == 401:
                        refreshed = _refresh_oauth2_token()
                        if refreshed:
                            retry_client = _get_oauth2_client()
                            if retry_client is not None:
                                try:
                                    bk_resp = retry_client.get_bookmarks(
                                        max_results=50,
                                        tweet_fields=["created_at", "text", "attachments"],
                                        expansions=["attachments.media_keys"],
                                        media_fields=["url", "preview_image_url", "type"],
                                    )
                                    if bk_resp and bk_resp.data:
                                        bk_media_map: dict = {}
                                        if bk_resp.includes and "media" in bk_resp.includes:
                                            for m in bk_resp.includes["media"]:
                                                bk_media_map[m.media_key] = getattr(m, "url", None) or getattr(
                                                    m, "preview_image_url", None
                                                )
                                        for tweet in bk_resp.data:
                                            url = f"https://fxtwitter.com/i/web/status/{tweet.id}"
                                            att = getattr(tweet, "attachments", None) or {}
                                            mks = att.get("media_keys") or []
                                            bk_tweet_media_urls = []
                                            for mk in mks:
                                                resolved = bk_media_map.get(mk)
                                                if resolved:
                                                    bk_tweet_media_urls.append(resolved)
                                            bk_media_url: Optional[str] = bk_tweet_media_urls[0] if bk_tweet_media_urls else None
                                            bk_media_urls_json: Optional[str] = json.dumps(bk_tweet_media_urls) if bk_tweet_media_urls else None
                                            ts = (
                                                tweet.created_at.isoformat()
                                                if tweet.created_at
                                                else datetime.now(timezone.utc).isoformat()
                                            )
                                            items.append(("twitter", url, bk_media_url, tweet.text, ts, bk_media_urls_json))
                                except Exception as retry_exc:
                                    if _twitter_is_auth_error(retry_exc):
                                        _twitter_set_auth_backoff("bookmarks", retry_exc)
                                    else:
                                        logger.warning(
                                            "Twitter scraper: bookmarks fetch failed after token refresh: %s",
                                            _twitter_auth_message(retry_exc),
                                        )
                            else:
                                _twitter_set_auth_backoff("bookmarks", exc)
                        else:
                            _twitter_set_auth_backoff("bookmarks", exc)
                    elif _twitter_is_auth_error(exc):
                        _twitter_set_auth_backoff("bookmarks", exc)
                    else:
                        logger.warning("Twitter scraper: bookmarks fetch failed: %s", _twitter_auth_message(exc))
        else:
            logger.debug("Twitter scraper: OAuth 2.0 token not configured, skipping bookmarks.")

        new_count = 0
        newly_inserted: list[tuple] = []
        for platform, orig_url, media_url, text_content, ts, media_urls_json in items:
            existing = conn.execute(
                "SELECT id FROM drool_archive WHERE original_url = ?", (orig_url,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO drool_archive (platform, original_url, media_url, media_urls, text_content, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, orig_url, media_url or None, media_urls_json, text_content or None, ts),
            )
            newly_inserted.append((platform, orig_url, media_url, text_content, ts))
            new_count += 1
        conn.commit()
        if new_count:
            logger.info("Twitter scraper: archived %d new item(s).", new_count)
            _notify_new_items(newly_inserted)
    except Exception as exc:
        logger.error("Twitter scraper error: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bluesky scraper
# ---------------------------------------------------------------------------


def _scrape_bluesky() -> None:
    """Fetch liked posts from Bluesky and store new ones in drool_archive."""
    if not _ATPROTO_AVAILABLE:
        logger.debug("atproto is not installed; Bluesky scraper disabled.")
        return

    handle       = _load_credential("drool_bsky_handle",       "BSKY_HANDLE").lstrip("@")
    app_password = _load_credential("drool_bsky_app_password", "BSKY_APP_PASSWORD")

    if not handle or not app_password:
        logger.debug("Bluesky scraper: credentials not configured, skipping.")
        return

    client = _AtprotoClient()
    authenticated = False

    # Try to resume a previously saved session (avoids hitting the
    # createSession rate limit of 10 logins per day).
    stored_session = _load_credential("drool_bsky_session_string", "")
    if stored_session:
        try:
            client.login(session_string=stored_session)
            authenticated = True
            logger.debug("Bluesky scraper: resumed existing session.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Bluesky scraper: could not resume session, will re-login: %s", exc)
            client = _AtprotoClient()

    if not authenticated:
        try:
            client.login(handle, app_password)
            authenticated = True
        except Exception as exc:
            logger.warning("Bluesky scraper: could not authenticate: %s", exc)
            return

    # Persist the (possibly refreshed) session so the next run can reuse it.
    try:
        new_session = client.export_session_string()
        _conn = get_db_connection()
        _conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('drool_bsky_session_string', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (new_session,),
        )
        _conn.commit()
        _conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Bluesky scraper: could not persist session: %s", exc)

    conn = get_db_connection()
    try:
        items: list[tuple] = []

        try:
            resp = client.app.bsky.feed.get_actor_likes({"actor": handle, "limit": 50})
            for feed_view in (resp.feed or []):
                post = feed_view.post
                at_uri = post.uri  # at://did/app.bsky.feed.post/rkey
                parts = at_uri.split("/")
                did  = parts[2] if len(parts) > 2 else handle
                rkey = parts[-1] if parts else ""
                url  = f"https://bsky.app/profile/{did}/post/{rkey}"

                text = ""
                record = getattr(post, "record", None)
                if record:
                    text = getattr(record, "text", "") or ""

                bsky_media_urls: list[str] = []
                embed = getattr(post, "embed", None)
                if embed:
                    # Direct image embed (app.bsky.embed.images#view) – collect all
                    images = getattr(embed, "images", None)
                    if images:
                        for img in images:
                            img_url = getattr(img, "fullsize", None) or getattr(img, "thumb", None)
                            if img_url:
                                bsky_media_urls.append(img_url)
                    # Record-with-media (app.bsky.embed.recordWithMedia#view)
                    if not bsky_media_urls:
                        media = getattr(embed, "media", None)
                        if media:
                            media_images = getattr(media, "images", None)
                            if media_images:
                                for img in media_images:
                                    img_url = getattr(img, "fullsize", None) or getattr(img, "thumb", None)
                                    if img_url:
                                        bsky_media_urls.append(img_url)
                            if not bsky_media_urls:
                                ext = getattr(media, "external", None)
                                if ext:
                                    ext_url = getattr(ext, "thumb", None)
                                    if ext_url:
                                        bsky_media_urls.append(ext_url)
                    # External link card (app.bsky.embed.external#view)
                    if not bsky_media_urls:
                        external = getattr(embed, "external", None)
                        if external:
                            ext_url = getattr(external, "thumb", None)
                            if ext_url:
                                bsky_media_urls.append(ext_url)
                    # Video embed (app.bsky.embed.video#view) – use thumbnail
                    if not bsky_media_urls:
                        thumb = getattr(embed, "thumbnail", None)
                        if thumb:
                            bsky_media_urls.append(thumb)

                media_url: Optional[str] = bsky_media_urls[0] if bsky_media_urls else None
                media_urls_json: Optional[str] = json.dumps(bsky_media_urls) if bsky_media_urls else None

                indexed_at = getattr(post, "indexed_at", None)
                if indexed_at:
                    ts = indexed_at if isinstance(indexed_at, str) else indexed_at.isoformat()
                else:
                    ts = datetime.now(timezone.utc).isoformat()

                items.append(("bluesky", url, media_url, text, ts, media_urls_json))
        except Exception as exc:
            logger.warning("Bluesky scraper: liked posts fetch failed: %s", exc)

        new_count = 0
        newly_inserted: list[tuple] = []
        for platform, orig_url, media_url, text_content, ts, media_urls_json in items:
            existing = conn.execute(
                "SELECT id FROM drool_archive WHERE original_url = ?", (orig_url,)
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO drool_archive (platform, original_url, media_url, media_urls, text_content, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, orig_url, media_url or None, media_urls_json, text_content or None, ts),
            )
            newly_inserted.append((platform, orig_url, media_url, text_content, ts))
            new_count += 1
        conn.commit()
        if new_count:
            logger.info("Bluesky scraper: archived %d new item(s).", new_count)
            _notify_new_items(newly_inserted)
    except Exception as exc:
        logger.error("Bluesky scraper error: %s", exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Combined job (runs every 5 minutes)
# ---------------------------------------------------------------------------


async def run_drool_scrape() -> None:
    """Entry point called by APScheduler every 5 minutes."""
    logger.info("Drool scraper: starting run.")
    try:
        _scrape_reddit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Drool scraper: Reddit job error: %s", exc)
    try:
        _scrape_twitter()
    except Exception as exc:  # noqa: BLE001
        logger.error("Drool scraper: Twitter job error: %s", exc)
    try:
        _scrape_bluesky()
    except Exception as exc:  # noqa: BLE001
        logger.error("Drool scraper: Bluesky job error: %s", exc)
    logger.info("Drool scraper: run complete.")


def start_drool_scheduler() -> None:
    """Register the scrape job and start the scheduler (idempotent)."""
    if scheduler.running:
        return
    scheduler.add_job(
        run_drool_scrape,
        trigger="interval",
        minutes=5,
        id="drool_scrape",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Drool scraper scheduler started (every 5 minutes).")


def stop_drool_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Drool scraper scheduler stopped.")
