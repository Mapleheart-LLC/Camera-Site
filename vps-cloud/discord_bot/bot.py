"""
Discord management bot for mochii.live

Fully manages the Discord server and deeply integrates with every site feature.
The bot has a playful, flirty, adult-oriented personality to match the platform.

Server management
-----------------
  /setup          Scaffold all roles, categories and channels (idempotent)
  /sync-roles     Sync Fanvue subscriber roles for all members or a specific one
  /announce       Post a message to #announcements as a branded embed
  /rename-server  Rename the Discord server (AI picks a name if none given)
  /ai-announce    AI writes and posts an announcement given a topic

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

Personality / fun
-----------------
  /rate           Get a cheeky personal rating from the bot
  /treat          Claim a random treat (Follower+)
  /collar         Show your tier as a themed collar embed
  /beg            Beg the bot — outcomes depend on your tier 🐾
  /peek           Surprise random Drool Log item (Follower+)
  /ai-vibe        AI reads the current context and describes the kennel vibe

Links page
----------
  /links          Browse site links
  /add-link       Add a link (AI suggests emoji if omitted)
  /remove-link    Remove a link (select menu)
  /feature-link   Promote a link to the top of the page

Community
---------
  /server-info    Aggregated stats embed
  /verify         DM yourself an account-linking guide
  /whoami         Show your own Fanvue tier and Discord link status

Background automation
---------------------
  • Status rotation every 15 minutes (cycling cheeky statuses)
  • Role sync every 30 minutes
  • Posts new Drool Log items to #drool-log (polls every 5 minutes)
  • Updates #now-playing when the Spotify track changes (polls every 2 minutes)
  • Weekly Whimper highlight every Monday (checks hourly)
  • Daily Treat posted to #treat-jar at noon UTC (checks hourly)
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
OPENAI_API_KEY      Enables AI features (/rename-server auto-name, /add-link emoji
                    suggestion, /ai-vibe, /ai-announce). Gracefully disabled if unset.
"""

import asyncio
import json
import logging
import os
import random
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

# ── Flavor text pools ─────────────────────────────────────────────────────────

