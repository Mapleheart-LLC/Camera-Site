"""
discord_webhook.py – Discord notification utility for the backend service.

Provides async helpers that post notifications to Discord channels.  Delivery
is attempted via the Discord Bot API (``POST /channels/{id}/messages``) when
both ``DISCORD_BOT_TOKEN`` and a channel ID are available, and falls back to
the legacy ``DISCORD_WEBHOOK_URL`` approach otherwise.  All failures are
logged as warnings so that a Discord outage never crashes the application or
fails a user's request (fire-and-forget semantics).

Channel IDs can be overridden at runtime via the admin dashboard (stored in
the ``settings`` table) without requiring a container restart.

Configuration
-------------
``DISCORD_BOT_TOKEN``
    Bot token from the Discord Developer Portal.

``DISCORD_QUESTION_CHANNEL_ID``
    Channel where new anonymous questions are posted (with a Reply button).

``DISCORD_NOTIFICATION_CHANNEL_ID``
    Channel where general site notifications are posted (e.g. answer published).

``DISCORD_ADMIN_CHANNEL_ID``
    Private channel for admin-facing operational alerts.

``DISCORD_STREAM_CHANNEL_ID``
    Channel for go-live / stream-ended announcements.

``DISCORD_WEBHOOK_URL``  *(legacy fallback)*
    Incoming Webhook URL.  Used only when the bot-token path is unavailable.

``BASE_URL``
    Public root of the site (e.g. ``https://mochii.live``).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

_DISCORD_API = "https://discord.com/api/v10"

# Discord colour for mochii.live muted pink.
_MOCHII_PINK: int = 0xE8AEB7

logger = logging.getLogger(__name__)


# ── Settings-table helpers ───────────────────────────────────────────────────
# These read from the shared SQLite settings table so that admin-dash changes
# take effect immediately without a container restart.


def _get_setting(key: str) -> Optional[str]:
    """Return a value from the settings table, or None if absent / on error."""
    try:
        from db import get_db_connection  # local import avoids circular import
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else None
    except Exception:
        return None


def _is_feature_enabled(setting_key: str, default: bool = True) -> bool:
    """Return True if the feature flag in the settings table is enabled."""
    val = _get_setting(setting_key)
    if val is None:
        return default
    return val.strip().lower() == "true"


def _effective_channel_id(setting_key: str, env_var: str) -> str:
    """Return channel ID: settings table value takes precedence over env var."""
    return (_get_setting(setting_key) or os.environ.get(env_var, "")).strip()


async def _post_to_channel(channel_id: str, payload: dict) -> None:
    """POST *payload* to a Discord channel via the Bot API."""
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        return
    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bot {bot_token}"},
            )
            if resp.status_code not in (200, 201, 204):
                logger.warning(
                    "Discord channel API returned unexpected status %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to post to Discord channel %s: %s", channel_id, exc)


async def _post_to_webhook(webhook_url: str, payload: dict) -> None:
    """POST *payload* to a Discord Incoming Webhook URL."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning(
                    "Discord webhook returned unexpected status %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to send Discord webhook notification: %s", exc)


async def send_discord_notification(
    content: str,
    question_text: str = "",
    is_embed: bool = True,
    question_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> None:
    """Post a notification to Discord.

    Respects the ``discord_notify_questions`` feature flag.  Channel ID is
    resolved from: explicit argument → settings table → env var → webhook URL.
    """
    if not _is_feature_enabled("discord_notify_questions", default=True):
        return

    base_url: str = os.environ.get("BASE_URL", "").rstrip("/")

    payload: dict = {"content": content}

    if is_embed:
        embed: dict = {
            "title": "📬 New note in the Puppy Pouch!",
            "description": f">>> {question_text}",
            "color": _MOCHII_PINK,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "mochii.live · Alpha Kennel"},
        }

        if question_id and base_url:
            reply_url = f"{base_url}/admin?q={question_id}"
            embed["url"] = reply_url

        payload["embeds"] = [embed]

        if question_id:
            payload["components"] = [
                {
                    "type": 1,  # ACTION_ROW
                    "components": [
                        {
                            "type": 2,    # BUTTON
                            "style": 1,   # PRIMARY (blurple)
                            "label": "Reply 🐾",
                            "custom_id": f"reply:{question_id}",
                        }
                    ],
                }
            ]

    # Resolve channel: explicit arg → settings table → env var → webhook URL.
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    resolved_channel = channel_id or _effective_channel_id(
        "discord_question_channel_id", "DISCORD_QUESTION_CHANNEL_ID"
    )
    if resolved_channel and bot_token:
        await _post_to_channel(resolved_channel, payload)
    else:
        webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook_url:
            await _post_to_webhook(webhook_url, payload)


