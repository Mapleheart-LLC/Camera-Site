"""
routers/internal.py – Private command relay endpoint.

Exposes POST /internal/relay-command, which is accessible only from the
Tailscale VPN subnet (100.64.0.0/10) and authenticated via a shared
EDGE_API_KEY header.  This endpoint is the VPS-side entry point for
commands arriving from Tailscale-connected peers (e.g. the Raspberry Pi
edge agent).
"""

import ipaddress
import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status

from models.commands import DeviceCommand

logger = logging.getLogger(__name__)

_TAILSCALE_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_EDGE_API_KEY: str = os.environ.get("EDGE_API_KEY", "")

router = APIRouter(prefix="/internal", tags=["internal"])


def _require_tailscale(request: Request) -> None:
    """Reject requests that do not originate from the Tailscale subnet."""
    client_host = request.client.host if request.client else None
    try:
        client_ip = ipaddress.IPv4Address(client_host or "")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to the Tailscale network.",
        )
    if client_ip not in _TAILSCALE_NETWORK:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to the Tailscale network.",
        )


def _require_edge_api_key(request: Request) -> None:
    """Validate the shared EDGE_API_KEY header."""
    if not _EDGE_API_KEY:
        # Misconfigured server – fail closed rather than open.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal relay is not configured.",
        )
    provided = request.headers.get("X-Edge-Api-Key", "")
    # Use a constant-time comparison to prevent timing-based key discovery.
    if not secrets.compare_digest(provided.encode(), _EDGE_API_KEY.encode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key.",
        )


@router.post("/relay-command", status_code=status.HTTP_202_ACCEPTED)
async def relay_command(
    command: DeviceCommand,
    _tailscale: None = Depends(_require_tailscale),
    _key: None = Depends(_require_edge_api_key),
) -> dict:
    """
    Accept a DeviceCommand arriving over the Tailscale VPN and acknowledge it.

    Access control
    --------------
    * Only requests originating from the Tailscale subnet (100.64.0.0/10)
      are permitted.
    * The ``X-Edge-Api-Key`` header must match the configured ``EDGE_API_KEY``.
    """
    logger.info(
        "Internal relay command received: device_type=%s action=%s duration=%s",
        command.device_type,
        command.action,
        command.duration,
    )
    return {
        "status": "accepted",
        "device_type": command.device_type,
        "action": command.action,
        "duration": command.duration,
    }
