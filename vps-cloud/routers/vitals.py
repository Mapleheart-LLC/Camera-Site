"""
routers/vitals.py – Biometric vitals ingestion and baseline engine.

Device-facing (protected by TPE webhook secret):
  POST /api/vitals/sync  – Receive and store arrays of biometric data from the
                           mobile app.  Computes the resting baseline and
                           classifies any active alert, then broadcasts a
                           ``vitals_update`` message to all connected Handler WS
                           clients.

Handler/Admin-facing (JWT Bearer auth – role 'handler' or 'admin'):
  GET  /api/vitals/history  – Return the last 60 minutes of heart-rate readings
                              for a device, plus the current baseline value and
                              alert status.  Used to populate the Chart.js graph
                              in the Handler Panel.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from db import get_db, get_db_connection
from dependencies import role_required
from routers.ws_manager import handler_ws as _handler_ws

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vitals"])

# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------

_CREATE_VITALS_SQL = """
    CREATE TABLE IF NOT EXISTS device_vitals (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id  TEXT    NOT NULL,
        heart_rate INTEGER NOT NULL,
        steps      INTEGER NOT NULL DEFAULT 0,
        timestamp  TEXT    NOT NULL
    )
"""

_CREATE_IDX_DEVICE_TS = (
    "CREATE INDEX IF NOT EXISTS idx_device_vitals_device_ts "
    "ON device_vitals(device_id, timestamp)"
)


def migrate_vitals(conn: sqlite3.Connection) -> None:
    """Create the device_vitals table and its indexes (idempotent)."""
    conn.execute(_CREATE_VITALS_SQL)
    conn.execute(_CREATE_IDX_DEVICE_TS)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(hours: float = 0, minutes: float = 0) -> str:
    delta = timedelta(hours=hours, minutes=minutes)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _get_baseline(db: sqlite3.Connection, device_id: str) -> Optional[float]:
    """Return the 4-hour moving-average resting heart rate for *device_id*.

    Returns ``None`` when there are no readings in the window.
    """
    cutoff = _iso_ago(hours=4)
    row = db.execute(
        """
        SELECT AVG(heart_rate) AS avg_hr
        FROM device_vitals
        WHERE device_id = ? AND timestamp >= ?
        """,
        (device_id, cutoff),
    ).fetchone()
    if row and row["avg_hr"] is not None:
        return round(float(row["avg_hr"]), 1)
    return None


def _steps_last_5min(db: sqlite3.Connection, device_id: str) -> int:
    """Return the total number of steps recorded in the last 5 minutes."""
    cutoff = _iso_ago(minutes=5)
    row = db.execute(
        """
        SELECT COALESCE(SUM(steps), 0) AS total_steps
        FROM device_vitals
        WHERE device_id = ? AND timestamp >= ?
        """,
        (device_id, cutoff),
    ).fetchone()
    return int(row["total_steps"]) if row else 0


# Alert threshold above baseline that triggers an anomaly.
_ALERT_BPM_DELTA = 35
# Step count threshold for classifying the alert as physical activity.
_PHYSICAL_ACTIVITY_STEPS = 50
# Step count at or below which the alert is classified as stress/excitement.
_STRESS_STEPS_MAX = 5


def _classify_alert(
    db: sqlite3.Connection,
    device_id: str,
    current_bpm: int,
    baseline: Optional[float],
) -> Optional[str]:
    """Return an alert classification string, or ``None`` when no alert is active.

    Rules:
      • No baseline yet → no alert.
      • current_bpm ≤ baseline + 35 → no alert.
      • current_bpm > baseline + 35 AND steps in last 5 min > 50  → 'Physical Activity'
      • current_bpm > baseline + 35 AND steps in last 5 min ≤ 5   → 'Excitement/Stress'
      • Otherwise (moderate steps, indeterminate) → 'Activity Alert'
    """
    if baseline is None:
        return None
    if current_bpm <= baseline + _ALERT_BPM_DELTA:
        return None

    steps = _steps_last_5min(db, device_id)
    if steps > _PHYSICAL_ACTIVITY_STEPS:
        return "Physical Activity"
    if steps <= _STRESS_STEPS_MAX:
        return "Excitement/Stress"
    return "Activity Alert"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class VitalsRecord(BaseModel):
    heart_rate: int
    steps: int = 0
    timestamp: Optional[str] = None  # ISO-8601; defaults to server time when absent


class VitalsBatch(BaseModel):
    """Internal (Camera-Site) format.  ``device_id`` is required and ``readings``
    holds pre-normalised heart_rate + steps records."""
    device_id: Optional[str] = None
    readings: Optional[List[VitalsRecord]] = None
    # tpeapp format: list of typed health records keyed by ``vitals``.
    vitals: Optional[List[dict]] = None


# ---------------------------------------------------------------------------
# Helper: resolve the effective TPE webhook secret from the DB settings table.
# Re-uses the same key as handler.py to avoid creating a separate setting.
# ---------------------------------------------------------------------------


def _effective_webhook_secret(db: sqlite3.Connection) -> str:
    """Return the TPE webhook secret stored in the settings table, or '' if unset."""
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'tpe_webhook_secret'"
    ).fetchone()
    return (row["value"] or "").strip() if row else ""


_BEARER_PREFIX = "Bearer "


def _epoch_ms_to_iso(ms: int) -> str:
    """Convert Unix epoch milliseconds to an ISO-8601 UTC string."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _normalise_tpeapp_vitals(vitals_list: List[dict]) -> List[VitalsRecord]:
    """Convert the tpeapp ``vitals`` array to a list of ``VitalsRecord``.

    The tpeapp sends one entry per health-data type::

        {"type": "heart_rate", "value": 72.0, "unit": "bpm",
         "start_ms": 1714555200000, "end_ms": 1714555260000}
        {"type": "steps",      "value": 120.0, "unit": "count", ...}

    Each entry becomes its own ``VitalsRecord`` (steps = 0 for heart_rate
    entries and heart_rate = 0 for steps entries).  This preserves full
    time-series fidelity — the baseline engine only uses heart_rate rows.
    """
    records: List[VitalsRecord] = []
    for item in vitals_list:
        try:
            vtype = str(item.get("type", "")).lower()
            value = float(item.get("value", 0))
            start_ms = item.get("start_ms")
            ts = _epoch_ms_to_iso(int(start_ms)) if start_ms else None

            if "heart_rate" in vtype:
                records.append(VitalsRecord(heart_rate=round(value), steps=0, timestamp=ts))
            elif "steps" in vtype:
                records.append(VitalsRecord(heart_rate=0, steps=round(value), timestamp=ts))
            # Skip unknown types (e.g. sleep, blood_oxygen) silently.
        except Exception:
            pass
    return records


