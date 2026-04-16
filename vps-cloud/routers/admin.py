"""
routers/admin.py – Admin-only management endpoints for mochii.live.

All endpoints are protected by HTTP Basic Auth via the ``get_admin_user``
dependency (ADMIN_USERNAME / ADMIN_PASSWORD environment variables).
This auth system is entirely separate from the username/password JWT flow.

Endpoints
---------
  GET    /api/admin/cameras            – list all cameras (full details)
  POST   /api/admin/cameras            – add a new camera
  PUT    /api/admin/cameras/{cam_id}   – update an existing camera
  DELETE /api/admin/cameras/{cam_id}   – remove a camera
  GET    /api/admin/stats              – user/camera counts + recent activations + camera access count
  GET    /api/admin/camera-logs        – 50 most recent camera service access log entries
  POST   /api/admin/control/{device}   – manually trigger an IoT device
  GET    /api/admin/settings           – current env-var / runtime configuration status
  PATCH  /api/admin/settings           – update runtime-configurable settings (e.g. mock_auth)
  GET    /api/admin/store/products     – list all products
  POST   /api/admin/store/products     – create a product
  PUT    /api/admin/store/products/{id} – update a product
  DELETE /api/admin/store/products/{id} – delete a product
  GET    /api/admin/store/orders       – list all orders (most recent first)
  GET    /api/admin/drool/credentials  – show which drool scraper credentials are configured
  PUT    /api/admin/drool/credentials  – save / clear drool scraper credentials
                                         (Reddit API, Reddit IFTTT, Twitter, Bluesky)
  DELETE /api/admin/drool/{entry_id}   – delete a single drool archive entry (+ its comments/reactions)
  POST   /api/admin/drool/purge-bad    – delete all entries whose original_url is not a valid http(s) URL
"""

import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote as _url_quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from db import get_db, get_db_connection, get_setting, set_setting
from dependencies import get_admin_user
from routers.tpe import _send_fcm_to_all

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_DEVICES = {"pishock", "lovense", "pavlok"}

GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")

CF_API_TOKEN: str = os.environ.get("CLAPI", "")
CF_ZONE_ID: str = os.environ.get("CLZONE", "")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# go2rtc helpers
# ---------------------------------------------------------------------------


def _effective_rtsp_url(
    rtsp_url: Optional[str],
    tapo_ip: Optional[str],
    tapo_username: Optional[str],
    tapo_password: Optional[str],
) -> Optional[str]:
    """Return the RTSP URL to use for this camera record."""
    if tapo_ip:
        user = _url_quote(tapo_username or "", safe="")
        pwd  = _url_quote(tapo_password  or "", safe="")
        return f"rtsp://{user}:{pwd}@{tapo_ip}/stream1"
    return rtsp_url or None


def _register_stream(slug: str, rtsp_url: Optional[str]) -> None:
    """Add or update a stream in go2rtc. Failures are logged and not re-raised."""
    if not rtsp_url:
        return
    try:
        with httpx.Client(timeout=5.0) as client:
            client.put(
                f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams",
                params={"name": slug, "src": rtsp_url},
            )
    except Exception as exc:
        logger.warning("Could not register stream '%s' with go2rtc: %s", slug, exc)


def _deregister_stream(slug: str) -> None:
    """Remove a stream from go2rtc. Failures are logged and not re-raised."""
    try:
        with httpx.Client(timeout=5.0) as client:
            client.delete(
                f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/streams",
                params={"name": slug},
            )
    except Exception as exc:
        logger.warning("Could not deregister stream '%s' from go2rtc: %s", slug, exc)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CameraCreate(BaseModel):
    display_name: str
    stream_slug: str
    minimum_access_level: int = 1
    rtsp_url: Optional[str] = None
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None
    rtmp_key: Optional[str] = None
    source_type: Optional[str] = None  # 'tapo', 'rtsp', or 'obs'


class CameraUpdate(BaseModel):
    display_name: Optional[str] = None
    stream_slug: Optional[str] = None
    minimum_access_level: Optional[int] = None
    rtsp_url: Optional[str] = None
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None
    rtmp_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Camera management
# ---------------------------------------------------------------------------


