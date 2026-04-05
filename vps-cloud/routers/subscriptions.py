"""
routers/subscriptions.py – Segpay subscription lifecycle webhook.

Receives Segpay S2S postbacks for subscription events and updates the
subscriber's ``access_level`` in the ``site_users`` table accordingly.

Every postback is also recorded in ``segpay_subscriptions`` for auditing.

Endpoint
--------
  POST /api/webhooks/subscriptions/segpay
"""

import hashlib
import hmac
import logging
import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, status

from db import get_db

router = APIRouter(tags=["subscriptions"])

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Segpay transaction type classification
# ---------------------------------------------------------------------------

# Transaction types that indicate an active / renewed subscription.
_ACTIVE_TRANS_TYPES = frozenset({"new_sale", "rebill", "approval"})

# Transaction types that mean the subscription has ended.
_INACTIVE_TRANS_TYPES = frozenset(
    {"void", "refund", "chargeback", "cancellation", "cancel"}
)

# Segpay ``x-responsecode`` values that mean "payment approved".
_PAID_CODES = frozenset({"1", "approved", "success"})


def _sub_access_level() -> int:
    """Return the access level to grant on a successful subscription.

    Defaults to 2.  Override via the ``SEGPAY_SUB_ACCESS_LEVEL`` env var.
    """
    try:
        return int(os.environ.get("SEGPAY_SUB_ACCESS_LEVEL", "2"))
    except ValueError:
        return 2


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@router.post("/api/webhooks/subscriptions/segpay")
async def segpay_subscription_webhook(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Process Segpay subscription lifecycle postbacks.

    Grants or revokes subscriber ``access_level`` based on the transaction
    type received in the postback:

    - ``NEW_SALE`` / ``REBILL`` (approved) → set ``access_level`` to
      ``SEGPAY_SUB_ACCESS_LEVEL`` (default 2).
    - ``VOID`` / ``REFUND`` / ``CHARGEBACK`` / ``CANCEL`` → set
      ``access_level`` to 0.
    """
    raw_body = await request.body()
    webhook_secret = os.environ.get("SEGPAY_SUB_WEBHOOK_SECRET", "")

    # ── Parse form-encoded body ─────────────────────────────────────────────
    try:
        parsed = parse_qs(
            raw_body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid postback body.",
        ) from exc

    def _first(key: str) -> str:
        return parsed.get(key, [""])[0].strip()

    # ── HMAC verification (when SEGPAY_SUB_WEBHOOK_SECRET is set) ───────────
    if webhook_secret:
        headers = dict(request.headers)
        provided_sig = _first("x-sig") or headers.get("x-sig", "")
        if not provided_sig:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Segpay webhook signature (x-sig).",
            )
        expected = hmac.new(
            webhook_secret.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, provided_sig.lower()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Segpay webhook signature mismatch.",
            )

    # ── Extract key fields ──────────────────────────────────────────────────
    email = (_first("x-billemail") or _first("email")).lower()
    trans_type = (_first("x-transtype") or _first("transtype") or "").lower()
    response_code = (
        _first("x-responsecode") or _first("response_code") or ""
    ).lower()
    subscription_id = _first("x-subscriptionid") or _first("subscriptionid") or None

    if not email:
        logger.warning(
            "Segpay subscription webhook: no email in postback – ignoring. "
            "trans_type=%r",
            trans_type,
        )
        return {"status": "ignored", "reason": "no_email"}

    # ── Classify the event ──────────────────────────────────────────────────
    is_active = (trans_type in _ACTIVE_TRANS_TYPES) and (
        response_code in _PAID_CODES or not response_code
    )
    is_inactive = trans_type in _INACTIVE_TRANS_TYPES

    if not is_active and not is_inactive:
        logger.info(
            "Segpay subscription webhook: unhandled trans_type=%r response_code=%r "
            "for email=%s – ignoring.",
            trans_type,
            response_code,
            email,
        )
        return {"status": "ignored", "reason": "unhandled_trans_type"}

    # ── Look up the subscriber ──────────────────────────────────────────────
    row = db.execute(
        "SELECT id, access_level FROM site_users WHERE email = ?",
        (email,),
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()

    if not row:
        logger.warning(
            "Segpay subscription webhook: no site_user found for email=%s "
            "(trans_type=%s).",
            email,
            trans_type,
        )
        # Still record the event so it can be matched manually if the user
        # registers after subscribing.
        db.execute(
            """
            INSERT INTO segpay_subscriptions
                (user_id, segpay_subscription_id, trans_type, status,
                 access_level_granted, email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (None, subscription_id, trans_type, "unmatched", None, email, now),
        )
        db.commit()
        return {"status": "no_user", "email": email}

    user_id = row["id"]

    # ── Grant or revoke access ──────────────────────────────────────────────
    if is_active:
        new_level = _sub_access_level()
        db.execute(
            "UPDATE site_users SET access_level = ? WHERE id = ?",
            (new_level, user_id),
        )
        db.execute(
            """
            INSERT INTO segpay_subscriptions
                (user_id, segpay_subscription_id, trans_type, status,
                 access_level_granted, email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, subscription_id, trans_type, "active", new_level, email, now),
        )
        db.commit()
        logger.info(
            "Segpay subscription: granted access_level=%d to user %s "
            "(email=%s, trans_type=%s).",
            new_level,
            user_id,
            email,
            trans_type,
        )
        return {"status": "granted", "access_level": new_level}

    else:  # is_inactive
        db.execute(
            "UPDATE site_users SET access_level = 0 WHERE id = ?",
            (user_id,),
        )
        db.execute(
            """
            INSERT INTO segpay_subscriptions
                (user_id, segpay_subscription_id, trans_type, status,
                 access_level_granted, email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, subscription_id, trans_type, "inactive", 0, email, now),
        )
        db.commit()
        logger.info(
            "Segpay subscription: revoked access for user %s (email=%s, trans_type=%s).",
            user_id,
            email,
            trans_type,
        )
        return {"status": "revoked"}
