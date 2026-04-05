"""
routers/monetization.py – Deeper monetization features.

Phase 4: tips, per-creator subscription tiers, bundles, PPV streams, digital downloads.

Endpoints
---------
  POST /api/tips                             – create a tip (authenticated)
  GET  /api/tips/received                    – creator's received tips (creator auth)

  GET  /api/creators/{handle}/tiers          – public tier listing
  POST   /api/creator/tiers                  – create a tier (creator)
  PATCH  /api/creator/tiers/{id}             – update a tier (creator)
  DELETE /api/creator/tiers/{id}             – delete a tier (creator)

  GET  /api/bundles                          – public bundle listing
  POST /api/bundles/{id}/subscribe           – purchase a bundle (authenticated)
  POST   /api/admin/bundles                  – create a bundle (admin)
  PATCH  /api/admin/bundles/{id}             – update a bundle (admin)
  POST   /api/admin/bundles/{id}/creators    – add creator to bundle (admin)
  DELETE /api/admin/bundles/{id}/creators/{handle} – remove creator from bundle (admin)

  POST /api/ppv/purchase                     – purchase PPV access for a camera (authenticated)
  GET  /api/ppv/my-purchases                 – list user's active PPV purchases

  GET  /api/downloads                        – list digital products (public / gated)
  GET  /api/downloads/{id}/buy               – one-time purchase of a download (authenticated)
  GET  /api/downloads/{id}/file              – download the file (authenticated + purchased)
  POST   /api/creator/downloads              – create a digital product (creator)
  PATCH  /api/creator/downloads/{id}         – update a digital product (creator)
  DELETE /api/creator/downloads/{id}         – delete a digital product (creator)
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from db import get_db
from dependencies import get_admin_user, get_current_creator, get_current_user, get_optional_user

router = APIRouter(tags=["monetization"])

logger = logging.getLogger(__name__)

_DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/tmp/downloads"))
try:
    _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TipRequest(BaseModel):
    creator_handle: str = Field(..., min_length=1, max_length=64)
    amount_cents: int = Field(..., ge=100, le=100000, description="Amount in cents (min $1)")
    message: Optional[str] = Field(None, max_length=300)


class TierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price_cents: int = Field(..., ge=100)
    access_level: int = Field(2, ge=1, le=10)
    segpay_package_id: Optional[str] = Field(None, max_length=64)
    segpay_price_point_id: Optional[str] = Field(None, max_length=64)


class TierUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price_cents: Optional[int] = Field(None, ge=100)
    access_level: Optional[int] = Field(None, ge=1, le=10)
    is_active: Optional[bool] = None
    segpay_package_id: Optional[str] = Field(None, max_length=64)
    segpay_price_point_id: Optional[str] = Field(None, max_length=64)


class BundleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price_cents: int = Field(..., ge=100)
    is_active: bool = True


class BundleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price_cents: Optional[int] = Field(None, ge=100)
    is_active: Optional[bool] = None


class BundleCreatorAdd(BaseModel):
    creator_handle: str = Field(..., min_length=1, max_length=64)
    access_level_granted: int = Field(2, ge=1, le=10)


class PpvPurchaseRequest(BaseModel):
    camera_id: int


class DigitalProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    price_cents: int = Field(..., ge=0)
    is_subscriber_only: bool = False


class DigitalProductUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    price_cents: Optional[int] = Field(None, ge=0)
    is_subscriber_only: Optional[bool] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------

@router.post("/api/tips", status_code=status.HTTP_201_CREATED)
def send_tip(
    payload: TipRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Submit a tip to a creator.

    In a production integration this would call the payment provider's
    one-time-charge API first and store the provider_ref.  Here we record the
    intent and return a placeholder URL (real integration delegated to payment
    provider).
    """
    user_id = current_user["fanvue_id"]
    creator = db.execute(
        "SELECT handle FROM creator_accounts WHERE handle = ? AND is_active = 1",
        (payload.creator_handle,),
    ).fetchone()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found.")

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO tips (from_user_id, creator_handle, amount_cents, message, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, payload.creator_handle, payload.amount_cents, payload.message, now),
    )
    db.commit()
    return {
        "detail": "Tip recorded.",
        "amount_cents": payload.amount_cents,
        "creator_handle": payload.creator_handle,
    }


