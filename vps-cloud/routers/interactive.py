"""
routers/interactive.py – IoT device control endpoints.

Provides two POST endpoints for Premium Subscribers (access_level == 3):
  - POST /api/control/pishock   – trigger a PiShock device
  - POST /api/control/lovense   – trigger a Lovense device

Both endpoints require a valid Fanvue JWT (Bearer token) and enforce that the
caller holds access_level 3 (Tier 2+ / Premium Subscriber).
"""

from fastapi import APIRouter, Depends, HTTPException, status

# get_current_user is imported from the shared dependencies module so that
# this router uses the same JWT validation logic as the rest of the application
# without creating a circular import with main.py.
from dependencies import get_current_user

router = APIRouter(prefix="/api/control", tags=["interactive"])

_PREMIUM_LEVEL = 3


def _require_premium(current_user: dict = Depends(get_current_user)) -> dict:
    """Raise 403 if the caller does not hold Premium Subscriber access (level 3+).

    Using ``<`` rather than ``!=`` follows the RBAC convention that higher
    access levels inherit lower-level permissions, so any future level above 3
    will also be granted access to these endpoints.
    """
    if current_user.get("access_level", 0) < _PREMIUM_LEVEL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Premium subscription (access level 3) required.",
        )
    return current_user


@router.post("/pishock")
async def control_pishock(current_user: dict = Depends(_require_premium)):
    """
    Trigger a PiShock device for the authenticated Premium Subscriber.

    Currently returns a mock success response; the real implementation will
    forward the command to the local-edge agent over the Tailscale VPN.
    """
    return {
        "status": "ok",
        "device": "pishock",
        "message": "Command accepted (mock response).",
        "user": current_user["fanvue_id"],
    }


@router.post("/lovense")
async def control_lovense(current_user: dict = Depends(_require_premium)):
    """
    Trigger a Lovense device for the authenticated Premium Subscriber.

    Currently returns a mock success response; the real implementation will
    forward the command to the local-edge agent over the Tailscale VPN.
    """
    return {
        "status": "ok",
        "device": "lovense",
        "message": "Command accepted (mock response).",
        "user": current_user["fanvue_id"],
    }
