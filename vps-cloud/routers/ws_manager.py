"""
routers/ws_manager.py – Shared WebSocket connection manager.

Houses the ``_HandlerWSManager`` class and its singleton ``handler_ws`` instance
so that both ``routers/handler.py`` and ``routers/vitals.py`` can broadcast to
connected Handler Panel clients without creating a circular import.

Audio relay
-----------
Devices connect to ``/ws/device-audio/{device_id}`` and stream binary audio
chunks.  The manager looks up which handler user is assigned to that device
and forwards every chunk directly to that handler's open ``/ws/handler``
WebSocket, bypassing the broadcast list entirely so the data reaches only the
intended recipient.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class _HandlerWSManager:
    """Manage Handler Panel WebSocket connections and audio relay."""

    def __init__(self) -> None:
        # All connected handler sockets – used for JSON broadcast (status/ping).
        self._connections: List[WebSocket] = []
        # user_id → WebSocket for targeted audio relay.
        self._handler_sockets: Dict[str, WebSocket] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, ws: WebSocket, user_id: Optional[str] = None) -> None:
        """Accept *ws* and register it for both broadcast and, when *user_id*
        is supplied, targeted audio relay."""
        await ws.accept()
        self._connections.append(ws)
        if user_id:
            self._handler_sockets[user_id] = ws

    def disconnect(self, ws: WebSocket, user_id: Optional[str] = None) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass
        if user_id and self._handler_sockets.get(user_id) is ws:
            del self._handler_sockets[user_id]

    # ------------------------------------------------------------------
    # JSON broadcast (unchanged semantics)
    # ------------------------------------------------------------------

    async def broadcast(self, data: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    # ------------------------------------------------------------------
    # Binary audio relay
    # ------------------------------------------------------------------

    async def relay_audio(
        self,
        device_id: str,
        chunk: bytes,
        db: sqlite3.Connection,
    ) -> bool:
        """Forward a binary *chunk* from *device_id* to its assigned handler.

        Looks up the handler assigned to *device_id* in the
        ``handler_device_assignments`` table, then sends the raw bytes to that
        handler's active WebSocket (if connected).

        Returns ``True`` when the chunk was delivered, ``False`` otherwise
        (handler not connected or not assigned).
        """
        row = db.execute(
            "SELECT handler_id FROM handler_device_assignments WHERE device_id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
        if not row:
            return False

        handler_id: str = row["handler_id"]
        ws = self._handler_sockets.get(handler_id)
        if ws is None:
            return False

        try:
            await asyncio.wait_for(ws.send_bytes(chunk), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            logger.warning("Audio relay timeout for handler %s; dropping chunk", handler_id)
            return False
        except Exception as exc:
            logger.warning("Audio relay error for handler %s: %s", handler_id, exc)
            self.disconnect(ws, handler_id)
            return False


#: Singleton used by handler.py and vitals.py.
handler_ws = _HandlerWSManager()
