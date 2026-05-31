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
import re
import json
import random
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

_DISCORD_API = "https://discord.com/api/v10"

# Discord colour for mochii.live muted pink.
_MOCHII_PINK: int = 0xE8AEB7
_DISCORD_COMPONENTS_MAX_BUTTONS = 25
_DISCORD_COMPONENTS_ROW_SIZE = 5

logger = logging.getLogger(__name__)

_X_LINK_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)(?P<path>/[^\s<>'\"]*)?",
    flags=re.IGNORECASE,
)

_COUNTER_NOTIFY_DEDUPE_WINDOW_SEC = max(
    10,
    int((os.environ.get("DISCORD_COUNTER_DEDUPE_WINDOW_SEC", "60") or "60").strip() or "60"),
)
_COUNTER_NOTIFY_ENABLED_DEFAULT = (
    os.environ.get("DISCORD_COUNTER_NOTIFY_ENABLED", "false").strip().lower() == "true"
)
_COUNTER_NOTIFY_EDGE_STEP_DEFAULT = max(
    1,
    int((os.environ.get("DISCORD_COUNTER_EDGE_MILESTONE_STEP", "10") or "10").strip() or "10"),
)
_COUNTER_NOTIFY_TABLE_READY = False


def rewrite_x_links_to_fxtwitter(text: str) -> str:
    """Rewrite Twitter/X public links to fxtwitter links for embeds/previews."""
    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        path = (match.group("path") or "").strip()
        return f"https://fxtwitter.com{path}"

    return _X_LINK_RE.sub(_replace, text)


def _rewrite_payload_links(value):
    if isinstance(value, str):
        return rewrite_x_links_to_fxtwitter(value)
    if isinstance(value, list):
        return [_rewrite_payload_links(item) for item in value]
    if isinstance(value, dict):
        return {k: _rewrite_payload_links(v) for k, v in value.items()}
    return value


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


def _load_message_variants(
    setting_key: str,
    env_var: str,
    defaults: list[str],
) -> list[str]:
    """Load non-empty message variant strings from settings/env with defaults."""
    raw = (_get_setting(setting_key) or os.environ.get(env_var, "")).strip()
    if not raw:
        return defaults

    variants: list[str] = []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        variants = [str(item).strip() for item in parsed if str(item).strip()]
    elif isinstance(parsed, str):
        variants = [line.strip() for line in parsed.splitlines() if line.strip()]
    else:
        variants = [line.strip() for line in raw.splitlines() if line.strip()]

    return variants or defaults


def _pick_message_variant(
    setting_key: str,
    env_var: str,
    defaults: list[str],
) -> str:
    """Pick one randomized message variant."""
    variants = _load_message_variants(setting_key, env_var, defaults)
    return random.choice(variants)


def _safe_format_variant(template: str, context: dict[str, str]) -> str:
    """Safely format known variant tokens without crashing on unknown braces."""
    if not template:
        return ""
    try:
        return template.format(**context)
    except Exception:
        # Keep delivery resilient even when admin-configured templates contain
        # unmatched braces or unknown placeholders.
        return template


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _counter_event_kind(event_type: str) -> Optional[str]:
    event = str(event_type or "").strip().lower()
    if event == "orgasm_recorded":
        return "orgasm"
    if event == "edge_recorded":
        return "edge"
    return None


def _counter_milestone_step() -> int:
    raw = (
        _get_setting("discord_counter_edge_milestone_step")
        or os.environ.get("DISCORD_COUNTER_EDGE_MILESTONE_STEP", "")
        or str(_COUNTER_NOTIFY_EDGE_STEP_DEFAULT)
    )
    try:
        return max(1, min(int(str(raw).strip()), 1000))
    except Exception:
        return _COUNTER_NOTIFY_EDGE_STEP_DEFAULT


def _counter_event_should_notify(kind: str, edge_count: Optional[int], step: int) -> bool:
    if kind == "orgasm":
        return True
    if kind == "edge":
        if edge_count is None:
            return False
        return edge_count > 0 and edge_count % step == 0
    return False


def _counter_bucket(timestamp_ms: Optional[int]) -> int:
    if timestamp_ms is None or timestamp_ms <= 0:
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return max(0, int((timestamp_ms / 1000) // _COUNTER_NOTIFY_DEDUPE_WINDOW_SEC))


def _counter_timestamp_iso(timestamp_ms: Optional[int]) -> str:
    if timestamp_ms is None or timestamp_ms <= 0:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _ensure_counter_notify_table(conn: sqlite3.Connection) -> None:
    global _COUNTER_NOTIFY_TABLE_READY
    if _COUNTER_NOTIFY_TABLE_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_counter_notification_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint   TEXT NOT NULL UNIQUE,
            event_type    TEXT NOT NULL,
            edge_count    INTEGER,
            orgasm_count  INTEGER,
            source        TEXT,
            device_id     TEXT,
            dedupe_bucket INTEGER NOT NULL,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_counter_notify_created_at ON discord_counter_notification_events(created_at DESC)"
    )
    conn.commit()
    _COUNTER_NOTIFY_TABLE_READY = True


async def maybe_send_counter_update_notification(
    *,
    event_type: str,
    edge_count: Any = None,
    orgasm_count: Any = None,
    source: str = "",
    device_id: str = "",
    timestamp_ms: Any = None,
    reason: str = "",
) -> dict[str, Any]:
    """Post deduplicated counter notifications to Discord.

    Uses hybrid cadence:
    - orgasms: notify every unique update
    - edges: notify only at milestone step boundaries
    """
    kind = _counter_event_kind(event_type)
    if kind is None:
        return {"sent": False, "skipped": "unsupported_event"}

    if not _is_feature_enabled(
        "discord_counter_notify_enabled",
        default=_COUNTER_NOTIFY_ENABLED_DEFAULT,
    ):
        return {"sent": False, "skipped": "disabled"}

    channel_id = _effective_channel_id(
        "discord_counter_channel_id",
        "DISCORD_COUNTER_CHANNEL_ID",
    )
    if not channel_id:
        return {"sent": False, "skipped": "missing_channel"}

    edge_val = _coerce_int(edge_count)
    orgasm_val = _coerce_int(orgasm_count)
    step = _counter_milestone_step()
    if not _counter_event_should_notify(kind, edge_val, step):
        return {"sent": False, "skipped": "cadence"}

    ts_ms = _coerce_int(timestamp_ms)
    bucket = _counter_bucket(ts_ms)
    fingerprint = hashlib.sha256(
        f"{kind}|{edge_val}|{orgasm_val}|{bucket}".encode("utf-8")
    ).hexdigest()

    created_at = datetime.now(timezone.utc).isoformat()
    try:
        from db import get_db_connection  # local import avoids circular import

        conn = get_db_connection()
        _ensure_counter_notify_table(conn)
        conn.execute(
            """
            INSERT INTO discord_counter_notification_events
                (fingerprint, event_type, edge_count, orgasm_count, source, device_id, dedupe_bucket, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                str(event_type or "").strip().lower(),
                edge_val,
                orgasm_val,
                str(source or "")[:100],
                str(device_id or "")[:120],
                bucket,
                created_at,
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return {"sent": False, "skipped": "deduped"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Counter notification dedupe insert failed: %s", exc)
        return {"sent": False, "skipped": "dedupe_error"}

    context = {
        "event_type": "orgasm" if kind == "orgasm" else "edge milestone",
        "edge_count": str(edge_val if edge_val is not None else 0),
        "orgasm_count": str(orgasm_val if orgasm_val is not None else 0),
        "device_id": str(device_id or "unknown device"),
        "source": str(source or "unknown"),
        "reason": str(reason or ""),
        "timestamp": _counter_timestamp_iso(ts_ms),
    }

    defaults = [
        "💗 Orgasm logged. Totals: {edge_count} edges • {orgasm_count} orgasms.",
        "🐾 Counter update: {event_type}. Totals now {edge_count}/{orgasm_count}.",
    ] if kind == "orgasm" else [
        "📈 Edge milestone reached ({edge_count}). Totals: {edge_count} edges • {orgasm_count} orgasms.",
        "⚡ Milestone hit: edge #{edge_count}. Current totals {edge_count}/{orgasm_count}.",
    ]

    message = _safe_format_variant(
        _pick_message_variant(
            "discord_counter_messages",
            "DISCORD_COUNTER_MESSAGES",
            defaults,
        ),
        context,
    ).strip()
    if not message:
        message = f"Counter update: {context['event_type']} ({context['edge_count']}/{context['orgasm_count']})"

    await send_discord_channel_message(channel_id=channel_id, content=message)
    return {"sent": True, "event_type": kind, "edge_count": edge_val, "orgasm_count": orgasm_val}


def load_reaction_role_options() -> dict[str, dict[str, Any]]:
    """Load normalized reaction-role options from settings/env JSON.

    Supported raw JSON formats:
    - {"puppy": "1234567890123", "mutt": "223..."}
    - {"puppy": {"role_id": "123...", "label": "Puppy", "emoji": "🐶"}}
    """
    raw = (
        _get_setting("discord_reaction_roles_json")
        or os.environ.get("DISCORD_REACTION_ROLES_JSON", "")
    ).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("Could not parse discord_reaction_roles_json as JSON")
        return {}
    if not isinstance(parsed, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for key, value in parsed.items():
        option_key = str(key or "").strip().lower()
        if not option_key:
            continue

        role_id = ""
        label = option_key.replace("_", " ").title()
        emoji = ""
        style = 2  # Secondary

        if isinstance(value, str):
            role_id = value.strip()
        elif isinstance(value, dict):
            role_id = str(value.get("role_id") or value.get("roleId") or "").strip()
            label = str(value.get("label") or label).strip() or label
            emoji = str(value.get("emoji") or "").strip()
            style_raw = value.get("style")
            if isinstance(style_raw, int) and style_raw in {1, 2, 3, 4}:
                style = style_raw
        else:
            continue

        if not role_id:
            continue

        normalized[option_key] = {
            "role_id": role_id,
            "label": label,
            "emoji": emoji,
            "style": style,
        }
    return normalized


def build_reaction_role_components(
    options: dict[str, dict[str, Any]],
    *,
    prefix: str = "rr",
) -> list[dict[str, Any]]:
    """Build Discord button rows for reaction-role self-assignment."""
    buttons: list[dict[str, Any]] = []
    for key, meta in options.items():
        if len(buttons) >= _DISCORD_COMPONENTS_MAX_BUTTONS:
            break
        label = str(meta.get("label") or key).strip()[:80]
        style = int(meta.get("style") or 2)
        if style not in {1, 2, 3, 4}:
            style = 2
        button: dict[str, Any] = {
            "type": 2,
            "style": style,
            "custom_id": f"{prefix}:{key}",
            "label": label,
        }
        emoji = str(meta.get("emoji") or "").strip()
        if emoji:
            button["emoji"] = {"name": emoji}
        buttons.append(button)

    rows: list[dict[str, Any]] = []
    for i in range(0, len(buttons), _DISCORD_COMPONENTS_ROW_SIZE):
        rows.append({
            "type": 1,
            "components": buttons[i:i + _DISCORD_COMPONENTS_ROW_SIZE],
        })
    return rows


async def _post_to_channel(channel_id: str, payload: dict) -> bool:
    """POST *payload* to a Discord channel via the Bot API.

    Returns True on success, False otherwise.
    """
    bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        return False
    payload = _rewrite_payload_links(payload)
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
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to post to Discord channel %s: %s", channel_id, exc)
        return False


async def _post_to_webhook(webhook_url: str, payload: dict) -> None:
    """POST *payload* to a Discord Incoming Webhook URL."""
    payload = _rewrite_payload_links(payload)
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

    variant_context = {
        "question": question_text,
        "question_id": question_id or "",
        "base_url": base_url,
    }
    variant_line = _safe_format_variant(
        _pick_message_variant(
        "discord_question_flair_messages",
        "DISCORD_QUESTION_FLAIR_MESSAGES",
        [
            "🐶 The pack inbox has fresh mail.",
            "🦴 Another secret just landed in the pouch.",
            "✨ New anonymous note received. Time to snoop.",
            "🐾 A new confession is waiting for attention.",
            "📬 New puppy mail arrived. Who wants first read?",
            "👀 A fresh note just hit the kennel queue.",
            "📝 New anonymous message in the pouch.",
            "💌 Someone sent a new secret to the pack.",
            "🚨 New question dropped. Reply squad assemble.",
            "🐕 New message posted. Time for pack wisdom.",
            "🌙 Late-night confession just came in.",
            "☕ Morning mail is here: one new anonymous note.",
            "🎀 The Puppy Pouch has a fresh entry.",
            "📦 New pouch delivery: one anonymous question.",
            "🔔 New question notification: ready for replies.",
            "🫶 Another brave message just arrived.",
            "🧷 Fresh note pinned to the pack board.",
            "📣 Pack update: new anonymous question received.",
            "🧠 New thought dropped in the Puppy Pouch.",
            "🐾 Mail call: the pouch has a new message.",
        ],
        ),
        variant_context,
    )

    if variant_line and variant_line not in content:
        payload["content"] = f"{content}\n{variant_line}".strip()

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

    answer_line = _pick_message_variant(
        "discord_answer_messages",
        "DISCORD_ANSWER_MESSAGES",
        [
            "✅ A note in the Puppy Pouch has been answered and published!",
            "📣 Fresh answer posted in the Puppy Pouch.",
            "🐕 The kennel just dropped a new published reply.",
            "🗞️ New answer is live for the pack to read.",
            "🎉 New reply just went live in the Puppy Pouch.",
            "💬 Answer published. The thread is ready.",
            "🌟 A fresh response is now live.",
            "📬 The pack posted a new public answer.",
            "📝 A new answer has been published.",
            "🔔 Update: one more answer is now live.",
            "🐾 Reply posted and visible now.",
            "📢 New published answer in the kennel feed.",
            "✨ Another response has officially dropped.",
            "🧵 Thread updated with a brand-new answer.",
            "📖 New answer available for the pack to read.",
            "🧠 Fresh insight posted in the Puppy Pouch.",
            "💗 New response just landed and is live.",
            "✅ Published: latest answer is up now.",
            "🐶 The pouch just got a new answer update.",
            "📌 New answer pinned in the feed.",
        ],
    )

    lines = [answer_line]
    if share_url:
        share_line = _safe_format_variant(
            _pick_message_variant(
                "discord_share_link_messages",
                "DISCORD_SHARE_LINK_MESSAGES",
                [
                    "Share it: {share_url}",
                    "Read and share: {share_url}",
                    "Pass it around the pack: {share_url}",
                    "Open the live share page: {share_url}",
                    "Jump to the published thread: {share_url}",
                    "Link drop: {share_url}",
                    "Pack link: {share_url}",
                    "Fresh share URL: {share_url}",
                    "Public view link: {share_url}",
                    "See it live here: {share_url}",
                    "Take a look and forward it: {share_url}",
                    "Direct link to share: {share_url}",
                ],
            ),
            {"share_url": share_url},
        )
        lines.append(share_line or f"Share it: {share_url}")

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


async def send_discord_channel_message(channel_id: str, content: str) -> bool:
    """Post plain text content to a specific Discord channel via bot API."""
    resolved = (channel_id or "").strip()
    body = (content or "").strip()
    if not resolved or not body:
        return False
    return await _post_to_channel(resolved, {"content": body})


async def send_discord_channel_payload(channel_id: str, payload: dict[str, Any]) -> bool:
    """Post an arbitrary Discord message payload to a specific channel."""
    resolved = (channel_id or "").strip()
    if not resolved:
        return False
    return await _post_to_channel(resolved, payload)


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
