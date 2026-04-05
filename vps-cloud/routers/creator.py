"""
routers/creator.py – Creator self-serve API endpoints.

Provides login for creator accounts and a full set of self-serve endpoints
so that each creator can manage their own profile, Q&A, and Drool Log
without touching any other creator's data or owner-only infrastructure.

All endpoints under ``/api/creator/`` (except login) are protected by
``get_current_creator`` which validates a JWT with ``role: creator``.

Endpoints
---------
  POST   /api/creator/login                     – authenticate; returns creator JWT
  GET    /api/creator/me                        – own profile
  PATCH  /api/creator/me                        – update bio / avatar / accent colour
  GET    /api/creator/questions                 – unanswered questions (own only)
  GET    /api/creator/questions/answered        – answered questions (own only)
  POST   /api/creator/questions/{id}/answer     – answer a question
  DELETE /api/creator/questions/{id}            – delete a question
  GET    /api/creator/drool                     – Drool Log entries (own only)
  DELETE /api/creator/drool/{id}                – remove a Drool Log entry
  GET    /api/creator/stats                     – subscriber count, recent tips, Q count
"""

import logging
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from dependencies import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    get_current_creator,
)
from routers.auth import _hash_password, _verify_password  # reuse stdlib hashing

router = APIRouter(prefix="/api/creator", tags=["creator"])

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Creator JWT lifetime (longer than subscriber tokens — 24 h default)
# ---------------------------------------------------------------------------
_CREATOR_TOKEN_MINUTES = 60 * 24  # 24 hours


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreatorLoginRequest(BaseModel):
    handle: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ProfilePatch(BaseModel):
    display_name: Optional[str] = Field(None, max_length=64)
    bio: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = Field(None, max_length=512)
    accent_color: Optional[str] = Field(None, max_length=16, pattern=r"^#[0-9a-fA-F]{3,8}$")


class AnswerPayload(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post("/login")
def creator_login(
    payload: CreatorLoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate a creator and return a short-lived JWT.

    The JWT carries ``role: creator`` so it cannot be used as a subscriber
    or admin token.
    """
    handle = payload.handle.lower().strip()

    row = db.execute(
        """
        SELECT id, handle, display_name, hashed_password, is_active
          FROM creator_accounts
         WHERE handle = ?
        """,
        (handle,),
    ).fetchone()

    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid handle or password.",
    )

    if not row:
        _hash_password("__timing_guard__")  # prevent timing-based enumeration
        raise _invalid

    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This creator account is deactivated. Contact the site owner.",
        )

    if not _verify_password(payload.password, row["hashed_password"]):
        raise _invalid

    token = create_access_token(
        {"sub": row["handle"], "role": "creator"},
        expires_delta=timedelta(minutes=_CREATOR_TOKEN_MINUTES),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "handle": row["handle"],
        "display_name": row["display_name"],
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/me")
def get_my_profile(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the creator's public + editable profile fields."""
    row = db.execute(
        """
        SELECT handle, display_name, bio, avatar_url, accent_color
          FROM creator_accounts
         WHERE handle = ?
        """,
        (handle,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")
    return dict(row)


@router.patch("/me")
def patch_my_profile(
    payload: ProfilePatch,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update the creator's editable profile fields (bio, avatar URL, accent colour)."""
    row = db.execute(
        "SELECT handle, display_name, bio, avatar_url, accent_color FROM creator_accounts WHERE handle = ?",
        (handle,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Creator not found.")

    new_display_name = payload.display_name if payload.display_name is not None else row["display_name"]
    new_bio          = payload.bio          if payload.bio          is not None else row["bio"]
    new_avatar_url   = payload.avatar_url   if payload.avatar_url   is not None else row["avatar_url"]
    new_accent_color = payload.accent_color if payload.accent_color is not None else row["accent_color"]

    db.execute(
        """
        UPDATE creator_accounts
           SET display_name = ?, bio = ?, avatar_url = ?, accent_color = ?
         WHERE handle = ?
        """,
        (new_display_name, new_bio, new_avatar_url, new_accent_color, handle),
    )
    db.commit()

    return {
        "handle": handle,
        "display_name": new_display_name,
        "bio": new_bio,
        "avatar_url": new_avatar_url,
        "accent_color": new_accent_color,
    }


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------


@router.get("/questions")
def creator_list_unanswered(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all unanswered questions for this creator."""
    rows = db.execute(
        """
        SELECT id, text, created_at
          FROM questions
         WHERE answer IS NULL AND creator_handle = ?
         ORDER BY created_at ASC
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/questions/answered")
def creator_list_answered(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all answered questions for this creator."""
    rows = db.execute(
        """
        SELECT id, text, answer, is_public, created_at
          FROM questions
         WHERE answer IS NOT NULL AND creator_handle = ?
         ORDER BY created_at DESC
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/questions/{question_id}/answer", status_code=status.HTTP_200_OK)
def creator_answer_question(
    question_id: str,
    payload: AnswerPayload,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Save an answer to a question belonging to this creator."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ? AND creator_handle = ?",
        (question_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute(
        "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ?",
        (payload.answer, question_id),
    )
    db.commit()
    return {"id": question_id, "message": "Answer saved 🐾"}


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def creator_delete_question(
    question_id: str,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Permanently delete a question belonging to this creator."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ? AND creator_handle = ?",
        (question_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Drool Log
# ---------------------------------------------------------------------------


@router.get("/drool")
def creator_list_drool(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return Drool Log entries belonging to this creator, newest first."""
    rows = db.execute(
        """
        SELECT id, platform, original_url AS url, media_url,
               text_content AS title, view_count,
               timestamp AS created_at
          FROM drool_archive
         WHERE creator_handle = ?
         ORDER BY id DESC
         LIMIT 200
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.delete("/drool/{entry_id}", status_code=status.HTTP_200_OK)
def creator_delete_drool(
    entry_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a Drool Log entry belonging to this creator."""
    row = db.execute(
        "SELECT id FROM drool_archive WHERE id = ? AND creator_handle = ?",
        (entry_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Entry not found.",
        )
    db.execute("DELETE FROM drool_reactions WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_comments WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_archive WHERE id = ?", (entry_id,))
    db.commit()
    return {"deleted": entry_id}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
def creator_stats(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return simple statistics scoped to this creator."""
    subscriber_count = db.execute(
        "SELECT COUNT(*) FROM site_users WHERE access_level >= 2"
    ).fetchone()[0]

    unanswered_count = db.execute(
        "SELECT COUNT(*) FROM questions WHERE answer IS NULL AND creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    answered_count = db.execute(
        "SELECT COUNT(*) FROM questions WHERE answer IS NOT NULL AND creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    drool_count = db.execute(
        "SELECT COUNT(*) FROM drool_archive WHERE creator_handle = ?",
        (handle,),
    ).fetchone()[0]

    return {
        "subscriber_count": subscriber_count,
        "unanswered_questions": unanswered_count,
        "answered_questions": answered_count,
        "drool_entries": drool_count,
    }
