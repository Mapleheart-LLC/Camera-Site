"""
discord-bot/bot.py – Standalone Discord gateway bot for mochii.live

Features
--------
  /stream   – Check if any camera stream is currently live (public embed)
  /link     – Ephemeral message with instructions to link a site account
  /ask      – Open a modal to submit an anonymous question to the Puppy Pouch
  /tier     – Show the caller's current subscriber tier (ephemeral)

  Reply button on question messages → modal → saves answer & publishes it

  on_member_join – optional welcome DM + admin channel notification

  Stream watcher – background task that polls go2rtc every 30 s and posts
  a go-live / stream-ended notification when live state changes

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
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
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

BOT_TOKEN    = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID     = os.environ.get("DISCORD_GUILD_ID", "")
GO2RTC_HOST  = os.environ.get("GO2RTC_HOST", "go2rtc")
GO2RTC_PORT  = os.environ.get("GO2RTC_PORT", "1984")
BASE_URL     = os.environ.get("BASE_URL", "").rstrip("/")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/data/camera_site.db")
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

    if is_live:
        embed = discord.Embed(
            title="🔴 Stream is LIVE!",
            description="Click below to watch now 🐾",
            color=_MOCHII_RED,
            url=stream_url,
            timestamp=datetime.now(timezone.utc),
        )
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
