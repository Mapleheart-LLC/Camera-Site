"""
routers/interactive.py – IoT device control endpoints.

Provides two POST endpoints for authenticated subscribers (access_level >= 1):
  - POST /api/control/pishock   – trigger a PiShock device
  - POST /api/control/lovense   – trigger a Lovense device

Access rules
------------
  level 0            – 403 Forbidden (no subscription).
  level 1 or 2       – Teaser access: device activates for 5 seconds; a
                       1-hour cooldown is then enforced via Redis.  If a
                       cooldown key already exists the endpoint returns
                       429 Too Many Requests with a ``Retry-After`` header.
  level 3 (Premium)  – No rate limit.

Both endpoints require a valid Fanvue JWT (Bearer token).
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from dependencies import get_current_user
from edge_relay import send_to_edge
from models.commands import DeviceCommand
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["interactive"])

_PREMIUM_LEVEL = 3
_COOLDOWN_SECONDS = 3600        # 1 hour
_TEASER_DURATION_SECONDS = 5    # IoT activation window for teaser users


async def _relay_to_edge(device_type: str, action: str, duration: int | None) -> None:
    """Forward a DeviceCommand to the edge, mapping exceptions to HTTPExceptions."""
    command = DeviceCommand(device_type=device_type, action=action, duration=duration)
    try:
        await send_to_edge(command)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except httpx.RequestError as exc:
        logger.error("Edge relay request failed for %s: %s", device_type, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the edge agent.",
        )
    except httpx.HTTPStatusError as exc:
        logger.error("Edge relay returned error for %s: %s", device_type, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Edge agent returned an error.",
        )


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
        redis: Redis = Depends(get_redis),
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


@router.post("/pishock")
async def control_pishock(
    current_user: dict = Depends(_make_teaser_dependency("pishock")),
):
    """
    Trigger a PiShock device for the authenticated subscriber.

    Premium users (level 3+) receive an unlimited activation.  Teaser users
    (levels 1–2) receive a 5-second activation followed by a 1-hour cooldown.

    Forwards the command to the local-edge agent over the Tailscale VPN.
    """
    access_level: int = current_user.get("access_level", 0)
    is_teaser = access_level < _PREMIUM_LEVEL
    duration = _TEASER_DURATION_SECONDS if is_teaser else None

    await _relay_to_edge("pishock", "shock", duration)

    response: dict = {
        "status": "ok",
        "device": "pishock",
        "user": current_user["fanvue_id"],
    }
    if is_teaser:
        response["activation_seconds"] = _TEASER_DURATION_SECONDS
        response["cooldown_seconds"] = _COOLDOWN_SECONDS
    return response


@router.post("/lovense")
async def control_lovense(
    current_user: dict = Depends(_make_teaser_dependency("lovense")),
):
    """
    Trigger a Lovense device for the authenticated subscriber.

    Premium users (level 3+) receive an unlimited activation.  Teaser users
    (levels 1–2) receive a 5-second activation followed by a 1-hour cooldown.

    Forwards the command to the local-edge agent over the Tailscale VPN.
    """
    access_level: int = current_user.get("access_level", 0)
    is_teaser = access_level < _PREMIUM_LEVEL
    duration = _TEASER_DURATION_SECONDS if is_teaser else None

    await _relay_to_edge("lovense", "vibrate", duration)

    response: dict = {
        "status": "ok",
        "device": "lovense",
        "user": current_user["fanvue_id"],
    }
    if is_teaser:
        response["activation_seconds"] = _TEASER_DURATION_SECONDS
        response["cooldown_seconds"] = _COOLDOWN_SECONDS
    return response
