"""
routers/ws_manager.py – Shared WebSocket connection manager.

Houses the ``_HandlerWSManager`` class and its singleton ``handler_ws`` instance
so that both ``routers/handler.py`` and ``routers/vitals.py`` can broadcast to
connected Handler Panel clients without creating a circular import.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class _HandlerWSManager:
    """Broadcast JSON payloads to every connected Handler Panel WebSocket client."""

    def __init__(self) -> None:
        self._connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, data: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


#: Singleton used by handler.py and vitals.py.
handler_ws = _HandlerWSManager()