# ---------------------------------------------------------------------------
# Device-facing endpoint
# ---------------------------------------------------------------------------


@router.post("/api/vitals/sync")
async def vitals_sync(
    body: VitalsBatch,
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    authorization: Optional[str] = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """
    Receive a batch of biometric readings from the mobile app and persist them.

    Accepts **two formats**:

    **Camera-Site format** (existing)::

        {"device_id": "...", "readings": [{"heart_rate": 72, "steps": 0}]}

    **tpeapp format** (``VitalsSyncService``)::

        {"vitals": [{"type": "heart_rate", "value": 72.0, "unit": "bpm",
                     "start_ms": 1714555200000, "end_ms": 1714555260000}]}

    When using the tpeapp format, ``device_id`` may be omitted from the body
    and supplied via the ``X-Device-ID`` HTTP header instead (the flutter HTTP
    client always sends this header).

    Protected by the TPE webhook secret (``Authorization: Bearer <secret>``).
    After storing the readings the endpoint computes the current resting baseline
    and classifies any active alert, then broadcasts a ``vitals_update`` message
    to all connected Handler WebSocket clients.
    """
    # ── Auth ─────────────────────────────────────────────────────────────────
    expected = _effective_webhook_secret(db)
    if expected:
        provided = ""
        if authorization and authorization.startswith(_BEARER_PREFIX):
            provided = authorization[len(_BEARER_PREFIX):].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # ── Resolve device_id ────────────────────────────────────────────────────
    # Prefer body.device_id; fall back to X-Device-ID header (sent by the
    # tpeapp flutter HTTP client).
    effective_device_id = (body.device_id or "").strip() or (x_device_id or "").strip()
    if not effective_device_id:
        raise HTTPException(status_code=400, detail="device_id must not be empty (provide in body or X-Device-ID header)")

    # ── Normalise readings ───────────────────────────────────────────────────
    # tpeapp sends {"vitals": [...]}.  Convert to internal VitalsRecord list.
    if body.vitals is not None:
        readings = _normalise_tpeapp_vitals(body.vitals)
    else:
        readings = body.readings or []

    if not readings:
        return {"stored": 0, "baseline": None, "alert_status": None}

    # ── Persist ───────────────────────────────────────────────────────────────
    now = _now_iso()
    rows_to_insert = [
        (effective_device_id, r.heart_rate, r.steps, r.timestamp or now)
        for r in readings
    ]
    db.executemany(
        "INSERT INTO device_vitals (device_id, heart_rate, steps, timestamp) VALUES (?, ?, ?, ?)",
        rows_to_insert,
    )
    db.commit()

    # ── Baseline + alert ──────────────────────────────────────────────────────
    # Use the most recent heart-rate reading for alert classification.
    # tpeapp may send only steps in the batch — guard against bpm=0.
    latest_bpm: int = next(
        (r.heart_rate for r in reversed(readings) if r.heart_rate > 0), 0
    )
    baseline = _get_baseline(db, effective_device_id)
    alert_status = _classify_alert(db, effective_device_id, latest_bpm, baseline) if latest_bpm > 0 else None

    # ── Broadcast to Handler Panel WebSocket clients ──────────────────────────
    await _handler_ws.broadcast(
        {
            "type": "vitals_update",
            "device_id": effective_device_id,
            "current_bpm": latest_bpm,
            "baseline": baseline,
            "alert_status": alert_status,
        }
    )

    return {
        "stored": len(rows_to_insert),
        "baseline": baseline,
        "alert_status": alert_status,
    }


# ---------------------------------------------------------------------------
# Handler/Admin panel endpoint
# ---------------------------------------------------------------------------


@router.get("/api/vitals/history")
def vitals_history(
    device_id: str = Query(..., description="Stable device identifier"),
    minutes: int = Query(default=60, ge=1, le=1440, description="Window in minutes"),
    current_user: dict = Depends(role_required("admin", "handler")),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Return the last *minutes* minutes of BPM readings for *device_id*.

    Response shape::

        {
            "device_id": "...",
            "readings": [
                {"timestamp": "...", "heart_rate": 75, "steps": 10},
                ...
            ],
            "baseline": 72.5,
            "alert_status": null | "Physical Activity" | "Excitement/Stress" | "Activity Alert"
        }

    Used by the Handler Panel Chart.js graph.
    """
    cutoff = _iso_ago(minutes=float(minutes))
    rows = db.execute(
        """
        SELECT timestamp, heart_rate, steps
        FROM device_vitals
        WHERE device_id = ? AND timestamp >= ?
        ORDER BY timestamp ASC
        """,
        (device_id, cutoff),
    ).fetchall()

    readings = [dict(r) for r in rows]

    baseline = _get_baseline(db, device_id)
    latest_bpm: Optional[int] = readings[-1]["heart_rate"] if readings else None
    alert_status = (
        _classify_alert(db, device_id, latest_bpm, baseline)
        if latest_bpm is not None
        else None
    )

    return {
        "device_id": device_id,
        "readings": readings,
        "baseline": baseline,
        "alert_status": alert_status,
    }
