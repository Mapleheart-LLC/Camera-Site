"""
routers/age_gate.py – Age verification supporting idswyft (self-hosted) and
DiDit (cloud-hosted) as interchangeable providers.

Select the active provider with the AGE_GATE_PROVIDER environment variable:
  AGE_GATE_PROVIDER=idswyft  (default)
  AGE_GATE_PROVIDER=didit

─────────────────────────────────────────────────────────────────────────────
Idswyft flow
─────────────────────────────────────────────────────────────────────────────
1.  Browser calls POST /api/age-gate/init
      → backend creates an idswyft verification session (age_only mode)
      → stores (verification_id, session_token) in age_verifications
      → returns {provider, session_token, verification_id}

2.  Browser POSTs the ID document to POST /api/age-gate/upload/{session_token}
      → backend proxies the multipart upload to idswyft
      → returns the idswyft step result

3.  Idswyft calls POST /api/age-gate/webhook when verification is terminal
      → backend updates age_verifications.status

4.  Browser polls GET /api/age-gate/status/{session_token}
      → returns {status: "pending"|"verified"|"failed"|"manual_review"}

5.  Browser calls POST /api/age-gate/confirm with {session_token}
      → backend validates status == "verified" in DB
      → sets HttpOnly age_verified=1 cookie (365 days)

─────────────────────────────────────────────────────────────────────────────
DiDit flow
─────────────────────────────────────────────────────────────────────────────
1.  Browser calls POST /api/age-gate/init
      → backend fetches a short-lived bearer token (client_credentials)
      → creates a DiDit session (POST /v3/session/) with vendor_data=session_token
      → returns {provider, session_token, verification_id, redirect_url}

2.  Browser redirects the user to redirect_url (DiDit's hosted verification UI).
    The upload endpoint is not used with DiDit.

3.  DiDit calls POST /api/age-gate/webhook with an X-Signature-V2 HMAC header
      → backend verifies the signature, maps DiDit status to internal status

4.  Browser polls GET /api/age-gate/status/{session_token}

5.  Browser calls POST /api/age-gate/confirm → cookie issued as usual
"""

import hashlib
import hmac as _hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGE_GATE_ENABLED: bool = os.environ.get("AGE_GATE_ENABLED", "true").lower() == "true"
AGE_GATE_PROVIDER: str = os.environ.get("AGE_GATE_PROVIDER", "idswyft").lower()

# ── Idswyft ──────────────────────────────────────────────────────────────────
IDSWYFT_API_URL: str = os.environ.get("IDSWYFT_API_URL", "http://idswyft-api:3001").rstrip("/")
IDSWYFT_API_KEY: str = os.environ.get("IDSWYFT_API_KEY", "")
IDSWYFT_WEBHOOK_SECRET: str = os.environ.get("IDSWYFT_WEBHOOK_SECRET", "")

_IDSWYFT_HEADERS = {"X-API-Key": IDSWYFT_API_KEY, "Content-Type": "application/json"}
_IDSWYFT_TIMEOUT = 60.0  # document ML processing can be slow

# ── DiDit ─────────────────────────────────────────────────────────────────────
DIDIT_CLIENT_ID: str = os.environ.get("DIDIT_CLIENT_ID", "")
DIDIT_CLIENT_SECRET: str = os.environ.get("DIDIT_CLIENT_SECRET", "")
DIDIT_WORKFLOW_ID: str = os.environ.get("DIDIT_WORKFLOW_ID", "")
DIDIT_WEBHOOK_SECRET: str = os.environ.get("DIDIT_WEBHOOK_SECRET", "")
# Where DiDit redirects the browser after the user completes verification.
# Defaults to {BASE_URL}/age-gate if BASE_URL is set, otherwise must be provided.
_BASE_URL: str = os.environ.get("BASE_URL", "").rstrip("/")
DIDIT_CALLBACK_URL: str = os.environ.get("DIDIT_CALLBACK_URL", f"{_BASE_URL}/age-gate" if _BASE_URL else "")

_DIDIT_AUTH_URL = "https://apx.didit.me/auth/v2/token"
_DIDIT_SESSION_URL = "https://verification.didit.me/v3/session/"
_DIDIT_TIMEOUT = 30.0
_DIDIT_TOKEN_REFRESH_BUFFER = 30  # seconds before expiry to proactively refresh

# Simple in-process bearer-token cache (reset on process restart)
_didit_token: Optional[str] = None
_didit_token_expires: float = 0.0

