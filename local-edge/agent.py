# ---------------------------------------------------------------------------
# Camera list/status API for admin panel dropdowns
# ---------------------------------------------------------------------------
from typing import List

# In-memory camera registry (could be replaced with persistent storage)
CAMERA_REGISTRY = []

@app.get("/cameras")
def list_cameras(x_edge_secret: str = Header(alias="X-Edge-Secret"), request: 'Request' = None):
    """
    Return the list of known cameras and their status for admin panel dropdowns.
    """
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected /cameras with invalid X-Edge-Secret header.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid edge secret.")

    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /cameras from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    # For now, just return the in-memory registry (could be enhanced to auto-discover or persist)
    return {"cameras": CAMERA_REGISTRY}
# ---------------------------------------------------------------------------
# Transcoding/format conversion API (ffmpeg-based)
# ---------------------------------------------------------------------------
import subprocess

class TranscodeRequest(BaseModel):
    source_url: str
    output_url: str
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    extra_args: Optional[str] = None

@app.post("/transcode-stream")
def transcode_stream(
    req: TranscodeRequest,
    x_edge_secret: str = Header(alias="X-Edge-Secret"),
    request: 'Request' = None
):
    """
    Start a transcoding process from source_url to output_url using ffmpeg.
    Returns process info or error.
    """
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected /transcode-stream with invalid X-Edge-Secret header.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid edge secret.")

    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /transcode-stream from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", req.source_url,
        "-c:v", req.video_codec,
        "-c:a", req.audio_codec,
        "-f", "rtsp", req.output_url
    ]
    if req.extra_args:
        ffmpeg_cmd.extend(req.extra_args.split())

    try:
        proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info(f"Started ffmpeg transcoding: {' '.join(ffmpeg_cmd)} (pid={proc.pid})")
        return {"status": "started", "pid": proc.pid, "cmd": ffmpeg_cmd}
    except Exception as exc:
        logger.error(f"Failed to start ffmpeg: {exc}")
        return {"error": str(exc)}
# ---------------------------------------------------------------------------
# Camera discovery and health monitoring API
# ---------------------------------------------------------------------------
import socket
import time

from fastapi.responses import JSONResponse

def _ping_host(ip: str, port: int = 554, timeout: float = 1.0) -> bool:
    """Try to open a socket to the given IP/port (RTSP default 554)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

@app.get("/discover-cameras")
def discover_cameras(
    subnet: str = "192.168.1.", start: int = 1, end: int = 20,
    x_edge_secret: str = Header(alias="X-Edge-Secret"),
    request: 'Request' = None
):
    """
    Scan a subnet for Tapo cameras (by attempting RTSP connection).
    Returns a list of IPs and their RTSP port status.
    """
    # Secure: require shared secret
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected /discover-cameras with invalid X-Edge-Secret header.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid edge secret.")

    # Optional: IP allowlist (set ALLOWLIST env var as comma-separated IPs)
    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /discover-cameras from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    results = []
    for i in range(start, end + 1):
        ip = f"{subnet}{i}"
        is_up = _ping_host(ip)
        results.append({"ip": ip, "rtsp_open": is_up})
    return JSONResponse({"cameras": results, "scanned": f"{subnet}{start}-{end}"})

@app.get("/camera-health")
def camera_health(ip: str, x_edge_secret: str = Header(alias="X-Edge-Secret"), request: 'Request' = None):
    """
    Check health/status of a specific camera IP (RTSP port check).
    """
    # Secure: require shared secret
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected /camera-health with invalid X-Edge-Secret header.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid edge secret.")

    # Optional: IP allowlist
    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /camera-health from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    is_up = _ping_host(ip)
    return {"ip": ip, "rtsp_open": is_up}
# ---------------------------------------------------------------------------
# On-demand stream registration API
# ---------------------------------------------------------------------------

from typing import Optional

class RegisterStreamRequest(BaseModel):
    name: str
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None
    rtsp_url: Optional[str] = None

@app.post("/register-stream")
async def register_stream(
    req: RegisterStreamRequest,
    _: None = Depends(_verify_edge_secret),
):
    """
    Register (or relay) a Tapo or RTSP camera stream on demand.
    - If tapo_ip is provided, build RTSP URL from Tapo credentials.
    - If rtsp_url is provided, use it directly.
    Returns the RTSP URL being relayed.
    """
    if req.tapo_ip:
        # Use provided credentials or fallback to agent defaults
        username = req.tapo_username or TAPO_EMAIL
        password = req.tapo_password or TAPO_PASSWORD
        if not username or not password:
            return {"error": "Missing Tapo credentials"}
        # Standard Tapo RTSP path (may need adjustment per model)
        rtsp_url = f"rtsp://{username}:{password}@{req.tapo_ip}:554/stream1"
    elif req.rtsp_url:
        rtsp_url = req.rtsp_url
    else:
        return {"error": "No camera IP or RTSP URL provided"}

    # Here you would start/relay the stream (e.g., via go2rtc, ffmpeg, or a proxy)
    # For now, just return the RTSP URL for confirmation
    # TODO: Integrate with go2rtc or relay process if needed
    logger.info(f"Registered stream '{req.name}' → {rtsp_url}")
    return {"status": "ok", "name": req.name, "rtsp_url": rtsp_url}

"""
agent.py – Local-edge agent for the Camera Site.

