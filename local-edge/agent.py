import asyncio
import logging
import os
import socket
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VPS_COMMAND_URL: str = os.environ.get("VPS_COMMAND_URL", "http://vps-cloud:8000/api/edge/commands")

_edge_secret_raw: str | None = os.environ.get("EDGE_SECRET")
if not _edge_secret_raw:
    raise RuntimeError("EDGE_SECRET environment variable is not set.")
EDGE_SECRET: str = _edge_secret_raw

POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "5"))
AGENT_PORT: int = int(os.environ.get("AGENT_PORT", "8080"))
TAPO_PORT: int = int(os.environ.get("TAPO_PORT", "80"))
TAPO_EMAIL: str = os.environ.get("TAPO_EMAIL", "")
TAPO_PASSWORD: str = os.environ.get("TAPO_PASSWORD", "")

CAMERA_REGISTRY = []

# ---------------------------------------------------------------------------
# FastAPI Application & Models
# ---------------------------------------------------------------------------
app = FastAPI(title="Camera Site – Edge Agent")

class TranscodeRequest(BaseModel):
    source_url: str
    output_url: str
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    extra_args: Optional[str] = None

class RegisterStreamRequest(BaseModel):
    name: str
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None
    rtsp_url: Optional[str] = None

# ---------------------------------------------------------------------------
# Dependencies & Helpers
# ---------------------------------------------------------------------------
def _verify_edge_secret(x_edge_secret: str = Header(alias="X-Edge-Secret")) -> None:
    if x_edge_secret != EDGE_SECRET:
        logger.warning("Rejected request with invalid X-Edge-Secret header.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid edge secret.")

def _ping_host(ip: str, port: int = 554, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def _tapo_credentials() -> Tuple[str, str]:
    if not TAPO_EMAIL.strip() or not TAPO_PASSWORD.strip():
        raise RuntimeError("TAPO_EMAIL and TAPO_PASSWORD environment variables must be set.")
    return (TAPO_EMAIL, TAPO_PASSWORD)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok", "role": "tapo-camera-bridge"}

@app.get("/cameras", dependencies=[Depends(_verify_edge_secret)])
def list_cameras(request: Request = None):
    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /cameras from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")
            
    return {"cameras": CAMERA_REGISTRY}

@app.post("/transcode-stream", dependencies=[Depends(_verify_edge_secret)])
def transcode_stream(req: TranscodeRequest, request: Request = None):
    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /transcode-stream from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", req.source_url,
        "-c:v", req.video_codec, "-c:a", req.audio_codec,
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

@app.get("/discover-cameras", dependencies=[Depends(_verify_edge_secret)])
def discover_cameras(subnet: str = "192.168.1.", start: int = 1, end: int = 20, request: Request = None):
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

@app.get("/camera-health", dependencies=[Depends(_verify_edge_secret)])
def camera_health(ip: str, request: Request = None):
    allowlist = os.environ.get("ALLOWLIST", "").split(",") if os.environ.get("ALLOWLIST") else []
    if allowlist and request:
        client_ip = request.client.host
        if client_ip not in allowlist:
            logger.warning(f"Rejected /camera-health from non-allowlisted IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP not allowed.")

    is_up = _ping_host(ip)
    return {"ip": ip, "rtsp_open": is_up}

@app.post("/register-stream", dependencies=[Depends(_verify_edge_secret)])
async def register_stream(req: RegisterStreamRequest):
    if req.tapo_ip:
        username = req.tapo_username or TAPO_EMAIL
        password = req.tapo_password or TAPO_PASSWORD
        if not username or not password:
            return {"error": "Missing Tapo credentials"}
        rtsp_url = f"rtsp://{username}:{password}@{req.tapo_ip}:554/stream1"
    elif req.rtsp_url:
        rtsp_url = req.rtsp_url
    else:
        return {"error": "No camera IP or RTSP URL provided"}

    logger.info(f"Registered stream '{req.name}' → {rtsp_url}")
    return {"status": "ok", "name": req.name, "rtsp_url": rtsp_url}

# ---------------------------------------------------------------------------
# Background Tasks
# ---------------------------------------------------------------------------
def auto_discover_and_register():
    while True:
        logger.info("[Auto] Discovering cameras on subnet...")
        subnet = os.environ.get("DISCOVERY_SUBNET", "192.168.1.")
        start = int(os.environ.get("DISCOVERY_START", 1))
        end = int(os.environ.get("DISCOVERY_END", 20))
        discovered = []
        for i in range(start, end + 1):
            ip = f"{subnet}{i}"
            if _ping_host(ip):
                rtsp_url = f"rtsp://{TAPO_EMAIL}:{TAPO_PASSWORD}@{ip}:554/stream1"
                discovered.append({
                    "name": f"Tapo-{ip}",
                    "ip": ip,
                    "rtsp_url": rtsp_url
                })
        global CAMERA_REGISTRY
        CAMERA_REGISTRY = discovered
        logger.info(f"[Auto] Registered {len(discovered)} cameras.")
        time.sleep(86400)

async def handle_camera_route(command: dict) -> None:
    target = command.get("target", "")
    logger.info("TODO: reroute camera stream to %s", target)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Edge agent starting – HTTP server on port %s (Tapo camera bridge mode)", AGENT_PORT)
    threading.Thread(target=auto_discover_and_register, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")