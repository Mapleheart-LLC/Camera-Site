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

Set ``BASE_URL`` to the public root of the site (e.g. ``https://mochii.live``)
so that deep-link URLs included in embed titles resolve correctly.
"""

import logging
import os
from typing import Optional

import httpx

# Discord color for mochii.live muted pink (0xE8AEB7 → decimal).
_MOCHII_PINK: int = 0xE8AEB7

_OG_PREVIEW_LEN = 100

logger = logging.getLogger(__name__)


async def send_discord_notification(
    content: str,
    question_text: str = "",
    is_embed: bool = True,
    question_id: Optional[str] = None,
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
    question_id:
        Optional UUID of the question.  When provided (and ``BASE_URL`` is
        set), the embed title becomes a clickable deep-link to the admin
        panel, and an "Action" field is added to the embed.

    The function swallows all exceptions and logs a warning on failure so
    that a Discord outage can never crash the application or fail a user's
    request.
    """
    # Read env vars at call time so values injected by Docker / Komodo at
    # container startup are always picked up (module-level reads would capture
    # an empty string if the module is imported before the vars are set).
    webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
    base_url: str = os.environ.get("BASE_URL", "").rstrip("/")

    if not webhook_url:
        return

    payload: dict = {"content": content}

    if is_embed:
        preview = question_text[:_OG_PREVIEW_LEN]
        if len(question_text) > _OG_PREVIEW_LEN:
            preview += "…"

        embed: dict = {
            "title": "New Question Received! 🐾",
            "description": preview,
            "color": _MOCHII_PINK,
            "footer": {"text": "Log into the Alpha Kennel to reply."},
        }

        if question_id and base_url:
            reply_url = f"{base_url}/admin/questions/{question_id}"
            embed["url"] = reply_url
            embed["fields"] = [
                {
                    "name": "Action",
                    "value": f"[🐾 Click here to reply and share to Twitter]({reply_url})",
                    "inline": False,
                }
            ]

        payload["embeds"] = [embed]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning(
                    "Discord webhook returned unexpected status %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:  # noqa: BLE001 – intentional broad catch
        logger.warning("Failed to send Discord notification: %s", exc)
