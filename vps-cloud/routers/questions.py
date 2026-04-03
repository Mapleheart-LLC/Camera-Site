"""
routers/questions.py – Puppy Pouch anonymous Q&A endpoints.

Public endpoints (no authentication required):
  POST /api/questions                – submit an anonymous question (≤ 280 chars)
  GET  /api/questions/public         – list all answered, public questions
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from db import get_db
from discord_webhook import send_discord_notification

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
        INSERT INTO questions (id, text, answer, is_public, created_at)
        VALUES (?, ?, NULL, 0, ?)
        """,
        (question_id, payload.text, created_at),
    )
    db.commit()

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
def list_public_questions(db: sqlite3.Connection = Depends(get_db)):
    """Return all answered questions that are marked as public."""
    rows = db.execute(
        """
        SELECT id, text, answer, created_at
        FROM questions
        WHERE is_public = 1 AND answer IS NOT NULL
        ORDER BY created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]
