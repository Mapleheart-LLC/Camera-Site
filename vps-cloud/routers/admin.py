"""
routers/admin.py – Admin-only management endpoints for mochii.live.

All endpoints are protected by HTTP Basic Auth via the ``get_admin_user``
dependency (ADMIN_USERNAME / ADMIN_PASSWORD environment variables).
This auth system is entirely separate from the Fanvue OAuth / JWT flow.

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
  PUT    /api/admin/drool/credentials  – save / clear drool scraper credentials (Reddit, Twitter, Bluesky)
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote as _url_quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from db import get_db, get_setting, set_setting
from dependencies import get_admin_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

_VALID_DEVICES = {"pishock", "lovense"}

GO2RTC_HOST: str = os.environ.get("GO2RTC_HOST", "localhost")
GO2RTC_PORT: str = os.environ.get("GO2RTC_PORT", "1984")

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


class CameraUpdate(BaseModel):
    display_name: Optional[str] = None
    stream_slug: Optional[str] = None
    minimum_access_level: Optional[int] = None
    rtsp_url: Optional[str] = None
    tapo_ip: Optional[str] = None
    tapo_username: Optional[str] = None
    tapo_password: Optional[str] = None


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
               rtsp_url, tapo_ip, tapo_username, tapo_password
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
                 rtsp_url, tapo_ip, tapo_username, tapo_password)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.display_name,
                payload.stream_slug,
                payload.minimum_access_level,
                payload.rtsp_url or None,
                payload.tapo_ip or None,
                payload.tapo_username or None,
                payload.tapo_password or None,
            ),
        )
        db.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not add camera: {exc}",
        ) from exc

    effective_url = _effective_rtsp_url(
        payload.rtsp_url, payload.tapo_ip, payload.tapo_username, payload.tapo_password
    )
    _register_stream(payload.stream_slug, effective_url)

    row = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password
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
    try:
        db.execute(
            """
            UPDATE cameras
            SET display_name = ?, stream_slug = ?, minimum_access_level = ?,
                rtsp_url = ?, tapo_ip = ?, tapo_username = ?, tapo_password = ?
            WHERE id = ?
            """,
            (new_name, new_slug, new_level, new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass, cam_id),
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

    effective_url = _effective_rtsp_url(new_rtsp, new_tapo_ip, new_tapo_user, new_tapo_pass)
    _register_stream(new_slug, effective_url)

    updated = db.execute(
        """
        SELECT id, display_name, stream_slug, minimum_access_level,
               rtsp_url, tapo_ip, tapo_username, tapo_password
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
    Manually trigger an IoT device without rate limiting or a Fanvue JWT.

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
    return {
        "status": "ok",
        "device": device,
        "message": "Admin command accepted (mock response).",
        "triggered_by": admin_user,
    }


# ---------------------------------------------------------------------------
# Puppy Pouch – admin question management
# ---------------------------------------------------------------------------


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
    return {"id": question_id, "message": "Answer saved and question is now public 🐾"}


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
        "fanvue_client_id_set": bool(os.environ.get("FANVUE_CLIENT_ID")),
        "fanvue_client_secret_set": bool(os.environ.get("FANVUE_CLIENT_SECRET")),
        "fanvue_redirect_uri": os.environ.get(
            "FANVUE_REDIRECT_URI", "http://localhost:8000/auth/callback"
        ),
        "fanvue_creator_id_set": bool(os.environ.get("FANVUE_CREATOR_ID")),
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
    "reddit_client_id":      ("drool_reddit_client_id",      "REDDIT_CLIENT_ID",      False),
    "reddit_client_secret":  ("drool_reddit_client_secret",  "REDDIT_CLIENT_SECRET",  True),
    "reddit_username":       ("drool_reddit_username",       "REDDIT_USERNAME",       False),
    "reddit_password":       ("drool_reddit_password",       "REDDIT_PASSWORD",       True),
    "reddit_user_agent":     ("drool_reddit_user_agent",     "REDDIT_USER_AGENT",     False),
    "twitter_bearer_token":  ("drool_twitter_bearer_token",  "TWITTER_BEARER_TOKEN",  True),
    "twitter_user_id":       ("drool_twitter_user_id",       "TWITTER_USER_ID",       False),
    "twitter_api_key":       ("drool_twitter_api_key",       "TWITTER_API_KEY",       True),
    "twitter_api_secret":    ("drool_twitter_api_secret",    "TWITTER_API_SECRET",    True),
    "twitter_access_token":  ("drool_twitter_access_token",  "TWITTER_ACCESS_TOKEN",  True),
    "twitter_access_secret": ("drool_twitter_access_secret", "TWITTER_ACCESS_SECRET", True),
    "bsky_handle":           ("drool_bsky_handle",           "BSKY_HANDLE",           False),
    "bsky_app_password":     ("drool_bsky_app_password",     "BSKY_APP_PASSWORD",     True),
}


class DroolCredsUpdate(BaseModel):
    reddit_client_id:      Optional[str] = None
    reddit_client_secret:  Optional[str] = None
    reddit_username:       Optional[str] = None
    reddit_password:       Optional[str] = None
    reddit_user_agent:     Optional[str] = None
    twitter_bearer_token:  Optional[str] = None
    twitter_user_id:       Optional[str] = None
    twitter_api_key:       Optional[str] = None
    twitter_api_secret:    Optional[str] = None
    twitter_access_token:  Optional[str] = None
    twitter_access_secret: Optional[str] = None
    bsky_handle:           Optional[str] = None
    bsky_app_password:     Optional[str] = None


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
        result[field] = {
            "db_set":        bool(db_val),
            "env_set":       bool(env_val),
            "effective_set": bool(db_val or env_val),
            "source":        source,
        }
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

