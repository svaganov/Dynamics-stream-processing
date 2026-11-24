"""FastAPI application entry point."""

import os
import sys

# Add project root (parent of v2/) to sys.path so 'v2.src...' imports work
# __file__ = .../v2/src/api/main.py → parent of v2/ is dirname(dirname(dirname(dirname(__file__))))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from v2.src.core.config import config
from v2.src.api.routers import health
from v2.src.api.websocket import handler as ws_handler


# Path to frontend static files (relative to this file)
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    print("[FastAPI] Creating app...")
    app = FastAPI(
        title=config.app_name,
        version=config.version,
        debug=config.debug,
    )
    print("[FastAPI] App created, mounting routers...")

    # CORS for frontend development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # REST routers
    app.include_router(health.router, prefix="/api", tags=["health"])

    # WebSocket router
    app.include_router(ws_handler.router, tags=["websocket"])

    # Debug: simple WebSocket echo for testing connectivity
    @app.websocket("/ws-echo")
    async def ws_echo(websocket):
        await websocket.accept()
        await websocket.send_text("echo: connected")
        while True:
            msg = await websocket.receive_text()
            await websocket.send_text(f"echo: {msg}")

    # Serve frontend static files at /app/ subpath
    if os.path.isdir(FRONTEND_DIR):
        app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

    return app


app = create_app()
