"""
drool_scraper.py – Permanent Record scraper for The Drool Log.

Fetches liked/saved content from Reddit (via praw), Twitter/X (via tweepy),
and Bluesky (via atproto) on a 5-minute schedule using APScheduler, saving
only new items to the drool_archive table.

Configuration (environment variables – all also settable via admin panel)
-------------------------------------------------------------------------
REDDIT_CLIENT_ID       – Reddit OAuth app client ID
REDDIT_CLIENT_SECRET   – Reddit OAuth app client secret
REDDIT_USERNAME        – Reddit account username to scrape
REDDIT_PASSWORD        – Reddit account password
REDDIT_USER_AGENT      – User-agent string (e.g. "drool-log/1.0 by u/yourname")

TWITTER_BEARER_TOKEN   – Twitter/X app-only Bearer Token (for likes / bookmarks)
TWITTER_USER_ID        – Numeric Twitter/X user ID to scrape
TWITTER_API_KEY        – Twitter/X API Key (consumer key) – needed for bookmarks
TWITTER_API_SECRET     – Twitter/X API Secret
TWITTER_ACCESS_TOKEN   – Twitter/X Access Token (user auth) – needed for bookmarks
TWITTER_ACCESS_SECRET  – Twitter/X Access Token Secret

BSKY_HANDLE            – Bluesky handle (e.g. yourname.bsky.social)
BSKY_APP_PASSWORD      – Bluesky app password (from Settings → App Passwords)

DISCORD_WEBHOOK_URL    – (shared) Discord webhook for new-item pings
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import get_db_connection

# Optional dependencies – imported at module level with graceful fallback.
# When credentials are absent the scrapers short-circuit before any API call.
try:
    import praw as _praw  # type: ignore[import-untyped]
    _PRAW_AVAILABLE = True
except ImportError:  # noqa: BLE001
    _praw = None  # type: ignore[assignment]
    _PRAW_AVAILABLE = False

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


def _reddit_mode() -> str:
    """Return 'api' or 'ifttt' based on the stored drool_reddit_mode setting."""
    return _load_credential("drool_reddit_mode", "REDDIT_MODE") or "api"

# ---------------------------------------------------------------------------
# Scheduler (module-level singleton; started / stopped by main.py lifespan)
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Discord helper (reuse existing webhook)
# ---------------------------------------------------------------------------


async def _ping_discord_new_item(platform: str, url: str, text: str) -> None:
    """Send a Discord ping for a newly archived item."""
    from discord_webhook import send_discord_notification  # local import avoids circular

    snippet = (text or url)[:200]
    await send_discord_notification(
        content=f"🐾 A new {platform} secret has been logged in the Drool Archive! {snippet}",
        is_embed=False,
    )


def _notify_new_items(new_items: list[tuple]) -> None:
    """Fire Discord pings for each newly inserted item (best-effort, sync wrapper)."""
    for platform, orig_url, _media, text_content, _ts in new_items:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_ping_discord_new_item(platform, orig_url, text_content or ""))
            else:
                loop.run_until_complete(
                    _ping_discord_new_item(platform, orig_url, text_content or "")
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Discord ping for new drool item failed: %s", exc)


# ---------------------------------------------------------------------------
# Reddit scraper
# ---------------------------------------------------------------------------


def _get_praw_reddit() -> Optional[object]:
    """Return an authenticated praw.Reddit instance or None if creds are missing."""
    if not _PRAW_AVAILABLE:
        logger.debug("praw is not installed; Reddit scraper disabled.")
        return None

    client_id     = _load_credential("drool_reddit_client_id",     "REDDIT_CLIENT_ID")
    client_secret = _load_credential("drool_reddit_client_secret", "REDDIT_CLIENT_SECRET")
    username      = _load_credential("drool_reddit_username",      "REDDIT_USERNAME")
    password      = _load_credential("drool_reddit_password",      "REDDIT_PASSWORD")
    user_agent    = _load_credential("drool_reddit_user_agent",    "REDDIT_USER_AGENT") or "drool-log/1.0"

    if not all([client_id, client_secret, username, password]):
        return None

    try:
        return _praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent,
        )
    except Exception as exc:
        logger.warning("Could not initialise praw Reddit client: %s", exc)
        return None


def _scrape_reddit() -> None:
    """Fetch upvoted and saved Reddit items and store new ones in drool_archive.

    Skipped when reddit_mode is 'ifttt' (items arrive via the webhook endpoint
    instead of being polled).
    """
    if _reddit_mode() == "ifttt":
        logger.debug("Reddit scraper: mode is 'ifttt', skipping poll.")
        return

    reddit = _get_praw_reddit()
    if reddit is None:
        logger.debug("Reddit scraper: credentials not configured, skipping.")
        return

    conn = get_db_connection()
    try:
        items: list[tuple] = []

        try:
            me = reddit.user.me()
        except Exception as exc:
            logger.warning("Reddit scraper: could not authenticate: %s", exc)
            return

        # Upvoted posts
        try:
            for submission in me.upvoted(limit=50):
                url = f"https://www.reddit.com{submission.permalink}"
                media = getattr(submission, "url", None)
                text = getattr(submission, "title", "") or ""
                ts = datetime.fromtimestamp(
                    submission.created_utc, tz=timezone.utc
                ).isoformat()
                items.append(("reddit", url, media, text, ts))
        except Exception as exc:
            logger.warning("Reddit scraper: upvoted fetch failed: %s", exc)

        # Saved items
        try:
            for item in me.saved(limit=50):
                if hasattr(item, "permalink"):
                    url = f"https://www.reddit.com{item.permalink}"
                    media = getattr(item, "url", None)
                    text = getattr(item, "title", "") or getattr(item, "body", "") or ""
                    ts = datetime.fromtimestamp(
                        item.created_utc, tz=timezone.utc
                    ).isoformat()
                    items.append(("reddit", url, media, text, ts))
        except Exception as exc:
            logger.warning("Reddit scraper: saved fetch failed: %s", exc)

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
            logger.info("Reddit scraper: archived %d new item(s).", new_count)
            _notify_new_items(newly_inserted)
    except Exception as exc:
        logger.error("Reddit scraper error: %s", exc)
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

    if not bearer:
        return None

    try:
        return _tweepy.Client(
            bearer_token=bearer,
            consumer_key=api_key or None,
            consumer_secret=api_secret or None,
            access_token=access_token or None,
            access_token_secret=access_secret or None,
            wait_on_rate_limit=False,
        )
    except Exception as exc:
        logger.warning("Could not initialise tweepy Client: %s", exc)
        return None


def _scrape_twitter() -> None:
    """Fetch liked and bookmarked tweets and store new ones in drool_archive."""
    client = _get_tweepy_client()
    if client is None:
        logger.debug("Twitter scraper: credentials not configured, skipping.")
        return

    user_id = _load_credential("drool_twitter_user_id", "TWITTER_USER_ID")
    if not user_id:
        logger.debug("Twitter scraper: TWITTER_USER_ID not set, skipping.")
        return

    conn = get_db_connection()
    try:
        items: list[tuple] = []

        # Liked tweets
        try:
            resp = client.get_liked_tweets(
                id=user_id,
                max_results=50,
                tweet_fields=["created_at", "text", "attachments"],
                expansions=["attachments.media_keys"],
                media_fields=["url", "preview_image_url"],
            )
            if resp and resp.data:
                media_map: dict = {}
                if resp.includes and "media" in resp.includes:
                    for m in resp.includes["media"]:
                        media_map[m.media_key] = getattr(m, "url", None) or getattr(
                            m, "preview_image_url", None
                        )
                for tweet in resp.data:
                    url = f"https://twitter.com/i/web/status/{tweet.id}"
                    media_url: Optional[str] = None
                    att = getattr(tweet, "attachments", None) or {}
                    mk = (att.get("media_keys") or [None])[0]
                    if mk:
                        media_url = media_map.get(mk)
                    ts = (
                        tweet.created_at.isoformat()
                        if tweet.created_at
                        else datetime.now(timezone.utc).isoformat()
                    )
                    items.append(("twitter", url, media_url, tweet.text, ts))
        except Exception as exc:
            logger.warning("Twitter scraper: liked tweets fetch failed: %s", exc)

        # Bookmarks (requires user-level OAuth 1.0a or OAuth 2.0 user context)
        try:
            resp = client.get_bookmarks(
                id=user_id,
                max_results=50,
                tweet_fields=["created_at", "text", "attachments"],
                expansions=["attachments.media_keys"],
                media_fields=["url", "preview_image_url"],
            )
            if resp and resp.data:
                media_map = {}
                if resp.includes and "media" in resp.includes:
                    for m in resp.includes["media"]:
                        media_map[m.media_key] = getattr(m, "url", None) or getattr(
                            m, "preview_image_url", None
                        )
                for tweet in resp.data:
                    url = f"https://twitter.com/i/web/status/{tweet.id}"
                    media_url = None
                    att = getattr(tweet, "attachments", None) or {}
                    mk = (att.get("media_keys") or [None])[0]
                    if mk:
                        media_url = media_map.get(mk)
                    ts = (
                        tweet.created_at.isoformat()
                        if tweet.created_at
                        else datetime.now(timezone.utc).isoformat()
                    )
                    items.append(("twitter", url, media_url, tweet.text, ts))
        except Exception as exc:
            logger.warning("Twitter scraper: bookmarks fetch failed: %s", exc)

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

    try:
        client = _AtprotoClient()
        client.login(handle, app_password)
    except Exception as exc:
        logger.warning("Bluesky scraper: could not authenticate: %s", exc)
        return

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

                media_url: Optional[str] = None
                embed = getattr(post, "embed", None)
                if embed:
                    # Direct image embed (app.bsky.embed.images#view)
                    images = getattr(embed, "images", None)
                    if images:
                        media_url = (
                            getattr(images[0], "fullsize", None)
                            or getattr(images[0], "thumb", None)
                        )
                    # Record-with-media (app.bsky.embed.recordWithMedia#view)
                    if not media_url:
                        media = getattr(embed, "media", None)
                        if media:
                            media_images = getattr(media, "images", None)
                            if media_images:
                                media_url = (
                                    getattr(media_images[0], "fullsize", None)
                                    or getattr(media_images[0], "thumb", None)
                                )
                            if not media_url:
                                ext = getattr(media, "external", None)
                                if ext:
                                    media_url = getattr(ext, "thumb", None)
                    # External link card (app.bsky.embed.external#view)
                    if not media_url:
                        external = getattr(embed, "external", None)
                        if external:
                            media_url = getattr(external, "thumb", None)

                indexed_at = getattr(post, "indexed_at", None)
                if indexed_at:
                    ts = indexed_at if isinstance(indexed_at, str) else indexed_at.isoformat()
                else:
                    ts = datetime.now(timezone.utc).isoformat()

                items.append(("bluesky", url, media_url, text, ts))
        except Exception as exc:
            logger.warning("Bluesky scraper: liked posts fetch failed: %s", exc)

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
