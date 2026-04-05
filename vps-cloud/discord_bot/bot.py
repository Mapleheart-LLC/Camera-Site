"""
Discord management bot for mochii.live

Fully manages the Discord server and deeply integrates with every site feature:

Server management
-----------------
  /setup          Scaffold all roles, categories and channels (idempotent)
  /sync-roles     Sync Fanvue subscriber roles for all members or a specific one
  /announce       Post a message to #announcements as a branded embed

Site features
-------------
  /ask            Submit an anonymous note to the Puppy Pouch
  /pouch          Browse recent answered Puppy Pouch notes
  /drool          Browse the Drool Log (Follower+)
  /weekly-whimper Show the most-reacted Drool Log item this week
  /nowplaying     Show the currently playing Spotify track
  /queue          Search Spotify and add a track to the creator's queue (Subscriber+)
  /store          Browse the store catalogue
  /zap            Activate a PiShock / Lovense device (Follower+, cooldown-aware)

Community
---------
  /server-info    Aggregated stats embed
  /verify         DM yourself an account-linking guide
  /whoami         Show your own Fanvue tier and Discord link status

Background automation
---------------------
  • Role sync every 30 minutes
  • Posts new Drool Log items to #drool-log (polls every 5 minutes)
  • Updates #now-playing when the Spotify track changes (polls every 2 minutes)
  • Weekly Whimper highlight every Monday (checks hourly)
  • Welcomes new members via DM with a verification guide

Required environment variables
-------------------------------
DISCORD_BOT_TOKEN   Bot token from the Discord Developer Portal.
BACKEND_API_URL     Internal URL of the FastAPI backend (e.g. http://backend:8000).
BASE_URL            Public site URL (e.g. https://mochii.live).

Optional environment variables
-------------------------------
DISCORD_GUILD_ID    Restrict slash-command registration to one guild so commands
                    appear instantly instead of waiting up to 1 hour globally.
BOT_STATE_FILE      Path to the JSON state file
                    (default: /app/state/bot_state.json).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("mochii.bot")

# ── Branding ──────────────────────────────────────────────────────────────────

_PINK    = 0xE8AEB7   # mochii.live muted pink
_GOLD    = 0xFEE75C
_PURPLE  = 0x9B59B6
_BLURPLE = 0x5865F2
_GREEN   = 0x57F287
_RED     = 0xED4245
_GRAY    = 0x36393F

# ── Role definitions (ordered high → low in server hierarchy) ─────────────────

_ROLE_DEFS: list[dict] = [
    {
        "name": "⭐ Admin",
        "color": _GOLD,
        "hoist": True,
        "mentionable": False,
        "permissions": discord.Permissions(administrator=True),
    },
    {
        "name": "🛡️ Moderator",
        "color": _GREEN,
        "hoist": True,
        "mentionable": False,
        "permissions": discord.Permissions(
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            manage_nicknames=True,
            view_audit_log=True,
            mute_members=True,
            move_members=True,
            moderate_members=True,
        ),
    },
    {
        "name": "💎 Tier 2",
        "color": _PURPLE,
        "hoist": True,
        "mentionable": True,
        "permissions": discord.Permissions.none(),
    },
    {
        "name": "🌸 Subscriber",
        "color": _PINK,
        "hoist": True,
        "mentionable": True,
        "permissions": discord.Permissions.none(),
    },
    {
        "name": "🐾 Follower",
        "color": _BLURPLE,
        "hoist": True,
        "mentionable": True,
        "permissions": discord.Permissions.none(),
    },
    {
        "name": "✅ Linked",
        "color": _GRAY,
        "hoist": False,
        "mentionable": False,
        "permissions": discord.Permissions.none(),
    },
]

# Fanvue access_level → minimum role name required (highest tier first).
_TIER_ROLES: list[tuple[str, int]] = [
    ("💎 Tier 2",     3),
    ("🌸 Subscriber", 2),
    ("🐾 Follower",   1),
    ("✅ Linked",     0),
]
_ALL_TIER_NAMES = {name for name, _ in _TIER_ROLES}

# ── Channel / category structure ──────────────────────────────────────────────
# state_key  → written to the bot state file after setup so background tasks
#              know which channel to post to.
# notify_key → these channel IDs must also be set as env vars in the backend.

_CHANNEL_STRUCTURE: list[dict] = [
    {
        "category": "📢 INFORMATION",
        "min_role": None,   # visible to @everyone
        "channels": [
            {"name": "rules",         "topic": "Server rules — please read before participating.", "readonly": True},
            {"name": "announcements", "topic": "Official announcements from mochii.live.", "readonly": True, "state_key": "announcements_channel"},
            {"name": "roles-info",    "topic": "How to verify your Fanvue account and earn subscriber roles.", "readonly": True},
        ],
    },
    {
        "category": "💬 COMMUNITY",
        "min_role": None,   # open to everyone in the server
        "channels": [
            {"name": "general",       "topic": "General chat — say hello! 🐾"},
            {"name": "introductions", "topic": "New to the server? Introduce yourself!"},
            {"name": "off-topic",     "topic": "Anything goes (within the rules)."},
        ],
    },
    {
        "category": "🐾 FOLLOWER ZONE",
        "min_role": "🐾 Follower",
        "channels": [
            {"name": "follower-chat", "topic": "Follower-exclusive chat. 🐾"},
            {"name": "puppy-pouch",   "topic": "Answered Puppy Pouch notes — posted automatically. 🐾", "readonly": True, "state_key": "puppy_pouch_channel"},
            {"name": "drool-log",     "topic": "New Drool Log items — posted automatically. 🔥",         "readonly": True, "state_key": "drool_channel"},
        ],
    },
    {
        "category": "🌸 SUBSCRIBER LOUNGE",
        "min_role": "🌸 Subscriber",
        "channels": [
            {"name": "subscriber-chat",  "topic": "Subscriber-only chat. 🌸"},
            {"name": "mochii-updates",   "topic": "Content updates and exclusive news. 🌸",                "readonly": True, "state_key": "updates_channel"},
            {"name": "now-playing",      "topic": "Current Spotify track — updated automatically. 🎵",    "readonly": True, "state_key": "nowplaying_channel"},
        ],
    },
    {
        "category": "💎 PREMIUM DEN",
        "min_role": "💎 Tier 2",
        "channels": [
            {"name": "premium-chat",   "topic": "Tier 2 exclusive chat. 💎"},
            {"name": "camera-lounge",  "topic": "Stream and camera status alerts. 📷",  "readonly": True, "state_key": "camera_channel"},
        ],
    },
    {
        "category": "🔒 STAFF",
        "min_role": "🛡️ Moderator",
        "channels": [
            {"name": "staff-chat",  "topic": "Staff discussion."},
            {"name": "mod-log",     "topic": "Moderation action log."},
            {"name": "bot-logs",    "topic": "Bot event log."},
        ],
    },
    {
        "category": "📬 BOT NOTIFICATIONS",
        "min_role": "⭐ Admin",
        "channels": [
            {"name": "questions",     "topic": "Incoming Puppy Pouch notes. → set DISCORD_QUESTION_CHANNEL_ID to this ID",    "notify_key": "DISCORD_QUESTION_CHANNEL_ID"},
            {"name": "notifications", "topic": "Answer-published events. → set DISCORD_NOTIFICATION_CHANNEL_ID to this ID",   "notify_key": "DISCORD_NOTIFICATION_CHANNEL_ID"},
            {"name": "admin-alerts",  "topic": "Operational alerts (purchases, activations). → set DISCORD_ADMIN_CHANNEL_ID",  "notify_key": "DISCORD_ADMIN_CHANNEL_ID"},
        ],
    },
]

# ── State file ────────────────────────────────────────────────────────────────

_DEFAULT_STATE: dict = {
    "guilds": {},
    "last_drool_id":            0,
    "last_nowplaying_track_id": None,
    "last_weekly_whimper_date": None,
}


def _state_path() -> Path:
    return Path(os.environ.get("BOT_STATE_FILE", "/app/state/bot_state.json"))


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            with p.open() as f:
                return {**_DEFAULT_STATE, **json.load(f)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load state file: %s — using defaults.", exc)
    return dict(_DEFAULT_STATE)


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save state file: %s", exc)


_state: dict = _load_state()


def _guild_channel(guild_id: int, key: str) -> Optional[int]:
    """Return a stored channel ID for *key* in *guild_id*, or None."""
    channel_id = _state.get("guilds", {}).get(str(guild_id), {}).get(key)
    return int(channel_id) if channel_id else None


def _set_guild_channel(guild_id: int, key: str, channel_id: int) -> None:
    _state.setdefault("guilds", {}).setdefault(str(guild_id), {})[key] = str(channel_id)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _backend() -> str:
    return os.environ.get("BACKEND_API_URL", "http://backend:8000").rstrip("/")


def _bot_headers() -> dict:
    return {"Authorization": f"Bot {os.environ.get('DISCORD_BOT_TOKEN', '')}"}


def _base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/")


async def _api_get(path: str, **params) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}{path}", params=params, headers=_bot_headers())
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET %s failed: %s", path, exc)
    return None


async def _api_post(path: str, json_body: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(f"{_backend()}{path}", json=json_body, headers=_bot_headers())
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("POST %s failed: %s", path, exc)
    return None


async def _get_member_level(discord_id: str) -> Optional[int]:
    """Return Fanvue access_level for a linked Discord user, or None."""
    data = await _api_get("/api/discord/bot/member", discord_id=discord_id)
    if data and data.get("is_linked"):
        return int(data["access_level"])
    return None


async def _get_all_linked() -> list[dict]:
    data = await _api_get("/api/discord/bot/members")
    return data if isinstance(data, list) else []


# ── Permission builders ───────────────────────────────────────────────────────

def _role_index(name: str) -> int:
    for i, d in enumerate(_ROLE_DEFS):
        if d["name"] == name:
            return i
    return len(_ROLE_DEFS)


def _build_category_overwrites(
    everyone: discord.Role,
    role_map: dict[str, discord.Role],
    min_role_name: Optional[str],
) -> dict:
    if min_role_name is None:
        return {everyone: discord.PermissionOverwrite(read_messages=True, send_messages=False)}
    threshold = _role_index(min_role_name)
    ow: dict = {everyone: discord.PermissionOverwrite(read_messages=False, send_messages=False)}
    for i, defn in enumerate(_ROLE_DEFS):
        if i <= threshold:
            role = role_map.get(defn["name"])
            if role:
                ow[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    return ow


def _build_channel_overwrites(
    everyone: discord.Role,
    role_map: dict[str, discord.Role],
    min_role_name: Optional[str],
    readonly: bool,
) -> Optional[dict]:
    if not readonly:
        return None  # inherit from category

    if min_role_name is None:
        # Public readonly: everyone reads, Admins/Mods write (admin bypasses anyway)
        return {everyone: discord.PermissionOverwrite(read_messages=True, send_messages=False)}

    # Private readonly: min_role+ reads; only Admin/Mod write
    threshold = _role_index(min_role_name)
    ow: dict = {everyone: discord.PermissionOverwrite(read_messages=False, send_messages=False)}
    for i, defn in enumerate(_ROLE_DEFS):
        if i <= threshold:
            role = role_map.get(defn["name"])
            if role:
                can_send = defn["name"] in ("⭐ Admin", "🛡️ Moderator")
                ow[role] = discord.PermissionOverwrite(read_messages=True, send_messages=can_send)
    return ow


# ── Server setup ──────────────────────────────────────────────────────────────

async def _ensure_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    existing  = {r.name: r for r in guild.roles}
    role_map: dict[str, discord.Role] = {}

    for defn in _ROLE_DEFS:
        name = defn["name"]
        if name in existing:
            role_map[name] = existing[name]
        else:
            role = await guild.create_role(
                name=name,
                color=discord.Color(defn["color"]),
                hoist=defn["hoist"],
                mentionable=defn["mentionable"],
                permissions=defn["permissions"],
                reason="mochii.live server setup",
            )
            role_map[name] = role
            logger.info("Created role: %s", name)

    # Reorder: place our roles just below the bot's highest role.
    bot_top = max((r.position for r in guild.me.roles), default=2)
    positions: dict[discord.Role, int] = {}
    for i, defn in enumerate(_ROLE_DEFS):
        role = role_map.get(defn["name"])
        if role:
            target = max(1, bot_top - 1 - i)
            if role.position != target:
                positions[role] = target
    if positions:
        try:
            await guild.edit_role_positions(positions, reason="mochii.live server setup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not reorder roles: %s", exc)

    return role_map


async def _ensure_channels(
    guild: discord.Guild,
    role_map: dict[str, discord.Role],
) -> tuple[dict[str, str], dict[str, discord.TextChannel]]:
    """
    Create/update all categories and channels.

    Returns (notify_map, state_channel_map):
      notify_map        key → channel ID string  (for env-var printout)
      state_channel_map state_key → channel       (for state file)
    """
    everyone     = guild.default_role
    notify_map:  dict[str, str] = {}
    state_map:   dict[str, discord.TextChannel] = {}

    for cat_defn in _CHANNEL_STRUCTURE:
        cat_name      = cat_defn["category"]
        min_role_name: Optional[str] = cat_defn.get("min_role")
        cat_ow        = _build_category_overwrites(everyone, role_map, min_role_name)

        category = discord.utils.get(guild.categories, name=cat_name)
        if category is None:
            category = await guild.create_category(
                cat_name, overwrites=cat_ow, reason="mochii.live server setup",
            )
            logger.info("Created category: %s", cat_name)
        else:
            await category.edit(overwrites=cat_ow, reason="mochii.live server setup")

        for ch_defn in cat_defn["channels"]:
            ch_name    = ch_defn["name"]
            ch_topic   = ch_defn.get("topic", "")
            ch_ow      = _build_channel_overwrites(
                everyone, role_map, min_role_name, ch_defn.get("readonly", False)
            )
            notify_key: Optional[str] = ch_defn.get("notify_key")
            state_key:  Optional[str] = ch_defn.get("state_key")

            channel = discord.utils.get(guild.text_channels, name=ch_name)
            if channel is None:
                kw: dict = {"category": category, "topic": ch_topic, "reason": "mochii.live server setup"}
                if ch_ow is not None:
                    kw["overwrites"] = ch_ow
                channel = await guild.create_text_channel(ch_name, **kw)
                logger.info("Created #%s", ch_name)
            else:
                kw = {"category": category, "topic": ch_topic, "reason": "mochii.live server setup"}
                if ch_ow is not None:
                    kw["overwrites"] = ch_ow
                await channel.edit(**kw)

            if notify_key:
                notify_map[notify_key] = str(channel.id)
            if state_key:
                state_map[state_key] = channel

    return notify_map, state_map


async def run_server_setup(guild: discord.Guild) -> str:
    """Full idempotent server scaffold. Returns a markdown summary."""
    role_map              = await _ensure_roles(guild)
    notify_map, state_map = await _ensure_channels(guild, role_map)

    # Persist channel IDs to state file.
    for key, channel in state_map.items():
        _set_guild_channel(guild.id, key, channel.id)
    _save_state(_state)

    lines = [
        f"✅ **Server setup complete for {guild.name}!**\n",
        "**Roles:** " + " · ".join(f"`{n}`" for n in role_map),
        "",
        "**Backend env vars — copy into `compose.yaml` and redeploy:**",
        "```",
        *[f"{k}={v}" for k, v in notify_map.items()],
        "```",
        "",
        "Also ensure `DISCORD_PUBLIC_KEY` is set so the Puppy Pouch reply button works.",
        "Bot is now managing this server. 🐾",
    ]
    return "\n".join(lines)


# ── Role sync ─────────────────────────────────────────────────────────────────

async def sync_member_roles(
    guild: discord.Guild,
    target: Optional[discord.Member] = None,
) -> str:
    role_map = {r.name: r for r in guild.roles if r.name in _ALL_TIER_NAMES}

    if target is not None:
        members_to_sync = [target]
        raw_level = await _get_member_level(str(target.id))
        access_map: dict[str, Optional[int]] = {str(target.id): raw_level}
    else:
        members_to_sync = [m for m in guild.members if not m.bot]
        linked = await _get_all_linked()
        access_map = {str(item["discord_id"]): item["access_level"] for item in linked}

    updated = 0
    for m in members_to_sync:
        if m.bot:
            continue
        level: Optional[int] = access_map.get(str(m.id))
        desired: set[str] = set()
        if level is not None:
            for role_name, threshold in _TIER_ROLES:
                if level >= threshold:
                    desired.add(role_name)

        current = {r.name for r in m.roles if r.name in _ALL_TIER_NAMES}
        to_add    = desired - current
        to_remove = current - desired
        if not to_add and not to_remove:
            continue

        try:
            if to_add:
                await m.add_roles(*(role_map[n] for n in to_add if n in role_map),
                                  reason="mochii.live subscriber sync")
            if to_remove:
                await m.remove_roles(*(role_map[n] for n in to_remove if n in role_map),
                                     reason="mochii.live subscriber sync")
            updated += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Role sync error for %s: %s", m, exc)

    scope = f"`{target.display_name}`" if target else f"all {len(members_to_sync)} member(s)"
    return f"✅ Role sync complete for {scope}. **{updated}** member(s) updated."


# ── Embed builders ────────────────────────────────────────────────────────────

def _tier_label(level: int) -> str:
    if level >= 3:   return "💎 Tier 2 (Premium)"
    if level == 2:   return "🌸 Subscriber (Tier 1)"
    if level == 1:   return "🐾 Follower"
    return "🔗 Linked (no active subscription)"


def _question_embed(q: dict) -> discord.Embed:
    base = _base_url()
    share = f"{base}/q/{q['id']}" if base else ""
    embed = discord.Embed(
        title="📬 Puppy Pouch",
        description=f"**Q:** {q['text']}\n\n**A:** {q['answer']}",
        color=discord.Color(_PINK),
        url=share or discord.Embed.Empty,
    )
    embed.set_footer(text=f"mochii.live · {q.get('created_at', '')[:10]}")
    return embed


def _drool_embed(item: dict) -> discord.Embed:
    platform  = item.get("platform", "unknown").capitalize()
    url       = item.get("original_url", "")
    text      = (item.get("text_content") or "")[:300]
    media_url = item.get("media_url")

    embed = discord.Embed(
        title=f"🔥 New Drool Log item — {platform}",
        description=text or None,
        color=discord.Color(_RED),
        url=url or discord.Embed.Empty,
    )
    if media_url:
        embed.set_image(url=media_url)

    drool_base = f"{_base_url()}/drool.html" if _base_url() else ""
    embed.set_footer(text=f"mochii.live · Drool Log{' · ' + drool_base if drool_base else ''}")
    return embed


def _nowplaying_embed(data: dict) -> Optional[discord.Embed]:
    if not data.get("is_playing"):
        return None
    track   = data.get("track_name", "Unknown")
    artist  = data.get("artist_name", "Unknown")
    album   = data.get("album_name", "")
    art_url = data.get("album_art_url")
    track_url = data.get("track_url", "")

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track}**\n{artist}" + (f"\n*{album}*" if album else ""),
        color=discord.Color(_GREEN),
        url=track_url or discord.Embed.Empty,
    )
    if art_url:
        embed.set_thumbnail(url=art_url)
    embed.set_footer(text="mochii.live · Spotify")
    return embed


def _product_embeds(products: list[dict]) -> list[discord.Embed]:
    embeds = []
    base = _base_url()
    for p in products[:10]:
        embed = discord.Embed(
            title=p.get("name", "Product"),
            description=(p.get("description") or "")[:200] or None,
            color=discord.Color(_PINK),
            url=f"{base}/store.html" if base else discord.Embed.Empty,
        )
        price = p.get("price")
        if price is not None:
            embed.add_field(name="Price", value=f"${price:.2f}", inline=True)
        img = p.get("image_url")
        if img:
            embed.set_thumbnail(url=img)
        embeds.append(embed)
    return embeds


# ── Bot ───────────────────────────────────────────────────────────────────────

class MochiiBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents(guilds=True, members=True)
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        guild_id_str = os.environ.get("DISCORD_GUILD_ID", "")
        if guild_id_str:
            guild_obj = discord.Object(id=int(guild_id_str))
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info("Slash commands synced to guild %s", guild_id_str)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 h)")


bot = MochiiBot()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID %s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="mochii.live 🐾")
    )
    for task in (auto_sync_roles, drool_feed_task, nowplaying_task, weekly_whimper_task):
        if not task.is_running():
            task.start()


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    logger.info("Joined guild: %s (%s)", guild.name, guild.id)
    ch = guild.system_channel or next(
        (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
    )
    if ch:
        await ch.send(
            "👋 **mochii.live bot has arrived!**\n\n"
            "Run `/setup` (administrator required) to automatically scaffold "
            "all roles, channels and permissions.\n\n"
            "After setup, members can use `/verify` to link their Fanvue account "
            "and unlock subscriber channels. 🐾"
        )


@bot.event
async def on_member_join(member: discord.Member) -> None:
    """Auto-assign tier roles for already-linked members; DM all new members."""
    level = await _get_member_level(str(member.id))
    if level is not None:
        role_map = {r.name: r for r in member.guild.roles if r.name in _ALL_TIER_NAMES}
        roles_to_add = [
            role_map[name]
            for name, threshold in _TIER_ROLES
            if level >= threshold and name in role_map
        ]
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="mochii.live auto-role on join")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not assign join roles for %s: %s", member, exc)

    base = _base_url()
    if not base:
        return
    try:
        embed = discord.Embed(
            title="Welcome to the pack! 🐾",
            description=(
                f"Hey {member.mention}! To unlock subscriber channels, link your "
                f"Fanvue account:\n\n"
                f"**1.** Go to **{base}** and log in with Fanvue\n"
                f"**2.** Open your profile and click **Link Discord**\n"
                f"**3.** Come back and use `/whoami` to confirm your tier 🌸\n\n"
                f"Or use `/verify` in the server for a quick guide."
            ),
            color=discord.Color(_PINK),
        )
        await member.send(embed=embed)
    except discord.Forbidden:
        pass  # DMs disabled


# ── Background tasks ──────────────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def auto_sync_roles() -> None:
    for guild in bot.guilds:
        try:
            result = await sync_member_roles(guild)
            logger.info("[auto-sync] %s: %s", guild.name, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[auto-sync] %s failed: %s", guild.name, exc)


@tasks.loop(minutes=5)
async def drool_feed_task() -> None:
    """Poll /api/drool and post new items to #drool-log in each guild."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": 1, "page_size": 10})
        if not resp.is_success:
            return
        items: list[dict] = resp.json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[drool-feed] Fetch failed: %s", exc)
        return

    last_id: int = _state.get("last_drool_id", 0)
    # Skip the pinned "Weekly Whimper" (always first item) to avoid duplication
    # with the weekly_whimper_task.  Guard against an empty list before indexing.
    pinned_id: int = items[0].get("id", -1) if items else -1
    new_items = sorted(
        [it for it in items if it.get("id", 0) > last_id and it.get("id") != pinned_id],
        key=lambda x: x.get("id", 0),
    )

    if not new_items:
        return

    for guild in bot.guilds:
        ch_id = _guild_channel(guild.id, "drool_channel")
        if not ch_id:
            continue
        channel = guild.get_channel(ch_id)
        if channel is None:
            continue
        for item in new_items:
            try:
                await channel.send(embed=_drool_embed(item))
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("[drool-feed] Could not post to %s: %s", channel, exc)

    max_id = max(it.get("id", 0) for it in new_items)
    _state["last_drool_id"] = max(last_id, max_id)
    _save_state(_state)


