"""
discord-bot/bot.py – Standalone Discord gateway bot for mochii.live

Features
--------
  /stream   – Check if any camera stream is currently live (public embed)
  /link     – Ephemeral message with instructions to link a site account
  /ask      – Open a modal to submit an anonymous question to the Puppy Pouch
  /tier     – Show the caller's current subscriber tier (ephemeral)
  /notify   – Toggle DM opt-in for stream go-live notifications
  /poke     – Trigger a connected IoT device (access_level ≥ 1 required)
  /token    – Get a one-time 5-minute login link for the site

  Reply button on question messages → modal → saves answer & publishes it

  on_member_join – optional welcome DM + admin channel notification

  Stream watcher – background task that polls go2rtc every 30 s and posts
  a go-live / stream-ended notification when live state changes, and DMs
  opted-in members via the notify-me list.

All runtime toggles and message templates are stored in the shared SQLite
``settings`` table so the admin dash can update them without a restart.

Environment variables
---------------------
DISCORD_BOT_TOKEN            (required) Bot token
DISCORD_GUILD_ID             (optional) Guild ID – omit for global command sync
DISCORD_QUESTION_CHANNEL_ID  Channel for new Q&A notes (with Reply button)
DISCORD_NOTIFICATION_CHANNEL_ID  Channel for published answers / events
DISCORD_ADMIN_CHANNEL_ID     Private channel for operational alerts
DISCORD_STREAM_CHANNEL_ID    Channel for go-live / stream-ended announcements
GO2RTC_HOST                  go2rtc host (default: go2rtc)
GO2RTC_PORT                  go2rtc port (default: 1984)
BASE_URL                     Public site root URL (e.g. https://mochii.live)
DATABASE_PATH                Shared SQLite database (default: /app/data/camera_site.db)
SECRET_KEY                   Shared JWT secret (same as backend SECRET_KEY) – used by /token
BACKEND_URL                  Internal backend URL (default: http://backend:8000)
ADMIN_USERNAME               HTTP Basic admin username – used by /poke
ADMIN_PASSWORD               HTTP Basic admin password – used by /poke
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("mochii-bot")

# ── Environment variables ────────────────────────────────────────────────────

BOT_TOKEN      = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID       = os.environ.get("DISCORD_GUILD_ID", "")
GO2RTC_HOST    = os.environ.get("GO2RTC_HOST", "go2rtc")
GO2RTC_PORT    = os.environ.get("GO2RTC_PORT", "1984")
BASE_URL       = os.environ.get("BASE_URL", "").rstrip("/")
DATABASE_PATH  = os.environ.get("DATABASE_PATH", "/app/data/camera_site.db")
SECRET_KEY     = os.environ.get("SECRET_KEY", "")
BACKEND_URL    = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# How often (seconds) to poll go2rtc for live-state changes; override via env var.
STREAM_POLL_INTERVAL = int(os.environ.get("DISCORD_STREAM_POLL_INTERVAL", "30"))

# Channel ID env-var fallbacks (settings table values take precedence at runtime)
_ENV_QUESTION_CH     = os.environ.get("DISCORD_QUESTION_CHANNEL_ID", "")
_ENV_NOTIFICATION_CH = os.environ.get("DISCORD_NOTIFICATION_CHANNEL_ID", "")
_ENV_ADMIN_CH        = os.environ.get("DISCORD_ADMIN_CHANNEL_ID", "")
_ENV_STREAM_CH       = os.environ.get("DISCORD_STREAM_CHANNEL_ID", "")

_MOCHII_PINK  = 0xE8AEB7
_MOCHII_RED   = 0xFF5C5C
_MOCHII_GREY  = 0x5C5C5C

# ── Database helpers ─────────────────────────────────────────────────────────


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        conn = _db()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default


def _is_enabled(setting_key: str, default: bool = True) -> bool:
    val = _get_setting(setting_key)
    if val is None:
        return default
    return val.strip().lower() == "true"


def _channel(setting_key: str, env_fallback: str) -> str:
    """Return the effective Discord channel ID: settings table > env var."""
    return (_get_setting(setting_key) or env_fallback).strip()


# ── Notify-me helpers ────────────────────────────────────────────────────────


def _get_notify_users() -> list[str]:
    """Return the list of Discord IDs opted-in to go-live DM notifications."""
    raw = _get_setting("discord_notify_me_users", "[]")
    try:
        return _json.loads(raw)
    except Exception:
        return []


def _set_notify_users(users: list[str]) -> None:
    """Persist the notify-me Discord ID list to the settings table."""
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("discord_notify_me_users", _json.dumps(users)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed to save notify-me list: %s", exc)


# ── Poke cooldowns (in-memory) ───────────────────────────────────────────────
# key: (discord_id, device)  value: monotonic timestamp when cooldown expires
_poke_cooldowns: dict[tuple[str, str], float] = {}
_POKE_COOLDOWN_TIER12 = 3600   # 1 hour  for access_level 1–2
_POKE_COOLDOWN_TIER3  = 300    # 5 minutes for access_level 3+


# ── Bot client ───────────────────────────────────────────────────────────────


class MochiiBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # Required for on_member_join
        intents.message_content = False  # Not needed
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        """Sync slash commands and start background tasks."""
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            logger.info("Synced %d slash commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d slash commands globally", len(synced))
        self.loop.create_task(_stream_live_watcher(), name="stream-live-watcher")

    async def on_ready(self) -> None:
        logger.info("Discord bot ready – %s (ID: %s)", self.user, self.user.id)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Route component interactions (buttons). App commands are handled by the tree."""
        if interaction.type is discord.InteractionType.component:
            custom_id: str = interaction.data.get("custom_id", "")
            if custom_id.startswith("reply:"):
                await _handle_reply_button(interaction, custom_id[len("reply:"):])
                return
        # All other interaction types (app commands, modals, autocomplete) are
        # routed to the CommandTree through the client's internal dispatch – we
        # don't need to call tree.on_interaction here.

    async def on_member_join(self, member: discord.Member) -> None:
        await _handle_member_join(member)


