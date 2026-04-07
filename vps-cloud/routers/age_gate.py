"""
routers/age_gate.py – Age verification via idswyft.

Flow
----
1.  Browser calls POST /api/age-gate/init
      → backend creates an idswyft verification session (age_only mode)
      → stores (verification_id, session_token) in age_verifications
      → returns {session_token, verification_id} to the browser

2.  Browser POSTs the ID document to POST /api/age-gate/upload/{session_token}
      → backend proxies the multipart upload to idswyft /api/v2/verify/{id}/front-document
      → returns the idswyft step result

3.  Idswyft calls POST /api/age-gate/webhook when verification is terminal
      → backend updates age_verifications.status

4.  Browser polls GET /api/age-gate/status/{session_token}
      → returns {status: "pending"|"verified"|"failed"|"manual_review"}

5.  Browser calls POST /api/age-gate/confirm with {session_token}
      → backend validates status == "verified" in DB
      → sets HttpOnly age_verified=1 cookie (365 days)
      → returns {ok: true}
"""

import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IDSWYFT_API_URL: str = os.environ.get("IDSWYFT_API_URL", "http://idswyft-api:3001").rstrip("/")
IDSWYFT_API_KEY: str = os.environ.get("IDSWYFT_API_KEY", "")
IDSWYFT_WEBHOOK_SECRET: str = os.environ.get("IDSWYFT_WEBHOOK_SECRET", "")
AGE_GATE_ENABLED: bool = os.environ.get("AGE_GATE_ENABLED", "true").lower() == "true"
# Transmit the age_verified cookie over HTTPS only.  Defaults to True (secure).
# Set SECURE_COOKIES=false only for local HTTP development.
_SECURE_COOKIES: bool = os.environ.get("SECURE_COOKIES", "true").lower() != "false"

_IDSWYFT_HEADERS = {"X-API-Key": IDSWYFT_API_KEY, "Content-Type": "application/json"}

# Timeout for calls to the idswyft API (document ML processing can be slow)
_IDSWYFT_TIMEOUT = 60.0

# Cookie lifetime – 365 days
_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

