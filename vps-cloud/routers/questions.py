"""
routers/questions.py – Puppy Pouch anonymous Q&A endpoints.

Public endpoints (no authentication required):
  POST /api/questions                – submit an anonymous question (≤ 280 chars)
  GET  /api/questions/public         – list all answered, public questions
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from db import get_db
from discord_webhook import send_discord_notification
from routers.tpe import _send_fcm_to_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/questions", tags=["questions"])

_MAX_QUESTION_LENGTH = 280  # Must stay in sync with _NOTE_MAX in static/index.html


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class QuestionSubmit(BaseModel):
    text: str = Field(..., min_length=1, max_length=_MAX_QUESTION_LENGTH)


class PublicQuestion(BaseModel):
    id: str
    text: str
    answer: str
    created_at: str
    source_type: str
    publication_tier: str


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_question(
    payload: QuestionSubmit,
    db: sqlite3.Connection = Depends(get_db),
):
    """Accept an anonymous question and store it for admin review."""
    question_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO questions (id, text, answer, is_public, created_at, source_type, publication_tier)
        VALUES (?, ?, NULL, 0, ?, 'anon', 'safe')
        """,
        (question_id, payload.text, created_at),
    )
    db.commit()

    try:
        preview = payload.text[:120] + ("…" if len(payload.text) > 120 else "")
        _send_fcm_to_all(db, {
            "action":           "NEW_QUESTION",
            "question_id":      question_id,
            "question_preview": preview,
        })
    except Exception as exc:
        logger.warning("NEW_QUESTION FCM push failed: %s", exc)
        # Question already saved — don't fail the response

    # Notify via Discord webhook.  Failures are silently logged; the question
    # has already been persisted so the user always receives a success response.
    await send_discord_notification(
        content="🐾 A new note has been dropped in the Puppy Pouch!",
        question_text=payload.text,
        is_embed=True,
        question_id=question_id,
        channel_id=os.environ.get("DISCORD_QUESTION_CHANNEL_ID"),
    )

    return {"id": question_id, "message": "Your question has been submitted 🐾"}


@router.get("/public", response_model=list[PublicQuestion])
def list_public_questions(
    source: str | None = Query(None, description="Optional source filter: anon or limbo"),
    tier: str | None = Query(None, description="Optional tier filter: safe, sensitive, or extreme"),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return answered public questions, optionally filtered by source and tier."""
    allowed_sources = {"anon", "limbo"}
    allowed_tiers = {"safe", "sensitive", "extreme"}

    clauses = ["is_public = 1", "answer IS NOT NULL"]
    params: list[str] = []

    if source is not None:
        source_value = source.strip().lower()
        if source_value not in allowed_sources:
            raise HTTPException(status_code=400, detail="Invalid source filter")
        clauses.append("LOWER(source_type) = ?")
        params.append(source_value)

    if tier is not None:
        tier_value = tier.strip().lower()
        if tier_value not in allowed_tiers:
            raise HTTPException(status_code=400, detail="Invalid tier filter")
        clauses.append("LOWER(publication_tier) = ?")
        params.append(tier_value)

    where_clause = " AND ".join(clauses)
    rows = db.execute(
        f"""
        SELECT id, text, answer, created_at, source_type, publication_tier
        FROM questions
        WHERE {where_clause}
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]