@router.get("/api/tips/received")
def get_received_tips(
    limit: int = 50,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Return tips received by the authenticated creator (most recent first)."""
    rows = db.execute(
        """
        SELECT t.id, t.from_user_id, t.amount_cents, t.message, t.created_at,
               COALESCE(su.display_name, su.username) AS from_display
          FROM tips t
          LEFT JOIN site_users su ON su.id = t.from_user_id
         WHERE t.creator_handle = ?
         ORDER BY t.created_at DESC
         LIMIT ?
        """,
        (handle, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-creator subscription tiers
# ---------------------------------------------------------------------------

@router.get("/api/creators/{handle}/tiers")
def list_creator_tiers(
    handle: str,
    db: sqlite3.Connection = Depends(get_db),
):
    """Public listing of active subscription tiers for a creator."""
    rows = db.execute(
        """
        SELECT id, name, description, price_cents, access_level
          FROM subscription_tiers
         WHERE creator_handle = ? AND is_active = 1
         ORDER BY price_cents
        """,
        (handle,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/creator/tiers", status_code=status.HTTP_201_CREATED)
def create_tier(
    payload: TierCreate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new subscription tier for this creator."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        INSERT INTO subscription_tiers
            (creator_handle, name, description, price_cents, access_level, is_active,
             segpay_package_id, segpay_price_point_id, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            handle,
            payload.name,
            payload.description,
            payload.price_cents,
            payload.access_level,
            payload.segpay_package_id,
            payload.segpay_price_point_id,
            now,
        ),
    )
    db.commit()
    return {"id": cursor.lastrowid, "detail": "Tier created."}


@router.patch("/api/creator/tiers/{tier_id}")
def update_tier(
    tier_id: int,
    payload: TierUpdate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Update a subscription tier owned by this creator."""
    row = db.execute(
        "SELECT id FROM subscription_tiers WHERE id = ? AND creator_handle = ?",
        (tier_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tier not found.")

    updates: dict = {}
    for field in ("name", "description", "price_cents", "access_level",
                  "segpay_package_id", "segpay_price_point_id"):
        val = getattr(payload, field)
        if val is not None:
            updates[field] = val
    if payload.is_active is not None:
        updates["is_active"] = int(payload.is_active)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE subscription_tiers SET {set_clause} WHERE id = ?",
            list(updates.values()) + [tier_id],
        )
        db.commit()
    return {"detail": "Tier updated."}


@router.delete("/api/creator/tiers/{tier_id}")
def delete_tier(
    tier_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Soft-delete (deactivate) a subscription tier."""
    result = db.execute(
        "UPDATE subscription_tiers SET is_active = 0 WHERE id = ? AND creator_handle = ?",
        (tier_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tier not found.")
    return {"detail": "Tier deactivated."}


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

@router.get("/api/bundles")
def list_bundles(db: sqlite3.Connection = Depends(get_db)):
    """Public list of active bundles with their included creators."""
    bundles = db.execute(
        "SELECT * FROM bundles WHERE is_active = 1 ORDER BY price_cents",
    ).fetchall()
    result = []
    for b in bundles:
        creators = db.execute(
            "SELECT creator_handle, access_level_granted FROM bundle_creators WHERE bundle_id = ?",
            (b["id"],),
        ).fetchall()
        d = dict(b)
        d["creators"] = [dict(c) for c in creators]
        result.append(d)
    return result


@router.post("/api/bundles/{bundle_id}/subscribe", status_code=status.HTTP_201_CREATED)
def purchase_bundle(
    bundle_id: int,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Purchase a bundle — grants access levels for all included creators."""
    bundle = db.execute(
        "SELECT * FROM bundles WHERE id = ? AND is_active = 1", (bundle_id,)
    ).fetchone()
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found.")

    user_id = current_user["fanvue_id"]
    now = datetime.now(timezone.utc).isoformat()

    # Record bundle purchase (idempotent).
    db.execute(
        """
        INSERT INTO bundle_purchases (user_id, bundle_id, purchased_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, bundle_id) DO NOTHING
        """,
        (user_id, bundle_id, now),
    )

    # Grant access for each creator in the bundle.
    creators = db.execute(
        "SELECT creator_handle, access_level_granted FROM bundle_creators WHERE bundle_id = ?",
        (bundle_id,),
    ).fetchall()
    for c in creators:
        db.execute(
            """
            INSERT INTO user_subscriptions (user_id, creator_handle, status, started_at)
            VALUES (?, ?, 'active', ?)
            ON CONFLICT(user_id, creator_handle) DO UPDATE SET status = 'active'
            """,
            (user_id, c["creator_handle"], now),
        )
        # Also bump access_level on site_users for legacy gate compat.
        db.execute(
            """
            UPDATE site_users SET access_level = MAX(access_level, ?)
            WHERE id = ?
            """,
            (c["access_level_granted"], user_id),
        )
    db.commit()
    return {"detail": "Bundle purchased and access granted."}


# Admin bundle management

@router.post("/api/admin/bundles", status_code=status.HTTP_201_CREATED)
def admin_create_bundle(
    payload: BundleCreate,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        "INSERT INTO bundles (name, description, price_cents, is_active, created_at) VALUES (?,?,?,?,?)",
        (payload.name, payload.description, payload.price_cents, int(payload.is_active), now),
    )
    db.commit()
    return {"id": cursor.lastrowid}


@router.patch("/api/admin/bundles/{bundle_id}")
def admin_update_bundle(
    bundle_id: int,
    payload: BundleUpdate,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    updates: dict = {}
    for f in ("name", "description", "price_cents"):
        v = getattr(payload, f)
        if v is not None:
            updates[f] = v
    if payload.is_active is not None:
        updates["is_active"] = int(payload.is_active)
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE bundles SET {set_clause} WHERE id = ?",
            list(updates.values()) + [bundle_id],
        )
        db.commit()
    return {"detail": "Bundle updated."}


@router.post("/api/admin/bundles/{bundle_id}/creators", status_code=status.HTTP_201_CREATED)
def admin_add_bundle_creator(
    bundle_id: int,
    payload: BundleCreatorAdd,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not db.execute("SELECT id FROM bundles WHERE id = ?", (bundle_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Bundle not found.")
    try:
        db.execute(
            "INSERT INTO bundle_creators (bundle_id, creator_handle, access_level_granted) VALUES (?,?,?)",
            (bundle_id, payload.creator_handle, payload.access_level_granted),
        )
        db.commit()
    except Exception:
        raise HTTPException(status_code=409, detail="Creator already in bundle.")
    return {"detail": "Creator added to bundle."}


@router.delete("/api/admin/bundles/{bundle_id}/creators/{handle}")
def admin_remove_bundle_creator(
    bundle_id: int,
    handle: str,
    _admin: str = Depends(get_admin_user),
    db: sqlite3.Connection = Depends(get_db),
):
    result = db.execute(
        "DELETE FROM bundle_creators WHERE bundle_id = ? AND creator_handle = ?",
        (bundle_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Entry not found.")
    return {"detail": "Creator removed from bundle."}


# ---------------------------------------------------------------------------
# Pay-Per-View streams
# ---------------------------------------------------------------------------

@router.post("/api/ppv/purchase", status_code=status.HTTP_201_CREATED)
def purchase_ppv(
    payload: PpvPurchaseRequest,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Purchase PPV access for a camera."""
    camera = db.execute(
        "SELECT id, ppv_price_cents FROM cameras WHERE id = ?", (payload.camera_id,)
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found.")
    if not camera["ppv_price_cents"]:
        raise HTTPException(status_code=400, detail="This camera does not require PPV payment.")

    user_id = current_user["fanvue_id"]
    now = datetime.now(timezone.utc).isoformat()

    # In production: call payment provider, get provider_ref, then insert.
    db.execute(
        """
        INSERT INTO ppv_purchases (user_id, camera_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, camera_id) DO NOTHING
        """,
        (user_id, payload.camera_id, now),
    )
    db.commit()
    return {
        "detail": "PPV access granted.",
        "camera_id": payload.camera_id,
        "amount_cents": camera["ppv_price_cents"],
    }


@router.get("/api/ppv/my-purchases")
def my_ppv_purchases(
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """List the authenticated user's active PPV camera purchases."""
    user_id = current_user["fanvue_id"]
    rows = db.execute(
        """
        SELECT pp.camera_id, c.display_name, pp.created_at, pp.expires_at
          FROM ppv_purchases pp
          JOIN cameras c ON c.id = pp.camera_id
         WHERE pp.user_id = ?
           AND (pp.expires_at IS NULL OR pp.expires_at > ?)
        """,
        (user_id, datetime.now(timezone.utc).isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Digital Downloads
# ---------------------------------------------------------------------------

@router.get("/api/downloads")
def list_downloads(
    creator_handle: Optional[str] = None,
    current_user: Optional[dict] = Depends(get_optional_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Public list of available digital products."""
    is_subscriber = current_user is not None and current_user.get("access_level", 0) >= 2
    if creator_handle:
        if is_subscriber:
            rows = db.execute(
                "SELECT id, creator_handle, name, description, price_cents, is_subscriber_only "
                "FROM digital_products WHERE creator_handle = ? AND is_active = 1",
                (creator_handle,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, creator_handle, name, description, price_cents, is_subscriber_only "
                "FROM digital_products WHERE creator_handle = ? AND is_active = 1 AND is_subscriber_only = 0",
                (creator_handle,),
            ).fetchall()
    else:
        if is_subscriber:
            rows = db.execute(
                "SELECT id, creator_handle, name, description, price_cents, is_subscriber_only "
                "FROM digital_products WHERE is_active = 1"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, creator_handle, name, description, price_cents, is_subscriber_only "
                "FROM digital_products WHERE is_active = 1 AND is_subscriber_only = 0"
            ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/downloads/{product_id}/buy")
def buy_download(
    product_id: int,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Record a digital product purchase (payment integration delegated to provider)."""
    product = db.execute(
        "SELECT * FROM digital_products WHERE id = ? AND is_active = 1", (product_id,)
    ).fetchone()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")

    user_id = current_user["fanvue_id"]
    # Subscriber-only check.
    if product["is_subscriber_only"] and current_user.get("access_level", 0) < 2:
        raise HTTPException(status_code=402, detail="Subscription required.")

    now = datetime.now(timezone.utc).isoformat()
    try:
        db.execute(
            "INSERT INTO digital_purchases (user_id, product_id, purchased_at) VALUES (?,?,?)",
            (user_id, product_id, now),
        )
        db.commit()
    except Exception:
        pass  # Already purchased – idempotent

    return {"detail": "Purchase recorded.", "product_id": product_id}


@router.get("/api/downloads/{product_id}/file")
def get_download_file(
    product_id: int,
    current_user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    """Stream the purchased digital product file."""
    product = db.execute(
        "SELECT * FROM digital_products WHERE id = ? AND is_active = 1", (product_id,)
    ).fetchone()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found.")

    user_id = current_user["fanvue_id"]
    # Check purchase record (or subscriber bypass if product is free-for-subs).
    purchased = db.execute(
        "SELECT id FROM digital_purchases WHERE user_id = ? AND product_id = ?",
        (user_id, product_id),
    ).fetchone()
    is_subscriber = current_user.get("access_level", 0) >= 2

    if not purchased and not (product["is_subscriber_only"] and is_subscriber):
        raise HTTPException(status_code=403, detail="Purchase required to download.")

    file_key = product["file_key"]
    if not file_key:
        raise HTTPException(status_code=404, detail="File not yet uploaded.")

    file_path = _DOWNLOADS_DIR / file_key
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on server.")

    return FileResponse(str(file_path), filename=file_path.name)


# Creator digital product management

@router.post("/api/creator/downloads", status_code=status.HTTP_201_CREATED)
async def creator_create_download(
    file: UploadFile = File(...),
    name: str = "",
    description: str = "",
    price_cents: int = 0,
    is_subscriber_only: bool = False,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Upload a digital product file and register it."""
    if not name:
        name = file.filename or "Untitled"

    file_key = f"{uuid.uuid4()}_{file.filename}"
    dest = _DOWNLOADS_DIR / file_key
    content = await file.read()
    dest.write_bytes(content)

    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        INSERT INTO digital_products
            (creator_handle, name, description, price_cents, file_key, is_subscriber_only, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (handle, name, description, price_cents, file_key, int(is_subscriber_only), now),
    )
    db.commit()
    return {"id": cursor.lastrowid, "file_key": file_key}


@router.patch("/api/creator/downloads/{product_id}")
def creator_update_download(
    product_id: int,
    payload: DigitalProductUpdate,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    row = db.execute(
        "SELECT id FROM digital_products WHERE id = ? AND creator_handle = ?",
        (product_id, handle),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found.")

    updates: dict = {}
    for f in ("name", "description", "price_cents"):
        v = getattr(payload, f)
        if v is not None:
            updates[f] = v
    if payload.is_subscriber_only is not None:
        updates["is_subscriber_only"] = int(payload.is_subscriber_only)
    if payload.is_active is not None:
        updates["is_active"] = int(payload.is_active)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE digital_products SET {set_clause} WHERE id = ?",
            list(updates.values()) + [product_id],
        )
        db.commit()
    return {"detail": "Product updated."}


@router.delete("/api/creator/downloads/{product_id}")
def creator_delete_download(
    product_id: int,
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    result = db.execute(
        "UPDATE digital_products SET is_active = 0 WHERE id = ? AND creator_handle = ?",
        (product_id, handle),
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"detail": "Product deactivated."}
