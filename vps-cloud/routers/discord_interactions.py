"""
routers/discord_interactions.py – Discord Interactions endpoint.

Handles button clicks and modal submissions sent by Discord to this server.
Discord requires:
  - POST /discord/interactions
  - Ed25519 signature verification on every request
  - Immediate response (< 3 s) – all DB work is fast and synchronous

Environment variables
---------------------
DISCORD_PUBLIC_KEY
    Hex-encoded Ed25519 public key from the Discord Developer Portal
    (Application → General Information → Public Key).
    If absent or empty, interactions return 501 Not Implemented so the
    button still fails safely rather than accepting unverified payloads.
"""

import logging
import os
import httpx

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request, status

from db import get_db_connection
from discord_webhook import load_reaction_role_options
from routers.admin import _persist_and_publish_question_answer


logger = logging.getLogger(__name__)

router = APIRouter(tags=["discord"])

# ── Discord interaction types ────────────────────────────────────────────────
_PING               = 1
_MESSAGE_COMPONENT  = 3
_MODAL_SUBMIT       = 5

# ── Discord interaction callback types ──────────────────────────────────────
_PONG               = 1
_CHANNEL_MESSAGE    = 4
_MODAL              = 9

# ── Discord limits ───────────────────────────────────────────────────────────
_DISCORD_TEXT_INPUT_MAX_LEN = 4000  # max characters in a modal text input value


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> None:
    """Raise HTTPException 401 if the Discord signature is invalid."""
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed signature headers") from exc
    try:
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub.verify(sig_bytes, timestamp.encode() + body)
    except InvalidSignature as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid request signature") from exc


@router.post("/discord/interactions")
async def discord_interactions(request: Request):
    """Entry point for all Discord Interactions (buttons, modals)."""
    public_key_hex: str = os.environ.get("DISCORD_PUBLIC_KEY", "")
    if not public_key_hex:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Discord interactions not configured")

    # Verify signature before touching the body
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp  = request.headers.get("X-Signature-Timestamp", "")
    body       = await request.body()
    _verify_signature(public_key_hex, signature, timestamp, body)

    data = await request.json()
    interaction_type = data.get("type")

    # ── PING (Discord verification handshake) ────────────────────────────────
    if interaction_type == _PING:
        return {"type": _PONG}

    # ── Button click → open reply modal ─────────────────────────────────────
    if interaction_type == _MESSAGE_COMPONENT:
        custom_id: str = data.get("data", {}).get("custom_id", "")
        if custom_id.startswith("rr:"):
            option_key = custom_id[len("rr:"):].strip().lower()
            options = load_reaction_role_options()
            selected = options.get(option_key)
            if not selected:
                return {
                    "type": _CHANNEL_MESSAGE,
                    "data": {
                        "content": "⚠️ That role option is not available anymore.",
                        "flags": 64,
                    },
                }

            guild_id = str(data.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", "")).strip()
            member = data.get("member") or {}
            user = member.get("user") or data.get("user") or {}
            user_id = str(user.get("id") or "").strip()
            member_roles = {str(r).strip() for r in (member.get("roles") or [])}
            role_id = str(selected.get("role_id") or "").strip()
            role_label = str(selected.get("label") or option_key).strip()

            if not guild_id or not user_id or not role_id:
                return {
                    "type": _CHANNEL_MESSAGE,
                    "data": {
                        "content": "⚠️ Could not process that role toggle right now.",
                        "flags": 64,
                    },
                }

            should_add = role_id not in member_roles
            ok = await _toggle_member_role(
                guild_id=guild_id,
                user_id=user_id,
                role_id=role_id,
                should_add=should_add,
            )
            if not ok:
                return {
                    "type": _CHANNEL_MESSAGE,
                    "data": {
                        "content": "⚠️ Role update failed. Check bot permissions and role hierarchy.",
                        "flags": 64,
                    },
                }

            return {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": (
                        f"✅ Role added: **{role_label}**"
                        if should_add
                        else f"🗑️ Role removed: **{role_label}**"
                    ),
                    "flags": 64,
                },
            }

        if not custom_id.startswith("reply:"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown component")

        question_id = custom_id[len("reply:"):]
        question_text = _fetch_question_text(question_id)
        if question_text is None:
            # Question was already answered or deleted – inform the user
            return {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": "⚠️ That note has already been answered or deleted.",
                    "flags": 64,  # EPHEMERAL
                },
            }

        return {
            "type": _MODAL,
            "data": {
                "custom_id": f"submit_reply:{question_id}",
                "title": "Reply to Note 🐾",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,          # TEXT_INPUT
                                "custom_id": "question_ref",
                                "label": "Note (for reference)",
                                "style": 2,         # PARAGRAPH
                                "value": question_text[:4000],
                                "required": False,
                                "min_length": 0,
                            }
                        ],
                    },
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "answer",
                                "label": "Your Reply",
                                "style": 2,
                                "placeholder": "Write your answer… 🐾",
                                "required": True,
                                "max_length": 2000,
                            }
                        ],
                    },
                ],
            },
        }

    # ── Modal submission → save answer ──────────────────────────────────────
    if interaction_type == _MODAL_SUBMIT:
        custom_id = data.get("data", {}).get("custom_id", "")
        if not custom_id.startswith("submit_reply:"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown modal")

        question_id = custom_id[len("submit_reply:"):]
        answer_text  = _extract_component_value(data, "answer")

        if not answer_text or not answer_text.strip():
            return {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": "⚠️ Reply was empty – please try again.",
                    "flags": 64,  # EPHEMERAL
                },
            }

        with get_db_connection() as db:
            publish_result = await _persist_and_publish_question_answer(
                question_id=question_id,
                answer_text=answer_text.strip(),
                db=db,
                only_if_unanswered=True,
            )

        if not publish_result.get("saved"):
            return {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": "⚠️ Could not save that reply – the note may have already been answered.",
                    "flags": 64,
                },
            }

        share_url = str(publish_result.get("share_url") or "")
        lines = ["✅ Reply saved and published!"]
        if share_url:
            lines.append(f"Share it: {share_url}")

        return {
            "type": _CHANNEL_MESSAGE,
            "data": {
                "content": "\n".join(lines),
                "flags": 64,  # EPHEMERAL – only visible to the replier
            },
        }

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown interaction type")


# ── DB helpers ───────────────────────────────────────────────────────────────

def _fetch_question_text(question_id: str) -> str | None:
    """Return the unanswered question text or None if not found / already answered."""
    with get_db_connection() as db:
        row = db.execute(
            "SELECT text FROM questions WHERE id = ? AND answer IS NULL",
            (question_id,),
        ).fetchone()
    return row["text"] if row else None


def _extract_component_value(interaction_data: dict, custom_id: str) -> str:
    """Walk the nested components tree and return the value for *custom_id*."""
    for row in interaction_data.get("data", {}).get("components", []):
        for component in row.get("components", []):
            if component.get("custom_id") == custom_id:
                return component.get("value", "")
    return ""


async def _toggle_member_role(
    *,
    guild_id: str,
    user_id: str,
    role_id: str,
    should_add: bool,
) -> bool:
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token:
        return False

    method = "PUT" if should_add else "DELETE"
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(
                method,
                url,
                headers={"Authorization": f"Bot {bot_token}"},
            )
            return resp.status_code in (200, 201, 204)
    except Exception:
        return False
