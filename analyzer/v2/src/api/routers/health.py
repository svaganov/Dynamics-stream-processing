"""Health check and status endpoints."""

from fastapi import APIRouter

from v2.src.core.config import config

router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    """Basic health check endpoint."""
    return {
        "status": "ok",
        "version": config.version,
        "app_name": config.app_name,
    }


@router.get("/status")
async def server_status() -> dict:
    """Extended server status with connection info."""
    from v2.src.api.websocket.manager import manager

    return {
        "status": "ok",
        "version": config.version,
        "websocket_clients": manager.client_count,
        "config": {
            "sample_rate": config.audio.sample_rate,
            "channels": config.audio.channels,
            "ws_port": config.websocket.port,
        },
    }