_FLAVOR_TEXT: dict[str, list[str]] = {
    # Bot status rotation — cycling every 15 min
    "status": [
        "the Drool Log 👀",
        "good puppies misbehave 🐾",
        "for new notes in the Pouch 💌",
        "subscribers drool 🤤",
        "the pack closely 🔍",
        "your collar status 💎",
        "mochii.live 🌸",
        "you 😏",
        "premium pets sleep soundly 💤",
        "who needs a treat 🍖",
    ],
    # Drool log embed prefixes
    "drool_intro": [
        "🤤 oh look what washed up on the shore",
        "👀 mochii has been drooling over this",
        "🔥 fresh shame for the gallery",
        "😈 this one's going in the Drool Log",
        "💦 a new addition to the collection",
        "🐾 the pack has been talking about this",
        "🌸 someone got caught being delicious",
        "🤭 oh my… straight to the Drool Log",
    ],
    # Drool log footers
    "drool_footer": [
        "mochii.live · come drool with us",
        "mochii.live · the shame gallery awaits",
        "mochii.live · sniff sniff 🐾",
        "mochii.live · good toys get archived",
        "mochii.live · drool responsibly",
    ],
    # Q&A embed footers
    "pouch_footer": [
        "mochii.live · secrets and confessions 💌",
        "mochii.live · whisper something naughty",
        "mochii.live · the Puppy Pouch holds all your secrets 🐾",
        "mochii.live · drop a note, get a reply 🌸",
        "mochii.live · anonymous & delicious",
    ],
    # Ask command confirms
    "ask_confirm": [
        "📬 shhh… your secret's in the Pouch. 🐾",
        "📬 sneaky little note delivered. mochii will see it when she's ready 🌸",
        "📬 your confession has been received. behave. 😏",
        "📬 into the Puppy Pouch it goes… try not to squirm while you wait 🐾",
        "📬 noted! now sit and wait like a good pup 🐾",
    ],
    # Spotify now-playing flavour
    "nowplaying_mood": [
        "mochii is vibing to this rn 🎶",
        "this is what plays in the kennel 🎵",
        "currently setting the mood 😏",
        "dancing in her collar to this 🌸",
        "the pack has taste 🎵",
        "close your eyes and listen 💜",
    ],
    # Welcome DM extras
    "welcome_suffix": [
        "collar optional, curiosity mandatory 🐾",
        "sit. stay. enjoy. 🌸",
        "no biting… unless you're a 💎 Tier 2 🐾",
        "the kennel is warm. make yourself comfortable. 😏",
        "good pups get treats here 🍖",
    ],
    # Zap public taunts (posted to the pack when someone fires a device)
    "zap_public": [
        "{user} just made mochii feel something 😳⚡",
        "someone ({user}) pushed the button… 🐾⚡",
        "{user} sent a little *something* mochii's way 💜",
        "uh oh — {user} has been *very* naughty ⚡😈",
        "{user} pressed the big red button. good puppy? 🐾⚡",
        "mochii felt that one — thanks, {user} 🌸⚡",
    ],
    # Rate command descriptors (paired with a score)
    "rate_descriptor": [
        ("a Very Good Pup™", "🐾"),
        ("absolutely feral in the best way", "🔥"),
        ("a menace and a delight", "😈"),
        ("suspiciously well-behaved", "👀"),
        ("clearly here for the Drool Log", "🤤"),
        ("premium material, honestly", "💎"),
        ("a little messy, very loveable", "🌸"),
        ("boldly unhinged", "😏"),
        ("an absolute treat", "🍖"),
        ("the pack's favourite troublemaker", "🐾"),
        ("dangerously adorable", "💕"),
        ("in need of a collar fitting", "💜"),
        ("certified good girl/boy/enby", "✅"),
        ("drool-worthy, according to the log", "💦"),
        ("a premium-tier disaster", "💎"),
    ],
    # Treat command messages
    "treat": [
        "🍖 here's your treat: you exist and that's already devastating to everyone around you. good.",
        "🌸 treat time: mochii thinks you're doing great and anyone who disagrees gets zapped.",
        "💜 your treat is the knowledge that the Drool Log has at least one entry for you.",
        "🐾 treat dispensed: you have permission to be a little feral today.",
        "🍖 mochii says: you're a very good pup and you deserve belly rubs and chaos in equal measure.",
        "✨ your treat: a personalised compliment — you have excellent taste in Discord servers.",
        "🔥 treat unlocked: the pack has voted and you're today's MVP (Most Valued Pup).",
        "😏 your treat is the unshakeable confidence that comes from being *this* degenerate.",
        "💕 treat delivered: you're soft, loveable, and absolutely feral. perfect.",
        "🌸 mochii says sit and stay — your treat is coming. just kidding. the treat *is* you.",
    ],
    # Beg outcomes — success
    "beg_success": [
        "🐾 *sigh*… fine. you begged so well mochii can't resist. here's a virtual treat 🍖",
        "🌸 okay okay, the puppy eyes worked. just this once. don't tell the others.",
        "😏 mochii is very weak for a good beg. treat incoming. you're welcome.",
        "💕 the audacity of your begging has been rewarded. don't get used to it.",
        "🍖 good pup! that was a very convincing beg. mochii is proud of you.",
    ],
    # Beg outcomes — failure
    "beg_fail": [
        "🐾 nope. try again when you've learned to sit properly.",
        "😤 the beg was sub-par. work on your puppy eyes and come back.",
        "😂 lmao nice try. mochii says no.",
        "🚫 beg denied. have you tried being *more* pathetic? might work.",
        "👀 mochii watched you beg and is not impressed. 0/10 form. back to training.",
    ],
    # Beg outcomes — premium bonus
    "beg_premium": [
        "💎 a Tier 2 begging?? oh the audacity. fine — you win everything. here's a gold star ⭐",
        "💎 premium pup gets premium treatment. your beg was honestly gorgeous. have everything.",
        "💎 mochii can't say no to her best tier. spoiled. absolutely spoiled. 🌸",
    ],
    # Peek tease lines (shown above the drool embed)
    "peek_tease": [
        "👀 *psst* — don't tell anyone i showed you this…",
        "🤭 mochii left the Drool Log open. take a peek 🔍",
        "😈 i found something in the archives for you…",
        "💦 a little something from the collection:",
        "🌸 here's a random drool for your viewing pleasure~",
        "🐾 sniffed this one out just for you:",
    ],
    # No-subscription gate messages
    "gate_follower": [
        "🐾 psst — this is follower territory. use `/verify` to get your collar and come back! 🌸",
        "🔒 you need to be a Fanvue follower to enter. `/verify` is your leash in 🐾",
        "👀 follower-only zone! link your account with `/verify` and join the pack.",
    ],
    "gate_subscriber": [
        "🌸 ooh, subscriber-only! link your Fanvue account with `/verify` to unlock 🐾",
        "💎 this one's for subscribers. `/verify` then subscribe on Fanvue to get in~",
        "🔒 premium feature ahead — link your Fanvue account with `/verify` first 🌸",
    ],
}


def _pick(key: str) -> str:
    """Return a random string from a flavor-text pool."""
    return random.choice(_FLAVOR_TEXT.get(key, ["—"]))


# ── Status rotation ───────────────────────────────────────────────────────────

