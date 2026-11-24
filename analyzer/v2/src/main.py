"""Uvicorn entry point for Media Stream Analyzer v2."""

import os
import sys

# Add project root (parent of v2/) to sys.path so 'v2.src...' imports work
# __file__ = .../v2/src/main.py → parent of v2/ is dirname(dirname(dirname(__file__)))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import uvicorn

from v2.src.core.config import config


def main() -> None:
    """Run the FastAPI application with uvicorn."""
    print(f"[Main] Starting server on {config.websocket.host}:{config.websocket.port}")
    print(f"[Main] Frontend URL: http://{config.websocket.host}:{config.websocket.port}/app/")
    print(f"[Main] WebSocket URL: ws://{config.websocket.host}:{config.websocket.port}/ws")
    uvicorn.run(
        "v2.src.api.main:app",
        host=config.websocket.host,
        port=config.websocket.port,
        reload=config.debug,
        log_level=config.log_level,
    )


if __name__ == "__main__":
    main()
