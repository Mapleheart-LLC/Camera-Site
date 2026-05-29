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

WebRTC Signaling
----------------
The server acts as a signaling relay for WebRTC screen-sharing sessions
established between a Handler and a Device.  Devices register their
``/ws/device-audio/{device_id}`` socket in ``_signaling_sockets``; the handler
panel sends ``webrtc_offer`` / ``webrtc_ice_candidate`` actions via
``/ws/handler`` and the manager routes them to the correct device socket.
Likewise, ``webrtc_answer`` and ``webrtc_ice_candidate`` messages originating
from a device are routed to its assigned handler via ``relay_signal_to_handler``.
SDP payloads and ICE candidates are forwarded verbatim – the server never
inspects or modifies their contents.
"""

from __future__ import annotations

import asyncio
import json
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
        # device_id → WebSocket for WebRTC signaling relay (/ws/device-audio/{device_id}).
        self._signaling_sockets: Dict[str, WebSocket] = {}
        # legacy handler_key (e.g., username) -> canonical user_id
        self._handler_key_cache: Dict[str, Optional[str]] = {}

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
        ws, resolved_handler_id = self._resolve_handler_socket(db, handler_id)
        if ws is None:
            return False

        try:
            await asyncio.wait_for(ws.send_bytes(chunk), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            logger.warning("Audio relay timeout for handler %s; dropping chunk", resolved_handler_id or handler_id)
            return False
        except Exception as exc:
            logger.warning("Audio relay error for handler %s: %s", resolved_handler_id or handler_id, exc)
            self.disconnect(ws, resolved_handler_id or handler_id)
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

    def connected_device_ids(self) -> list[str]:
        """Return current hot-mic/device websocket IDs."""
        return list(self._device_sockets.keys())

    def connected_device_count(self) -> int:
        """Return current number of hot-mic/device websockets."""
        return len(self._device_sockets)

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
        payload = json.dumps({"command": command})
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

    async def send_device_payload(self, payload: dict, device_id: Optional[str] = None) -> int:
        """Send an arbitrary JSON payload frame to one or all connected devices.

        Used as a transport fallback when MQTT publish is unavailable but the
        device still maintains a live ``/ws`` session.
        """
        sent = 0
        dead_ids: List[str] = []
        for did, ws in list(self._device_sockets.items()):
            if device_id is not None and did != device_id:
                continue
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=5.0)
                sent += 1
            except Exception:
                dead_ids.append(did)
        for did in dead_ids:
            self._device_sockets.pop(did, None)
        return sent

    # ------------------------------------------------------------------
    # WebRTC signaling relay
    # ------------------------------------------------------------------

    def connect_signaling_device(self, device_id: str, ws: WebSocket) -> None:
        """Register a device WebSocket for WebRTC signaling relay."""
        self._signaling_sockets[device_id] = ws
        logger.debug("Signaling device connected: %s (total: %d)", device_id, len(self._signaling_sockets))

    def disconnect_signaling_device(self, device_id: str, ws: WebSocket) -> None:
        """Remove a device WebSocket from the signaling registry."""
        if self._signaling_sockets.get(device_id) is ws:
            del self._signaling_sockets[device_id]
        logger.debug("Signaling device disconnected: %s (total: %d)", device_id, len(self._signaling_sockets))

    async def relay_signal_to_device(self, device_id: str, payload: dict) -> bool:
        """Forward a WebRTC signaling *payload* from a handler to *device_id*.

        The payload is delivered verbatim – SDP and ICE candidate contents are
        never inspected or modified.

        Returns ``True`` when the message was delivered, ``False`` when the
        device is not currently connected.
        """
        ws = self._signaling_sockets.get(device_id)
        if ws is None:
            logger.debug("Signaling relay to device %s failed: not connected", device_id)
            return False
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            logger.warning("Signaling relay timeout for device %s; dropping frame", device_id)
            return False
        except Exception as exc:
            logger.warning("Signaling relay error for device %s: %s", device_id, exc)
            self.disconnect_signaling_device(device_id, ws)
            return False

    async def relay_signal_to_handler(
        self,
        device_id: str,
        payload: dict,
        db: sqlite3.Connection,
    ) -> bool:
        """Forward a WebRTC signaling *payload* from *device_id* to its assigned handler.

        Looks up the handler assigned to *device_id* in the
        ``handler_device_assignments`` table and sends the payload – with
        ``device_id`` added so the handler can match it to the right peer – to
        that handler's active WebSocket.

        SDP and ICE candidate contents are never inspected or modified.

        Returns ``True`` when the message was delivered, ``False`` otherwise.
        """
        row = db.execute(
            "SELECT handler_id FROM handler_device_assignments WHERE device_id = ? LIMIT 1",
            (device_id,),
        ).fetchone()
        if not row:
            logger.debug("Signaling relay from device %s failed: no handler assigned", device_id)
            return False

        handler_id: str = row["handler_id"]
        ws, resolved_handler_id = self._resolve_handler_socket(db, handler_id)
        if ws is None:
            logger.debug(
                "Signaling relay from device %s failed: handler %s not connected",
                device_id,
                handler_id,
            )
            return False

        # Inject device_id so the handler panel can identify the peer.
        routed = {**payload, "device_id": device_id}
        try:
            await asyncio.wait_for(ws.send_json(routed), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Signaling relay timeout for handler %s; dropping frame",
                resolved_handler_id or handler_id,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Signaling relay error for handler %s: %s",
                resolved_handler_id or handler_id,
                exc,
            )
            self.disconnect(ws, resolved_handler_id or handler_id)
            return False

    def _resolve_handler_socket(
        self,
        db: sqlite3.Connection,
        handler_key: str,
    ) -> tuple[Optional[WebSocket], Optional[str]]:
        """Resolve a handler assignment key (user_id or username) to a live socket."""
        ws = self._handler_sockets.get(handler_key)
        if ws is not None:
            return ws, handler_key

        cached_handler_id = self._handler_key_cache.get(handler_key)
        if cached_handler_id:
            return self._handler_sockets.get(cached_handler_id), cached_handler_id
        # Negative-cache miss from a previous username lookup; skip another DB hit.
        if handler_key in self._handler_key_cache and cached_handler_id is None:
            return None, None

        row = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE LIMIT 1",
            (handler_key,),
        ).fetchone()
        if not row:
            self._handler_key_cache[handler_key] = None
            return None, None

        resolved_handler_id = row["id"]
        self._handler_key_cache[handler_key] = resolved_handler_id
        return self._handler_sockets.get(resolved_handler_id), resolved_handler_id


#: Singleton used by handler.py, vitals.py, and tpe.py.
handler_ws = _HandlerWSManager()
