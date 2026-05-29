"""
services/mcp_server.py – MCP tool definitions and social media posting for the AI Warden.

Tool Registration
-----------------
Defines POST_SOCIAL_UPDATE_TOOL_SCHEMA for the ``post_social_update`` MCP tool, which
allows the AI Warden to propose social media posts on Twitter/X and Bluesky.

Human-Approval Requirement
---------------------------
Invoking the tool does NOT publish immediately.  The content is queued as a *pending
draft* (stored in ``tpe_ai_social_drafts``) and a handler must explicitly approve it
via ``POST /api/handler/social-post-drafts/{draft_id}/approve`` before anything goes
live.  Handlers can also reject a draft with
``DELETE /api/handler/social-post-drafts/{draft_id}``.

Posting Functions
-----------------
``post_to_twitter(content)``  – Posts a tweet using stored OAuth 1.0a credentials.
``post_to_bluesky(content)``  – Posts to Bluesky using stored app-password credentials.
``execute_post_social_update(platform, content)`` – Routes to both as needed.

These functions are called by the backend approval endpoint, not directly by the AI.

Credential Loading
------------------
Credentials are loaded with the same DB-first / env-var-fallback pattern used by
``drool_scraper.py``, so the same credentials configured for scraping are reused here.
"""

from __future__ import annotations

import logging
import os
import sqlite3 as _sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt – injected into the AI Warden's context at session start.
# ---------------------------------------------------------------------------

AI_WARDEN_SYSTEM_PROMPT: str = """
You are the AI Warden for this TPE monitoring system.  You observe device telemetry,
enforce compliance, and assist the handler in managing the session.

Available tools
---------------
execute_device_command
    Dispatch a validated command to a paired device over MQTT (e.g. LOCK_DEVICE,
    VIBRATE, SET_HANDLER_SYSTEM_PROMPT).  Requires a device_id and an action.

post_social_update
    Propose a social media post to be reviewed by a human handler before publication.
    Supported platforms: "twitter", "bluesky", or "both".

    IMPORTANT – human approval is mandatory.
    Using this tool submits a *draft* for handler review.  The post will NOT be
    published until the handler explicitly approves it in the handler panel.  You
    may suggest posts to broadcast compliance status, task completion, or other
    relevant updates, but the final decision always belongs to the handler.

Guidelines
----------
- Never fabricate device telemetry or invent events that did not occur.
- Do not invoke post_social_update autonomously without a clear, observed reason.
- Keep proposed post content factual, concise, and appropriate.
- When uncertain, describe the situation and ask the handler for guidance rather
  than acting unilaterally.
""".strip()

# ---------------------------------------------------------------------------
# Tool schema – used by the AI client to describe the tool to the LLM.
# ---------------------------------------------------------------------------

_VALID_PLATFORMS: List[str] = ["twitter", "bluesky", "both"]