bot = MochiiBot()


# ── Slash commands ───────────────────────────────────────────────────────────


@bot.tree.command(name="stream", description="Check if the stream is currently live")
async def cmd_stream(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    is_live = await _check_any_stream_live()
    stream_url = f"{BASE_URL}/member" if BASE_URL else None

    # Count total viewers across all streams
    total_viewers = 0
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams")
        if resp.status_code == 200:
            for stream_info in resp.json().values():
                total_viewers += len(stream_info.get("consumers", []))
    except Exception:
        pass

    if is_live:
        embed = discord.Embed(
            title="🔴 Stream is LIVE!",
            description="Click below to watch now 🐾",
            color=_MOCHII_RED,
            url=stream_url,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👀 Viewers", value=str(total_viewers), inline=True)
        embed.set_footer(text="mochii.live")
        await interaction.followup.send(embed=embed)
    else:
        embed = discord.Embed(
            title="⚫ Stream is Offline",
            description="Not live right now – check back later! 🐾",
            color=_MOCHII_GREY,
        )
        embed.set_footer(text="mochii.live")
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="schedule", description="View the upcoming stream schedule")
async def cmd_schedule(interaction: discord.Interaction) -> None:
    import json as _json
    raw = _get_setting("stream_schedule")
    schedule = []
    if raw:
        try:
            schedule = _json.loads(raw)
        except Exception:
            pass
    if not schedule:
        embed = discord.Embed(
            title="📅 Stream Schedule",
            description="No upcoming streams scheduled yet – check back later! 🐾",
            color=_MOCHII_PINK,
        )
        await interaction.response.send_message(embed=embed)
        return
    lines = []
    for slot in schedule[:10]:
        day = slot.get("day", "")
        time_str = slot.get("time", "")
        note = slot.get("note", "")
        line = f"**{day}** {time_str}"
        if note:
            line += f" – {note}"
        lines.append(line)
    embed = discord.Embed(
        title="📅 Stream Schedule",
        description="\n".join(lines),
        color=_MOCHII_PINK,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="mochii.live")
    if BASE_URL:
        embed.url = BASE_URL
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="link",
    description="Get instructions to link your Discord account to your site account",
)
async def cmd_link(interaction: discord.Interaction) -> None:
    if not BASE_URL:
        await interaction.response.send_message(
            "⚠️ The site URL hasn't been configured yet – ask an admin.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🔗 **Link your Discord account to your site account:**\n"
        f"1. Log in at **{BASE_URL}/member**\n"
        f"2. Go to your profile settings and click **Link Discord**.\n\n"
        f"Once linked, `/tier` will show your subscriber tier.",
        ephemeral=True,
    )