# ── Shared ────────────────────────────────────────────────────────────────────
_SECURE_COOKIES: bool = os.environ.get("SECURE_COOKIES", "true").lower() != "false"
_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 365 days

router = APIRouter(prefix="/api/age-gate", tags=["age-gate"])


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------


def _idswyft_configured() -> bool:
    return bool(IDSWYFT_API_URL and IDSWYFT_API_KEY)


def _didit_configured() -> bool:
    return bool(DIDIT_CLIENT_ID and DIDIT_CLIENT_SECRET and DIDIT_WORKFLOW_ID)


async def _get_didit_token() -> str:
    """Return a valid DiDit bearer token, refreshing when near expiry."""
    global _didit_token, _didit_token_expires
    if _didit_token and time.time() < _didit_token_expires - _DIDIT_TOKEN_REFRESH_BUFFER:
        return _didit_token
    try:
        async with httpx.AsyncClient(timeout=_DIDIT_TIMEOUT) as client:
            resp = await client.post(
                _DIDIT_AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": DIDIT_CLIENT_ID,
                    "client_secret": DIDIT_CLIENT_SECRET,
                },
            )
    except httpx.RequestError as exc:
        logger.error("DiDit token request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Unable to reach DiDit authentication service.")
    if not resp.is_success:
        logger.error("DiDit token endpoint returned %s: %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="DiDit authentication failed.")
    data = resp.json()
    _didit_token = data["access_token"]
    _didit_token_expires = time.time() + data.get("expires_in", 3600)
    return _didit_token


# DiDit status → internal status mapping
_DIDIT_STATUS_MAP = {
    "Approved": "verified",
    "Declined": "failed",
    "In Review": "manual_review",
}


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
    """Create a new age verification session and return provider-specific data."""
    if not AGE_GATE_ENABLED:
        raise HTTPException(status_code=503, detail="Age gate is not enabled on this server.")

    session_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    # ── DiDit ─────────────────────────────────────────────────────────────────
    if AGE_GATE_PROVIDER == "didit":
        if not _didit_configured():
            raise HTTPException(
                status_code=503,
                detail="DiDit age verification is not configured. Contact the site administrator.",
            )
        token = await _get_didit_token()
        payload: dict = {
            "workflow_id": DIDIT_WORKFLOW_ID,
            "vendor_data": session_token,
        }
        if DIDIT_CALLBACK_URL:
            payload["callback"] = DIDIT_CALLBACK_URL
        try:
            async with httpx.AsyncClient(timeout=_DIDIT_TIMEOUT) as client:
                resp = await client.post(
                    _DIDIT_SESSION_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.RequestError as exc:
            logger.error("DiDit session creation failed: %s", exc)
            raise HTTPException(status_code=502, detail="Unable to reach DiDit verification service.")
        if not resp.is_success:
            logger.error("DiDit session endpoint returned %s: %s", resp.status_code, resp.text)
            raise HTTPException(status_code=502, detail="DiDit verification service returned an error.")
        data = resp.json()
        verification_id: str = data.get("session_id") or data.get("id", "")
        redirect_url: str = data.get("url", "")
        if not verification_id:
            logger.error("DiDit session response missing session_id: %s", data)
            raise HTTPException(status_code=502, detail="Unexpected response from DiDit service.")
        db.execute(
            """
            INSERT INTO age_verifications
                (verification_id, session_token, idswyft_user_id, status, created_at, provider)
            VALUES (?, ?, ?, 'pending', ?, 'didit')
            """,
            (verification_id, session_token, session_token, now),
        )
        db.commit()
        logger.info("DiDit verification session created: session_id=%s", verification_id)
        return {
            "provider": "didit",
            "session_token": session_token,
            "verification_id": verification_id,
            "redirect_url": redirect_url,
        }

    # ── Idswyft (default) ─────────────────────────────────────────────────────
    if not _idswyft_configured():
        raise HTTPException(
            status_code=503,
            detail="Age verification service is not configured. Contact the site administrator.",
        )
    idswyft_user_id = str(uuid.uuid4())
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
    verification_id = data.get("verification_id") or data.get("id")
    if not verification_id:
        logger.error("idswyft init response missing verification_id: %s", data)
        raise HTTPException(status_code=502, detail="Unexpected response from age verification service.")
    db.execute(
        """
        INSERT INTO age_verifications
            (verification_id, session_token, idswyft_user_id, status, created_at, provider)
        VALUES (?, ?, ?, 'pending', ?, 'idswyft')
        """,
        (verification_id, session_token, idswyft_user_id, now),
    )
    db.commit()
    logger.info("Idswyft verification session created: verification_id=%s", verification_id)
    return {"provider": "idswyft", "session_token": session_token, "verification_id": verification_id}


@router.post("/upload/{session_token}")
async def age_gate_upload(
    session_token: str,
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Proxy the front-of-ID upload to idswyft. Not applicable for DiDit sessions."""
    if not AGE_GATE_ENABLED:
        raise HTTPException(status_code=503, detail="Age gate is not enabled.")

    row = db.execute(
        "SELECT verification_id, status, provider FROM age_verifications WHERE session_token = ?",
        (session_token,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row["provider"] == "didit":
        raise HTTPException(
            status_code=400,
            detail="Document upload is not used with the DiDit provider; "
                   "redirect the user to the redirect_url returned by /init instead.",
        )
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Session already in status: {row['status']}.")

    verification_id = row["verification_id"]
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
    """
    Receive a verification completion webhook from idswyft or DiDit.

    Provider is auto-detected from payload structure:
      • DiDit payloads carry a top-level "event" field.
      • Idswyft payloads carry a top-level "verification_id" field.
    """
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    # ── DiDit webhook ──────────────────────────────────────────────────────────
    if "event" in payload:
        if DIDIT_WEBHOOK_SECRET:
            sig = request.headers.get("X-Signature-V2", "")
            if not sig:
                raise HTTPException(status_code=403, detail="Missing DiDit webhook signature.")
            # Re-encode with sorted keys and unescaped Unicode to match DiDit's
            # signature generation exactly (see DiDit webhook docs, X-Signature-V2).
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            expected = _hmac.new(
                DIDIT_WEBHOOK_SECRET.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not _hmac.compare_digest(sig, expected):
                raise HTTPException(status_code=403, detail="Invalid DiDit webhook signature.")

        if payload.get("event") != "status.updated":
            return {"ok": True}

        data = payload.get("data", {})
        didit_status: str = data.get("status", "")
        internal_status = _DIDIT_STATUS_MAP.get(didit_status)
        if not internal_status:
            # Intermediate status (In Progress, etc.) – acknowledge and ignore
            return {"ok": True}

        # vendor_data carries our session_token back in the webhook payload
        vendor_session_token: Optional[str] = data.get("vendor_data")
        session_id: Optional[str] = data.get("session_id")

        now = datetime.now(timezone.utc).isoformat()
        if vendor_session_token:
            result = db.execute(
                """
                UPDATE age_verifications
                   SET status      = ?,
                       verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END
                 WHERE session_token = ? AND provider = 'didit'
                """,
                (internal_status, internal_status, now, vendor_session_token),
            )
        elif session_id:
            result = db.execute(
                """
                UPDATE age_verifications
                   SET status      = ?,
                       verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END
                 WHERE verification_id = ? AND provider = 'didit'
                """,
                (internal_status, internal_status, now, session_id),
            )
        else:
            raise HTTPException(status_code=400, detail="DiDit webhook missing session_id and vendor_data.")

        db.commit()
        if result.rowcount:
            logger.info("DiDit webhook: status=%s → %s", didit_status, internal_status)
        else:
            logger.warning("DiDit webhook: no matching session for payload %s", data)
        return {"ok": True}

    # ── Idswyft webhook ────────────────────────────────────────────────────────
    if IDSWYFT_WEBHOOK_SECRET:
        provided = request.headers.get("X-Webhook-Secret", "")
        if not _hmac.compare_digest(provided, IDSWYFT_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    verification_id: Optional[str] = payload.get("verification_id")
    new_status: Optional[str] = payload.get("status")

    if not verification_id or not new_status:
        raise HTTPException(status_code=400, detail="Missing verification_id or status.")

    if new_status not in ("verified", "failed", "manual_review"):
        return {"ok": True}

    now = datetime.now(timezone.utc).isoformat()
    result = db.execute(
        """
        UPDATE age_verifications
           SET status      = ?,
               verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END
         WHERE verification_id = ? AND provider = 'idswyft'
        """,
        (new_status, new_status, now, verification_id),
    )
    db.commit()
    if result.rowcount:
        logger.info("Idswyft webhook: id=%s status=%s", verification_id, new_status)
    else:
        logger.warning("Idswyft webhook: unknown verification_id=%s", verification_id)
    return {"ok": True}


@router.post("/confirm")
async def age_gate_confirm(
    body: _ConfirmRequest,
    response: Response,
    db: sqlite3.Connection = Depends(get_db),
):
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
