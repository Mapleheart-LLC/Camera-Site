"""
discord_webhook.py – Discord Webhook notification utility.

Provides a single async helper that posts a notification to a configured
Discord webhook URL.  If the webhook is not configured or the request fails,
a warning is logged and the caller is not affected (fire-and-forget semantics).

Configuration
-------------
Set the ``DISCORD_WEBHOOK_URL`` environment variable to a valid Discord
Incoming Webhook URL.  If the variable is absent or empty, all calls to
``send_discord_notification`` silently no-op.
"""

import logging
import os
from typing import Optional

import httpx

DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Discord color for mochii.live muted pink (0xE8AEB7 → decimal).
_MOCHII_PINK: int = 0xE8AEB7

_OG_PREVIEW_LEN = 100

logger = logging.getLogger(__name__)


async def send_discord_notification(
    content: str,
    question_text: str = "",
    is_embed: bool = True,
) -> None:
    """Post a notification to the configured Discord webhook.

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

    The function swallows all exceptions and logs a warning on failure so
    that a Discord outage can never crash the application or fail a user's
    request.
    """
    if not DISCORD_WEBHOOK_URL:
        return

    payload: dict = {"content": content}

    if is_embed:
        preview = question_text[:_OG_PREVIEW_LEN]
        if len(question_text) > _OG_PREVIEW_LEN:
            preview += "…"
        payload["embeds"] = [
            {
                "title": "New Question Received! 🐾",
                "description": preview,
                "color": _MOCHII_PINK,
                "footer": {"text": "Log into the Alpha Kennel to reply."},
            }
        ]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(DISCORD_WEBHOOK_URL, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning(
                    "Discord webhook returned unexpected status %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:  # noqa: BLE001 – intentional broad catch
        logger.warning("Failed to send Discord notification: %s", exc)