@router.get("/cameras")
def admin_list_cameras(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all cameras with their full database record."""
    rows = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password, rtmp_key
        FROM cameras ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
def admin_add_camera(
    payload: CameraCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Insert a new camera record and register its stream with go2rtc."""
    try:
        cursor = db.execute(
            """
            INSERT INTO cameras
                (display_name, stream_slug, minimum_access_level,
                 rtsp_url, tapo_ip, tapo_username, tapo_password, rtmp_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.display_name,
                payload.stream_slug,
                payload.minimum_access_level,
                payload.rtsp_url or None,
                payload.tapo_ip or None,
                payload.tapo_username or None,
                payload.tapo_password or None,
                payload.rtmp_key or None,
            ),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not add camera: {exc}",
        ) from exc

    # If OBS, auto-generate RTMP key
    rtmp_key = payload.rtmp_key or None
    if getattr(payload, 'source_type', None) == 'obs':
        import secrets
        rtmp_key = secrets.token_hex(16)
        db.execute("UPDATE cameras SET rtmp_key = ? WHERE id = ?", (rtmp_key, cursor.lastrowid))
        db.commit()
        effective_url = f"rtmp://localhost:1935/{rtmp_key}"
    elif rtmp_key:
        effective_url = f"rtmp://localhost:1935/{rtmp_key}"
    else:
        effective_url = _effective_rtsp_url(
            payload.rtsp_url, payload.tapo_ip, payload.tapo_username, payload.tapo_password
        )
    _register_stream(payload.stream_slug, effective_url)

    row = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password, rtmp_key
        FROM cameras WHERE id = ?
        """,
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


@router.put("/cameras/{cam_id}")
def admin_update_camera(
    cam_id: int,
    payload: CameraUpdate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update one or more fields on an existing camera record."""
    row = db.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found.",
        )
    old_slug = row["stream_slug"]

    def _pick(new_val, old_val):
        """Use new_val if provided (normalising empty string to None), else keep old_val."""
        return (new_val or None) if new_val is not None else old_val

    new_name      = payload.display_name         if payload.display_name        is not None else row["display_name"]
    new_slug      = payload.stream_slug          if payload.stream_slug         is not None else row["stream_slug"]
    new_level     = payload.minimum_access_level if payload.minimum_access_level is not None else row["minimum_access_level"]
    new_rtsp      = _pick(payload.rtsp_url,      row["rtsp_url"])
    new_tapo_ip   = _pick(payload.tapo_ip,       row["tapo_ip"])
    new_tapo_user = _pick(payload.tapo_username, row["tapo_username"])
    new_tapo_pass = _pick(payload.tapo_password, row["tapo_password"])
    new_rtmp_key  = _pick(payload.rtmp_key,      row["rtmp_key"])
    try:
        db.execute(
            """
            UPDATE cameras
            SET display_name = ?, stream_slug = ?, minimum_access_level = ?,
                rtsp_url = ?, tapo_ip = ?, tapo_username = ?, tapo_password = ?, rtmp_key = ?
            WHERE id = ?
            """,
            (new_name, new_slug, new_level, new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass, new_rtmp_key, cam_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not update camera: {exc}",
        ) from exc

    # If the slug changed, remove the old stream name from go2rtc first.
    if new_slug != old_slug:
        _deregister_stream(old_slug)

    if new_rtmp_key:
        effective_url = f"rtmp://localhost:1935/{new_rtmp_key}"
    else:
        effective_url = _effective_rtsp_url(new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass)
    _register_stream(new_slug, effective_url)

    updated = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password, rtmp_key
        FROM cameras WHERE id = ?
        """,
        (cam_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/cameras/{cam_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_camera(
    cam_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Remove a camera from the database and deregister its stream from go2rtc."""
    row = db.execute("SELECT stream_slug FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found.",
        )
    slug = row["stream_slug"]
    db.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
    db.commit()
    _deregister_stream(slug)


@router.post("/cameras/{cam_id}/generate-rtmp-key", status_code=status.HTTP_200_OK)
def admin_generate_rtmp_key(
    cam_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Generate a new random RTMP key for the given camera and register the stream."""
    row = db.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    rtmp_key = secrets.token_hex(16)
    db.execute("UPDATE cameras SET rtmp_key = ? WHERE id = ?", (rtmp_key, cam_id))
    db.commit()
    # Deregister any existing source then register RTMP
    _deregister_stream(row["stream_slug"])
    _register_stream(row["stream_slug"], f"rtmp://localhost:1935/{rtmp_key}")
    return {"rtmp_key": rtmp_key}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats")
def admin_stats(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return a summary of registered users, cameras, IoT activations, and camera service accesses."""
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_cameras = db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    total_camera_accesses = db.execute("SELECT COUNT(*) FROM camera_service_logs").fetchone()[0]
    recent = db.execute(
        """
        SELECT device, actor, activated_at
        FROM activations
        ORDER BY id DESC
        LIMIT 20
        """
    ).fetchall()
    return {
        "total_users": total_users,
        "total_cameras": total_cameras,
        "total_camera_accesses": total_camera_accesses,
        "recent_activations": [dict(r) for r in recent],
    }


@router.get("/camera-logs")
def admin_camera_logs(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the 50 most recent camera service access log entries."""
    rows = db.execute(
        """
        SELECT user_id, access_level, camera_count, accessed_at
        FROM camera_service_logs
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Manual IoT control (admin-only, no rate limit)
# ---------------------------------------------------------------------------


@router.post("/control/{device}")
def admin_control_device(
    device: str,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Manually trigger an IoT device without rate limiting or an auth JWT.

    Lovense and Pavlok commands are forwarded to the paired TPE app via FCM.
    PiShock uses a direct connection (not relayed through the app).

    Logs the activation to the ``activations`` table with the admin username
    as the actor so it appears in the stats history.
    """
    if device not in _VALID_DEVICES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown device '{device}'. Valid options: {sorted(_VALID_DEVICES)}",
        )
    db.execute(
        "INSERT INTO activations (device, actor, activated_at) VALUES (?, ?, ?)",
        (device, f"admin:{admin_user}", datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    if device == "lovense":
        _send_fcm_to_all(db, {
            "action":      "LOVENSE_COMMAND",
            "toy_command": "vibrate",
            "toy_level":   "10",
        })
        message = "Command forwarded to app."
    elif device == "pavlok":
        _send_fcm_to_all(db, {
            "action":             "PAVLOK_COMMAND",
            "pavlok_cmd":         "vibrate",
            "pavlok_intensity":   "100",
            "pavlok_duration_ms": "5000",
        })
        message = "Command forwarded to app."
    else:
        message = "Admin command accepted (mock response)."

    return {
        "status": "ok",
        "device": device,
        "message": message,
        "triggered_by": admin_user,
    }


# ---------------------------------------------------------------------------
# Puppy Pouch – admin question management
# ---------------------------------------------------------------------------

# Maximum tweet body length for answer text (Twitter limit is 280; reserve
# space for a newline + the share URL which Twitter counts as ~23 chars).
_TWEET_MAX_TEXT = 250


def _post_answer_tweet(question_id: str, answer_text: str) -> bool:
    """Post the answer as a tweet using the stored OAuth 2.0 access token.

    Attempts a single token refresh if the first request returns 401.
    Returns True on success, False on any failure (non-blocking).
    """
    db_key_token   = "drool_twitter_oauth2_access_token"
    db_key_refresh = "drool_twitter_oauth2_refresh_token"
    db_key_client_id     = "drool_twitter_client_id"
    db_key_client_secret = "drool_twitter_client_secret"

    def _load(db_key: str, env_key: str = "") -> str:
        try:
            conn = get_db_connection()
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (db_key,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return os.environ.get(env_key, "") if env_key else ""

    def _refresh_token() -> str:
        client_id     = _load(db_key_client_id,     "TWITTER_CLIENT_ID")
        client_secret = _load(db_key_client_secret, "TWITTER_CLIENT_SECRET")
        refresh_tok   = _load(db_key_refresh)
        if not (client_id and client_secret and refresh_tok):
            return ""
        try:
            import base64
            creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            r = httpx.post(
                "https://api.twitter.com/2/oauth2/token",
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
            new_access  = data.get("access_token", "")
            new_refresh = data.get("refresh_token", "")
            if new_access:
                conn = get_db_connection()
                set_setting(conn, db_key_token, new_access)
                if new_refresh:
                    set_setting(conn, db_key_refresh, new_refresh)
                conn.commit()
                conn.close()
            return new_access
        except Exception as exc:
            logger.warning("Tweet post: token refresh failed: %s", exc)
            return ""

    def _do_post(token: str, text: str) -> int:
        try:
            r = httpx.post(
                "https://api.twitter.com/2/tweets",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"text": text},
                timeout=15.0,
            )
            return r.status_code
        except Exception as exc:
            logger.warning("Tweet post HTTP error: %s", exc)
            return 0

    access_token = _load(db_key_token)
    if not access_token:
        logger.debug("Tweet post: no OAuth 2.0 access token configured.")
        return False

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("Tweet post: BASE_URL not set – cannot build an absolute share URL; skipping tweet.")
        return False
    share_url = f"{base_url}/q/{question_id}"

    truncated = answer_text[:_TWEET_MAX_TEXT] + "..." if len(answer_text) > _TWEET_MAX_TEXT else answer_text
    tweet_text = f"{truncated}\n\n{share_url}"

    status_code = _do_post(access_token, tweet_text)
    if status_code == 401:
        # Token likely expired – refresh once and retry.
        new_token = _refresh_token()
        if new_token:
            status_code = _do_post(new_token, tweet_text)

    if status_code in (200, 201):
        logger.info("Answer tweeted for question %s", question_id)
        return True

    logger.warning("Tweet post failed (status %s) for question %s", status_code, question_id)
    return False


class AnswerPayload(BaseModel):
    answer: str


@router.get("/questions")
def admin_list_unanswered_questions(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all questions that have not yet been answered."""
    rows = db.execute(
        """
        SELECT id, text, created_at
        FROM questions
        WHERE answer IS NULL
        ORDER BY created_at ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/questions/answered")
def admin_list_answered_questions(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all questions that have already been answered."""
    rows = db.execute(
        """
        SELECT id, text, answer, is_public, created_at
        FROM questions
        WHERE answer IS NOT NULL
        ORDER BY created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/questions/{question_id}/answer", status_code=status.HTTP_200_OK)
def admin_answer_question(
    question_id: str,
    payload: AnswerPayload,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Save an answer to the specified question and mark it as public."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute(
        "UPDATE questions SET answer = ?, is_public = 1 WHERE id = ?",
        (payload.answer, question_id),
    )
    db.commit()
    tweeted = _post_answer_tweet(question_id, payload.answer)
    return {
        "id": question_id,
        "message": "Answer saved and question is now public 🐾",
        "tweeted": tweeted,
    }
  
  def _post_answer_bluesky(question_id: str, answer_text: str) -> bool:
    """Post the answer to Bluesky using app password."""
    def _load(db_key: str, env_key: str = "") -> str:
        try:
            conn = get_db_connection()
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (db_key,)).fetchone()
            conn.close()
            if row and row[0]: return row[0]
        except Exception: pass
        return os.environ.get(env_key, "") if env_key else ""

    handle = _load("drool_bsky_handle", "BSKY_HANDLE")
    app_password = _load("drool_bsky_app_password", "BSKY_APP_PASSWORD")

    if not handle or not app_password:
        logger.debug("Bluesky post: no handle or app password configured.")
        return False

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("Bluesky post: BASE_URL not set – cannot build an absolute share URL; skipping post.")
        return False
        
    share_url = f"{base_url}/q/{question_id}"
    
    # Bluesky limit is 300. Reserve space for URL and newlines.
    max_text = 250
    truncated = answer_text[:max_text] + "..." if len(answer_text) > max_text else answer_text
    post_text = f"{truncated}\n\n{share_url}"

    try:
        import httpx
        from datetime import datetime, timezone
        
        # 1. Authenticate to create a session
        r_auth = httpx.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": app_password},
            timeout=10.0
        )
        r_auth.raise_for_status()
        session = r_auth.json()
        
        # 2. Build the record (calculating byte offsets for the clickable link)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        text_bytes = post_text.encode('utf-8')
        url_bytes = share_url.encode('utf-8')
        byte_start = text_bytes.find(url_bytes)
        byte_end = byte_start + len(url_bytes)

        record = {
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "createdAt": now,
            "facets": [{
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": share_url}]
            }]
        }

        # 3. Post the record
        r_post = httpx.post(
            "
          


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_question(
    question_id: str,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Permanently delete a question."""
    row = db.execute(
        "SELECT id FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found.",
        )
    db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Danger Zone – runtime configuration / environment variable settings
# ---------------------------------------------------------------------------

_DEFAULT_SECRET_KEY = "changeme-replace-in-production!!"
_DEMO_SECRET_KEY = "demo-mode-insecure-do-not-use-in-production"


class SettingsPatch(BaseModel):
    mock_auth: Optional[bool] = None


@router.get("/settings")
def get_settings(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the current status of all important runtime / environment settings.

    Security-sensitive values (SECRET_KEY, passwords, OAuth secrets) are
    never returned – only a *status* string indicating whether they are set
    to a known-insecure default or a custom value.
    """
    secret_key = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY", "")
    if not secret_key or secret_key == _DEFAULT_SECRET_KEY:
        sk_status = "default"
    elif secret_key == _DEMO_SECRET_KEY:
        sk_status = "demo"
    else:
        sk_status = "custom"

    mock_env_raw = os.environ.get("MOCK_AUTH", "").lower()
    mock_auth_env = mock_env_raw == "true"

    db_mock_raw = get_setting(db, "mock_auth")
    mock_auth_db: Optional[bool] = None if db_mock_raw is None else (db_mock_raw.lower() == "true")
    mock_auth_effective = mock_auth_db if mock_auth_db is not None else mock_auth_env

    return {
        "secret_key_status": sk_status,
        "mock_auth_env": mock_auth_env,
        "mock_auth_db": mock_auth_db,
        "mock_auth_effective": mock_auth_effective,
        "discord_guild_id_set": bool(os.environ.get("DISCORD_GUILD_ID")),
        "discord_bot_token_set": bool(os.environ.get("DISCORD_BOT_TOKEN")),
        "discord_role_level_1_set": bool(os.environ.get("DISCORD_ROLE_LEVEL_1")),
        "discord_role_level_2_set": bool(os.environ.get("DISCORD_ROLE_LEVEL_2")),
        "discord_role_level_3_set": bool(os.environ.get("DISCORD_ROLE_LEVEL_3")),
        "admin_configured": (
            bool(os.environ.get("ADMIN_USERNAME"))
            and bool(os.environ.get("ADMIN_PASSWORD"))
        ),
        "go2rtc_host": os.environ.get("GO2RTC_HOST", "localhost"),
        "go2rtc_port": os.environ.get("GO2RTC_PORT", "1984"),
    }


@router.patch("/settings", status_code=status.HTTP_200_OK)
def patch_settings(
    body: SettingsPatch,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update runtime-configurable settings stored in the database.

    Only ``mock_auth`` may be changed here; it overrides the MOCK_AUTH
    environment variable without requiring a container restart.  This lets
    operators switch from demo mode to production mode on the fly.

    Security-critical settings (SECRET_KEY, OAuth credentials, admin
    password) must be changed via environment variables and a container
    restart – they are intentionally not exposed through this endpoint.
    """
    updated: list[str] = []
    if body.mock_auth is not None:
        set_setting(db, "mock_auth", "true" if body.mock_auth else "false")
        updated.append("mock_auth")
        logger.info(
            "Admin '%s' set mock_auth=%s via Danger Zone settings.",
            admin_user,
            body.mock_auth,
        )
    return {"updated": updated}


# ---------------------------------------------------------------------------
# Store – product management
# ---------------------------------------------------------------------------


class ProductCreate(BaseModel):
    name: str
    description: Optional[str] = None
    price: float
    image_url: Optional[str] = None
    is_printful: bool = False
    printful_variant_id: Optional[str] = None
    stock_count: Optional[int] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    image_url: Optional[str] = None
    is_printful: Optional[bool] = None
    printful_variant_id: Optional[str] = None
    stock_count: Optional[int] = None


@router.get("/store/products")
def admin_list_products(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all products (including out-of-stock ones)."""
    rows = db.execute(
        """
        SELECT id, name, description, price, image_url,
               is_printful, printful_variant_id, stock_count
          FROM products ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("/store/products", status_code=status.HTTP_201_CREATED)
def admin_create_product(
    payload: ProductCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new product."""
    try:
        cursor = db.execute(
            """
            INSERT INTO products
                (name, description, price, image_url,
                 is_printful, printful_variant_id, stock_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.description or None,
                payload.price,
                payload.image_url or None,
                1 if payload.is_printful else 0,
                payload.printful_variant_id or None,
                payload.stock_count,
            ),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not create product: {exc}",
        ) from exc
    row = db.execute(
        "SELECT id, name, description, price, image_url, is_printful, printful_variant_id, stock_count FROM products WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


@router.put("/store/products/{product_id}")
def admin_update_product(
    product_id: int,
    payload: ProductUpdate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update an existing product."""
    row = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    new_name = payload.name if payload.name is not None else row["name"]
    new_desc = payload.description if payload.description is not None else row["description"]
    new_price = payload.price if payload.price is not None else row["price"]
    new_image = payload.image_url if payload.image_url is not None else row["image_url"]
    new_printful = payload.is_printful if payload.is_printful is not None else bool(row["is_printful"])
    new_variant = payload.printful_variant_id if payload.printful_variant_id is not None else row["printful_variant_id"]
    new_stock = payload.stock_count if payload.stock_count is not None else row["stock_count"]

    try:
        db.execute(
            """
            UPDATE products
               SET name = ?, description = ?, price = ?, image_url = ?,
                   is_printful = ?, printful_variant_id = ?, stock_count = ?
             WHERE id = ?
            """,
            (new_name, new_desc or None, new_price, new_image or None,
             1 if new_printful else 0, new_variant or None, new_stock, product_id),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not update product: {exc}",
        ) from exc

    updated = db.execute(
        "SELECT id, name, description, price, image_url, is_printful, printful_variant_id, stock_count FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    return dict(updated)


@router.delete("/store/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_product(
    product_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a product. Fails if the product has associated orders."""
    row = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    in_use = db.execute(
        "SELECT 1 FROM order_items WHERE product_id = ? LIMIT 1", (product_id,)
    ).fetchone()
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a product that has existing orders.",
        )
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Store – order management
# ---------------------------------------------------------------------------


@router.get("/store/orders")
def admin_list_orders(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all orders, most recent first, with their line items."""
    orders = db.execute(
        """
        SELECT id, external_transaction_id, provider_name, status,
               customer_email, total_amount, shipping_address, created_at
          FROM orders
         ORDER BY created_at DESC
        """
    ).fetchall()

    result = []
    for order in orders:
        o = dict(order)
        items = db.execute(
            """
            SELECT oi.quantity, oi.unit_price, p.id AS product_id, p.name AS product_name
              FROM order_items oi
              JOIN products p ON p.id = oi.product_id
             WHERE oi.order_id = ?
            """,
            (o["id"],),
        ).fetchall()
        o["items"] = [dict(item) for item in items]
        result.append(o)
    return result


# ---------------------------------------------------------------------------
# Drool Log – scraper credentials management
# ---------------------------------------------------------------------------

# Mapping: request field → (settings_table_key, env_var_fallback, is_secret)
_DROOL_CRED_MAP: dict[str, tuple[str, str, bool]] = {
    # Per-platform on/off toggles – '1' = enabled (default), '0' = disabled
    "reddit_enabled":  ("drool_reddit_enabled",  "REDDIT_ENABLED",  False),
    "twitter_enabled": ("drool_twitter_enabled",  "TWITTER_ENABLED", False),
    "bsky_enabled":    ("drool_bsky_enabled",     "BSKY_ENABLED",    False),
    # Reddit mode toggle – value is 'api' (default), 'ifttt', or 'gsheet'
    "reddit_mode":           ("drool_reddit_mode",           "REDDIT_MODE",           False),
    # Reddit API credentials (used when reddit_mode == 'api')
    "reddit_client_id":      ("drool_reddit_client_id",      "REDDIT_CLIENT_ID",      False),
    "reddit_client_secret":  ("drool_reddit_client_secret",  "REDDIT_CLIENT_SECRET",  True),
    "reddit_username":       ("drool_reddit_username",       "REDDIT_USERNAME",       False),
    "reddit_password":       ("drool_reddit_password",       "REDDIT_PASSWORD",       True),
    "reddit_user_agent":     ("drool_reddit_user_agent",     "REDDIT_USER_AGENT",     False),
    # Reddit IFTTT secret (used when reddit_mode == 'ifttt')
    "reddit_ifttt_secret":   ("drool_reddit_ifttt_secret",   "REDDIT_IFTTT_SECRET",   True),
    # Google Sheets CSV export URL (used when reddit_mode == 'gsheet')
    # Share the sheet as "Anyone with the link can view", then:
    #   File → Share → Publish to web → CSV → copy the link
    "reddit_gsheet_csv_url":   ("drool_reddit_gsheet_csv_url",   "REDDIT_GSHEET_CSV_URL",   False),
    # Second sheet URL – IFTTT needs a separate applet for upvotes vs saves,
    # which write to different sheets.  Leave blank if only one sheet is in use.
    "reddit_gsheet_csv_url_2": ("drool_reddit_gsheet_csv_url_2", "REDDIT_GSHEET_CSV_URL_2", False),
    "twitter_user_id":            ("drool_twitter_user_id",              "TWITTER_USER_ID",        False),
    # OAuth 2.0 credentials (set Client ID + Secret first, then use the
    # "Connect Twitter/X" button to complete the PKCE flow and populate the
    # access/refresh tokens and user ID automatically).
    "twitter_client_id":          ("drool_twitter_client_id",            "TWITTER_CLIENT_ID",      False),
    "twitter_client_secret":      ("drool_twitter_client_secret",        "TWITTER_CLIENT_SECRET",  True),
    "twitter_oauth2_access_token":  ("drool_twitter_oauth2_access_token",  "",                     True),
    "twitter_oauth2_refresh_token": ("drool_twitter_oauth2_refresh_token", "",                     True),
    "bsky_handle":           ("drool_bsky_handle",           "BSKY_HANDLE",           False),
    "bsky_app_password":     ("drool_bsky_app_password",     "BSKY_APP_PASSWORD",     True),
}

# Fields for which the actual value (not just set/not-set) is safe to expose
# in the GET response because they are non-sensitive identifiers, mode flags,
# or public URLs.
_DROOL_CRED_EXPOSE_VALUE: set[str] = {
    "reddit_enabled",
    "twitter_enabled",
    "bsky_enabled",
    "reddit_mode",
    "reddit_username",
    "reddit_gsheet_csv_url",
    "reddit_gsheet_csv_url_2",
    "twitter_user_id",
    "bsky_handle",
}

_DROOL_ENABLED_FIELDS: frozenset[str] = frozenset({
    "reddit_enabled", "twitter_enabled", "bsky_enabled",
})

_REDDIT_MODE_DEFAULT = "api"  # valid values: 'api', 'ifttt', 'gsheet'


class DroolCredsUpdate(BaseModel):
    reddit_enabled:  Optional[str] = None  # '1' = enabled (default), '0' = disabled
    twitter_enabled: Optional[str] = None
    bsky_enabled:    Optional[str] = None
    reddit_mode:           Optional[str] = None  # 'api', 'ifttt', or 'gsheet'
    reddit_client_id:      Optional[str] = None
    reddit_client_secret:  Optional[str] = None
    reddit_username:       Optional[str] = None
    reddit_password:       Optional[str] = None
    reddit_user_agent:     Optional[str] = None
    reddit_ifttt_secret:   Optional[str] = None
    reddit_gsheet_csv_url:   Optional[str] = None
    reddit_gsheet_csv_url_2: Optional[str] = None
    twitter_user_id:               Optional[str] = None
    twitter_client_id:             Optional[str] = None
    twitter_client_secret:         Optional[str] = None
    twitter_oauth2_access_token:   Optional[str] = None
    twitter_oauth2_refresh_token:  Optional[str] = None
    bsky_handle:                   Optional[str] = None
    bsky_app_password:             Optional[str] = None


@router.get("/drool/credentials")
def get_drool_credentials(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return the configuration status of all drool scraper credentials.

    Actual credential values are never returned – only whether each field
    is set (and from which source: 'db', 'env', or 'none').
    """
    result: dict[str, dict] = {}
    for field, (db_key, env_key, _is_secret) in _DROOL_CRED_MAP.items():
        db_row = db.execute(
            "SELECT value FROM settings WHERE key = ?", (db_key,)
        ).fetchone()
        db_val = db_row["value"] if db_row else None
        env_val = os.environ.get(env_key, "")
        source = "db" if db_val else ("env" if env_val else "none")
        entry: dict = {
            "db_set":        bool(db_val),
            "env_set":       bool(env_val),
            "effective_set": bool(db_val or env_val),
            "source":        source,
        }
        # For non-sensitive mode flags, also return the actual current value.
        # reddit_mode defaults to 'api'; all other exposed fields default to "".
        if field in _DROOL_CRED_EXPOSE_VALUE:
            entry["value"] = db_val or env_val or (
                _REDDIT_MODE_DEFAULT if field == "reddit_mode" else ""
            )
        result[field] = entry
    return result


@router.put("/drool/credentials", status_code=status.HTTP_200_OK)
def put_drool_credentials(
    payload: DroolCredsUpdate,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Save drool scraper credentials to the database settings table.

    Pass an empty string for a field to clear it (removing the DB override
    so the env-var fallback takes effect).  Omit a field (null) to leave it
    unchanged.
    """
    updated: list[str] = []
    for field, (db_key, _env_key, _is_secret) in _DROOL_CRED_MAP.items():
        value = getattr(payload, field, None)
        if value is None:
            continue  # not provided – leave unchanged
        # Validate the mode field
        if field == "reddit_mode" and value not in (_REDDIT_MODE_DEFAULT, "ifttt", "gsheet", ""):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="reddit_mode must be 'api', 'ifttt', or 'gsheet'.",
            )
        # Validate enabled flags
        if field in _DROOL_ENABLED_FIELDS and value not in ("0", "1", ""):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} must be '0' or '1'.",
            )
        if value == "":
            # Empty string → delete the DB entry (revert to env fallback)
            db.execute("DELETE FROM settings WHERE key = ?", (db_key,))
        else:
            set_setting(db, db_key, value)
        updated.append(field)
    if updated:
        db.commit()
        logger.info(
            "Admin '%s' updated drool credentials: %s",
            admin_user,
            ", ".join(updated),
        )
    return {"updated": updated}


@router.delete("/drool/{entry_id}", status_code=status.HTTP_200_OK)
def delete_drool_entry(
    entry_id: int,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a single drool archive entry and all its comments and reactions."""
    row = db.execute("SELECT id FROM drool_archive WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found.")
    db.execute("DELETE FROM drool_reactions WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_comments WHERE drool_id = ?", (entry_id,))
    db.execute("DELETE FROM drool_archive WHERE id = ?", (entry_id,))
    db.commit()
    logger.info("Admin '%s' deleted drool entry #%d.", admin_user, entry_id)
    return {"deleted": entry_id}


@router.post("/drool/purge-bad", status_code=status.HTTP_200_OK)
def purge_bad_drool_entries(
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete all drool archive entries whose original_url is not a valid http(s):// URL.

    This cleans up garbage rows created when the Google Sheets CSV URL was
    misconfigured and returned an HTML page that was mistakenly parsed as CSV
    data (causing JavaScript fragments to be stored as post URLs/titles).
    """
    bad_rows = db.execute(
        "SELECT id FROM drool_archive WHERE original_url NOT LIKE 'http://%' AND original_url NOT LIKE 'https://%'"
    ).fetchall()
    bad_ids = [row["id"] for row in bad_rows]
    if not bad_ids:
        return {"deleted": 0, "ids": []}
    placeholders = ",".join("?" * len(bad_ids))
    db.execute(f"DELETE FROM drool_reactions WHERE drool_id IN ({placeholders})", bad_ids)
    db.execute(f"DELETE FROM drool_comments WHERE drool_id IN ({placeholders})", bad_ids)
    db.execute(f"DELETE FROM drool_archive WHERE id IN ({placeholders})", bad_ids)
    db.commit()
    logger.info(
        "Admin '%s' purged %d bad drool entries (non-http URLs): %s",
        admin_user,
        len(bad_ids),
        bad_ids,
    )
    return {"deleted": len(bad_ids), "ids": bad_ids}



# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


class _CreateUserPayload(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    access_level: int = Field(0, ge=0, le=3)


class _UpdateUserPayload(BaseModel):
    password: Optional[str] = Field(None, min_length=8, max_length=128)
    access_level: Optional[int] = Field(None, ge=0, le=3)


@router.get("/users")
def list_users(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return all registered site users (no password hashes)."""
    rows = db.execute(
        "SELECT id, username, access_level FROM users ORDER BY rowid DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/users", status_code=201)
def create_user(
    body: _CreateUserPayload,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new site user account."""
    username_lower = body.username.lower()
    existing = db.execute(
        "SELECT id FROM users WHERE username = ?", (username_lower,)
    ).fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken.",
        )
    user_id = secrets.token_hex(16)
    pw_hash = _pwd_context.hash(body.password)
    db.execute(
        "INSERT INTO users (id, username, password_hash, access_level) VALUES (?, ?, ?, ?)",
        (user_id, username_lower, pw_hash, body.access_level),
    )
    db.commit()
    logger.info("Admin '%s' created user: username=%s id=%s", admin_user, username_lower, user_id)
    return {"id": user_id, "username": username_lower, "access_level": body.access_level}


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: _UpdateUserPayload,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update a user's password and/or access_level."""
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if body.password is not None:
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_pwd_context.hash(body.password), user_id),
        )
    if body.access_level is not None:
        db.execute(
            "UPDATE users SET access_level = ? WHERE id = ?",
            (body.access_level, user_id),
        )
    db.commit()
    logger.info("Admin '%s' updated user id=%s", admin_user, user_id)
    return {"updated": user_id}


@router.delete("/users/{user_id}", status_code=200)
def delete_user(
    user_id: str,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Delete a user account and their linked Discord entry."""
    row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    db.execute("UPDATE discord_accounts SET user_id = NULL WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    logger.info("Admin '%s' deleted user id=%s", admin_user, user_id)
    return {"deleted": user_id}


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics")
def get_analytics(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return aggregated analytics across users, cameras, store, Q&A, activations, and drool."""
    # ── Users ──────────────────────────────────────────────────────────────
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    user_levels = db.execute(
        "SELECT access_level, COUNT(*) AS cnt FROM users GROUP BY access_level"
    ).fetchall()
    by_access_level = {str(r["access_level"]): r["cnt"] for r in user_levels}

    # ── Camera access (last 30 days) ───────────────────────────────────────
    cam_total_30d = db.execute(
        "SELECT COUNT(*) FROM camera_service_logs WHERE accessed_at >= date('now','-30 days')"
    ).fetchone()[0]
    cam_by_day = db.execute(
        """
        SELECT substr(accessed_at, 1, 10) AS day, COUNT(*) AS cnt
          FROM camera_service_logs
         WHERE accessed_at >= date('now','-30 days')
         GROUP BY day ORDER BY day
        """
    ).fetchall()
    cam_by_level = db.execute(
        "SELECT access_level, COUNT(*) AS cnt FROM camera_service_logs GROUP BY access_level"
    ).fetchall()

    # ── Activations (last 30 days) ─────────────────────────────────────────
    act_total_30d = db.execute(
        "SELECT COUNT(*) FROM activations WHERE activated_at >= date('now','-30 days')"
    ).fetchone()[0]
    act_by_day = db.execute(
        """
        SELECT substr(activated_at, 1, 10) AS day, COUNT(*) AS cnt
          FROM activations
         WHERE activated_at >= date('now','-30 days')
         GROUP BY day ORDER BY day
        """
    ).fetchall()
    act_by_device = db.execute(
        "SELECT device, COUNT(*) AS cnt FROM activations GROUP BY device"
    ).fetchall()

    # ── Q&A ────────────────────────────────────────────────────────────────
    q_total = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    q_answered = db.execute("SELECT COUNT(*) FROM questions WHERE answer IS NOT NULL").fetchone()[0]
    q_by_day = db.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt
          FROM questions
         WHERE created_at >= date('now','-30 days')
         GROUP BY day ORDER BY day
        """
    ).fetchall()

    # ── Orders / revenue (last 30 days) ────────────────────────────────────
    orders_total = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    revenue_total_row = db.execute(
        "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status = 'paid'"
    ).fetchone()
    revenue_total = round(float(revenue_total_row[0]), 2)
    revenue_30d_row = db.execute(
        "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE status = 'paid' AND created_at >= date('now','-30 days')"
    ).fetchone()
    revenue_30d = round(float(revenue_30d_row[0]), 2)
    orders_by_status = db.execute(
        "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"
    ).fetchall()
    orders_by_day = db.execute(
        """
        SELECT substr(created_at, 1, 10) AS day,
               COUNT(*) AS cnt,
               ROUND(COALESCE(SUM(CASE WHEN status='paid' THEN total_amount ELSE 0 END), 0), 2) AS revenue
          FROM orders
         WHERE created_at >= date('now','-30 days')
         GROUP BY day ORDER BY day
        """
    ).fetchall()

    # ── Drool archive ───────────────────────────────────────────────────────
    drool_total = db.execute("SELECT COUNT(*) FROM drool_archive").fetchone()[0]
    drool_views_row = db.execute(
        "SELECT COALESCE(SUM(view_count), 0) FROM drool_archive"
    ).fetchone()
    drool_total_views = int(drool_views_row[0])
    drool_by_platform = db.execute(
        "SELECT platform, COUNT(*) AS cnt FROM drool_archive GROUP BY platform"
    ).fetchall()
    drool_by_day = db.execute(
        """
        SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS cnt
          FROM drool_archive
         WHERE timestamp >= date('now','-30 days')
         GROUP BY day ORDER BY day
        """
    ).fetchall()

    return {
        "users": {
            "total": total_users,
            "by_access_level": by_access_level,
        },
        "camera_access": {
            "total_30d": cam_total_30d,
            "by_day_30d": [{"date": r["day"], "count": r["cnt"]} for r in cam_by_day],
            "by_access_level": {str(r["access_level"]): r["cnt"] for r in cam_by_level},
        },
        "activations": {
            "total_30d": act_total_30d,
            "by_day_30d": [{"date": r["day"], "count": r["cnt"]} for r in act_by_day],
            "by_device": {r["device"]: r["cnt"] for r in act_by_device},
        },
        "questions": {
            "total": q_total,
            "answered": q_answered,
            "unanswered": q_total - q_answered,
            "by_day_30d": [{"date": r["day"], "count": r["cnt"]} for r in q_by_day],
        },
        "orders": {
            "total": orders_total,
            "revenue_total": revenue_total,
            "revenue_30d": revenue_30d,
            "by_status": {r["status"]: r["cnt"] for r in orders_by_status},
            "by_day_30d": [
                {"date": r["day"], "count": r["cnt"], "revenue": r["revenue"]}
                for r in orders_by_day
            ],
        },
        "drool": {
            "total": drool_total,
            "total_views": drool_total_views,
            "by_platform": {r["platform"]: r["cnt"] for r in drool_by_platform},
            "by_day_30d": [{"date": r["day"], "count": r["cnt"]} for r in drool_by_day],
        },
    }


@router.get("/analytics/cloudflare")
def get_cloudflare_analytics(
    _: str = Depends(get_admin_user),
):
    """Fetch 30-day Zone analytics from the Cloudflare Analytics API.

    Returns ``{"configured": False}`` when ``CLAPI`` or ``CLZONE``
    are not set.  The time-series is aggregated to daily buckets so the
    frontend can render a consistent chart regardless of Cloudflare's chosen
    granularity (hourly or daily depending on the query window).

    Requires the API token to have *Zone → Analytics → Read* and
    *Zone → Zone → Read* permissions.
    """
    if not CF_API_TOKEN or not CF_ZONE_ID:
        return {"configured": False}

    # since=-43200 → 43,200 minutes ago (30 days); Cloudflare expects a negative integer of minutes
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/analytics/dashboard",
                params={"since": -43200, "continuous": True},
                headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            )
    except Exception as exc:
        logger.warning("Cloudflare analytics request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Cloudflare API.",
        )

    if resp.status_code == 403:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudflare API returned 403 – check CLAPI permissions.",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudflare API returned HTTP {resp.status_code}.",
        )

    body = resp.json()
    if not body.get("success"):
        errors = body.get("errors", [])
        detail = errors[0].get("message", "Unknown error") if errors else "Unknown error"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudflare API error: {detail}",
        )

    result = body.get("result", {})
    totals = result.get("totals", {})

    # Aggregate time-series buckets to daily granularity
    by_day: dict[str, dict] = {}
    for bucket in result.get("timeseries", []):
        day = (bucket.get("since") or "")[:10]
        if not day:
            continue
        if day not in by_day:
            by_day[day] = {"requests": 0, "pageviews": 0, "bandwidth": 0, "threats": 0, "uniques": 0}
        by_day[day]["requests"]  += bucket.get("requests",  {}).get("all", 0)
        by_day[day]["pageviews"] += bucket.get("pageviews", {}).get("all", 0)
        by_day[day]["bandwidth"] += bucket.get("bandwidth", {}).get("all", 0)
        by_day[day]["threats"]   += bucket.get("threats",   {}).get("all", 0)
        by_day[day]["uniques"]   += bucket.get("uniques",   {}).get("all", 0)

    return {
        "configured": True,
        "totals": {
            "requests":  totals.get("requests",  {}).get("all", 0),
            "pageviews": totals.get("pageviews", {}).get("all", 0),
            "bandwidth": totals.get("bandwidth", {}).get("all", 0),
            "threats":   totals.get("threats",   {}).get("all", 0),
            "uniques":   totals.get("uniques",   {}).get("all", 0),
        },
        "by_day": [
            {
                "date":      d,
                "requests":  by_day[d]["requests"],
                "pageviews": by_day[d]["pageviews"],
                "bandwidth": by_day[d]["bandwidth"],
                "threats":   by_day[d]["threats"],
                "uniques":   by_day[d]["uniques"],
            }
            for d in sorted(by_day.keys())
        ],
    }


# ---------------------------------------------------------------------------
# Discord bot – settings & status
# ---------------------------------------------------------------------------


def _bool_setting(db: sqlite3.Connection, key: str, default: Optional[bool] = None) -> Optional[bool]:
    val = get_setting(db, key)
    if val is None:
        return default
    return val.strip().lower() == "true"


class _DiscordSettingsPatch(BaseModel):
    discord_question_channel_id: Optional[str] = None
    discord_notification_channel_id: Optional[str] = None
    discord_admin_channel_id: Optional[str] = None
    discord_stream_channel_id: Optional[str] = None
    discord_stream_notifications_enabled: Optional[bool] = None
    discord_stream_live_message: Optional[str] = None
    discord_welcome_dm_enabled: Optional[bool] = None
    discord_welcome_dm_message: Optional[str] = None
    discord_notify_questions: Optional[bool] = None
    discord_notify_answers: Optional[bool] = None
    discord_notify_purchases: Optional[bool] = None


@router.get("/discord/settings")
def get_discord_settings(
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return Discord bot settings (settings-table values take precedence over env vars)."""

    def _str_setting(key: str, env: str = "") -> Optional[str]:
        """Settings-table value first, then env var, then None."""
        v = get_setting(db, key)
        if v is not None:
            return v
        ev = os.environ.get(env, "")
        return ev or None

    return {
        "discord_question_channel_id":         _str_setting("discord_question_channel_id",         "DISCORD_QUESTION_CHANNEL_ID"),
        "discord_notification_channel_id":     _str_setting("discord_notification_channel_id",     "DISCORD_NOTIFICATION_CHANNEL_ID"),
        "discord_admin_channel_id":            _str_setting("discord_admin_channel_id",            "DISCORD_ADMIN_CHANNEL_ID"),
        "discord_stream_channel_id":           _str_setting("discord_stream_channel_id",           "DISCORD_STREAM_CHANNEL_ID"),
        "discord_stream_notifications_enabled": _bool_setting(db, "discord_stream_notifications_enabled", default=False),
        "discord_stream_live_message":          get_setting(db, "discord_stream_live_message"),
        "discord_welcome_dm_enabled":           _bool_setting(db, "discord_welcome_dm_enabled",          default=False),
        "discord_welcome_dm_message":           get_setting(db, "discord_welcome_dm_message"),
        "discord_notify_questions":             _bool_setting(db, "discord_notify_questions",             default=True),
        "discord_notify_answers":               _bool_setting(db, "discord_notify_answers",               default=True),
        "discord_notify_purchases":             _bool_setting(db, "discord_notify_purchases",             default=True),
    }


@router.patch("/discord/settings", status_code=status.HTTP_200_OK)
def patch_discord_settings(
    body: _DiscordSettingsPatch,
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update Discord bot settings in the settings table (takes effect immediately)."""
    updated: list[str] = []

    str_fields = [
        ("discord_question_channel_id",     body.discord_question_channel_id),
        ("discord_notification_channel_id", body.discord_notification_channel_id),
        ("discord_admin_channel_id",        body.discord_admin_channel_id),
        ("discord_stream_channel_id",       body.discord_stream_channel_id),
        ("discord_stream_live_message",     body.discord_stream_live_message),
        ("discord_welcome_dm_message",      body.discord_welcome_dm_message),
    ]
    bool_fields = [
        ("discord_stream_notifications_enabled", body.discord_stream_notifications_enabled),
        ("discord_welcome_dm_enabled",           body.discord_welcome_dm_enabled),
        ("discord_notify_questions",             body.discord_notify_questions),
        ("discord_notify_answers",               body.discord_notify_answers),
        ("discord_notify_purchases",             body.discord_notify_purchases),
    ]

    for key, val in str_fields:
        if val is not None:
            set_setting(db, key, val)
            updated.append(key)

    for key, val in bool_fields:
        if val is not None:
            set_setting(db, key, "true" if val else "false")
            updated.append(key)

    logger.info("Admin '%s' updated Discord settings: %s", admin_user, updated)
    return {"updated": updated}


@router.get("/discord/status")
async def get_discord_status(_: str = Depends(get_admin_user)):
    """Return live Discord bot status (token validity, guild info)."""
    from discord_webhook import get_bot_status
    return await get_bot_status()


@router.post("/discord/test", status_code=status.HTTP_200_OK)
async def discord_test_notification(
    admin_user: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Send a test notification to the admin Discord channel."""
    from discord_webhook import send_admin_notification
    admin_channel = get_setting(db, "discord_admin_channel_id") or os.environ.get("DISCORD_ADMIN_CHANNEL_ID", "")
    if not admin_channel:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="DISCORD_ADMIN_CHANNEL_ID is not configured.",
        )
    await send_admin_notification(
        f"🧪 Test notification from the admin panel (triggered by **{admin_user}**) 🐾"
    )
    return {"sent": True}


# ---------------------------------------------------------------------------
# Stream goal
# ---------------------------------------------------------------------------


class _GoalPatch(BaseModel):
    enabled: Optional[bool] = None
    label: Optional[str] = None
    target_cents: Optional[int] = None
    current_cents: Optional[int] = None


@router.patch("/stream/goal", status_code=200)
def patch_stream_goal(
    body: _GoalPatch,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    updated = []
    if body.enabled is not None:
        set_setting(db, "tip_goal_enabled", "true" if body.enabled else "false")
        updated.append("enabled")
    if body.label is not None:
        set_setting(db, "tip_goal_label", body.label)
        updated.append("label")
    if body.target_cents is not None:
        set_setting(db, "tip_goal_target_cents", str(body.target_cents))
        updated.append("target_cents")
    if body.current_cents is not None:
        set_setting(db, "tip_goal_current_cents", str(body.current_cents))
        updated.append("current_cents")
    return {"updated": updated}


# ---------------------------------------------------------------------------
# Stream schedule
# ---------------------------------------------------------------------------


@router.get("/schedule")
def admin_get_schedule(_: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    import json as _json
    raw = get_setting(db, "stream_schedule", None)
    if raw:
        try:
            return {"schedule": _json.loads(raw)}
        except Exception:
            pass
    return {"schedule": []}


class _SchedulePatch(BaseModel):
    schedule: list


@router.patch("/schedule", status_code=200)
def admin_update_schedule(
    body: _SchedulePatch,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    import json as _json
    set_setting(db, "stream_schedule", _json.dumps(body.schedule))
    return {"updated": True, "schedule": body.schedule}


# ---------------------------------------------------------------------------
# File vault (content drops)
# ---------------------------------------------------------------------------


class _VaultItemCreate(BaseModel):
    title: str
    description: Optional[str] = None
    file_url: str
    minimum_access_level: int = 1
    sort_order: int = 0
    is_active: bool = True


class _VaultItemUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    file_url: Optional[str] = None
    minimum_access_level: Optional[int] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("/vault")
def admin_list_vault(_: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM content_drops ORDER BY sort_order, id DESC").fetchall()
    return [dict(r) for r in rows]


@router.post("/vault", status_code=201)
def admin_create_vault_item(
    body: _VaultItemCreate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    now = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        "INSERT INTO content_drops (title, description, file_url, minimum_access_level, sort_order, is_active, created_at) VALUES (?,?,?,?,?,?,?)",
        (body.title, body.description, body.file_url, body.minimum_access_level, body.sort_order, 1 if body.is_active else 0, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM content_drops WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@router.put("/vault/{item_id}")
def admin_update_vault_item(
    item_id: int,
    body: _VaultItemUpdate,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute("SELECT * FROM content_drops WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found.")
    new_title = body.title if body.title is not None else row["title"]
    new_desc = body.description if body.description is not None else row["description"]
    new_url = body.file_url if body.file_url is not None else row["file_url"]
    new_level = body.minimum_access_level if body.minimum_access_level is not None else row["minimum_access_level"]
    new_sort = body.sort_order if body.sort_order is not None else row["sort_order"]
    new_active = (1 if body.is_active else 0) if body.is_active is not None else row["is_active"]
    db.execute(
        "UPDATE content_drops SET title=?, description=?, file_url=?, minimum_access_level=?, sort_order=?, is_active=? WHERE id=?",
        (new_title, new_desc, new_url, new_level, new_sort, new_active, item_id),
    )
    db.commit()
    updated = db.execute("SELECT * FROM content_drops WHERE id = ?", (item_id,)).fetchone()
    return dict(updated)


@router.delete("/vault/{item_id}", status_code=204)
def admin_delete_vault_item(
    item_id: int,
    _: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute("SELECT id FROM content_drops WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found.")
    db.execute("DELETE FROM content_drops WHERE id = ?", (item_id,))
    db.commit()


# ---------------------------------------------------------------------------
# VOD gallery
# ---------------------------------------------------------------------------


class _VodCreate(BaseModel):
    title: str
    description: Optional[str] = None
    file_url: str
    thumbnail_url: Optional[str] = None
    minimum_access_level: int = 1
    duration_seconds: Optional[int] = None
    is_active: bool = True


class _VodUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    file_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    minimum_access_level: Optional[int] = None
    duration_seconds: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("/vods")
def admin_list_vods(_: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM vods ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@router.post("/vods", status_code=201)
def admin_create_vod(body: _VodCreate, _: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        "INSERT INTO vods (title, description, file_url, thumbnail_url, minimum_access_level, duration_seconds, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (body.title, body.description, body.file_url, body.thumbnail_url, body.minimum_access_level, body.duration_seconds, 1 if body.is_active else 0, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM vods WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@router.put("/vods/{vod_id}")
def admin_update_vod(vod_id: int, body: _VodUpdate, _: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM vods WHERE id = ?", (vod_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="VOD not found.")
    db.execute(
        "UPDATE vods SET title=?, description=?, file_url=?, thumbnail_url=?, minimum_access_level=?, duration_seconds=?, is_active=? WHERE id=?",
        (
            body.title if body.title is not None else row["title"],
            body.description if body.description is not None else row["description"],
            body.file_url if body.file_url is not None else row["file_url"],
            body.thumbnail_url if body.thumbnail_url is not None else row["thumbnail_url"],
            body.minimum_access_level if body.minimum_access_level is not None else row["minimum_access_level"],
            body.duration_seconds if body.duration_seconds is not None else row["duration_seconds"],
            (1 if body.is_active else 0) if body.is_active is not None else row["is_active"],
            vod_id,
        ),
    )
    db.commit()
    updated = db.execute("SELECT * FROM vods WHERE id = ?", (vod_id,)).fetchone()
    return dict(updated)


@router.delete("/vods/{vod_id}", status_code=204)
def admin_delete_vod(vod_id: int, _: str = Depends(get_admin_user), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT id FROM vods WHERE id = ?", (vod_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="VOD not found.")
    db.execute("DELETE FROM vods WHERE id = ?", (vod_id,))
    db.commit()
