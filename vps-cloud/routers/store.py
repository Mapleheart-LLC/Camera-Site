"""
routers/store.py – mochii.live e-commerce store endpoints.

Public endpoints (no authentication required – anyone can buy!):
  POST /api/store/checkout               – accept a cart, create a pending
                                           order, and return a Segpay
                                           checkout URL.
  POST /api/webhooks/payments/{provider} – receive payment provider postbacks,
                                           mark orders as paid, trigger
                                           Printful fulfilment, and send a
                                           Discord notification.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from db import get_db
from discord_webhook import send_admin_notification
from payments import get_payment_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["store"])


# ---------------------------------------------------------------------------
# Products endpoint
# ---------------------------------------------------------------------------


@router.get("/api/store/products")
def list_products(db: sqlite3.Connection = Depends(get_db)):
    """Return all products available in the store."""
    rows = db.execute(
        """
        SELECT id, name, description, price, image_url,
               is_printful, printful_variant_id, stock_count,
               creator_handle, creator_revenue_pct
          FROM products
         ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CartItem(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1)


class CheckoutRequest(BaseModel):
    cart: list[CartItem] = Field(..., min_length=1)
    customer_email: str = Field(..., min_length=3)
    shipping_address: dict[str, Any]


# ---------------------------------------------------------------------------
# Printful helper
# ---------------------------------------------------------------------------