async def send_answer_notification(share_url: str = "") -> None:
    """Post an answer-published notification to the notification channel.

    Respects the ``discord_notify_answers`` feature flag.
    """
    if not _is_feature_enabled("discord_notify_answers", default=True):
        return

    notification_channel_id = _effective_channel_id(
        "discord_notification_channel_id", "DISCORD_NOTIFICATION_CHANNEL_ID"
    )
    if not notification_channel_id:
        return

    lines = ["✅ A note in the Puppy Pouch has been answered and published!"]
    if share_url:
        lines.append(f"Share it: {share_url}")

    await _post_to_channel(notification_channel_id, {"content": "\n".join(lines)})


async def send_admin_notification(content: str) -> None:
    """Post an admin-facing operational alert to the admin channel.

    Respects the ``discord_notify_purchases`` feature flag (used for store
    events; other admin alerts always fire).
    """
    admin_channel_id = _effective_channel_id(
        "discord_admin_channel_id", "DISCORD_ADMIN_CHANNEL_ID"
    )
    if not admin_channel_id:
        return

    await _post_to_channel(admin_channel_id, {"content": content})


async def send_stream_live_notification(stream_title: str = "", stream_url: str = "") -> None:
    """Post a go-live announcement to the stream channel.

    Only fires when ``discord_stream_notifications_enabled`` is ``true`` in the
    settings table.  The message text uses the ``discord_stream_live_message``
    template (vars: ``{title}``, ``{url}``) if set.
    """
    if not _is_feature_enabled("discord_stream_notifications_enabled", default=False):
        return

    channel_id = _effective_channel_id("discord_stream_channel_id", "DISCORD_STREAM_CHANNEL_ID")
    if not channel_id:
        return

    template = (
        _get_setting("discord_stream_live_message")
        or "@here 🔴 **{title}** is now LIVE! {url}"
    )
    content = template.format(
        title=stream_title or "mochii.live",
        url=stream_url or "",
    ).strip()

    embed: dict = {
        "title": "🔴 Stream is LIVE!",
        "description": stream_title or "The stream is live now!",
        "color": 0xFF5C5C,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "mochii.live"},
    }
    if stream_url:
        embed["url"] = stream_url

    await _post_to_channel(channel_id, {"content": content, "embeds": [embed]})


async def send_stream_offline_notification() -> None:
    """Post a stream-ended notice to the stream channel."""
    if not _is_feature_enabled("discord_stream_notifications_enabled", default=False):
        return

    channel_id = _effective_channel_id("discord_stream_channel_id", "DISCORD_STREAM_CHANNEL_ID")
    if not channel_id:
        return

    await _post_to_channel(
        channel_id, {"content": "⚫ The stream has ended. Thanks for watching! 🐾"}
    )


async def send_discord_dm(discord_id: str, content: str) -> bool:
    """Open a DM channel with a Discord user and send *content*.

    Returns ``True`` on success, ``False`` on any failure.
    """
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Create (or fetch existing) DM channel
            dm_resp = await client.post(
                f"{_DISCORD_API}/users/@me/channels",
                json={"recipient_id": discord_id},
                headers={"Authorization": f"Bot {bot_token}"},
            )
            if dm_resp.status_code not in (200, 201):
                logger.warning(
                    "Could not open DM channel for discord_id=%s: %s",
                    discord_id,
                    dm_resp.status_code,
                )
                return False
            channel_id = dm_resp.json().get("id")
            if not channel_id:
                return False
            msg_resp = await client.post(
                f"{_DISCORD_API}/channels/{channel_id}/messages",
                json={"content": content},
                headers={"Authorization": f"Bot {bot_token}"},
            )
            return msg_resp.status_code in (200, 201)
    except Exception as exc:
        logger.warning("Failed to send DM to discord_id=%s: %s", discord_id, exc)
        return False


async def get_bot_status() -> dict:
    """Return a status dict describing the bot's current connectivity.

    Checks the bot token validity and, when ``DISCORD_GUILD_ID`` is set,
    fetches basic guild info (name, approximate member count).
    """
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    guild_id: str  = os.environ.get("DISCORD_GUILD_ID", "")

    result: dict = {
        "bot_token_set":    bool(bot_token),
        "guild_id_set":     bool(guild_id),
        "bot_valid":        False,
        "bot_username":     None,
        "guild_name":       None,
        "guild_member_count": None,
    }

    if not bot_token:
        return result

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            me_resp = await client.get(
                f"{_DISCORD_API}/users/@me",
                headers={"Authorization": f"Bot {bot_token}"},
            )
            if me_resp.status_code == 200:
                result["bot_valid"]    = True
                result["bot_username"] = me_resp.json().get("username")

            if guild_id and result["bot_valid"]:
                g_resp = await client.get(
                    f"{_DISCORD_API}/guilds/{guild_id}?with_counts=true",
                    headers={"Authorization": f"Bot {bot_token}"},
                )
                if g_resp.status_code == 200:
                    g = g_resp.json()
                    result["guild_name"]         = g.get("name")
                    result["guild_member_count"] = g.get("approximate_member_count")
    except Exception as exc:
        logger.warning("Error fetching Discord bot status: %s", exc)

    return result
