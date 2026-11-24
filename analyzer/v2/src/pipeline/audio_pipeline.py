"""Audio analysis pipeline: Input → Decoder → Analyzer → Broadcast.

Orchestrates the data flow from stream input to WebSocket clients.
"""

import asyncio
from typing import Optional
import numpy as np

from v2.src.inputs.http_stream import HTTPAudioInput
from v2.src.analyzers.audio.audio_analyzer import AudioAnalyzer
from v2.src.api.websocket.manager import manager
from v2.src.core.config import config


class AudioPipeline:
    """Real-time audio analysis pipeline.

    Connects HTTP input → FFmpeg decoder → AudioAnalyzer → WebSocket broadcast.

    Usage:
        pipeline = AudioPipeline()
        await pipeline.start("http://example.com/stream.mp3")
        # Metrics are automatically broadcast to all WebSocket clients
        await pipeline.stop()
    """

    def __init__(self) -> None:
        self._input = HTTPAudioInput()
        self._input.on_audio(self._on_audio)
        self._input.on_metadata(self._on_metadata)
        self._input.on_status(self._on_status)
        self._input.on_bitrate(self._on_bitrate)

        self._analyzer = AudioAnalyzer(
            sample_rate=config.audio.sample_rate,
            channels=config.audio.channels,
        )

        self._url: Optional[str] = None
        self._running = False
        self._last_metadata: Optional[dict] = None
        self._metadata_heartbeat_task: Optional[asyncio.Task] = None
        self._metadata_heartbeat_interval = 5.0  # seconds

    async def start(self, url: str) -> None:
        """Start pipeline with given stream URL."""
        if self._running:
            await self.stop()

        self._url = url
        self._running = True
        self._analyzer.reset()
        self._last_metadata = None
        self._metadata_heartbeat_task = asyncio.create_task(
            self._metadata_heartbeat_loop()
        )
        await self._input.connect(url)

    async def stop(self) -> None:
        """Stop pipeline and cleanup."""
        self._running = False
        if self._metadata_heartbeat_task:
            self._metadata_heartbeat_task.cancel()
            try:
                await self._metadata_heartbeat_task
            except asyncio.CancelledError:
                pass
            self._metadata_heartbeat_task = None
        await self._input.disconnect()
        self._url = None
        self._last_metadata = None

    def _on_audio(self, samples: np.ndarray) -> None:
        """Process decoded audio block and broadcast results."""
        if not self._running:
            return

        try:
            result = self._analyzer.process(samples)
        except Exception as e:
            import traceback
            print(f"[Pipeline] Analysis error: {e}")
            traceback.print_exc()
            return

        data = result.to_dict()

        # Broadcast to all connected WebSocket clients
        asyncio.create_task(
            manager.broadcast_json(
                {
                    "type": "metrics",
                    "data": data,
                }
            )
        )

    def _on_metadata(self, metadata: dict) -> None:
        """Forward stream metadata to WebSocket clients."""
        self._last_metadata = metadata
        print(f"[Metadata] Updated: {list(metadata.keys())}")
        asyncio.create_task(
            manager.broadcast_json(
                {
                    "type": "metadata",
                    "data": metadata,
                }
            )
        )

    async def _metadata_heartbeat_loop(self) -> None:
        """Periodically re-send the latest stream metadata to clients.

        Some clients rely on regular metadata refresh even when the ICY
        title has not changed. Interval is configured in __init__.
        """
        while self._running:
            try:
                await asyncio.sleep(self._metadata_heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Metadata] Heartbeat sleep error: {e}")
                break

            if not self._running:
                break

            if self._last_metadata:
                try:
                    await manager.broadcast_json(
                        {
                            "type": "metadata",
                            "data": self._last_metadata,
                        }
                    )
                    print(f"[Metadata] Heartbeat sent to {manager.client_count} clients")
                except Exception as e:
                    print(f"[Metadata] Heartbeat broadcast error: {e}")
            else:
                print("[Metadata] Heartbeat skipped: no metadata yet")

    def _on_bitrate(self, kbps: float) -> None:
        """Forward media bitrate (MP3 payload) to WebSocket clients."""
        asyncio.create_task(
            manager.broadcast_json(
                {
                    "type": "bitrate",
                    "kbps": round(kbps, 1),
                }
            )
        )

    def _on_status(self, status: str, info: dict) -> None:
        """Forward input status to WebSocket clients."""
        asyncio.create_task(
            manager.broadcast_json(
                {
                    "type": "stream_status",
                    "status": status,
                    **info,
                }
            )
        )

    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_metadata(self) -> Optional[dict]:
        return self._last_metadata or self._input.metadata


# Singleton instance
pipeline = AudioPipeline()