_STATUS_IDX: int = 0

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
            {"name": "rules",         "topic": "Server rules — read them or face the consequences 🐾", "readonly": True},
            {"name": "announcements", "topic": "Official announcements from mochii.live. 🌸", "readonly": True, "state_key": "announcements_channel"},
            {"name": "roles-info",    "topic": "How to verify your Fanvue account and earn your collar. 🐾", "readonly": True},
        ],
    },
    {
        "category": "💬 COMMUNITY",
        "min_role": None,   # open to everyone in the server
        "channels": [
            {"name": "general",       "topic": "Sit. Stay. Chat. 🐾 General conversation for the whole pack."},
            {"name": "introductions", "topic": "New pup? Introduce yourself and tell us how you found the kennel 🌸"},
            {"name": "off-topic",     "topic": "Go feral here. Anything goes (within the rules) 😈"},
        ],
    },
    {
        "category": "🐾 FOLLOWER ZONE",
        "min_role": "🐾 Follower",
        "channels": [
            {"name": "pack-chat",     "topic": "Follower-exclusive pack chat — bark, bite, and banter 🐾"},
            {"name": "beg-box",       "topic": "Drop your requests and beg nicely. mochii reads everything 🌸"},
            {"name": "puppy-pouch",   "topic": "Answered Puppy Pouch notes — your confessions, answered 💌", "readonly": True, "state_key": "puppy_pouch_channel"},
            {"name": "drool-log",     "topic": "New Drool Log items — mochii's personal shame gallery 🤤", "readonly": True, "state_key": "drool_channel"},
        ],
    },
    {
        "category": "🌸 SUBSCRIBER LOUNGE",
        "min_role": "🌸 Subscriber",
        "channels": [
            {"name": "subscriber-chat",  "topic": "Subscriber lounge — pampered pets only 🌸 come get cosy"},
            {"name": "treat-jar",        "topic": "Daily treats from mochii 🍖 one per day, don't be greedy", "readonly": True, "state_key": "treat_channel"},
            {"name": "mochii-updates",   "topic": "Content drops, subscriber-exclusive news, and chaos 🌸", "readonly": True, "state_key": "updates_channel"},
            {"name": "now-playing",      "topic": "Currently playing — updated automatically 🎵 queue something nice", "readonly": True, "state_key": "nowplaying_channel"},
        ],
    },
    {
        "category": "💎 PREMIUM DEN",
        "min_role": "💎 Tier 2",
        "channels": [
            {"name": "premium-chat",   "topic": "The den — for the prized toys and favourite pets 💎 you know who you are"},
            {"name": "camera-lounge",  "topic": "Stream and camera status alerts 📷 premium eyes only", "readonly": True, "state_key": "camera_channel"},
        ],
    },
    {
        "category": "🔒 STAFF",
        "min_role": "🛡️ Moderator",
        "channels": [
            {"name": "staff-chat",  "topic": "Staff discussion — keep the kennel running smoothly."},
            {"name": "mod-log",     "topic": "Moderation action log — who's been naughty today."},
            {"name": "bot-logs",    "topic": "Bot event log — the machine that keeps the pack in line."},
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
    "last_daily_treat_date":    None,
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


async def _api_put(path: str, json_body: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.put(f"{_backend()}{path}", json=json_body, headers=_bot_headers())
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PUT %s failed: %s", path, exc)
    return None


async def _api_delete(path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.delete(f"{_backend()}{path}", headers=_bot_headers())
        return resp.status_code in (200, 204)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DELETE %s failed: %s", path, exc)
    return False


# ── AI helper ─────────────────────────────────────────────────────────────────

_AI_SYSTEM = (
    "You are the mochii.live Discord bot — cheeky, flirty, adult-playful. "
    "mochii.live is an adult content creator platform with a puppy-play aesthetic "
    "(collar metaphors, 'the pack', 'kennel', 'Good Girl/Bad Puppy' vibes). "
    "Keep responses SHORT (1–3 sentences max), punchy and on-brand. "
    "No markdown headers. Minimal but well-chosen emojis."
)


async def _ai(prompt: str, max_tokens: int = 150) -> Optional[str]:
    """Call OpenAI gpt-4o-mini. Returns None if OPENAI_API_KEY is not set or the call fails."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": _AI_SYSTEM},
                        {"role": "user",   "content": prompt},
                    ],
                },
            )
        if resp.is_success:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logger.warning("AI API error %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI call failed: %s", exc)
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
        f"🐾 **the kennel is ready, {guild.name}!**\n",
        "**Roles created:** " + " · ".join(f"`{n}`" for n in role_map),
        "",
        "**Backend env vars — paste into `compose.yaml` and redeploy:**",
        "```",
        *[f"{k}={v}" for k, v in notify_map.items()],
        "```",
        "",
        "Also ensure `DISCORD_PUBLIC_KEY` is set so the Puppy Pouch reply button works.",
        "everything is set up. sit. stay. enjoy. 🌸",
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
    if level >= 3:   return "💎 Tier 2 — prized pet of the kennel"
    if level == 2:   return "🌸 Subscriber — pampered and collared"
    if level == 1:   return "🐾 Follower — a good pup with potential"
    return "🔗 Linked (not subscribed yet — collar pending)"


def _question_embed(q: dict) -> discord.Embed:
    base = _base_url()
    share = f"{base}/q/{q['id']}" if base else ""
    embed = discord.Embed(
        title="📬 Puppy Pouch",
        description=f"**Q:** {q['text']}\n\n**A:** {q['answer']}",
        color=discord.Color(_PINK),
        url=share or discord.Embed.Empty,
    )
    embed.set_footer(text=_pick("pouch_footer") + f"  ·  {q.get('created_at', '')[:10]}")
    return embed


def _drool_embed(item: dict) -> discord.Embed:
    platform  = item.get("platform", "unknown").capitalize()
    url       = item.get("original_url", "")
    text      = (item.get("text_content") or "")[:300]
    media_url = item.get("media_url")

    embed = discord.Embed(
        title=f"{_pick('drool_intro')} — {platform}",
        description=text or None,
        color=discord.Color(_RED),
        url=url or discord.Embed.Empty,
    )
    if media_url:
        embed.set_image(url=media_url)

    embed.set_footer(text=_pick("drool_footer"))
    return embed


def _nowplaying_embed(data: dict) -> Optional[discord.Embed]:
    if not data.get("is_playing"):
        return None
    track   = data.get("track_name", "Unknown")
    artist  = data.get("artist_name", "Unknown")
    album   = data.get("album_name", "")
    art_url = data.get("album_art_url")
    track_url = data.get("track_url", "")

    mood = _pick("nowplaying_mood")
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track}**\n{artist}" + (f"\n*{album}*" if album else "") + f"\n\n*{mood}*",
        color=discord.Color(_GREEN),
        url=track_url or discord.Embed.Empty,
    )
    if art_url:
        embed.set_thumbnail(url=art_url)
    embed.set_footer(text="mochii.live · Spotify — queue something good 🎶")
    return embed


def _product_embeds(products: list[dict]) -> list[discord.Embed]:
    embeds = []
    base = _base_url()
    captions = [
        "treat yourself — you've been so good 🌸",
        "mochii picked these out 💕",
        "premium pup approved 💎",
        "part of a balanced diet of chaos 🐾",
    ]
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
        embed.set_footer(text=random.choice(captions))
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
        activity=discord.Activity(type=discord.ActivityType.watching, name=_pick("status"))
    )
    for task in (status_rotation_task, auto_sync_roles, drool_feed_task,
                 nowplaying_task, weekly_whimper_task, daily_treat_task):
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
            "🐾 **the bot has arrived and she's not leaving.**\n\n"
            "Run `/setup` (administrator required) to automatically scaffold "
            "all roles, channels, categories and permissions.\n\n"
            "After setup, members can use `/verify` to link their Fanvue account "
            "and earn their collar. 🌸\n\n"
            "*sit. stay. behave.*"
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
        suffix = _pick("welcome_suffix")
        embed = discord.Embed(
            title="welcome to the kennel, pup 🐾",
            description=(
                f"hey {member.mention} — glad you found us 🌸\n\n"
                f"**want access to the good stuff?** link your Fanvue account:\n\n"
                f"**1.** Go to **{base}** and log in with Fanvue\n"
                f"**2.** Open your profile → click **Link Discord**\n"
                f"**3.** Come back and use `/whoami` to confirm your collar 🌸\n"
                f"**4.** Or just use `/verify` and i'll walk you through it.\n\n"
                f"*{suffix}*"
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


@tasks.loop(minutes=15)
async def status_rotation_task() -> None:
    """Cycle through cheeky bot statuses every 15 minutes."""
    global _STATUS_IDX
    statuses = _FLAVOR_TEXT["status"]
    text = statuses[_STATUS_IDX % len(statuses)]
    _STATUS_IDX += 1
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=text)
    )


@tasks.loop(hours=1)
async def daily_treat_task() -> None:
    """Post a daily treat to #treat-jar at noon UTC."""
    now = datetime.now(timezone.utc)
    if now.hour != 12:
        return

    today_str = now.strftime("%Y-%m-%d")
    if _state.get("last_daily_treat_date") == today_str:
        return

    treat_text = _pick("treat")

    for guild in bot.guilds:
        ch_id = _guild_channel(guild.id, "treat_channel")
        if not ch_id:
            continue
        channel = guild.get_channel(ch_id)
        if channel is None:
            continue
        embed = discord.Embed(
            title="🍖 Daily Treat",
            description=treat_text,
            color=discord.Color(_PINK),
        )
        embed.set_footer(text="mochii.live · one treat per day, don't be greedy 🌸")
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("[daily-treat] Could not post to %s: %s", channel, exc)

    _state["last_daily_treat_date"] = today_str
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
    embed.title = "🏆 Weekly Whimper — the pack's favourite drool this week"
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
            await channel.send(
                content="@here 🏆 **Weekly Whimper** — the most drooled-over item this week is…",
                embed=embed,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("[weekly-whimper] Could not post to %s: %s", channel, exc)

    _state["last_weekly_whimper_date"] = today_str
    _save_state(_state)


# ── Link select view ─────────────────────────────────────────────────────────

class LinkSelectView(discord.ui.View):
    """Reusable select menu for /remove-link and /feature-link."""

    def __init__(self, links: list[dict], action: str) -> None:
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(
                label=f"{link.get('emoji') or ''} {link['title']}"[:100].strip(),
                description=link["url"][:100],
                value=str(link["id"]),
            )
            for link in links[:25]
        ]
        self._action = action
        select = discord.ui.Select(placeholder=f"pick a link to {action}…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        link_id = int(interaction.data["values"][0])
        if self._action == "delete":
            ok = await _api_delete(f"/api/discord/bot/links/{link_id}")
            if ok:
                await interaction.response.edit_message(
                    content="✅ link removed from the site 🗑️", view=None
                )
            else:
                await interaction.response.edit_message(
                    content="❌ couldn't remove the link — try again", view=None
                )
        elif self._action == "feature":
            result = await _api_put(f"/api/discord/bot/links/{link_id}", {"sort_order": 0})
            if result:
                title = result.get("title", "link")
                await interaction.response.edit_message(
                    content=f"✅ **{title}** is now featured at the top of the links page 🌸", view=None
                )
            else:
                await interaction.response.edit_message(
                    content="❌ couldn't reorder the link — try again", view=None
                )


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
            await interaction.response.edit_message(
                content=random.choice([
                    "🎵 track added — good taste pup 🌸",
                    "🎵 queued! mochii will feel that one 😏",
                    "🎶 added to the queue~ you know what she likes 💕",
                    "🎵 that's going in the mix — nice pick 🐾",
                ]),
                view=None,
            )
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
    embed.set_footer(text="mochii.live 🌸 — sit down and pay attention")
    try:
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Announcement posted!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to post in `#announcements`.", ephemeral=True)


@bot.tree.command(name="rename-server", description="Rename the Discord server. Leave blank and AI picks a name. 🐾")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(name="New server name. Leave blank to let AI generate one.")
async def cmd_rename_server(interaction: discord.Interaction, name: str = None) -> None:
    await interaction.response.defer(ephemeral=True)

    if not name:
        stats = await _api_get("/api/discord/bot/stats")
        members = stats.get("user_count",    0) if stats else 0
        subs    = stats.get("sub_count",     0) if stats else 0
        tier2   = stats.get("tier2_count",   0) if stats else 0
        zaps    = stats.get("activation_count", 0) if stats else 0
        suggested = await _ai(
            f"Suggest a creative, adult-playful Discord server name for mochii.live. "
            f"Context: {members} Fanvue members, {subs} subscribers, {tier2} Tier 2 premiums, "
            f"{zaps} device activations. "
            f"Keep it to 3-5 words. Kennel/collar/puppy-play aesthetic. "
            f"Examples: 'mochii's naughty kennel', 'the drool log lounge', 'alpha kennel premium'. "
            f"Return ONLY the name — no quotes, no explanation.",
            max_tokens=20,
        )
        if not suggested:
            await interaction.followup.send(
                "❌ AI is unavailable (no `OPENAI_API_KEY` set). Provide a name directly: `/rename-server name:...`",
                ephemeral=True,
            )
            return
        name = suggested.strip('"\'').strip()

    old_name = interaction.guild.name
    try:
        await interaction.guild.edit(name=name, reason=f"Renamed by {interaction.user} via bot")
        await interaction.followup.send(
            f"✅ kennel renamed: **{old_name}** → **{name}** 🐾", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ missing `Manage Server` permission to rename the server.", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"❌ rename failed: {exc}", ephemeral=True)


@bot.tree.command(name="ai-announce", description="Let AI write an announcement and post it. 🌸 (Admin)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(topic="What to announce — AI will write the copy.")
async def cmd_ai_announce(interaction: discord.Interaction, topic: str) -> None:
    await interaction.response.defer(ephemeral=True)

    text = await _ai(
        f"Write a short Discord server announcement for mochii.live about: {topic}. "
        f"2–4 sentences. Flirty and on-brand. End with an emoji.",
        max_tokens=200,
    )
    if not text:
        await interaction.followup.send(
            "❌ AI unavailable — write it yourself with `/announce` 🐾", ephemeral=True
        )
        return

    ch_id = _guild_channel(interaction.guild.id, "announcements_channel")
    if not ch_id:
        await interaction.followup.send(
            f"📝 AI draft (run `/setup` to create `#announcements`):\n\n{text}", ephemeral=True
        )
        return

    channel = interaction.guild.get_channel(ch_id)
    if not channel:
        await interaction.followup.send(f"📝 AI draft:\n\n{text}", ephemeral=True)
        return

    embed = discord.Embed(description=text, color=discord.Color(_PINK))
    embed.set_footer(text="mochii.live 🌸 · crafted by AI, approved by the kennel")
    try:
        await channel.send(embed=embed)
        await interaction.followup.send(f"✅ posted!\n\n*\"{text}\"*", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ missing permission\n\nDraft:\n{text}", ephemeral=True)


@bot.tree.command(name="links", description="Browse the mochii.live links page. 🔗")
async def cmd_links(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    links = await _api_get("/api/discord/bot/links")
    base  = _base_url()

    if not links:
        await interaction.followup.send(
            f"🔗 no links set up yet{' — visit ' + base + '/links' if base else ''}.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🔗 mochii.live — Links",
        description=f"{base}/links" if base else None,
        color=discord.Color(_PINK),
    )
    for link in links[:25]:
        if not link.get("is_active"):
            continue
        emoji = link.get("emoji") or "🔗"
        embed.add_field(
            name=f"{emoji} {link['title']}",
            value=link["url"],
            inline=False,
        )
    embed.set_footer(text="mochii.live · link collection 🐾")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="add-link", description="Add a link to the site's links page. 🔗 (Admin)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    url="The URL to add.",
    title="Display title for the link.",
    emoji="Emoji prefix (AI picks one if omitted).",
)
async def cmd_add_link(
    interaction: discord.Interaction,
    url: str,
    title: str,
    emoji: str = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not emoji:
        suggested_emoji = await _ai(
            f"Pick exactly ONE emoji that best represents this link: title='{title}', url='{url}'. "
            f"Return ONLY the single emoji character — nothing else.",
            max_tokens=5,
        )
        emoji = (suggested_emoji or "🔗").strip()[:4]

    result = await _api_post("/api/discord/bot/links", {
        "title": title, "url": url, "emoji": emoji, "sort_order": 0,
    })
    if result:
        await interaction.followup.send(
            f"✅ link added: {emoji} **{title}** — {url}", ephemeral=True
        )
    else:
        await interaction.followup.send("❌ couldn't add the link right now — try again 🐾", ephemeral=True)


@bot.tree.command(name="remove-link", description="Remove a link from the site's links page. 🗑️ (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_link(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    links = await _api_get("/api/discord/bot/links")
    if not links:
        await interaction.followup.send("🔗 no links to remove.", ephemeral=True)
        return
    view = LinkSelectView(links, "delete")
    await interaction.followup.send("🗑️ pick a link to remove:", view=view, ephemeral=True)


@bot.tree.command(name="feature-link", description="Promote a link to the top of the links page. ⭐ (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_feature_link(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    links = await _api_get("/api/discord/bot/links")
    if not links:
        await interaction.followup.send("🔗 no links to feature.", ephemeral=True)
        return
    view = LinkSelectView(links, "feature")
    await interaction.followup.send("⭐ pick a link to promote to the top:", view=view, ephemeral=True)


# ·· Community ················································

@bot.tree.command(name="server-info", description="How big is the kennel? 🐾 See the stats.")
async def cmd_server_info(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    stats = await _api_get("/api/discord/bot/stats")
    guild = interaction.guild

    embed = discord.Embed(
        title=f"🐾 {guild.name} — Kennel Stats",
        color=discord.Color(_PINK),
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else discord.Embed.Empty)

    if stats:
        embed.add_field(name="Fanvue Members",  value=str(stats.get("user_count",    "—")), inline=True)
        embed.add_field(name="Collared",        value=str(stats.get("linked_count",  "—")), inline=True)
        embed.add_field(name="\u200b",          value="\u200b",                              inline=True)
        embed.add_field(name="💎 Tier 2",       value=str(stats.get("tier2_count",   "—")), inline=True)
        embed.add_field(name="🌸 Subscribers",  value=str(stats.get("sub_count",     "—")), inline=True)
        embed.add_field(name="🐾 Followers",    value=str(stats.get("follower_count","—")), inline=True)
        embed.add_field(name="💌 Q&A Answered", value=str(stats.get("answered_questions", "—")), inline=True)
        embed.add_field(name="🤤 Drool Items",  value=str(stats.get("drool_count",   "—")), inline=True)
        embed.add_field(name="🛒 Orders",       value=str(stats.get("order_count",   "—")), inline=True)
        zap_count = stats.get("activation_count", 0)
        embed.add_field(name="⚡ Zaps",         value=str(zap_count),                        inline=True)
    else:
        embed.description = "*stats unavailable right now — the kennel is offline 😴*"

    embed.add_field(name="Discord Members", value=str(guild.member_count), inline=True)
    embed.set_footer(text="mochii.live · Alpha Kennel — we keep records 😏")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="verify", description="Get a guide to link your Fanvue account and earn your collar. 🐾")
async def cmd_verify(interaction: discord.Interaction) -> None:
    base = _base_url()
    if not base:
        await interaction.response.send_message(
            "❌ site URL not configured yet — ask an admin 🐾", ephemeral=True
        )
        return
    embed = discord.Embed(
        title="🔗 earn your collar — link your Fanvue account",
        description=(
            f"**Step 1** — Go to **{base}** and log in with your Fanvue account.\n\n"
            "**Step 2** — Click your profile → **Link Discord**.\n\n"
            "**Step 3** — Authorise and come back here.\n\n"
            "**Step 4** — Use `/whoami` to check your tier 🌸\n\n"
            "Your Discord roles will update automatically once you're linked. "
            "the higher the tier, the deeper into the kennel you go 😏"
        ),
        color=discord.Color(_PINK),
    )
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message(
            "📬 sent you a DM with the instructions, pup 🐾", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="whoami", description="Show your Fanvue tier and collar status. 🐾")
async def cmd_whoami(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    data = await _api_get("/api/discord/bot/member", discord_id=str(interaction.user.id))
    if data is None or not data.get("is_linked"):
        await interaction.followup.send(
            "🔗 you're not linked to Fanvue yet — use `/verify` to get your collar, pup 🐾",
            ephemeral=True,
        )
        return
    level = data["access_level"]
    embed = discord.Embed(
        title="🐾 your collar status",
        color=discord.Color(_PINK),
    )
    embed.add_field(name="Discord",  value=interaction.user.mention, inline=True)
    embed.add_field(name="Tier",     value=_tier_label(level),       inline=True)
    embed.set_footer(text="mochii.live · Alpha Kennel — you're in the system 😏")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ·· Puppy Pouch ··············································

@bot.tree.command(name="ask", description="Drop an anonymous note in the Puppy Pouch 💌 mochii will answer.")
@app_commands.describe(question="Your anonymous question or confession (max 280 characters).")
async def cmd_ask(interaction: discord.Interaction, question: str) -> None:
    if len(question) > 280:
        await interaction.response.send_message(
            "❌ notes must be 280 characters or fewer — be concise, be naughty 🐾", ephemeral=True
        )
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                f"{_backend()}/api/questions",
                json={"text": question},
            )
        if resp.status_code in (200, 201):
            await interaction.response.send_message(_pick("ask_confirm"), ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ couldn't deliver your note right now — try again 🐾", ephemeral=True
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ask command error: %s", exc)
        await interaction.response.send_message(
            "❌ couldn't deliver your note right now — try again 🐾", ephemeral=True
        )


@bot.tree.command(name="pouch", description="Browse answered Puppy Pouch notes. 💌")
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
        await interaction.followup.send(
            "📭 nothing on that page yet — drop a note with `/ask` 🌸", ephemeral=True
        )
        return

    total_pages = max(1, (len(questions) + per_page - 1) // per_page)
    embeds = [_question_embed(q) for q in page_qs]
    embeds[0].set_author(name=f"💌 Puppy Pouch — Page {page} of {total_pages}")
    await interaction.followup.send(embeds=embeds[:10])


# ·· Drool Log ················································

def _has_follower_role(member: discord.Member) -> bool:
    return any(r.name in _ALL_TIER_NAMES for r in member.roles)


@bot.tree.command(name="drool", description="Browse the Drool Log. 🤤 (Follower+)")
@app_commands.describe(page="Page number (default: 1).")
async def cmd_drool(interaction: discord.Interaction, page: int = 1) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(_pick("gate_follower"), ephemeral=True)
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
        await interaction.followup.send("🔥 nothing on that page yet. the collection grows daily 👀", ephemeral=True)
        return

    total = data.get("total", len(items))
    per   = data.get("page_size", 3)
    total_pages = max(1, (total + per - 1) // per)
    embeds = [_drool_embed(it) for it in items[:3]]
    embeds[0].set_author(name=f"🤤 Drool Log — Page {page} of {total_pages}")
    await interaction.followup.send(embeds=embeds)


@bot.tree.command(name="weekly-whimper", description="Show the most-reacted Drool Log item this week. 🏆")
async def cmd_weekly_whimper(interaction: discord.Interaction) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(_pick("gate_follower"), ephemeral=True)
        return
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": 1, "page_size": 1})
        items = resp.json().get("items", []) if resp.is_success else []
    except Exception:
        items = []

    if not items:
        await interaction.followup.send("🏆 nothing crowned this week yet — check back later 🐾", ephemeral=True)
        return

    embed = _drool_embed(items[0])
    embed.title = "🏆 Weekly Whimper — the pack's most-drooled-over item"
    embed.color = discord.Color(_GOLD)
    await interaction.followup.send(embed=embed)


# ·· Spotify ··················································

@bot.tree.command(name="nowplaying", description="What's mochii vibing to right now? 🎵")
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
            await interaction.followup.send("🎵 Spotify isn't connected yet — ask an admin 🐾", ephemeral=True)
        else:
            await interaction.followup.send(
                random.choice([
                    "🎵 nothing's playing right now — the kennel is quiet 🌸",
                    "🎵 silence in the kennel… for now 😏",
                    "🎵 mochii isn't playing anything right now. the suspense 😈",
                ]),
                ephemeral=True,
            )
        return

    embed = _nowplaying_embed(data)
    if embed:
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("🎵 nothing's playing right now 🌸", ephemeral=True)


@bot.tree.command(name="queue", description="Search Spotify and add a track to mochii's queue. 🎵 (Subscriber+)")
@app_commands.describe(query="Track name or artist to search for.")
async def cmd_queue(interaction: discord.Interaction, query: str) -> None:
    # Check subscriber role
    member_roles = {r.name for r in interaction.user.roles}
    if not (member_roles & {"🌸 Subscriber", "💎 Tier 2", "⭐ Admin"}):
        await interaction.response.send_message(_pick("gate_subscriber"), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    result = await _api_get(
        "/api/discord/bot/spotify/search",
        q=query,
        discord_id=str(interaction.user.id),
    )
    tracks = (result or {}).get("tracks", [])
    if not tracks:
        err = (result or {}).get("error", "no results found 🎵")
        await interaction.followup.send(f"🎵 {err}", ephemeral=True)
        return

    view  = TrackSelectView(tracks, str(interaction.user.id))
    lines = [f"**{t['name']}** — {t['artist']}" for t in tracks[:5]]
    await interaction.followup.send(
        "🎵 **pick something good for the kennel:**\n" + "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines)),
        view=view,
        ephemeral=True,
    )


# ·· Store ·····················································

@bot.tree.command(name="store", description="Browse the mochii.live store 🛒 treat yourself.")
async def cmd_store(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/store/products")
        products = resp.json() if resp.is_success else []
    except Exception:
        products = []

    base = _base_url()
    if not products:
        await interaction.followup.send(
            f"🛒 the store is empty right now — check back soon 🌸{' · ' + base + '/store.html' if base else ''}"
        )
        return

    embeds = _product_embeds(products)
    header = discord.Embed(
        title="🛒 mochii.live Store",
        description=(
            f"👀 things worth spending money on~\n"
            + (f"\n**Browse at:** {base}/store.html" if base else "")
        ),
        color=discord.Color(_PINK),
    )
    await interaction.followup.send(embeds=[header, *embeds[:9]])


# ·· IoT ·······················································

_DEVICE_CHOICES = [
    app_commands.Choice(name="PiShock  ⚡", value="pishock"),
    app_commands.Choice(name="Lovense  💜", value="lovense"),
]


@bot.tree.command(name="zap", description="Activate a device and let mochii feel it 😏 (Follower+ required)")
@app_commands.describe(device="Which device to activate (default: pishock).")
@app_commands.choices(device=_DEVICE_CHOICES)
async def cmd_zap(interaction: discord.Interaction, device: app_commands.Choice[str] = None) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(_pick("gate_follower"), ephemeral=True)
        return

    device_value = device.value if device else "pishock"
    await interaction.response.defer(ephemeral=True)

    result = await _api_post(
        f"/api/discord/bot/control/{device_value}",
        {"discord_id": str(interaction.user.id)},
    )
    if result is None:
        await interaction.followup.send("❌ couldn't reach the backend right now — try again 🐾", ephemeral=True)
        return

    msg = result.get("message", "done.")
    if result.get("success"):
        await interaction.followup.send(msg, ephemeral=True)
        # Announce publicly to the pack
        taunt = _pick("zap_public").format(user=interaction.user.display_name)
        for guild in bot.guilds:
            if guild.id != interaction.guild_id:
                continue
            # Post in drool-log if available, otherwise updates channel
            ch_id = _guild_channel(guild.id, "drool_channel") or _guild_channel(guild.id, "updates_channel")
            if not ch_id:
                break
            channel = guild.get_channel(ch_id)
            if channel:
                try:
                    await channel.send(taunt)
                except (discord.Forbidden, discord.HTTPException):
                    pass
    else:
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)


# ── Fun / personality commands ────────────────────────────────────────────────

@bot.tree.command(name="ai-vibe", description="Ask AI what the current vibe of the kennel is. ✨")
async def cmd_ai_vibe(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    # Gather live context
    stats = await _api_get("/api/discord/bot/stats")
    np_data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            resp = await c.get(f"{_backend()}/api/spotify/now-playing")
        np_data = resp.json() if resp.is_success else {}
    except Exception:  # noqa: BLE001
        pass

    context_parts: list[str] = []
    if np_data.get("is_playing"):
        context_parts.append(
            f"currently playing '{np_data.get('track_name')}' by {np_data.get('artist_name')}"
        )
    if stats:
        context_parts.append(
            f"{stats.get('user_count', 0)} Fanvue members, "
            f"{stats.get('sub_count', 0)} subscribers, "
            f"{stats.get('tier2_count', 0)} Tier 2, "
            f"{stats.get('activation_count', 0)} total device activations, "
            f"{stats.get('drool_count', 0)} drool items archived"
        )
    context_str = "; ".join(context_parts) or "not much going on"

    vibe = await _ai(
        f"Describe the current vibe of mochii.live's kennel in 2-3 short sentences. "
        f"Context: {context_str}. Be cheeky, specific, and on-brand.",
        max_tokens=180,
    )
    if not vibe:
        await interaction.followup.send(
            "🤔 the vibe is… indeterminate right now. AI is taking a nap. 😴",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="✨ Current Kennel Vibe",
        description=vibe,
        color=discord.Color(_PINK),
    )
    embed.set_footer(text="mochii.live · AI vibe check — powered by gpt-4o-mini 🌸")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="rate", description="Get a completely objective and scientific rating from the bot 😏")
async def cmd_rate(interaction: discord.Interaction) -> None:
    score = random.randint(7, 10)   # we're kind here
    descriptor, emoji = random.choice(_FLAVOR_TEXT["rate_descriptor"])
    embed = discord.Embed(
        title=f"🔬 Official mochii.live Rating™",
        description=(
            f"{interaction.user.mention}\n\n"
            f"**Score:** {score}/10\n"
            f"**Verdict:** {descriptor} {emoji}\n\n"
            f"*methodology: vibes-based, collar-adjacent, 100% accurate*"
        ),
        color=discord.Color(_PINK),
    )
    embed.set_footer(text="mochii.live · ratings department 🐾")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="treat", description="Claim your daily treat 🍖 (Follower+)")
async def cmd_treat(interaction: discord.Interaction) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(_pick("gate_follower"), ephemeral=True)
        return
    treat = _pick("treat")
    embed = discord.Embed(
        title="🍖 Your Treat",
        description=treat,
        color=discord.Color(_PINK),
    )
    embed.set_footer(text=f"for {interaction.user.display_name} — you earned it 🌸")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="collar", description="View your tier as a collar badge 🐾")
async def cmd_collar(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    data = await _api_get("/api/discord/bot/member", discord_id=str(interaction.user.id))
    if not data or not data.get("is_linked"):
        embed = discord.Embed(
            title="🔓 no collar… yet",
            description=(
                "you're not linked to Fanvue — you don't have a collar assigned.\n\n"
                "use `/verify` to link your account and claim your place in the pack 🐾"
            ),
            color=discord.Color(_GRAY),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    level = data["access_level"]
    collar_art = {
        0: "🔗",
        1: "🐾",
        2: "🌸",
        3: "💎",
    }
    collar_desc = {
        0: "a plain link collar — you're in the system, pup",
        1: "a paw-print collar — good pup, you're in the pack",
        2: "a soft pink collar — pampered and privileged 🌸",
        3: "a diamond-studded collar — the absolute favourite 💎",
    }
    icon = collar_art.get(min(level, 3), "🔗")
    desc = collar_desc.get(min(level, 3), "a mysterious collar")
    embed = discord.Embed(
        title=f"{icon} Your Collar",
        description=(
            f"**{interaction.user.display_name}**\n"
            f"*{desc}*\n\n"
            f"**Tier:** {_tier_label(level)}"
        ),
        color=discord.Color([_GRAY, _BLURPLE, _PINK, _PURPLE][min(level, 3)]),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="mochii.live · Alpha Kennel 🐾")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="beg", description="Beg the bot for something 🐾 outcomes vary by tier")
async def cmd_beg(interaction: discord.Interaction) -> None:
    data = await _api_get("/api/discord/bot/member", discord_id=str(interaction.user.id))
    level = data["access_level"] if (data and data.get("is_linked")) else 0
    is_premium = level >= 3

    if is_premium:
        msg = _pick("beg_premium")
        color = _GOLD
    elif random.random() < 0.5:
        msg = _pick("beg_success")
        color = _PINK
    else:
        msg = _pick("beg_fail")
        color = _RED

    embed = discord.Embed(
        title="🐾 Begging in Progress…",
        description=f"{interaction.user.mention} is begging.\n\n{msg}",
        color=discord.Color(color),
    )
    embed.set_footer(text="mochii.live · beg responsibly 🌸")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="peek", description="Get a random surprise from the Drool Log 👀 (Follower+)")
async def cmd_peek(interaction: discord.Interaction) -> None:
    if not _has_follower_role(interaction.user):
        await interaction.response.send_message(_pick("gate_follower"), ephemeral=True)
        return
    await interaction.response.defer()
    try:
        # Fetch a few pages and pick randomly for surprise factor
        page = random.randint(1, 5)
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{_backend()}/api/drool", params={"page": page, "page_size": 10})
        data = resp.json() if resp.is_success else {}
        items = data.get("items", [])
        if not items and page > 1:
            # Fall back to page 1 if the random page was empty
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(f"{_backend()}/api/drool", params={"page": 1, "page_size": 10})
            items = resp.json().get("items", []) if resp.is_success else []
    except Exception:
        items = []

    if not items:
        await interaction.followup.send("👀 the Drool Log is empty right now — check back soon 🌸", ephemeral=True)
        return

    item = random.choice(items)
    tease = _pick("peek_tease")
    embed = _drool_embed(item)
    await interaction.followup.send(content=tease, embed=embed)


# ── Tree-level error handler ──────────────────────────────────────────────────

@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ you don't have permission to do that here, pup 🐾"
    else:
        logger.error("Slash command error: %s", error)
        msg = "❌ something went wrong. the kennel is glitching 😅"
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