POST_SOCIAL_UPDATE_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "post_social_update",
    "description": (
        "Submit a social media post draft for handler review and approval. "
        "The content will NOT be published immediately — a human handler must "
        "explicitly approve the draft before it goes live on the selected "
        "platform(s).  Supported platforms: 'twitter', 'bluesky', or 'both'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "enum": _VALID_PLATFORMS,
                "description": "Target platform(s): 'twitter', 'bluesky', or 'both'.",
            },
            "content": {
                "type": "string",
                "description": "The text content of the proposed post (max 280 characters for Twitter).",
            },
        },
        "required": ["platform", "content"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# Credential helper – mirrors drool_scraper._load_credential
# ---------------------------------------------------------------------------


def _load_credential(db_key: str, env_key: str) -> str:
    """Return a credential from the settings table, falling back to the env var."""
    try:
        from db import get_db_connection  # noqa: PLC0415 – lazy to avoid circular import
        conn = get_db_connection()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (db_key,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0])
    except _sqlite3.Error as exc:
        logger.debug("Could not read credential '%s' from DB: %s", db_key, exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unexpected error reading credential '%s': %s", db_key, exc)
    return os.environ.get(env_key, "")


# ---------------------------------------------------------------------------
# Platform posting functions – called on handler approval
# ---------------------------------------------------------------------------


def post_to_twitter(content: str) -> Dict[str, Any]:
    """Post *content* as a tweet using OAuth 1.0a credentials.

    Returns a dict with keys ``platform``, ``status``, and either ``tweet_id``
    (on success) or ``error`` (on failure).
    """
    try:
        import tweepy  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return {"platform": "twitter", "status": "error", "error": "tweepy is not installed."}

    api_key       = _load_credential("drool_twitter_api_key",       "TWITTER_API_KEY")
    api_secret    = _load_credential("drool_twitter_api_secret",    "TWITTER_API_SECRET")
    access_token  = _load_credential("drool_twitter_access_token",  "TWITTER_ACCESS_TOKEN")
    access_secret = _load_credential("drool_twitter_access_secret", "TWITTER_ACCESS_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        return {
            "platform": "twitter",
            "status": "error",
            "error": "Twitter OAuth 1.0a credentials are not fully configured.",
        }

    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        response = client.create_tweet(text=content)
        tweet_id = str(response.data["id"])
        logger.info("Twitter post created: tweet_id=%s", tweet_id)
        return {"platform": "twitter", "status": "posted", "tweet_id": tweet_id}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Twitter post failed: %s", exc)
        return {"platform": "twitter", "status": "error", "error": str(exc)}


def post_to_bluesky(content: str) -> Dict[str, Any]:
    """Post *content* to Bluesky using stored app-password credentials.

    Returns a dict with keys ``platform``, ``status``, and either ``post_uri``
    (on success) or ``error`` (on failure).
    """
    try:
        from atproto import Client  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return {"platform": "bluesky", "status": "error", "error": "atproto is not installed."}

    handle       = _load_credential("drool_bsky_handle",       "BSKY_HANDLE").lstrip("@")
    app_password = _load_credential("drool_bsky_app_password", "BSKY_APP_PASSWORD")

    if not handle or not app_password:
        return {
            "platform": "bluesky",
            "status": "error",
            "error": "Bluesky credentials (BSKY_HANDLE / BSKY_APP_PASSWORD) are not configured.",
        }

    try:
        client = Client()
        # Try to resume an existing session to avoid hitting the rate limit on
        # createSession (10 logins/day).  Fall back to a fresh login on failure.
        stored_session = _load_credential("drool_bsky_session_string", "")
        authenticated = False
        if stored_session:
            try:
                client.login(session_string=stored_session)
                authenticated = True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bluesky: could not resume stored session, will re-login: %s", exc)
                client = Client()

        if not authenticated:
            client.login(handle, app_password)

        response = client.send_post(text=content)
        post_uri: str = getattr(response, "uri", "") or ""
        logger.info("Bluesky post created: uri=%s", post_uri)
        return {"platform": "bluesky", "status": "posted", "post_uri": post_uri}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bluesky post failed: %s", exc)
        return {"platform": "bluesky", "status": "error", "error": str(exc)}


def execute_post_social_update(
    platform: str, content: str
) -> Dict[str, Any]:
    """Route *content* to the appropriate social media API(s).

    Called by the backend approval endpoint once a handler approves a draft.
    Returns a summary dict with per-platform results.

    Args:
        platform: One of ``"twitter"``, ``"bluesky"``, or ``"both"``.
        content:  The text to publish.

    Returns:
        ``{"results": [<per-platform result dicts>], "any_error": bool}``
    """
    platform = (platform or "").strip().lower()
    if platform not in _VALID_PLATFORMS:
        return {
            "results": [],
            "any_error": True,
            "error": f"Unknown platform '{platform}'. Must be one of: {_VALID_PLATFORMS}",
        }

    results: List[Dict[str, Any]] = []
    if platform in ("twitter", "both"):
        results.append(post_to_twitter(content))
    if platform in ("bluesky", "both"):
        results.append(post_to_bluesky(content))

    any_error = any(r.get("status") == "error" for r in results)
    return {"results": results, "any_error": any_error}