@bot.tree.command(name="tier", description="Check your current subscriber tier")
async def cmd_tier(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)

    access_level: Optional[int] = None
    try:
        conn = _db()
        row = conn.execute(
            """
            SELECT u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()
        conn.close()
        if row:
            access_level = int(row["access_level"])
    except Exception as exc:
        logger.warning("DB error in /tier for discord_id=%s: %s", discord_id, exc)

    if access_level is None:
        link_text = (
            f"Log in at {BASE_URL}/member and link your Discord account to check."
            if BASE_URL
            else "Log in and link your Discord account to check."
        )
        await interaction.response.send_message(
            f"🔗 Your Discord account isn't linked to a site account yet.\n{link_text}",
            ephemeral=True,
        )
        return

    tier_labels = {0: "Free 🐾", 1: "Pack Member", 2: "Alpha", 3: "Alpha+"}
    tier_name = tier_labels.get(access_level, f"Tier {access_level}")

    embed = discord.Embed(
        title="🐾 Subscriber Tier",
        description=f"Your current tier: **{tier_name}**",
        color=_MOCHII_PINK,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="notify",
    description="Toggle DM notifications for when the stream goes live 🔔",
)
async def cmd_notify(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)
    users = _get_notify_users()

    if discord_id in users:
        users.remove(discord_id)
        _set_notify_users(users)
        await interaction.response.send_message(
            "🔕 You've been removed from go-live DM alerts. "
            "Run `/notify` again to re-enable. 🐾",
            ephemeral=True,
        )
    else:
        users.append(discord_id)
        _set_notify_users(users)
        await interaction.response.send_message(
            "🔔 Done! You'll receive a DM the next time the stream goes live. "
            "Run `/notify` again to unsubscribe. 🐾",
            ephemeral=True,
        )


@bot.tree.command(
    name="poke",
    description="Activate the creator's connected toy 🐾 (active subscription required)",
)
@app_commands.describe(device="Which device to poke")
@app_commands.choices(device=[
    app_commands.Choice(name="PiShock ⚡", value="pishock"),
    app_commands.Choice(name="Lovense 💜", value="lovense"),
])
async def cmd_poke(
    interaction: discord.Interaction,
    device: app_commands.Choice[str],
) -> None:
    if not _is_enabled("discord_poke_enabled", default=False):
        await interaction.response.send_message(
            "⚠️ `/poke` isn't enabled right now – check back later! 🐾",
            ephemeral=True,
        )
        return

    discord_id = str(interaction.user.id)
    access_level: Optional[int] = None

    try:
        conn = _db()
        row = conn.execute(
            """
            SELECT u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()
        conn.close()
        if row:
            access_level = int(row["access_level"])
    except Exception as exc:
        logger.warning("DB error in /poke for discord_id=%s: %s", discord_id, exc)

    if access_level is None:
        link_text = (
            f"Log in at {BASE_URL}/member and link your Discord account first."
            if BASE_URL
            else "Link your Discord account to your site account first."
        )
        await interaction.response.send_message(
            f"🔗 Your Discord account isn't linked to a site account yet.\n{link_text}",
            ephemeral=True,
        )
        return

    if access_level < 1:
        await interaction.response.send_message(
            "🔒 An active subscription is required to use `/poke`. 🐾",
            ephemeral=True,
        )
        return

    # Check per-user, per-device cooldown
    device_name = device.value
    ck = (discord_id, device_name)
    now = asyncio.get_event_loop().time()
    unblock_at = _poke_cooldowns.get(ck, 0.0)
    if now < unblock_at:
        remaining = int(unblock_at - now)
        mins, secs = divmod(remaining, 60)
        wait_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        await interaction.response.send_message(
            f"⏳ Cooldown active – try again in **{wait_str}**. 🐾",
            ephemeral=True,
        )
        return

    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        await interaction.response.send_message(
            "⚠️ Device control is not configured on this server. Ask an admin.", ephemeral=True
        )
        return

    # Call the backend admin control endpoint
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/admin/control/{device_name}",
                auth=(ADMIN_USERNAME, ADMIN_PASSWORD),
            )
    except Exception as exc:
        logger.warning("Poke request to backend failed: %s", exc)
        await interaction.response.send_message(
            "⚠️ Couldn't reach the device controller – try again in a moment. 🐾",
            ephemeral=True,
        )
        return

    if resp.status_code != 200:
        await interaction.response.send_message(
            f"⚠️ Device controller returned an error (HTTP {resp.status_code}). 🐾",
            ephemeral=True,
        )
        return

    # Set cooldown after a successful trigger
    cooldown = _POKE_COOLDOWN_TIER3 if access_level >= 3 else _POKE_COOLDOWN_TIER12
    _poke_cooldowns[ck] = now + cooldown
    cooldown_str = "5 minutes" if access_level >= 3 else "1 hour"

    device_labels = {"pishock": "⚡ PiShock", "lovense": "💜 Lovense"}
    label = device_labels.get(device_name, device_name)

    embed = discord.Embed(
        title=f"{label} activated! 🐾",
        description="Command sent to the creator's device.",
        color=_MOCHII_PINK,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Next poke available in {cooldown_str}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="token",
    description="Get a one-time 5-minute link to log in to the site without a password 🔑",
)
async def cmd_token(interaction: discord.Interaction) -> None:
    if not BASE_URL:
        await interaction.response.send_message(
            "⚠️ The site URL hasn't been configured yet – ask an admin.", ephemeral=True
        )
        return

    if not SECRET_KEY:
        await interaction.response.send_message(
            "⚠️ Token generation isn't configured on this server – ask an admin.", ephemeral=True
        )
        return

    discord_id = str(interaction.user.id)
    user_id: Optional[str] = None
    access_level: int = 0

    try:
        conn = _db()
        row = conn.execute(
            """
            SELECT u.id, u.access_level
            FROM   discord_accounts da
            JOIN   users u ON u.id = da.user_id
            WHERE  da.discord_id = ?
            """,
            (discord_id,),
        ).fetchone()
        conn.close()
        if row:
            user_id = str(row["id"])
            access_level = int(row["access_level"])
    except Exception as exc:
        logger.warning("DB error in /token for discord_id=%s: %s", discord_id, exc)

    if user_id is None:
        link_text = (
            f"Log in at {BASE_URL}/member and link your Discord account first."
            if BASE_URL
            else "Link your Discord account to your site account first."
        )
        await interaction.response.send_message(
            f"🔗 Your Discord account isn't linked to a site account yet.\n{link_text}",
            ephemeral=True,
        )
        return

    import jwt as _pyjwt

    payload = {
        "sub": user_id,
        "access_level": access_level,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "one_time": True,
    }
    token = _pyjwt.encode(payload, SECRET_KEY, algorithm="HS256")
    login_url = f"{BASE_URL}/member?token={token}"

    await interaction.response.send_message(
        f"🔑 **One-time login link** – expires in **5 minutes**:\n"
        f"{login_url}\n\n"
        "⚠️ Keep this private – it logs directly into your account!",
        ephemeral=True,
    )


