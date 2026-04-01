"""
agent.py – Local-edge agent for the Camera Site.

Runs two concurrent tasks:
  1. A FastAPI HTTP server that accepts commands forwarded by the VPS relay
     via POST /execute.
  2. A polling loop that periodically fetches queued commands from the VPS.

The VPS communicates with this agent over a Tailscale site-to-site VPN so
that no edge ports need to be exposed to the public internet.

Hardware currently supported
-----------------------------
  device_type == "switch"
      Uses the plugp100 library to control a Tapo P100/P110 smart plug:
      turn on → wait for the requested duration → turn off.

Required environment variables
--------------------------------
  EDGE_SECRET    Shared secret for authenticating requests from the VPS.
  TAPO_EMAIL     Tapo cloud account e-mail address.
  TAPO_PASSWORD  Tapo cloud account password.

Optional environment variables
--------------------------------
  VPS_COMMAND_URL  Full URL of the VPS polling endpoint.
  POLL_INTERVAL    Polling cadence in seconds (default: 5).
  AGENT_PORT       Port this FastAPI server listens on (default: 8080).
  TAPO_PORT        LAN port used to reach Tapo devices (default: 80).

Run:
    pip install -r requirements.txt
    python agent.py
"""

import asyncio
import logging
import os

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, status
from plugp100.common.credentials import AuthCredential
from plugp100.new.tapo.p100plug import P100Plug
from plugp100.new.tapo_client import TapoClient
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------

# URL of the VPS command polling endpoint.
VPS_COMMAND_URL: str = os.environ.get(
    "VPS_COMMAND_URL", "http://vps-cloud:8000/api/edge/commands"
)

# Shared secret used to authenticate requests between the VPS and this agent.
# MUST be set via the EDGE_SECRET environment variable before running.
_edge_secret_raw: str | None = os.environ.get("EDGE_SECRET")
if not _edge_secret_raw:
    raise RuntimeError(
        "EDGE_SECRET environment variable is not set. "
        "Set it to a strong shared secret before starting the edge agent."
    )
EDGE_SECRET: str = _edge_secret_raw

# How often (seconds) the agent polls the VPS for new commands.
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "5"))

# Port this FastAPI server listens on.
AGENT_PORT: int = int(os.environ.get("AGENT_PORT", "8080"))

# LAN port used to reach Tapo devices (almost always 80, but configurable).
TAPO_PORT: int = int(os.environ.get("TAPO_PORT", "80"))

# Tapo smart-plug credentials (loaded lazily so missing values are caught at
# runtime rather than at import time).
TAPO_EMAIL: str = os.environ.get("TAPO_EMAIL", "")
TAPO_PASSWORD: str = os.environ.get("TAPO_PASSWORD", "")

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Camera Site – Edge Agent")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ExecuteCommand(BaseModel):
    """Command payload forwarded from the VPS relay."""

    device_type: str
    device_ip: str
    duration: float = 5.0


# ---------------------------------------------------------------------------
# Authentication dependency
# ---------------------------------------------------------------------------


def _verify_edge_secret(x_edge_secret: str = Header(alias="X-Edge-Secret")) -> None:
    """Reject requests that do not carry the correct shared secret."""
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected request with invalid X-Edge-Secret header.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid edge secret.",
        )


# ---------------------------------------------------------------------------
# /execute endpoint
# ---------------------------------------------------------------------------


@app.post("/execute")
async def execute_command(
    cmd: ExecuteCommand,
    _: None = Depends(_verify_edge_secret),
) -> dict:
    """
    Receive a command from the VPS relay and execute it on local hardware.

    Currently supported device types
    ----------------------------------
    ``switch``
        Connects to the Tapo smart plug at ``device_ip``, turns it on,
        waits ``duration`` seconds, then turns it off.
    """
    logger.info(
        "Received /execute command: device_type=%s device_ip=%s duration=%.1fs",
        cmd.device_type,
        cmd.device_ip,
        cmd.duration,
    )

    if cmd.device_type == "switch":
        await _tapo_switch_cycle(cmd.device_ip, cmd.duration)
        logger.info(
            "Switch command completed successfully: device_ip=%s duration=%.1fs",
            cmd.device_ip,
            cmd.duration,
        )
        return {"status": "ok", "device_ip": cmd.device_ip, "duration": cmd.duration}

    logger.warning("Unsupported device_type received: %s", cmd.device_type)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unsupported device_type: '{cmd.device_type}'. Supported types: switch",
    )


