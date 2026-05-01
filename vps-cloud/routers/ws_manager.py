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

Hot-mic (tpeapp)
----------------
Devices from the TPE Flutter app connect to ``/ws`` (no device_id in path).
The manager registers these connections in ``_device_sockets`` keyed by
device_id (or a generated ID when none is provided).  Binary audio chunks
from these devices are broadcast to all connected handler sockets so the
partner panel can listen.  The manager also exposes ``send_mic_command()``
so the handler panel can send ``START_HOT_MIC`` / ``STOP_HOT_MIC`` commands
back to one or all connected devices.
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
        # device_id → WebSocket for TPE hot-mic relay (tpeapp /ws endpoint).
        self._device_sockets: Dict[str, WebSocket] = {}

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

        # db connections are always created via get_db_connection() which sets
        # row_factory = sqlite3.Row, so dict-style column access is safe.
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

    async def relay_audio_broadcast(self, chunk: bytes) -> None:
        """Broadcast binary audio *chunk* to all connected handler sockets.

        Used by the ``/ws`` hot-mic endpoint when the device_id is unknown or
        not yet assigned to a specific handler.
        """
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await asyncio.wait_for(ws.send_bytes(chunk), timeout=5.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    # ------------------------------------------------------------------
    # Device hot-mic socket registry (tpeapp /ws endpoint)
    # ------------------------------------------------------------------

    def connect_device(self, device_id: str, ws: WebSocket) -> None:
        """Register a TPE device WebSocket for hot-mic relay."""
        self._device_sockets[device_id] = ws
        logger.debug("Hot-mic device connected: %s (total: %d)", device_id, len(self._device_sockets))

    def disconnect_device(self, device_id: str, ws: WebSocket) -> None:
        """Remove a TPE device WebSocket from the registry."""
        if self._device_sockets.get(device_id) is ws:
            del self._device_sockets[device_id]
        logger.debug("Hot-mic device disconnected: %s (total: %d)", device_id, len(self._device_sockets))

    async def send_mic_command(self, command: str, device_id: Optional[str] = None) -> int:
        """Send a ``{"command": <command>}`` JSON frame to one or all devices.

        ``command`` is typically ``"START_HOT_MIC"`` or ``"STOP_HOT_MIC"``.
        When *device_id* is ``None`` the command is broadcast to every connected
        device.  Returns the number of devices the command was sent to.
        """
        import json as _json
        payload = _json.dumps({"command": command})
        sent = 0

        dead_ids: List[str] = []
        for did, ws in list(self._device_sockets.items()):
            if device_id is not None and did != device_id:
                continue
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
                sent += 1
            except Exception:
                dead_ids.append(did)
        for did in dead_ids:
            self._device_sockets.pop(did, None)
        return sent


#: Singleton used by handler.py, vitals.py, and tpe.py.
handler_ws = _HandlerWSManager()
