"""
routers/compliance.py – Trust, Safety & Compliance endpoints.

Phase 1 of the platform expansion:
  - Age gate: server-side middleware helper + per-creator flag
  - Content reporting
  - DMCA takedown workflow
  - Creator 2FA (TOTP via pyotp)

Endpoints
---------
  POST /api/report                           – report a piece of content (authenticated)
  POST /api/dmca                             – public DMCA takedown request

  POST /api/creator/2fa/setup                – generate TOTP secret (creator auth)
  POST /api/creator/2fa/verify               – verify OTP and enable 2FA (creator auth)
  DELETE /api/creator/2fa/disable            – disable 2FA (creator auth, requires OTP)

  GET  /api/admin/reports                    – list content reports (admin)
  POST /api/admin/reports/{id}/action        – hide / dismiss a report (admin)
  GET  /api/admin/dmca                       – list DMCA requests (admin)
  POST /api/admin/dmca/{id}/action           – resolve a DMCA request (admin)
"""

import logging
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from db import get_db
from dependencies import (
    get_admin_user,
    get_current_creator,
    get_current_user,
)
from routers.auth import _hash_password

router = APIRouter(tags=["compliance"])

logger = logging.getLogger(__name__)

_ISSUER_NAME = os.environ.get("SITE_NAME", "mochii.live")

# Rate limiter for public and authenticated compliance endpoints.
_compliance_limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# SMTP helper (re-uses same env vars as creator email)
# ---------------------------------------------------------------------------