@tasks.loop(minutes=2)
async def nowplaying_task() -> None:
    """Post to #now-playing whenever the Spotify track changes."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            resp = await c.get(f"{_backend()}/api/spotify/now-playing")
        if not resp.is_success:
            return
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[nowplaying] Fetch failed: %s", exc)
        return

    if not data.get("is_playing"):
        return

    track_id = data.get("track_id") or data.get("track_name")
    if track_id == _state.get("last_nowplaying_track_id"):
        return

    embed = _nowplaying_embed(data)
    if embed is None:
        return

    for guild in bot.guilds:
        ch_id = _guild_channel(guild.id, "nowplaying_channel")
        if not ch_id:
            continue
        channel = guild.get_channel(ch_id)
        if channel is None:
            continue
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("[nowplaying] Could not post to %s: %s", channel, exc)

    _state["last_nowplaying_track_id"] = track_id
    _save_state(_state)


@tasks.loop(hours=1)
async def weekly_whimper_task() -> None:
    """Post the Weekly Whimper (most-reacted Drool Log item) every Monday."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:  # Monday = 0
        return

    today_str = now.strftime("%Y-%m-%d")
    if _state.get("last_weekly_whimper_date") == today_str:
        return

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": 1, "page_size": 1})
        if not resp.is_success:
            return
        items = resp.json().get("items", [])
        if not items:
            return
        whimper = items[0]  # First item is the Weekly Whimper (pinned)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[weekly-whimper] Fetch failed: %s", exc)
        return

    embed = _drool_embed(whimper)
    embed.title = "🏆 Weekly Whimper — Top Drool Log Item"
    embed.color = discord.Color(_GOLD)

    for guild in bot.guilds:
        # Post to announcements channel.
        ch_id = _guild_channel(guild.id, "announcements_channel")
        if not ch_id:
            continue
        channel = guild.get_channel(ch_id)
        if channel is None:
            continue
        try:
            await channel.send(content="@here 🏆 This week's **Weekly Whimper**!", embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("[weekly-whimper] Could not post to %s: %s", channel, exc)

    _state["last_weekly_whimper_date"] = today_str
    _save_state(_state)


# ── Spotify queue view ────────────────────────────────────────────────────────

class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: list[dict], discord_id: str) -> None:
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(
                label=f"{t['name'][:80]}",
                description=f"{t['artist'][:50]} · {t['duration'] // 60}:{t['duration'] % 60:02d}",
                value=t["uri"],
            )
            for t in tracks[:5]
        ]
        select = discord.ui.Select(placeholder="Pick a track to queue…", options=options)
        select.callback = self._on_select
        self._discord_id = discord_id
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        track_uri = interaction.data["values"][0]
        result = await _api_post("/api/discord/bot/spotify/queue",
                                 {"discord_id": self._discord_id, "track_uri": track_uri})
        if result and result.get("success"):
            await interaction.response.edit_message(content="🎵 Track added to queue!", view=None)
        else:
            msg = (result or {}).get("message", "Could not add track.")
            await interaction.response.edit_message(content=f"❌ {msg}", view=None)