# ---------------------------------------------------------------------------
# Tapo helpers
# ---------------------------------------------------------------------------


def _tapo_credentials() -> AuthCredential:
    """
    Build Tapo credentials from environment variables.

    Raises ``RuntimeError`` if TAPO_EMAIL or TAPO_PASSWORD are not set,
    so the error surfaces at the moment a command is dispatched rather than
    silently failing at startup.
    """
    if not TAPO_EMAIL.strip() or not TAPO_PASSWORD.strip():
        raise RuntimeError(
            "TAPO_EMAIL and TAPO_PASSWORD environment variables must be set "
            "to control Tapo smart switches."
        )
    return AuthCredential(username=TAPO_EMAIL, password=TAPO_PASSWORD)


async def _tapo_switch_cycle(device_ip: str, duration: float) -> None:
    """
    Authenticate with the Tapo switch at *device_ip*, turn it on, wait
    *duration* seconds, then turn it off.

    The client session is always closed in the ``finally`` block to avoid
    leaking TCP connections across repeated calls.
    """
    credentials = _tapo_credentials()
    client = TapoClient(credentials, (device_ip, TAPO_PORT))
    try:
        await client.login()
        plug = P100Plug(client)

        logger.info("Turning ON Tapo switch at %s", device_ip)
        await plug.turn_on()

        await asyncio.sleep(duration)

        logger.info("Turning OFF Tapo switch at %s (after %.1fs)", device_ip, duration)
        await plug.turn_off()
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# VPS polling loop
# ---------------------------------------------------------------------------


async def poll_commands() -> None:
    """Fetch and dispatch commands from the VPS in a steady polling loop."""
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
                    logger.warning("VPS returned status %s", resp.status_code)
            except httpx.RequestError as exc:
                logger.error("Could not reach VPS: %s", exc)

            await asyncio.sleep(POLL_INTERVAL)


async def dispatch(command: dict) -> None:
    """Route an incoming polled command to the appropriate handler."""
    action = command.get("action", "")
    logger.info("Dispatching polled command: %s", command)

    if action == "tapo_switch":
        await handle_tapo_switch(command)
    elif action == "camera_route":
        await handle_camera_route(command)
    else:
        logger.warning("Unknown command action: '%s'", action)


async def handle_tapo_switch(command: dict) -> None:
    """Handle a polled tapo_switch command."""
    device_ip = command.get("device_ip", "")
    duration = float(command.get("duration", 5.0))
    if not device_ip:
        logger.error("tapo_switch command missing 'device_ip': %s", command)
        return
    try:
        await _tapo_switch_cycle(device_ip, duration)
        logger.info("Polled switch command executed successfully for %s", device_ip)
    except RuntimeError as exc:
        logger.error(
            "Configuration error for Tapo switch at %s (duration=%.1fs): %s",
            device_ip, duration, exc,
        )
    except ConnectionError as exc:
        logger.error(
            "Connection failure for Tapo switch at %s (duration=%.1fs): %s",
            device_ip, duration, exc,
        )
    except Exception as exc:
        logger.error(
            "Unexpected error executing Tapo switch at %s (duration=%.1fs): %s",
            device_ip, duration, exc,
        )


async def handle_camera_route(command: dict) -> None:
    """Stub: Update AI camera routing."""
    target = command.get("target", "")
    logger.info("TODO: reroute camera stream to %s", target)


# ---------------------------------------------------------------------------
# Entry point – run FastAPI server and polling loop concurrently
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Edge agent starting – HTTP server on port %s, polling VPS at %s",
        AGENT_PORT,
        VPS_COMMAND_URL,
    )

    async def _main() -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
        )
        await asyncio.gather(server.serve(), poll_commands())

    asyncio.run(_main())