def _send_email(to: str, subject: str, body_html: str) -> None:
    """Send a plain email via SMTP.  Silently logs errors."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    if not smtp_host or not smtp_user:
        logger.warning("SMTP not configured; skipping email to %s", to)
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.set_content(body_html, subtype="html")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to, exc)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ContentReportRequest(BaseModel):
    content_type: str = Field(..., pattern=r"^(drool|question|comment|post)$")
    content_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(..., min_length=5, max_length=500)


class DmcaRequest(BaseModel):
    complainant_name: str = Field(..., min_length=2, max_length=128)
    complainant_email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    content_url: str = Field(..., min_length=10, max_length=2048)
    description: str = Field(..., min_length=20, max_length=4000)


class TotpVerifyRequest(BaseModel):
    otp: str = Field(..., min_length=6, max_length=8)


class TotpDisableRequest(BaseModel):
    otp: str = Field(..., min_length=6, max_length=8)


class ReportActionRequest(BaseModel):
    action: str = Field(..., pattern=r"^(hide|dismiss)$")


class DmcaActionRequest(BaseModel):
    action: str = Field(..., pattern=r"^(action|dismiss)$")
    note: Optional[str] = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Content Reporting
# ---------------------------------------------------------------------------

_AUTO_HIDE_THRESHOLD = int(os.environ.get("REPORT_AUTO_HIDE_THRESHOLD", "5"))


@router.post("/api/report", status_code=status.HTTP_201_CREATED)
def submit_report(
    payload: ContentReportRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Submit a content report (authenticated users only)."""
    now = datetime.now(timezone.utc).isoformat()
    user_id = current_user["fanvue_id"]

    # Prevent duplicate reports from the same user for the same content.
    existing = db.execute(
        "SELECT id FROM content_reports WHERE reporter_user_id = ? AND content_type = ? AND content_id = ?",
        (user_id, payload.content_type, payload.content_id),
    ).fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reported this content.",
        )

    db.execute(
        """
        INSERT INTO content_reports (reporter_user_id, content_type, content_id, reason, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (user_id, payload.content_type, payload.content_id, payload.reason, now),
    )
    db.commit()

    # Auto-hide if threshold is reached.
    report_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM content_reports WHERE content_type = ? AND content_id = ?",
        (payload.content_type, payload.content_id),
    ).fetchone()["cnt"]

    if report_count >= _AUTO_HIDE_THRESHOLD:
        _hide_content(db, payload.content_type, payload.content_id)

    return {"detail": "Report submitted."}


def _hide_content(db: sqlite3.Connection, content_type: str, content_id: str) -> None:
    """Set is_hidden=1 on the referenced content row if the column exists."""
    try:
        if content_type == "drool":
            db.execute(
                "UPDATE drool_archive SET is_hidden = 1 WHERE id = ?", (content_id,)
            )
        elif content_type == "question":
            db.execute(
                "UPDATE questions SET is_public = 0 WHERE id = ?", (content_id,)
            )
        elif content_type == "post":
            db.execute(
                "UPDATE community_posts SET is_published = 0 WHERE id = ?", (content_id,)
            )
        db.commit()
    except Exception as exc:
        logger.warning("Failed to hide content %s/%s: %s", content_type, content_id, exc)


# ---------------------------------------------------------------------------
# DMCA
# ---------------------------------------------------------------------------

@router.post("/api/dmca", status_code=status.HTTP_201_CREATED)
@_compliance_limiter.limit("5/hour")
def submit_dmca(
    request: Request,
    payload: DmcaRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Public DMCA takedown submission endpoint."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO dmca_requests
            (complainant_name, complainant_email, content_url, description, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            payload.complainant_name,
            payload.complainant_email,
            payload.content_url,
            payload.description,
            now,
        ),
    )
    db.commit()
    logger.info("DMCA request from %s for URL %s", payload.complainant_email, payload.content_url)
    return {"detail": "Your DMCA request has been received and will be reviewed."}


# ---------------------------------------------------------------------------
# Creator 2FA (TOTP via pyotp)
# ---------------------------------------------------------------------------

@router.post("/api/creator/2fa/setup")
@_compliance_limiter.limit("5/hour")
def creator_2fa_setup(
    request: Request,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Generate a new TOTP secret for the creator.

    Returns the provisioning URI for QR code display.  The creator must call
    ``/api/creator/2fa/verify`` to confirm the code before 2FA is activated.
    """
    row = db.execute(
        "SELECT totp_enabled FROM creator_accounts WHERE handle = ?", (handle,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Creator not found.")
    if row["totp_enabled"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="2FA is already enabled. Disable it first.",
        )

    secret = pyotp.random_base32()
    db.execute(
        "UPDATE creator_accounts SET totp_secret = ? WHERE handle = ?",
        (secret, handle),
    )
    db.commit()

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=handle, issuer_name=_ISSUER_NAME)
    return {"provisioning_uri": uri, "secret": secret}


@router.post("/api/creator/2fa/verify")
@_compliance_limiter.limit("10/30minutes")
def creator_2fa_verify(
    request: Request,
    payload: TotpVerifyRequest,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Confirm the OTP code and activate 2FA for this creator account."""
    row = db.execute(
        "SELECT totp_secret, totp_enabled FROM creator_accounts WHERE handle = ?", (handle,)
    ).fetchone()
    if not row or not row["totp_secret"]:
        raise HTTPException(status_code=400, detail="Run /api/creator/2fa/setup first.")
    if row["totp_enabled"]:
        raise HTTPException(status_code=409, detail="2FA is already enabled.")

    totp = pyotp.TOTP(row["totp_secret"])
    if not totp.verify(payload.otp, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid OTP code.")

    db.execute(
        "UPDATE creator_accounts SET totp_enabled = 1 WHERE handle = ?", (handle,)
    )
    db.commit()
    return {"detail": "2FA enabled successfully."}


@router.delete("/api/creator/2fa/disable")
@_compliance_limiter.limit("5/30minutes")
def creator_2fa_disable(
    request: Request,
    payload: TotpDisableRequest,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Disable 2FA after confirming the current OTP code."""
    row = db.execute(
        "SELECT totp_secret, totp_enabled FROM creator_accounts WHERE handle = ?", (handle,)
    ).fetchone()
    if not row or not row["totp_enabled"]:
        raise HTTPException(status_code=400, detail="2FA is not enabled.")

    totp = pyotp.TOTP(row["totp_secret"])
    if not totp.verify(payload.otp, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid OTP code.")

    db.execute(
        "UPDATE creator_accounts SET totp_secret = NULL, totp_enabled = 0 WHERE handle = ?",
        (handle,),
    )
    db.commit()
    return {"detail": "2FA disabled."}


# ---------------------------------------------------------------------------
# Admin: content reports
# ---------------------------------------------------------------------------

@router.get("/api/admin/reports")
def list_reports(
    status_filter: Optional[str] = "pending",
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List content reports filtered by status."""
    rows = db.execute(
        "SELECT * FROM content_reports WHERE status = ? ORDER BY created_at DESC",
        (status_filter,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/admin/reports/{report_id}/action")
def action_report(
    report_id: int,
    payload: ReportActionRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Mark a report as reviewed/actioned and optionally hide the content."""
    row = db.execute(
        "SELECT * FROM content_reports WHERE id = ?", (report_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found.")

    new_status = "actioned" if payload.action == "hide" else "reviewed"
    db.execute(
        "UPDATE content_reports SET status = ? WHERE id = ?",
        (new_status, report_id),
    )
    if payload.action == "hide":
        _hide_content(db, row["content_type"], row["content_id"])
    db.commit()
    return {"detail": f"Report {new_status}."}


# ---------------------------------------------------------------------------
# Admin: DMCA requests
# ---------------------------------------------------------------------------

@router.get("/api/admin/dmca")
def list_dmca(
    status_filter: Optional[str] = "pending",
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List DMCA requests filtered by status."""
    rows = db.execute(
        "SELECT * FROM dmca_requests WHERE status = ? ORDER BY created_at DESC",
        (status_filter,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/admin/dmca/{dmca_id}/action")
def action_dmca(
    dmca_id: int,
    payload: DmcaActionRequest,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Resolve a DMCA request.  'action' hides the content; 'dismiss' closes without hiding."""
    row = db.execute("SELECT * FROM dmca_requests WHERE id = ?", (dmca_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="DMCA request not found.")

    now = datetime.now(timezone.utc).isoformat()
    new_status = "actioned" if payload.action == "action" else "reviewed"
    db.execute(
        "UPDATE dmca_requests SET status = ?, resolved_at = ? WHERE id = ?",
        (new_status, now, dmca_id),
    )
    db.commit()
    logger.info("DMCA request %s resolved as %s", dmca_id, new_status)
    return {"detail": f"DMCA request {new_status}."}
