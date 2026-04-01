"""
edge_relay.py – Utility for forwarding commands to the local-edge agent.

Uses httpx to POST a DeviceCommand to the Raspberry Pi's internal
Tailscale IP.  Requires EDGE_TAILSCALE_IP and EDGE_API_KEY to be set as
environment variables.
"""

import logging
import os

import httpx

from models.commands import DeviceCommand

EDGE_TAILSCALE_IP: str = os.environ.get("EDGE_TAILSCALE_IP", "")
EDGE_API_KEY: str = os.environ.get("EDGE_API_KEY", "")
EDGE_PORT: str = os.environ.get("EDGE_PORT", "8001")

logger = logging.getLogger(__name__)


async def send_to_edge(command: DeviceCommand) -> dict:
    """
    Forward *command* to the Raspberry Pi local-edge agent over Tailscale.

    Raises
    ------
    RuntimeError
        If ``EDGE_TAILSCALE_IP`` or ``EDGE_API_KEY`` are not configured.
    httpx.HTTPStatusError
        If the edge agent returns a non-2xx HTTP response.
    httpx.RequestError
        If the HTTP request itself fails (e.g. network unreachable).
    """
    if not EDGE_TAILSCALE_IP:
        raise RuntimeError("EDGE_TAILSCALE_IP is not configured.")
    if not EDGE_API_KEY:
        raise RuntimeError("EDGE_API_KEY is not configured.")

    url = f"http://{EDGE_TAILSCALE_IP}:{EDGE_PORT}/internal/relay-command"
    logger.debug(
        "Forwarding command to edge: device_type=%s action=%s",
        command.device_type,
        command.action,
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            json=command.model_dump(),
            headers={"X-Edge-Api-Key": EDGE_API_KEY},
        )
        resp.raise_for_status()
    return resp.json()
