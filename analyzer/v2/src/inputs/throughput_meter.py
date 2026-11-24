"""Network throughput meter for HTTP streams.

Measures real download speed (kbps) by reading the stream directly.
"""

import asyncio
import time
from typing import Callable, Optional
import aiohttp


class NetworkThroughputMeter:
    """Measure network throughput for a given URL.

    Usage:
        meter = NetworkThroughputMeter()
        meter.on_data(callback)
        await meter.start("http://example.com/stream.mp3")
        # ... later ...
        await meter.stop()
    """

    def __init__(self, measure_interval_sec: float = 5.0, sample_duration_sec: float = 1.0):
        self._measure_interval = measure_interval_sec
        self._sample_duration = sample_duration_sec
        self._callback: Optional[Callable[[float], None]] = None
        self._url: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def on_data(self, callback: Callable[[float], None]) -> None:
        """Register callback for throughput updates (kbps)."""
        self._callback = callback

    async def start(self, url: str) -> None:
        """Start measuring throughput."""
        if self._running:
            return
        self._running = True
        self._url = url
        self._task = asyncio.create_task(self._measure_loop())

    async def stop(self) -> None:
        """Stop measuring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._url = None

    async def _measure_loop(self) -> None:
        """Periodic measurement loop."""
        while self._running:
            try:
                kbps = await self._measure_once()
                if self._callback and kbps > 0:
                    self._callback(kbps)
            except Exception as e:
                print(f"[Throughput] Measurement error: {e}")

            # Wait until next measurement
            try:
                await asyncio.wait_for(
                    self._wait_for_stop(), timeout=self._measure_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _measure_once(self) -> float:
        """Measure throughput for sample_duration seconds.

        Returns throughput in kbps (kilobits per second).
        """
        url = self._url
        if not url:
            return 0.0

        timeout = aiohttp.ClientTimeout(total=self._sample_duration + 5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                start_time = time.monotonic()
                total_bytes = 0

                while self._running:
                    chunk = await response.content.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    elapsed = time.monotonic() - start_time
                    if elapsed >= self._sample_duration:
                        break

                elapsed = time.monotonic() - start_time
                if elapsed <= 0:
                    return 0.0

                # kbps = bytes * 8 / 1000 / seconds
                kbps = (total_bytes * 8) / (elapsed * 1000)
                return kbps

    async def _wait_for_stop(self) -> None:
        """Block until stop() is called."""
        while self._running:
            await asyncio.sleep(0.1)
