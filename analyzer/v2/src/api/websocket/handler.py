"""WebSocket endpoint for real-time stream analysis."""

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from v2.src.api.websocket.manager import manager
from v2.src.core.config import config

router = APIRouter()

# Lazy import to avoid startup failures if pipeline dependencies are missing
_pipeline = None

def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from v2.src.pipeline.audio_pipeline import pipeline
        _pipeline = pipeline
    return _pipeline


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Main WebSocket endpoint for real-time analysis data.

    Client messages (JSON):
        {"action": "subscribe", "protocol": "mp3"}
        {"action": "connect_stream", "url": "http://..."}
        {"action": "disconnect_stream"}
        {"action": "ping"}

    Server messages (JSON):
        {"type": "connected", "client_count": N}
        {"type": "metrics", "data": {...}}
        {"type": "error", "message": "..."}
        {"type": "pong"}
    """
    print(f"[WS] New connection from {websocket.client}")
    await manager.connect(websocket)

    try:
        # Send welcome message
        await manager.send_json(
            websocket,
            {
                "type": "connected",
                "client_count": manager.client_count,
                "version": config.version,
            },
        )

        while True:
            # Receive and process client messages
            raw = await websocket.receive_text()
            try:
                msg: Dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_json(
                    websocket, {"type": "error", "message": "Invalid JSON"}
                )
                continue

            action = msg.get("action")

            if action == "ping":
                await manager.send_json(websocket, {"type": "pong"})

            elif action == "subscribe":
                protocol = msg.get("protocol", "unknown")
                manager.set_protocol(websocket, protocol)
                await manager.send_json(
                    websocket,
                    {
                        "type": "subscribed",
                        "protocol": protocol,
                    },
                )

            elif action == "connect_stream":
                url = msg.get("url", "")
                if not url:
                    await manager.send_json(
                        websocket,
                        {"type": "error", "message": "Missing 'url' parameter"},
                    )
                    continue

                # Start pipeline asynchronously
                async def _start_safe():
                    try:
                        await _get_pipeline().start(url)
                        # Send latest metadata immediately to this client
                        # (in case metadata was already parsed before connection)
                        last_meta = _get_pipeline().last_metadata
                        if last_meta:
                            await manager.send_json(
                                websocket,
                                {"type": "metadata", "data": last_meta},
                            )
                    except Exception as e:
                        print(f"[WS] Pipeline start failed: {e}")
                        import traceback
                        traceback.print_exc()
                        await manager.send_json(
                            websocket,
                            {"type": "error", "message": f"Pipeline start failed: {e}"},
                        )
                asyncio.create_task(_start_safe())

            elif action == "disconnect_stream":
                asyncio.create_task(_get_pipeline().stop())

            else:
                await manager.send_json(
                    websocket,
                    {"type": "error", "message": f"Unknown action: {action}"},
                )

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
