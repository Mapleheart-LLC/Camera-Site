"""
routers/interactive.py – IoT device control endpoints.

Provides POST endpoints for authenticated subscribers (access_level >= 1):
  - POST /api/control/pishock   – trigger a PiShock device (direct/VPN)
  - POST /api/control/lovense   – trigger a Lovense device via the TPE app
  - POST /api/control/pavlok    – trigger a Pavlok device via the TPE app

Access rules
------------
  level 0            – 403 Forbidden (no subscription).
  level 1 or 2       – Teaser access: device activates for 5 seconds; a
                       1-hour cooldown is then enforced via Redis.  If a
                       cooldown key already exists the endpoint returns
                       429 Too Many Requests with a ``Retry-After`` header.
  level 3 (Premium)  – No rate limit.

Lovense and Pavlok commands are routed through the paired TPE app via FCM
(``LOVENSE_COMMAND`` / ``PAVLOK_COMMAND`` data messages).  PiShock continues
to use a direct connection and is not relayed through the app.

Both endpoints require a valid JWT (Bearer token).
Each successful activation is logged to the ``activations`` SQLite table.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from db import get_db
from dependencies import get_current_user
from redis_client import get_redis
from routers.tpe import _send_fcm_to_all

router = APIRouter(prefix="/api/control", tags=["interactive"])

_PREMIUM_LEVEL = 3
_COOLDOWN_SECONDS = 3600        # 1 hour
_TEASER_DURATION_SECONDS = 5    # IoT activation window for teaser users


def _cooldown_key(fanvue_id: str, device: str) -> str:
    """Build the Redis key used to track a user's per-device cooldown."""
    return f"teaser:cooldown:{device}:{fanvue_id}"


def _make_teaser_dependency(device: str):
    """
    Return a FastAPI dependency that enforces teaser rate-limiting for *device*.

    - Level 0   → 403 Forbidden.
    - Level 1/2 → 429 if a Redis cooldown key is present (with ``Retry-After``
                  seconds remaining); otherwise set a 1-hour cooldown and allow.
    - Level 3+  → no rate limit, pass through immediately.
    """

    async def check_teaser_limit(
        current_user: dict = Depends(get_current_user),
        redis: Optional[Redis] = Depends(get_redis),
    ) -> dict:
        access_level: int = current_user.get("access_level", 0)
        fanvue_id: str = current_user["fanvue_id"]

        if access_level < 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An active subscription is required.",
            )

        if access_level >= _PREMIUM_LEVEL:
            # Premium subscribers have no rate limit.
            return current_user

        # Teaser path (access levels 1 and 2) – enforce the 1-hour cooldown.
        # If Redis is unavailable, skip rate-limiting and grant access.
        if redis is None:
            return current_user

        key = _cooldown_key(fanvue_id, device)
        ttl: int = await redis.ttl(key)

        # ttl > 0  : key exists and has remaining time → cooldown active
        # ttl == -1: key exists with no expiry (should not happen, but treat as
        #            active to avoid bypassing the cooldown)
        # ttl == -2: key does not exist → grant access
        if ttl > 0 or ttl == -1:
            retry_after = ttl if ttl > 0 else _COOLDOWN_SECONDS
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Cooldown active. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

        # No active cooldown – grant access and start the 1-hour cooldown now.
        await redis.set(key, "1", ex=_COOLDOWN_SECONDS)
        return current_user

    return check_teaser_limit


def _log_activation(db: sqlite3.Connection, device: str, actor: str) -> None:
    """Insert a row into the activations log table."""
    db.execute(
        "INSERT INTO activations (device, actor, activated_at) VALUES (?, ?, ?)",
        (device, actor, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


@router.post("/pishock")
async def control_pishock(
    current_user: dict = Depends(_make_teaser_dependency("pishock")),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Trigger a PiShock device for the authenticated subscriber.

    Premium users (level 3+) receive an unlimited activation.  Teaser users
    (levels 1–2) receive a 5-second activation followed by a 1-hour cooldown.

    Currently returns a mock success response; the real implementation will
    forward the command to the local-edge agent over the Tailscale VPN.
    """
    access_level: int = current_user.get("access_level", 0)
    is_teaser = access_level < _PREMIUM_LEVEL
    _log_activation(db, "pishock", current_user["fanvue_id"])
    response: dict = {
        "status": "ok",
        "device": "pishock",
        "message": "Command accepted (mock response).",
        "user": current_user["fanvue_id"],
    }
    if is_teaser:
        response["activation_seconds"] = _TEASER_DURATION_SECONDS
        response["cooldown_seconds"] = _COOLDOWN_SECONDS
    return response


@router.post("/lovense")
async def control_lovense(
    current_user: dict = Depends(_make_teaser_dependency("lovense")),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Trigger a Lovense device for the authenticated subscriber.

    Premium users (level 3+) receive an unlimited activation.  Teaser users
    (levels 1–2) receive a 5-second activation followed by a 1-hour cooldown.

    The command is forwarded to the paired TPE app via an FCM ``LOVENSE_COMMAND``
    data message; the app then relays it to the connected Lovense toy.
    """
    access_level: int = current_user.get("access_level", 0)
    is_teaser = access_level < _PREMIUM_LEVEL
    _log_activation(db, "lovense", current_user["fanvue_id"])

    toy_level = "5" if is_teaser else "10"
    _send_fcm_to_all(db, {
        "action":    "LOVENSE_COMMAND",
        "toy_command": "vibrate",
        "toy_level": toy_level,
    })

    response: dict = {
        "status": "ok",
        "device": "lovense",
        "message": "Command forwarded to app.",
        "user": current_user["fanvue_id"],
    }
    if is_teaser:
        response["activation_seconds"] = _TEASER_DURATION_SECONDS
        response["cooldown_seconds"] = _COOLDOWN_SECONDS
    return response


@router.post("/pavlok")
async def control_pavlok(
    current_user: dict = Depends(_make_teaser_dependency("pavlok")),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Trigger a Pavlok device for the authenticated subscriber.

    Premium users (level 3+) receive an unlimited activation.  Teaser users
    (levels 1–2) receive a 5-second activation followed by a 1-hour cooldown.

    The command is forwarded to the paired TPE app via an FCM ``PAVLOK_COMMAND``
    data message; the app then relays it to the connected Pavlok device.
    """
    access_level: int = current_user.get("access_level", 0)
    is_teaser = access_level < _PREMIUM_LEVEL
    _log_activation(db, "pavlok", current_user["fanvue_id"])

    intensity = "50" if is_teaser else "100"
    _send_fcm_to_all(db, {
        "action":             "PAVLOK_COMMAND",
        "pavlok_cmd":         "vibrate",
        "pavlok_intensity":   intensity,
        "pavlok_duration_ms": str(_TEASER_DURATION_SECONDS * 1000),
    })

    response: dict = {
        "status": "ok",
        "device": "pavlok",
        "message": "Command forwarded to app.",
        "user": current_user["fanvue_id"],
    }
    if is_teaser:
        response["activation_seconds"] = _TEASER_DURATION_SECONDS
        response["cooldown_seconds"] = _COOLDOWN_SECONDS
    return response