# ── Slash commands ────────────────────────────────────────────────────────────

# ·· Admin ·····················································

@bot.tree.command(name="setup", description="Scaffold all roles, categories and channels for mochii.live.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        summary = await run_server_setup(interaction.guild)
    except Exception as exc:  # noqa: BLE001
        logger.error("Setup error: %s", exc)
        await interaction.followup.send(f"❌ Setup failed: {exc}", ephemeral=True)
        return
    await interaction.followup.send(summary, ephemeral=True)


@bot.tree.command(name="sync-roles", description="Sync Fanvue subscriber roles for all members or one.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(member="Specific member to sync. Leave blank to sync everyone.")
async def cmd_sync_roles(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        result = await sync_member_roles(interaction.guild, member)
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"❌ Sync failed: {exc}", ephemeral=True)
        return
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="announce", description="Post an announcement to #announcements.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message="The announcement text.")
async def cmd_announce(interaction: discord.Interaction, message: str) -> None:
    ch_id = _guild_channel(interaction.guild.id, "announcements_channel")
    if not ch_id:
        await interaction.response.send_message(
            "❌ No `#announcements` channel found. Run `/setup` first.", ephemeral=True
        )
        return
    channel = interaction.guild.get_channel(ch_id)
    if channel is None:
        await interaction.response.send_message("❌ `#announcements` channel not found in this server.", ephemeral=True)
        return
    embed = discord.Embed(description=message, color=discord.Color(_PINK))
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="mochii.live")
    try:
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Announcement posted!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to post in `#announcements`.", ephemeral=True)


# ·· Community ················································

@bot.tree.command(name="server-info", description="Show server stats and subscriber counts.")
async def cmd_server_info(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    stats = await _api_get("/api/discord/bot/stats")
    guild = interaction.guild

    embed = discord.Embed(title=f"📊 {guild.name}", color=discord.Color(_PINK))
    embed.set_thumbnail(url=guild.icon.url if guild.icon else discord.Embed.Empty)

    if stats:
        embed.add_field(name="Fanvue Users",    value=str(stats.get("user_count",    "—")), inline=True)
        embed.add_field(name="Linked Accounts", value=str(stats.get("linked_count",  "—")), inline=True)
        embed.add_field(name="\u200b",          value="\u200b",                              inline=True)
        embed.add_field(name="💎 Tier 2",       value=str(stats.get("tier2_count",   "—")), inline=True)
        embed.add_field(name="🌸 Subscribers",  value=str(stats.get("sub_count",     "—")), inline=True)
        embed.add_field(name="🐾 Followers",    value=str(stats.get("follower_count","—")), inline=True)
        embed.add_field(name="📬 Q&A Answered", value=str(stats.get("answered_questions", "—")), inline=True)
        embed.add_field(name="🔥 Drool Items",  value=str(stats.get("drool_count",   "—")), inline=True)
        embed.add_field(name="🛒 Orders",       value=str(stats.get("order_count",   "—")), inline=True)
    else:
        embed.description = "*Stats unavailable right now.*"

    embed.add_field(name="Discord Members", value=str(guild.member_count), inline=True)
    embed.set_footer(text="mochii.live · Alpha Kennel")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="verify", description="Get a guide to link your Fanvue account.")
async def cmd_verify(interaction: discord.Interaction) -> None:
    base = _base_url()
    if not base:
        await interaction.response.send_message(
            "❌ Site URL not configured yet — ask an admin.", ephemeral=True
        )
        return
    embed = discord.Embed(
        title="🔗 Link Your Fanvue Account",
        description=(
            f"**Step 1** — Go to **{base}** and log in with your Fanvue account.\n\n"
            "**Step 2** — Click your profile → **Link Discord**.\n\n"
            "**Step 3** — Authorise the connection and return here.\n\n"
            "**Step 4** — Use `/whoami` to confirm your tier! 🌸\n\n"
            f"Once linked, your Discord roles will update automatically to reflect "
            f"your Fanvue subscription."
        ),
        color=discord.Color(_PINK),
    )
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("📬 Sent you a DM with the instructions!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="whoami", description="Show your Fanvue tier and link status.")
async def cmd_whoami(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    data = await _api_get("/api/discord/bot/member", discord_id=str(interaction.user.id))
    if data is None or not data.get("is_linked"):
        await interaction.followup.send(
            "🔗 Your Discord account isn't linked to Fanvue yet. Use `/verify` to get started!",
            ephemeral=True,
        )
        return
    level = data["access_level"]
    embed = discord.Embed(
        title="👤 Your Account",
        color=discord.Color(_PINK),
    )
    embed.add_field(name="Discord",      value=interaction.user.mention, inline=True)
    embed.add_field(name="Fanvue Tier",  value=_tier_label(level),       inline=True)
    embed.set_footer(text="mochii.live · Alpha Kennel")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ·· Puppy Pouch ··············································

@bot.tree.command(name="ask", description="Submit an anonymous note to the Puppy Pouch. 🐾")
@app_commands.describe(question="Your anonymous question or note (max 280 characters).")
async def cmd_ask(interaction: discord.Interaction, question: str) -> None:
    if len(question) > 280:
        await interaction.response.send_message(
            "❌ Notes must be 280 characters or fewer.", ephemeral=True
        )
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                f"{_backend()}/api/questions",
                json={"text": question},
            )
        if resp.status_code in (200, 201):
            await interaction.response.send_message(
                "📬 Your note has been dropped in the Puppy Pouch! 🐾", ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Could not submit note right now.", ephemeral=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ask command error: %s", exc)
        await interaction.response.send_message("❌ Could not submit note right now.", ephemeral=True)


@bot.tree.command(name="pouch", description="Browse recent answered Puppy Pouch notes. 🐾")
@app_commands.describe(page="Page number (default: 1).")
async def cmd_pouch(interaction: discord.Interaction, page: int = 1) -> None:
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/questions/public")
        questions = resp.json() if resp.is_success else []
    except Exception:
        questions = []

    per_page = 3
    start    = (page - 1) * per_page
    page_qs  = questions[start: start + per_page]

    if not page_qs:
        await interaction.followup.send("📭 No answered notes found on that page.", ephemeral=True)
        return

    total_pages = max(1, (len(questions) + per_page - 1) // per_page)
    embeds = [_question_embed(q) for q in page_qs]
    embeds[0].set_author(name=f"Puppy Pouch — Page {page}/{total_pages}")
    await interaction.followup.send(embeds=embeds[:10])


# ·· Drool Log ················································

def _has_follower_role(member: discord.Member) -> bool:
    return any(r.name in _ALL_TIER_NAMES for r in member.roles)


@bot.tree.command(name="drool", description="Browse the Drool Log. 🔥 (Follower+)")
@app_commands.describe(page="Page number (default: 1).")
async def cmd_drool(interaction: discord.Interaction, page: int = 1) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(
            "🐾 The Drool Log is for Fanvue followers only. Use `/verify` to link your account!",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": page, "page_size": 3})
        data = resp.json() if resp.is_success else {}
    except Exception:
        data = {}

    items = data.get("items", [])
    if not items:
        await interaction.followup.send("🔥 Nothing on that page yet.", ephemeral=True)
        return

    total = data.get("total", len(items))
    per   = data.get("page_size", 3)
    total_pages = max(1, (total + per - 1) // per)
    embeds = [_drool_embed(it) for it in items[:3]]
    embeds[0].set_author(name=f"Drool Log — Page {page}/{total_pages}")
    await interaction.followup.send(embeds=embeds)


@bot.tree.command(name="weekly-whimper", description="Show the most-reacted Drool Log item this week. 🏆")
async def cmd_weekly_whimper(interaction: discord.Interaction) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(
            "🐾 Use `/verify` to link your Fanvue account and unlock the Drool Log!",
            ephemeral=True,
        )
        return
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": 1, "page_size": 1})
        items = resp.json().get("items", []) if resp.is_success else []
    except Exception:
        items = []

    if not items:
        await interaction.followup.send("🏆 No items found.", ephemeral=True)
        return

    embed = _drool_embed(items[0])
    embed.title = "🏆 This Week's Weekly Whimper"
    embed.color = discord.Color(_GOLD)
    await interaction.followup.send(embed=embed)


# ·· Spotify ··················································

@bot.tree.command(name="nowplaying", description="Show the currently playing Spotify track. 🎵")
async def cmd_nowplaying(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            resp = await c.get(f"{_backend()}/api/spotify/now-playing")
        data = resp.json() if resp.is_success else {}
    except Exception:
        data = {}

    if not data.get("is_playing"):
        if not data.get("configured"):
            await interaction.followup.send("🎵 Spotify is not connected yet.", ephemeral=True)
        else:
            await interaction.followup.send("🎵 Nothing is playing right now.", ephemeral=True)
        return

    embed = _nowplaying_embed(data)
    if embed:
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("🎵 Nothing is playing right now.", ephemeral=True)


@bot.tree.command(name="queue", description="Search Spotify and add a track to the queue. 🎵 (Subscriber+)")
@app_commands.describe(query="Track name or artist to search for.")
async def cmd_queue(interaction: discord.Interaction, query: str) -> None:
    # Check subscriber role
    member_roles = {r.name for r in interaction.user.roles}
    if not (member_roles & {"🌸 Subscriber", "💎 Tier 2", "⭐ Admin"}):
        await interaction.response.send_message(
            "🌸 Spotify queueing is a subscriber feature. Link your Fanvue account with `/verify`!",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    result = await _api_get(
        "/api/discord/bot/spotify/search",
        q=query,
        discord_id=str(interaction.user.id),
    )
    tracks = (result or {}).get("tracks", [])
    if not tracks:
        err = (result or {}).get("error", "No results found.")
        await interaction.followup.send(f"🎵 {err}", ephemeral=True)
        return

    view  = TrackSelectView(tracks, str(interaction.user.id))
    lines = [f"**{t['name']}** — {t['artist']}" for t in tracks[:5]]
    await interaction.followup.send(
        "🎵 **Search results — pick a track:**\n" + "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines)),
        view=view,
        ephemeral=True,
    )


# ·· Store ·····················································

@bot.tree.command(name="store", description="Browse the mochii.live store. 🛒")
async def cmd_store(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/store/products")
        products = resp.json() if resp.is_success else []
    except Exception:
        products = []

    if not products:
        base = _base_url()
        await interaction.followup.send(
            f"🛒 The store is empty right now.{' Visit ' + base + '/store.html' if base else ''}"
        )
        return

    embeds = _product_embeds(products)
    base   = _base_url()
    header = discord.Embed(
        title="🛒 mochii.live Store",
        description=f"Check it out at **{base}/store.html**" if base else None,
        color=discord.Color(_PINK),
    )
    await interaction.followup.send(embeds=[header, *embeds[:9]])


# ·· IoT ·······················································

_DEVICE_CHOICES = [
    app_commands.Choice(name="PiShock  ⚡", value="pishock"),
    app_commands.Choice(name="Lovense  💜", value="lovense"),
]


@bot.tree.command(name="zap", description="Activate a device. 🐾 (Follower+ required)")
@app_commands.describe(device="Which device to activate (default: pishock).")
@app_commands.choices(device=_DEVICE_CHOICES)
async def cmd_zap(interaction: discord.Interaction, device: app_commands.Choice[str] = None) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(
            "🐾 You need to be at least a Fanvue follower to use this. Use `/verify` to link!",
            ephemeral=True,
        )
        return

    device_value = device.value if device else "pishock"
    await interaction.response.defer(ephemeral=True)

    result = await _api_post(
        f"/api/discord/bot/control/{device_value}",
        {"discord_id": str(interaction.user.id)},
    )
    if result is None:
        await interaction.followup.send("❌ Could not reach the backend. Try again later.", ephemeral=True)
        return

    msg = result.get("message", "Done.")
    if result.get("success"):
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)


# ── Tree-level error handler ──────────────────────────────────────────────────

@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You don't have permission to use this command."
    else:
        logger.error("Slash command error: %s", error)
        msg = "❌ An unexpected error occurred."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:  # noqa: BLE001
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