async def _trigger_printful_order(order_id: str, db: sqlite3.Connection) -> None:
    """Submit a Printful fulfilment order for all printful items in an order."""
    printful_api_key = os.environ.get("PRINTFUL_API_KEY", "")
    if not printful_api_key:
        logger.warning(
            "PRINTFUL_API_KEY not set – skipping Printful fulfilment for order %s",
            order_id,
        )
        return

    order_row = db.execute(
        "SELECT * FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if not order_row:
        logger.warning("Order %s not found when triggering Printful", order_id)
        return

    order = dict(order_row)
    shipping_address: dict = json.loads(order.get("shipping_address") or "{}")

    # Collect printful line items for this order.
    item_rows = db.execute(
        """
        SELECT oi.quantity, p.printful_variant_id, p.name, p.price
          FROM order_items oi
          JOIN products p ON p.id = oi.product_id
         WHERE oi.order_id = ? AND p.is_printful = 1
        """,
        (order_id,),
    ).fetchall()

    if not item_rows:
        return

    recipient = {
        "name": shipping_address.get("name", ""),
        "address1": shipping_address.get("address1", ""),
        "address2": shipping_address.get("address2", ""),
        "city": shipping_address.get("city", ""),
        "state_code": shipping_address.get("state", ""),
        "country_code": shipping_address.get("country", "US"),
        "zip": shipping_address.get("zip", ""),
        "email": order.get("customer_email", ""),
    }

    printful_items = [
        {
            "variant_id": row["printful_variant_id"],
            "quantity": row["quantity"],
        }
        for row in item_rows
    ]

    payload = {
        "recipient": recipient,
        "items": printful_items,
        "retail_costs": {"total": str(order.get("total_amount", 0))},
        "external_id": order_id,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.printful.com/orders",
                json=payload,
                headers={"Authorization": f"Bearer {printful_api_key}"},
            )
        if resp.status_code in (200, 201):
            logger.info("Printful order created successfully for order %s", order_id)
        else:
            logger.warning(
                "Printful API returned %s for order %s: %s",
                resp.status_code,
                order_id,
                resp.text[:300],
            )
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        logger.warning("Failed to trigger Printful order for %s: %s", order_id, exc)


# ---------------------------------------------------------------------------
# Checkout endpoint
# ---------------------------------------------------------------------------


@router.post("/api/store/checkout")
async def checkout(
    payload: CheckoutRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a Segpay checkout session and return the redirect URL.

    Access level is **not** required – anyone can buy from the store.
    """
    base_url = os.environ.get("BASE_URL", "").rstrip("/")

    # Resolve products from the database.
    product_ids = [item.product_id for item in payload.cart]
    placeholders = ",".join("?" * len(product_ids))
    rows = db.execute(
        f"SELECT * FROM products WHERE id IN ({placeholders})", product_ids
    ).fetchall()
    products = {row["id"]: dict(row) for row in rows}

    if len(products) != len(set(product_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more products were not found.",
        )

    # Build enriched cart and compute total amount.
    enriched_cart: list[dict] = []
    total_amount = 0.0

    for item in payload.cart:
        product = products[item.product_id]
        stock = product.get("stock_count")
        if stock is not None and stock <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{product['name']}' is out of stock.",
            )
        line_total = float(product["price"]) * item.quantity
        total_amount += line_total
        enriched_cart.append(
            {
                "product_id": item.product_id,
                "name": product["name"],
                "quantity": item.quantity,
                "price": float(product["price"]),
            }
        )

    total_amount = round(total_amount, 2)

    # Persist the order with 'pending' status.
    order_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO orders
            (id, external_transaction_id, provider_name, status,
             customer_email, total_amount, shipping_address, created_at)
        VALUES (?, NULL, 'segpay', 'pending', ?, ?, ?, ?)
        """,
        (
            order_id,
            payload.customer_email,
            total_amount,
            json.dumps(payload.shipping_address),
            now,
        ),
    )

    # Persist order line items.
    for item in payload.cart:
        product = products[item.product_id]
        db.execute(
            """
            INSERT INTO order_items (order_id, product_id, quantity, unit_price)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, item.product_id, item.quantity, float(product["price"])),
        )

    db.commit()

    # Create checkout session via the configured payment provider.
    provider = get_payment_provider()
    try:
        checkout_url = await provider.create_checkout_session(
            order_id=order_id,
            cart=enriched_cart,
            customer_email=payload.customer_email,
            total_amount=total_amount,
            success_url=f"{base_url}/store.html?status=success",
            cancel_url=f"{base_url}/store.html?status=cancel",
        )
    except ValueError as exc:
        logger.error("Failed to create checkout session for order %s: %s", order_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to create payment session: {exc}",
        ) from exc

    return {"checkout_url": checkout_url, "order_id": order_id}


# ---------------------------------------------------------------------------
# Webhook / postback router
# ---------------------------------------------------------------------------


@router.post("/api/webhooks/payments/{provider}")
async def payment_webhook(
    provider: str,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Generic webhook router that forwards payloads to the correct provider.

    On successful verification:
    1. Updates the order status to ``'paid'``.
    2. Triggers Printful fulfilment when any line item is a Printful product.
    3. Sends a Discord notification to the Puppy Pouch channel.
    """
    try:
        payment_provider = get_payment_provider(provider)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown payment provider: '{provider}'",
        )

    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        webhook_data = await payment_provider.verify_webhook(raw_body, headers)
    except ValueError as exc:
        logger.warning(
            "Webhook verification failed for provider '%s': %s", provider, exc
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    order_id: str = webhook_data.get("order_id", "")
    payment_status: str = webhook_data.get("status", "unknown")
    external_transaction_id: str = webhook_data.get("external_transaction_id", "")

    if not order_id:
        logger.warning(
            "Webhook from '%s' did not include an order_id: %s", provider, webhook_data
        )
        return {"received": True}

    # Update order record.
    db.execute(
        """
        UPDATE orders
           SET status = ?,
               external_transaction_id = ?,
               provider_name = ?
         WHERE id = ?
        """,
        (payment_status, external_transaction_id, provider, order_id),
    )
    db.commit()

    if payment_status == "paid":
        # Check if any line item requires Printful fulfilment.
        needs_printful = db.execute(
            """
            SELECT 1
              FROM order_items oi
              JOIN products p ON p.id = oi.product_id
             WHERE oi.order_id = ? AND p.is_printful = 1
             LIMIT 1
            """,
            (order_id,),
        ).fetchone()

        if needs_printful:
            await _trigger_printful_order(order_id, db)

        # Notify the admin Discord channel.
        await send_admin_notification(
            "🛒 A new treat has been purchased! Check the Kennel for details."
        )

    return {"received": True}
