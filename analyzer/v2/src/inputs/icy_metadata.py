"""Direct ICY metadata reader for Icecast/Shoutcast streams.

Fetches only metadata (does not decode audio) using the ICY protocol:
1. Send HTTP GET with header `Icy-MetaData: 1`.
2. Server replies with `icy-metaint: N` (metadata interval in bytes).
3. After every N audio bytes read 1 length byte, then length*16 metadata bytes.
4. Parse `StreamTitle='...'` from metadata block.

This is used as a fallback/reinforcement when FFmpeg stderr metadata updates
are delayed or not emitted for a particular stream.
"""

import asyncio
import html
import re
from typing import Callable, Optional
from urllib.parse import urlparse


class ICYMetadataReader:
    """Read ICY metadata from a stream URL without decoding audio."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callback: Optional[Callable[[dict], None]] = None
        self._url: Optional[str] = None

    def on_metadata(self, callback: Callable[[dict], None]) -> None:
        """Register callback for metadata updates."""
        self._callback = callback

    async def start(self, url: str) -> None:
        """Start the ICY metadata reader."""
        if self._running:
            return
        self._running = True
        self._url = url
        self._task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Stop the ICY metadata reader."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._url = None

    @staticmethod
    def _parse_stream_title(meta_text: str) -> Optional[dict]:
        """Extract artist/title from `StreamTitle='...'` block."""
        m = re.search(r"StreamTitle='([^']*)'", meta_text)
        if not m:
            return None
        # Icecast 2.4 экранирует не-latin1 символы HTML-сущностями (&#1050;...)
        title = html.unescape(m.group(1)).strip()
        if not title:
            return None
        if " - " in title:
            artist, song = title.split(" - ", 1)
            return {"artist": artist.strip(), "title": song.strip()}
        return {"title": title, "artist": "Unknown Artist"}

    async def _read_loop(self) -> None:
        """Main loop: connect, read headers, then poll metadata blocks."""
        try:
            parsed = urlparse(self._url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=(parsed.scheme == "https")
                ),
                timeout=10.0,
            )

            request_lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host}",
                "Icy-MetaData: 1",
                "User-Agent: MediaStreamAnalyzer/2.0",
                "Connection: close",
                "",
                "",
            ]
            writer.write("\r\n".join(request_lines).encode("utf-8"))
            await writer.drain()

            # Read HTTP headers
            headers: dict[str, str] = {}
            status_line = await reader.readline()
            if not status_line:
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    break
                if ":" in text:
                    key, value = text.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            metaint_header = headers.get("icy-metaint")
            if not metaint_header:
                print("[ICY] No icy-metaint header; direct metadata reading unavailable")
                return

            try:
                metaint = int(metaint_header)
            except ValueError:
                print(f"[ICY] Invalid icy-metaint value: {metaint_header}")
                return

            if metaint <= 0:
                print("[ICY] icy-metaint is 0; no inline metadata on this stream")
                return

            print(f"[ICY] Reading metadata every {metaint} bytes from {self._url}")

            while self._running:
                # Skip audio payload
                audio_data = await reader.readexactly(metaint)
                if not audio_data:
                    break

                # Metadata length byte: value * 16 = metadata bytes count
                length_byte = await reader.readexactly(1)
                meta_length = length_byte[0] * 16
                if meta_length > 0:
                    meta_bytes = await reader.readexactly(meta_length)
                    # Trim trailing NUL padding used by some servers
                    meta_bytes = meta_bytes.rstrip(b"\x00")
                    meta_text = meta_bytes.decode("utf-8", errors="replace")
                    metadata = self._parse_stream_title(meta_text)
                    if metadata:
                        print(f"[ICY] Metadata update: {metadata}")
                        if self._callback:
                            try:
                                self._callback(metadata)
                            except Exception as e:
                                print(f"[ICY] Callback error: {e}")

        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError:
            print("[ICY] Stream closed by server")
        except Exception as e:
            print(f"[ICY] Reader error: {e}")
        finally:
            print("[ICY] Reader finished")
