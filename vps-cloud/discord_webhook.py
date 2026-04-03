"""
discord_webhook.py – Discord notification utility.

Provides async helpers that post notifications to Discord.  Delivery is
attempted via the Discord Bot API (``POST /channels/{id}/messages``) when
both ``DISCORD_BOT_TOKEN`` and a channel ID are available, and falls back to
the legacy ``DISCORD_WEBHOOK_URL`` approach otherwise.  All failures are
logged as warnings so that a Discord outage never crashes the application or
fails a user's request (fire-and-forget semantics).

Configuration
-------------
``DISCORD_BOT_TOKEN``
    Bot token from the Discord Developer Portal.  Required for channel-ID
    based delivery.

``DISCORD_QUESTION_CHANNEL_ID``
    Channel where new anonymous questions are posted (with a Reply button).

``DISCORD_NOTIFICATION_CHANNEL_ID``
    Channel where general site notifications are posted (e.g. a question has
    been answered and published).

``DISCORD_ADMIN_CHANNEL_ID``
    Private channel for admin-facing operational alerts (e.g. new store
    purchases, system events).

``DISCORD_WEBHOOK_URL``  *(legacy fallback)*
    Incoming Webhook URL.  Used only when the bot-token path is unavailable.

``BASE_URL``
    Public root of the site (e.g. ``https://mochii.live``).  Used to build
    deep-link URLs inside embeds.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

_DISCORD_API = "https://discord.com/api/v10"

# Discord color for mochii.live muted pink (0xE8AEB7 → decimal).
_MOCHII_PINK: int = 0xE8AEB7

logger = logging.getLogger(__name__)


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

    Parameters
    ----------
    content:
        Plain-text message shown above the embed (or as the only message
        if *is_embed* is ``False``).
    question_text:
        The question text used to populate the embed description.  Only
        relevant when *is_embed* is ``True``.
    is_embed:
        When ``True`` (default), attach a rich Discord embed with a
        truncated preview of *question_text*.
    question_id:
        Optional UUID of the question.  When provided (and ``BASE_URL`` is
        set), the embed title becomes a clickable deep-link to the admin
        reply modal.
    channel_id:
        Discord channel ID to post to via the Bot API.  When ``None``, the
        function falls back to ``DISCORD_WEBHOOK_URL``.

    The function swallows all exceptions and logs a warning on failure so
    that a Discord outage can never crash the application or fail a user's
    request.
    """
    # Read env vars at call time so values injected by Docker / Komodo at
    # container startup are always picked up (module-level reads would capture
    # an empty string if the module is imported before the vars are set).
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
            # /admin?q={id} deep-links directly into the admin panel and opens
            # the reply modal for this question.
            reply_url = f"{base_url}/admin?q={question_id}"
            embed["url"] = reply_url

        payload["embeds"] = [embed]

        # Button component so the admin can reply directly from Discord.
        # Clicking it triggers a modal via the /discord/interactions endpoint.
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

    # Prefer bot-token + channel-ID delivery; fall back to webhook URL.
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    if channel_id and bot_token:
        await _post_to_channel(channel_id, payload)
    else:
        webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook_url:
            await _post_to_webhook(webhook_url, payload)


async def send_answer_notification(share_url: str = "") -> None:
    """Post an answer-published notification to the notification channel.

    Called after a question is answered so that the notification channel
    receives a public share link.  No-ops if ``DISCORD_NOTIFICATION_CHANNEL_ID``
    is not set.
    """
    notification_channel_id: str = os.environ.get("DISCORD_NOTIFICATION_CHANNEL_ID", "")
    if not notification_channel_id:
        return

    lines = ["✅ A note in the Puppy Pouch has been answered and published!"]
    if share_url:
        lines.append(f"Share it: {share_url}")

    payload: dict = {"content": "\n".join(lines)}
    await _post_to_channel(notification_channel_id, payload)


async def send_admin_notification(content: str) -> None:
    """Post an admin-facing operational alert to the admin channel.

    Used for internal events that only admins need to see, such as new store
    purchases.  No-ops if ``DISCORD_ADMIN_CHANNEL_ID`` is not set.
    """
    admin_channel_id: str = os.environ.get("DISCORD_ADMIN_CHANNEL_ID", "")
    if not admin_channel_id:
        return

    await _post_to_channel(admin_channel_id, {"content": content})
