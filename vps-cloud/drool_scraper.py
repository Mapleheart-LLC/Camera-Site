"""
drool_scraper.py – Permanent Record scraper for The Drool Log.

Fetches liked/saved content from Reddit (via praw) and Twitter/X (via tweepy)
on a 5-minute schedule using APScheduler, saving only new items to the
drool_archive table.

Configuration (environment variables)
--------------------------------------
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

DISCORD_WEBHOOK_URL    – (shared) Discord webhook for new-item pings
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import get_db_connection

logger = logging.getLogger(__name__)

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
    import asyncio  # noqa: PLC0415

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
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    username = os.environ.get("REDDIT_USERNAME", "")
    password = os.environ.get("REDDIT_PASSWORD", "")
    user_agent = os.environ.get("REDDIT_USER_AGENT", "drool-log/1.0")

    if not all([client_id, client_secret, username, password]):
        return None

    try:
        import praw  # noqa: PLC0415

        return praw.Reddit(
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
    """Fetch upvoted and saved Reddit items and store new ones in drool_archive."""
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
    bearer = os.environ.get("TWITTER_BEARER_TOKEN", "")
    api_key = os.environ.get("TWITTER_API_KEY", "")
    api_secret = os.environ.get("TWITTER_API_SECRET", "")
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN", "")
    access_secret = os.environ.get("TWITTER_ACCESS_SECRET", "")

    if not bearer:
        return None

    try:
        import tweepy  # noqa: PLC0415

        return tweepy.Client(
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

    user_id = os.environ.get("TWITTER_USER_ID", "")
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