This agent is now focused on bridging Tapo cameras to the site.
It is responsible for:
    - Managing Tapo camera credentials and RTSP streaming.
    - Routing/relaying camera streams to the VPS or go2rtc as needed.

Required environment variables
-----------------------------
    EDGE_SECRET    Shared secret for authenticating requests from the VPS.
    TAPO_EMAIL     Tapo cloud account e-mail address.
    TAPO_PASSWORD  Tapo cloud account password.

Optional environment variables
------------------------------
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


# LAN port used to reach Tapo cameras (default 80, configurable).
TAPO_PORT: int = int(os.environ.get("TAPO_PORT", "80"))

# Tapo camera credentials (loaded lazily so missing values are caught at runtime).
TAPO_EMAIL: str = os.environ.get("TAPO_EMAIL", "")
TAPO_PASSWORD: str = os.environ.get("TAPO_PASSWORD", "")

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Camera Site – Edge Agent")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------




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
# Camera bridge endpoints (to be implemented)
# ---------------------------------------------------------------------------

# Example stub endpoint for future camera bridge/relay logic
@app.get("/health")
def health_check():
    return {"status": "ok", "role": "tapo-camera-bridge"}


def _tapo_credentials() -> AuthCredential:
    """
    Build Tapo credentials from environment variables.
    Raises RuntimeError if TAPO_EMAIL or TAPO_PASSWORD are not set.
    """
    if not TAPO_EMAIL.strip() or not TAPO_PASSWORD.strip():
        raise RuntimeError(
            "TAPO_EMAIL and TAPO_PASSWORD environment variables must be set "
            "to access Tapo cameras."
        )
    # Return as a tuple for now; update as needed for camera RTSP auth
    return (TAPO_EMAIL, TAPO_PASSWORD)





async def handle_camera_route(command: dict) -> None:
    """Stub: Update AI camera routing or relay logic."""
    target = command.get("target", "")
    logger.info("TODO: reroute camera stream to %s", target)


# ---------------------------------------------------------------------------
# Entry point – run FastAPI server and polling loop concurrently
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Edge agent starting – HTTP server on port %s (Tapo camera bridge mode)",
        AGENT_PORT,
    )
    # Start auto-discovery in a background thread
    threading.Thread(target=auto_discover_and_register, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")

# --- Auto-discovery and registration background task ---
def auto_discover_and_register():
    import time
    while True:
        logger.info("[Auto] Discovering cameras on subnet...")
        subnet = os.environ.get("DISCOVERY_SUBNET", "192.168.1.")
        start = int(os.environ.get("DISCOVERY_START", 1))
        end = int(os.environ.get("DISCOVERY_END", 20))
        tapo_username = TAPO_EMAIL
        tapo_password = TAPO_PASSWORD
        discovered = []
        for i in range(start, end + 1):
            ip = f"{subnet}{i}"
            if _ping_host(ip):
                # Build RTSP URL
                rtsp_url = f"rtsp://{tapo_username}:{tapo_password}@{ip}:554/stream1"
                discovered.append({
                    "name": f"Tapo-{ip}",
                    "ip": ip,
                    "rtsp_url": rtsp_url
                })
        global CAMERA_REGISTRY
        CAMERA_REGISTRY = discovered
        logger.info(f"[Auto] Registered {len(discovered)} cameras.")
        # Sleep for 24 hours (86400 seconds)
        time.sleep(86400)
