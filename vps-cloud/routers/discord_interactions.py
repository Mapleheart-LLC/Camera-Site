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
import sqlite3

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request, status

from db import get_db_connection

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


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> None:
    """Raise HTTPException 401 if the Discord signature is invalid."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
    except (InvalidSignature, ValueError) as exc:
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

        saved = _save_answer(question_id, answer_text.strip())
        if not saved:
            return {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": "⚠️ Could not save that reply – the note may have already been answered.",
                    "flags": 64,
                },
            }

        base_url: str = os.environ.get("BASE_URL", "").rstrip("/")
        share_url = f"{base_url}/q/{question_id}" if base_url else ""
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


def _save_answer(question_id: str, answer: str) -> bool:
    """Persist the answer and mark the question public.  Returns True on success."""
    try:
        with get_db_connection() as db:
            cur = db.execute(
                "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ? AND answer IS NULL",
                (answer, question_id),
            )
            db.commit()
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.error("Failed to save Discord modal answer: %s", exc)
        return False


def _extract_component_value(interaction_data: dict, custom_id: str) -> str:
    """Walk the nested components tree and return the value for *custom_id*."""
    for row in interaction_data.get("data", {}).get("components", []):
        for component in row.get("components", []):
            if component.get("custom_id") == custom_id:
                return component.get("value", "")
    return ""