router = APIRouter(prefix="/api/age-gate", tags=["age-gate"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _idswyft_configured() -> bool:
    return bool(IDSWYFT_API_URL and IDSWYFT_API_KEY)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class _ConfirmRequest(BaseModel):
    session_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/init", status_code=201)
async def age_gate_init(db: sqlite3.Connection = Depends(get_db)):
    """Create a new idswyft age-only verification session and return a session_token."""
    if not AGE_GATE_ENABLED:
        raise HTTPException(status_code=503, detail="Age gate is not enabled on this server.")

    if not _idswyft_configured():
        raise HTTPException(
            status_code=503,
            detail="Age verification service is not configured. Contact the site administrator.",
        )

    # Generate a random UUID-like user_id for idswyft (must be a UUID)
    import uuid
    idswyft_user_id = str(uuid.uuid4())

    # Create the verification session on idswyft
    payload = {
        "user_id": idswyft_user_id,
        "verification_mode": "age_only",
        "document_type": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=_IDSWYFT_TIMEOUT) as client:
            resp = await client.post(
                f"{IDSWYFT_API_URL}/api/v2/verify/initialize",
                json=payload,
                headers=_IDSWYFT_HEADERS,
            )
    except httpx.RequestError as exc:
        logger.error("idswyft init request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Unable to reach age verification service.")

    if not resp.is_success:
        logger.error("idswyft init returned %s: %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Age verification service returned an error.")

    data = resp.json()
    verification_id: str = data.get("verification_id") or data.get("id")
    if not verification_id:
        logger.error("idswyft init response missing verification_id: %s", data)
        raise HTTPException(status_code=502, detail="Unexpected response from age verification service.")

    # Mint a session token the browser will hold
    session_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        """
        INSERT INTO age_verifications (verification_id, session_token, idswyft_user_id, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (verification_id, session_token, idswyft_user_id, now),
    )
    db.commit()
    logger.info("Age verification session created: verification_id=%s", verification_id)

    return {"session_token": session_token, "verification_id": verification_id}


@router.post("/upload/{session_token}")
async def age_gate_upload(
    session_token: str,
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Proxy the front-of-ID upload to idswyft on behalf of the browser."""
    if not AGE_GATE_ENABLED:
        raise HTTPException(status_code=503, detail="Age gate is not enabled.")

    row = db.execute(
        "SELECT verification_id, status FROM age_verifications WHERE session_token = ?",
        (session_token,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Session already in status: {row['status']}.")

    verification_id = row["verification_id"]

    # Read file bytes and proxy to idswyft
    file_bytes = await file.read()
    content_type = file.content_type or "image/jpeg"
    filename = file.filename or "document.jpg"

    try:
        async with httpx.AsyncClient(timeout=_IDSWYFT_TIMEOUT) as client:
            resp = await client.post(
                f"{IDSWYFT_API_URL}/api/v2/verify/{verification_id}/front-document",
                headers={"X-API-Key": IDSWYFT_API_KEY},
                files={"document": (filename, file_bytes, content_type)},
            )
    except httpx.RequestError as exc:
        logger.error("idswyft upload request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Unable to reach age verification service.")

    result = resp.json()

    # For age_only mode the result may already be terminal after front-document
    terminal_status: Optional[str] = None
    resp_status = result.get("status") or result.get("verification_status")
    if resp_status in ("verified", "failed", "manual_review"):
        terminal_status = resp_status

    if terminal_status:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE age_verifications SET status = ?, verified_at = ? WHERE session_token = ?",
            (terminal_status, now if terminal_status == "verified" else None, session_token),
        )
        db.commit()
        logger.info("Age verification %s after upload: session=%s", terminal_status, session_token)

    return JSONResponse(status_code=resp.status_code, content=result)


@router.get("/status/{session_token}")
def age_gate_status(session_token: str, db: sqlite3.Connection = Depends(get_db)):
    """Poll the verification status by session token."""
    row = db.execute(
        "SELECT status FROM age_verifications WHERE session_token = ?",
        (session_token,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"status": row["status"]}


@router.post("/webhook")
async def age_gate_webhook(request: Request, db: sqlite3.Connection = Depends(get_db)):
    """Receive a verification completion webhook from idswyft."""
    # Optional shared-secret validation
    if IDSWYFT_WEBHOOK_SECRET:
        provided = request.headers.get("X-Webhook-Secret", "")
        import hmac as _hmac
        if not _hmac.compare_digest(provided, IDSWYFT_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    verification_id: Optional[str] = payload.get("verification_id")
    new_status: Optional[str] = payload.get("status")

    if not verification_id or not new_status:
        raise HTTPException(status_code=400, detail="Missing verification_id or status.")

    if new_status not in ("verified", "failed", "manual_review"):
        # Unknown status – accept but don't update
        return {"ok": True}

    now = datetime.now(timezone.utc).isoformat()
    result = db.execute(
        """
        UPDATE age_verifications
           SET status      = ?,
               verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END
         WHERE verification_id = ?
        """,
        (new_status, new_status, now, verification_id),
    )
    db.commit()

    if result.rowcount:
        logger.info("Age verification webhook: id=%s status=%s", verification_id, new_status)
    else:
        logger.warning("Age verification webhook: unknown verification_id=%s", verification_id)

    return {"ok": True}


@router.post("/confirm")
async def age_gate_confirm(body: _ConfirmRequest, response: Response, db: sqlite3.Connection = Depends(get_db)):
    """Validate a completed verification and set the age_verified cookie."""
    row = db.execute(
        "SELECT status FROM age_verifications WHERE session_token = ?",
        (body.session_token,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")

    if row["status"] != "verified":
        raise HTTPException(
            status_code=403,
            detail=f"Age verification has not been approved (status: {row['status']}).",
        )

    response.set_cookie(
        key="age_verified",
        value="1",
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_SECURE_COOKIES,
    )
    logger.info("Age verification confirmed; cookie issued.")
    return {"ok": True}
