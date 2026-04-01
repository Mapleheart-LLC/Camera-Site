"""
agent.py – Local-edge agent stub.

This lightweight agent will eventually:
  - Poll the VPS for pending commands (via long-poll or WebSocket).
  - Trigger local Tapo smart switches over the LAN.
  - Handle AI camera routing decisions (e.g. redirect streams based on motion).

The VPS communicates with this agent over a Tailscale site-to-site VPN so that
no edge ports need to be exposed to the public internet.

Run:
    pip install -r requirements.txt
    python agent.py
"""

import asyncio
import logging
import os

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# URL of the VPS command endpoint (override via environment variable).
VPS_COMMAND_URL: str = os.environ.get(
    "VPS_COMMAND_URL", "http://vps-cloud:8000/api/edge/commands"
)
# Shared secret used to authenticate this agent with the VPS.
# This MUST be set via the EDGE_SECRET environment variable before running.
_edge_secret_raw: str | None = os.environ.get("EDGE_SECRET")
if not _edge_secret_raw:
    raise RuntimeError(
        "EDGE_SECRET environment variable is not set. "
        "Set it to a strong shared secret before starting the edge agent."
    )
EDGE_SECRET: str = _edge_secret_raw
# How often (seconds) the agent polls for new commands when long-polling is unavailable.
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "5"))


async def poll_commands() -> None:
    """Fetch and dispatch commands from the VPS in a tight loop."""
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(
                    VPS_COMMAND_URL,
                    headers={"X-Edge-Secret": EDGE_SECRET},
                )
                if resp.status_code == 200:
                    commands = resp.json()
                    for cmd in commands:
                        await dispatch(cmd)
                else:
                    logger.warning("VPS returned %s", resp.status_code)
            except httpx.RequestError as exc:
                logger.error("Could not reach VPS: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)


async def dispatch(command: dict) -> None:
    """Route an incoming command to the appropriate handler."""
    action = command.get("action", "")
    logger.info("Dispatching command: %s", command)

    if action == "tapo_switch":
        await handle_tapo_switch(command)
    elif action == "camera_route":
        await handle_camera_route(command)
    else:
        logger.warning("Unknown command action: %s", action)


async def handle_tapo_switch(command: dict) -> None:
    """Stub: Toggle a local Tapo smart switch."""
    device_ip = command.get("device_ip", "")
    state = command.get("state", "off")
    logger.info("TODO: set Tapo switch at %s to %s", device_ip, state)


async def handle_camera_route(command: dict) -> None:
    """Stub: Update AI camera routing."""
    target = command.get("target", "")
    logger.info("TODO: reroute camera stream to %s", target)


if __name__ == "__main__":
    logger.info("Edge agent starting – polling VPS at %s", VPS_COMMAND_URL)
    asyncio.run(poll_commands())
