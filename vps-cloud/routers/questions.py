"""
routers/questions.py – Puppy Pouch anonymous Q&A endpoints.

Public endpoints (no authentication required):
  POST /api/questions                – submit an anonymous question (≤ 280 chars)
  GET  /api/questions/public         – list all answered, public questions
"""

import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db

router = APIRouter(prefix="/api/questions", tags=["questions"])

_MAX_QUESTION_LENGTH = 280


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
def submit_question(
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
