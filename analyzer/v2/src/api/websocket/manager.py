"""WebSocket connection manager for real-time metric broadcast."""

import json
import asyncio
from typing import Set, Dict, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts messages."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._client_protocols: Dict[WebSocket, str] = {}

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        self._client_protocols[websocket] = "unknown"

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        self._client_protocols.pop(websocket, None)

    def set_protocol(self, websocket: WebSocket, protocol: str) -> None:
        self._client_protocols[websocket] = protocol

    async def send_json(self, websocket: WebSocket, data: Dict[str, Any]) -> None:
        try:
            await websocket.send_json(data)
        except Exception:
            self.disconnect(websocket)
            raise

    async def broadcast_json(self, data: Dict[str, Any]) -> None:
        """Send JSON message to all connected clients."""
        disconnected = set()
        for ws in list(self._connections):
            try:
                await ws.send_json(data)
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(ws)

    async def broadcast_to_protocol(self, protocol: str, data: Dict[str, Any]) -> None:
        """Send JSON message to clients subscribed to a specific protocol."""
        disconnected = set()
        for ws, proto in list(self._client_protocols.items()):
            if proto == protocol:
                try:
                    await ws.send_json(data)
                except Exception:
                    disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(ws)


# Singleton instance
manager = ConnectionManager()
