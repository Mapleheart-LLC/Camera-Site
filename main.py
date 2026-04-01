import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (override via environment variables in production)
# ---------------------------------------------------------------------------
SECRET_KEY: str = os.environ.get("SECRET_KEY", "changeme-replace-in-production!!")
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "camera_site.db")
GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")

_DEFAULT_KEY = "changeme-replace-in-production!!"

logger = logging.getLogger(__name__)

# Password hashing context (bcrypt)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the users and cameras tables if they do not already exist."""
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    NOT NULL UNIQUE,
            secret_code     TEXT    NOT NULL,
            has_paid        INTEGER NOT NULL DEFAULT 0,
            allowed_cameras TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT    NOT NULL,
            stream_slug  TEXT    NOT NULL UNIQUE
        )
        """
    )
    conn.commit()
    conn.close()


def get_db():
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception
    return username


# ---------------------------------------------------------------------------
# FastAPI app lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if SECRET_KEY == _DEFAULT_KEY:
        logger.warning(
            "SECRET_KEY is set to the default development value. "
            "Set a strong SECRET_KEY environment variable before deploying to production."
        )
    init_db()
    yield


app = FastAPI(title="Camera Site API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    secret_code: str


class CameraResponse(BaseModel):
    display_name: str
    stream_slug: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/login")
def login(
    request: LoginRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Authenticate a user and return a short-lived JWT access token."""
    user = db.execute(
        "SELECT * FROM users WHERE username = ?",
        (request.username,),
    ).fetchone()

    # Use a constant-time comparison via passlib to prevent timing attacks.
    # If no user is found, run a dummy verify to keep timing consistent.
    stored_hash = user["secret_code"] if user else "$2b$12$invalidhashfortimingneutralityXXXXXXXXXXXXXXXXXXXXXXXX"
    code_matches = pwd_context.verify(request.secret_code, stored_hash)

    if not user or not code_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or secret code",
        )
    if not user["has_paid"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account payment required to access streams",
        )

    token = create_access_token(
        {
            "sub": user["username"],
            "cameras": user["allowed_cameras"],
        },
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/stream")
def get_stream_urls(
    current_user: str = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return WebRTC/WebSocket stream URLs for the authenticated user's cameras."""
    user = db.execute(
        "SELECT allowed_cameras FROM users WHERE username = ?",
        (current_user,),
    ).fetchone()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    cameras = [c.strip() for c in user["allowed_cameras"].split(",") if c.strip()]
    streams = {
        cam: {
            "webrtc_api": f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/webrtc?src={cam}",
            "websocket": f"ws://{GO2RTC_HOST}:{GO2RTC_PORT}/api/ws?src={cam}",
        }
        for cam in cameras
    }
    return JSONResponse({"streams": streams})


@app.get("/api/my-cameras", response_model=list[CameraResponse])
def get_my_cameras(
    current_user: str = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the list of cameras the authenticated user is allowed to access."""
    user = db.execute(
        "SELECT allowed_cameras FROM users WHERE username = ?",
        (current_user,),
    ).fetchone()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    allowed_slugs = [s.strip() for s in user["allowed_cameras"].split(",") if s.strip()]
    if not allowed_slugs:
        return JSONResponse([])

    # Build the IN clause with one '?' placeholder per slug.
    # The placeholders string contains only literal '?' characters; all values
    # are passed as query parameters, so this is safe from SQL injection.
    placeholders = ",".join("?" * len(allowed_slugs))
    rows = db.execute(
        f"SELECT display_name, stream_slug FROM cameras WHERE stream_slug IN ({placeholders})",
        allowed_slugs,
    ).fetchall()

    return JSONResponse([{"display_name": row["display_name"], "stream_slug": row["stream_slug"]} for row in rows])


# ---------------------------------------------------------------------------
# Serve the static frontend (mount last so API routes take priority)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")