class AskModal(discord.ui.Modal, title="Ask a Question 🐾"):
    question = discord.ui.TextInput(
        label="Your anonymous question",
        style=discord.TextStyle.paragraph,
        placeholder="Type your question here…",
        required=True,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        question_text = self.question.value.strip()
        if not question_text:
            await interaction.response.send_message(
                "⚠️ Your question was empty – please try again.", ephemeral=True
            )
            return

        question_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            conn = _db()
            conn.execute(
                "INSERT INTO questions (id, text, is_public, created_at) VALUES (?, ?, 0, ?)",
                (question_id, question_text, now),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("Failed to save question via /ask: %s", exc)
            await interaction.response.send_message(
                "⚠️ Something went wrong saving your question. Please try again.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ Your question has been submitted anonymously to the Puppy Pouch! 🐾",
            ephemeral=True,
        )

        # Post to the question channel with a Reply button
        ch_id = _channel("discord_question_channel_id", _ENV_QUESTION_CH)
        if ch_id and _is_enabled("discord_notify_questions", default=True):
            channel = bot.get_channel(int(ch_id))
            if channel:
                embed = discord.Embed(
                    title="📬 New note in the Puppy Pouch!",
                    description=f">>> {question_text}",
                    color=_MOCHII_PINK,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text="mochii.live · Puppy Pouch")
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(
                    label="Reply 🐾",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"reply:{question_id}",
                ))
                await channel.send(embed=embed, view=view)


@bot.tree.command(name="ask", description="Submit an anonymous question to the Puppy Pouch 🐾")
async def cmd_ask(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(AskModal())


# ── Reply button → modal → save answer ───────────────────────────────────────


class ReplyModal(discord.ui.Modal, title="Reply to Note 🐾"):
    def __init__(self, question_id: str, question_preview: str) -> None:
        super().__init__()
        self._question_id = question_id
        self.preview = discord.ui.TextInput(
            label="Note (reference – not editable)",
            style=discord.TextStyle.paragraph,
            default=question_preview[:1024],
            required=False,
        )
        self.reply = discord.ui.TextInput(
            label="Your reply",
            style=discord.TextStyle.paragraph,
            placeholder="Write your answer… 🐾",
            required=True,
            max_length=2000,
        )
        self.add_item(self.preview)
        self.add_item(self.reply)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answer_text = self.reply.value.strip()
        if not answer_text:
            await interaction.response.send_message(
                "⚠️ Reply was empty – please try again.", ephemeral=True
            )
            return

        saved = False
        try:
            conn = _db()
            cur = conn.execute(
                "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ? AND answer IS NULL",
                (answer_text, self._question_id),
            )
            conn.commit()
            saved = cur.rowcount > 0
            conn.close()
        except Exception as exc:
            logger.error("Failed to save reply from Discord modal: %s", exc)
            await interaction.response.send_message(
                "⚠️ Failed to save the reply. Please try again.", ephemeral=True
            )
            return

        if not saved:
            await interaction.response.send_message(
                "⚠️ Couldn't save – that note may have already been answered.",
                ephemeral=True,
            )
            return

        share_url = f"{BASE_URL}/q/{self._question_id}" if BASE_URL else ""
        lines = ["✅ Reply saved and published! 🐾"]
        if share_url:
            lines.append(f"Share: {share_url}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # Notify the notification channel
        ch_id = _channel("discord_notification_channel_id", _ENV_NOTIFICATION_CH)
        if ch_id and _is_enabled("discord_notify_answers", default=True):
            channel = bot.get_channel(int(ch_id))
            if channel:
                content = "✅ A Puppy Pouch note has been answered and published!"
                if share_url:
                    content += f"\n{share_url}"
                await channel.send(content)


async def _handle_reply_button(interaction: discord.Interaction, question_id: str) -> None:
    """Open the ReplyModal when an admin clicks the Reply button on a question."""
    question_text = ""
    try:
        conn = _db()
        row = conn.execute(
            "SELECT text FROM questions WHERE id = ? AND answer IS NULL",
            (question_id,),
        ).fetchone()
        conn.close()
        if row is None:
            await interaction.response.send_message(
                "⚠️ That note has already been answered or deleted.", ephemeral=True
            )
            return
        question_text = row["text"]
    except Exception as exc:
        logger.warning("DB error fetching question for reply: %s", exc)
        await interaction.response.send_message(
            "⚠️ Database error – please try again.", ephemeral=True
        )
        return

    await interaction.response.send_modal(
        ReplyModal(question_id=question_id, question_preview=question_text)
    )


# ── Member join ──────────────────────────────────────────────────────────────


async def _handle_member_join(member: discord.Member) -> None:
    if not _is_enabled("discord_welcome_dm_enabled", default=False):
        return

    welcome_msg = _get_setting("discord_welcome_dm_message") or (
        f"Hey {member.mention}, welcome to the pack! 🐾\n\n"
        + (
            f"Check out the site at {BASE_URL} and link your account to unlock subscriber perks!\n"
            if BASE_URL
            else ""
        )
        + "Hope you enjoy it here 💕"
    )

    try:
        await member.send(welcome_msg)
        logger.info("Welcome DM sent to %s (%s)", member.name, member.id)
    except discord.Forbidden:
        logger.info("Welcome DM blocked by %s (%s) – DMs disabled", member.name, member.id)

    # Admin channel notification
    ch_id = _channel("discord_admin_channel_id", _ENV_ADMIN_CH)
    if ch_id:
        channel = bot.get_channel(int(ch_id))
        if channel:
            await channel.send(
                f"👋 New member joined: **{discord.utils.escape_markdown(member.name)}** "
                f"(`{member.id}`)"
            )


# ── Stream live watcher ───────────────────────────────────────────────────────


_stream_was_live: bool = False


async def _check_any_stream_live() -> bool:
    """Return True if any go2rtc stream has active producers."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams")
        if resp.status_code == 200:
            for stream_info in resp.json().values():
                if stream_info.get("producers"):
                    return True
    except Exception:
        pass
    return False


async def _stream_live_watcher() -> None:
    """Poll go2rtc every 30 s and post Discord notifications on live-state changes."""
    global _stream_was_live
    await bot.wait_until_ready()
    logger.info("Stream live watcher started")

    while not bot.is_closed():
        await asyncio.sleep(STREAM_POLL_INTERVAL)

        if not _is_enabled("discord_stream_notifications_enabled", default=False):
            continue

        ch_id = _channel("discord_stream_channel_id", _ENV_STREAM_CH)
        if not ch_id:
            continue

        try:
            is_live = await _check_any_stream_live()

            if is_live and not _stream_was_live:
                channel = bot.get_channel(int(ch_id))
                if channel:
                    # Get stream title from DB
                    stream_title = "mochii.live"
                    try:
                        conn = _db()
                        row = conn.execute(
                            "SELECT display_name FROM cameras LIMIT 1"
                        ).fetchone()
                        if row:
                            stream_title = row["display_name"]
                        conn.close()
                    except Exception:
                        pass

                    stream_url = f"{BASE_URL}/member" if BASE_URL else ""
                    template = (
                        _get_setting("discord_stream_live_message")
                        or "@here 🔴 **{title}** is now LIVE! {url}"
                    )
                    content = template.format(
                        title=stream_title, url=stream_url
                    ).strip()

                    embed = discord.Embed(
                        title="🔴 Stream is LIVE!",
                        description=stream_title,
                        color=_MOCHII_RED,
                        url=stream_url or None,
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.set_footer(text="mochii.live")
                    await channel.send(content=content, embed=embed)
                    logger.info("Go-live notification sent")

                # DM all opted-in users
                notify_users = _get_notify_users()
                if notify_users:
                    dm_content = (
                        _get_setting("discord_notify_me_dm_message")
                        or (
                            f"🔴 **mochii is LIVE!** Come watch → {stream_url}"
                            if stream_url
                            else "🔴 **mochii is LIVE!** Come watch now 🐾"
                        )
                    )
                    for uid in notify_users:
                        try:
                            user_obj = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                            await user_obj.send(dm_content)
                        except Exception as dm_exc:
                            logger.debug(
                                "Could not DM notify-me user %s: %s", uid, dm_exc
                            )

            elif not is_live and _stream_was_live:
                channel = bot.get_channel(int(ch_id))
                if channel:
                    await channel.send(
                        "⚫ The stream has ended. Thanks for watching! 🐾"
                    )
                logger.info("Stream-offline notification sent")

            _stream_was_live = is_live

        except Exception as exc:
            logger.warning("Stream live watcher error: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. "
            "Set it in your .env file or compose environment."
        )
    bot.run(BOT_TOKEN, log_handler=None)
