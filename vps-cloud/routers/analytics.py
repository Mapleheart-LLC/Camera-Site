"""
routers/analytics.py – Creator analytics endpoints.

Phase 5: revenue, content performance, subscriber growth.

Endpoints (all require creator JWT)
-------------------------------------
  GET /api/creator/analytics/revenue        – daily revenue breakdown (tips + store + subs)
  GET /api/creator/analytics/content        – per-content engagement metrics
  GET /api/creator/analytics/subscribers    – daily subscribe/cancel events
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone


from fastapi import APIRouter, Depends, Query
from db import get_db
from dependencies import get_current_creator

router = APIRouter(prefix="/api/creator/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)


def _period_start(period: str) -> str:
    """Convert period string like '30d', '90d' to an ISO timestamp."""
    days_map = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
    days = days_map.get(period, 30)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return since.isoformat()


@router.get("/revenue")
def revenue_dashboard(
    period: str = Query("30d", pattern=r"^\d+d$"),
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Aggregate daily revenue for the creator from three sources:
    - Tips
    - Store sales (creator_revenue_pct of order total)
    - Subscription events (flat credit per subscription event)
    """
    since = _period_start(period)

    # Tips by day.
    tips_rows = db.execute(
        """
        SELECT DATE(created_at) AS day, SUM(amount_cents) AS amount
          FROM tips
         WHERE creator_handle = ? AND created_at >= ?
         GROUP BY day
         ORDER BY day
        """,
        (handle, since),
    ).fetchall()
    tips_by_day = {r["day"]: r["amount"] for r in tips_rows}

    # Store revenue by day (sum of order_items that belong to this creator's products).
    store_rows = db.execute(
        """
        SELECT DATE(o.created_at) AS day,
               SUM(oi.unit_price * oi.quantity * p.creator_revenue_pct) AS amount
          FROM order_items oi
          JOIN products p ON p.id = oi.product_id
          JOIN orders o   ON o.id = oi.order_id
         WHERE p.creator_handle = ? AND o.created_at >= ? AND o.status = 'paid'
         GROUP BY day
         ORDER BY day
        """,
        (handle, since),
    ).fetchall()
    store_by_day = {r["day"]: int((r["amount"] or 0) * 100) for r in store_rows}  # convert to cents

    # Subscription events (each active event is worth the tier price if known).
    sub_rows = db.execute(
        """
        SELECT DATE(se.created_at) AS day, COUNT(*) AS new_subs
          FROM subscription_events se
         WHERE se.creator_handle = ? AND se.event_type = 'subscribe' AND se.created_at >= ?
         GROUP BY day
         ORDER BY day
        """,
        (handle, since),
    ).fetchall()

    # Build a unified timeline.
    all_days = sorted(
        set(list(tips_by_day.keys()) + list(store_by_day.keys()) + [r["day"] for r in sub_rows])
    )
    result = []
    for day in all_days:
        result.append(
            {
                "date": day,
                "tips_cents": tips_by_day.get(day, 0),
                "store_cents": store_by_day.get(day, 0),
                "total_cents": tips_by_day.get(day, 0) + store_by_day.get(day, 0),
            }
        )
    return {"period": period, "data": result}


@router.get("/content")
def content_performance(
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Per-content engagement metrics for this creator."""
    # Drool posts.
    drool_rows = db.execute(
        """
        SELECT d.id, d.text_content AS title, d.view_count,
               COUNT(DISTINCT r.id) AS reactions,
               COUNT(DISTINCT c.id) AS comments,
               (d.view_count + COUNT(DISTINCT c.id)*3 + COUNT(DISTINCT r.id)*2) AS engagement_score
          FROM drool_archive d
          LEFT JOIN drool_reactions r ON r.drool_id = d.id
          LEFT JOIN drool_comments c ON c.drool_id = d.id
         WHERE d.creator_handle = ?
         GROUP BY d.id
         ORDER BY engagement_score DESC
         LIMIT 50
        """,
        (handle,),
    ).fetchall()

    # Community posts.
    post_rows = db.execute(
        """
        SELECT id, title, view_count, published_at
          FROM community_posts
         WHERE creator_handle = ? AND is_published = 1
         ORDER BY view_count DESC
         LIMIT 50
        """,
        (handle,),
    ).fetchall()

    # Q&A answered questions.
    qa_rows = db.execute(
        """
        SELECT id, text, is_public
          FROM questions
         WHERE creator_handle = ? AND answer IS NOT NULL
         ORDER BY created_at DESC
         LIMIT 50
        """,
        (handle,),
    ).fetchall()

    return {
        "drool": [dict(r) for r in drool_rows],
        "posts": [dict(r) for r in post_rows],
        "qa": [dict(r) for r in qa_rows],
    }


@router.get("/subscribers")
def subscriber_growth(
    period: str = Query("90d", pattern=r"^\d+d$"),
    handle: str = Depends(get_current_creator),
    db: sqlite3.Connection = Depends(get_db),
):
    """Daily subscribe / cancel counts for this creator."""
    since = _period_start(period)
    rows = db.execute(
        """
        SELECT DATE(created_at) AS day, event_type, COUNT(*) AS count
          FROM subscription_events
         WHERE creator_handle = ? AND created_at >= ?
         GROUP BY day, event_type
         ORDER BY day
        """,
        (handle, since),
    ).fetchall()

    by_day: dict = {}
    for r in rows:
        d = r["day"]
        if d not in by_day:
            by_day[d] = {"date": d, "subscribes": 0, "cancels": 0}
        if r["event_type"] == "subscribe":
            by_day[d]["subscribes"] = r["count"]
        elif r["event_type"] == "cancel":
            by_day[d]["cancels"] = r["count"]

    # Also include overall active subscriber count.
    total_active = db.execute(
        "SELECT COUNT(*) AS cnt FROM user_subscriptions WHERE creator_handle = ? AND status = 'active'",
        (handle,),
    ).fetchone()["cnt"]

    return {
        "period": period,
        "total_active": total_active,
        "data": sorted(by_day.values(), key=lambda x: x["date"]),
    }
