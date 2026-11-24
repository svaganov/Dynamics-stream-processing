"""FFmpeg audio decoder for stream input.

Spawns ffmpeg subprocess to decode any stream URL to raw PCM f32le.
Outputs numpy arrays via async callback.
"""

import asyncio
import re
import time
import numpy as np
from typing import Callable, Optional
from dataclasses import dataclass

from v2.src.core.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS


@dataclass
class DecoderConfig:
    """FFmpeg decoder configuration."""
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    sample_format: str = "f32le"
    block_size_ms: int = 100

    @property
    def bytes_per_sample(self) -> int:
        return 4  # float32

    @property
    def frame_size(self) -> int:
        """Number of frames (samples per channel) in one block."""
        return int(self.sample_rate * self.block_size_ms / 1000)

    @property
    def chunk_bytes(self) -> int:
        """Bytes to read for one block."""
        return self.frame_size * self.channels * self.bytes_per_sample


class FFmpegAudioDecoder:
    """Decode audio streams via FFmpeg subprocess.

    Usage:
        decoder = FFmpegAudioDecoder()
        decoder.on_audio(callback)
        await decoder.start("http://example.com/stream.mp3")
        # ... later ...
        await decoder.stop()
    """

    def __init__(self, config: Optional[DecoderConfig] = None):
        self.config = config or DecoderConfig()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._bitrate_task: Optional[asyncio.Task] = None
        self._running = False
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        self._metadata_callback: Optional[Callable[[dict], None]] = None
        self._bitrate_callback: Optional[Callable[[float], None]] = None
        self._metadata: dict = {}
        self._technical_parsed: bool = False
        self._input_parsed: bool = False
        self._in_metadata_section: bool = False
        self._in_input_section: bool = False
        self._error_message: Optional[str] = None
        self._input_info_event: asyncio.Event = asyncio.Event()
        self._bytes_received: int = 0
        self._media_bitrate: Optional[float] = None  # kbps from stream headers (e.g. 256)
        self._icy_br_seen: bool = False  # icy-br header wins over ffmpeg's ADTS estimate
        self._error_callback: Optional[Callable[[str], None]] = None

        # Regex patterns for metadata/technical parsing
        self._stream_title_re = re.compile(r'StreamTitle\s*[:=]\s*[\'\"]?(.+?)[\'\"]?\s*(?:;|$)')
        self._icy_name_re = re.compile(r'icy-name\s*:\s*(.+)')
        self._icy_genre_re = re.compile(r'icy-genre\s*:\s*(.+)')
        self._icy_br_re = re.compile(r'icy-br\s*:\s*(\d+)')
        self._audio_stream_re = re.compile(r'Audio:\s*(\S+)(?:\s*\([^)]*\))*\s*,\s*(\d+)\s*Hz,\s*(\S+),')
        self._bitrate_re = re.compile(r'(\d+)\s*kb/s')
        self._input_format_re = re.compile(r'Input\s+#0[\s,]+([^,\s]+)')
        self._codec_re = re.compile(r'Stream\s+#0:\d+(?:\([^)]*\))?:\s+Audio:\s+(\w+)')
        self._metadata_item_re = re.compile(r'^\s{2,}(\S+)\s*:\s*(.+)$')

    def on_audio(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register callback for decoded audio blocks.

        Callback receives numpy array of shape (channels, n_samples).
        """
        self._callback = callback

    def on_metadata(self, callback: Callable[[dict], None]) -> None:
        """Register callback for stream metadata (ICY title, etc)."""
        self._metadata_callback = callback

    def on_bitrate(self, callback: Callable[[float], None]) -> None:
        """Register callback for input bitrate updates (kbps).

        Measures actual bytes read from FFmpeg stdout over time.
        This reflects the real network throughput of the stream.
        """
        self._bitrate_callback = callback

    def on_error(self, callback: Callable[[str], None]) -> None:
        """Register callback for fatal FFmpeg errors (e.g. 404, connection failed)."""
        self._error_callback = callback

    async def start(self, url: str) -> None:
        """Start ffmpeg subprocess and begin decoding."""
        if self._running:
            return

        self._running = True
        self._metadata = {}
        self._technical_parsed = False
        self._icy_br_seen = False
        self._media_bitrate = None
        self._input_parsed = False
        self._in_metadata_section = False
        self._in_input_section = False
        self._error_message = None
        self._input_info_event.clear()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",
            # Read input at native frame rate. This is crucial for HLS and other
            # segmented streams to avoid decoding entire segments in one burst.
            "-re",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", url,
            "-vn",  # no video
            "-f", self.config.sample_format,
            "-ar", str(self.config.sample_rate),
            "-ac", str(self.config.channels),
            "-",
        ]

        print(f"[FFmpeg] Starting: {' '.join(cmd)}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._task = asyncio.create_task(self._read_loop())
        self._bitrate_task = asyncio.create_task(self._bitrate_measure_loop())
        asyncio.create_task(self._read_stderr())

    async def wait_for_input_info(self, timeout: float = 5.0) -> Optional[str]:
        """Wait until FFmpeg has parsed input info or reported an error.

        Returns error message string if an error occurred, otherwise None.
        """
        try:
            await asyncio.wait_for(self._input_info_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._error_message

    async def stop(self) -> None:
        """Stop decoder and cleanup."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._bitrate_task:
            self._bitrate_task.cancel()
            try:
                await self._bitrate_task
            except asyncio.CancelledError:
                pass
            self._bitrate_task = None

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None

    def _decode_stderr_line(self, line: bytes) -> str:
        """Decode a stderr line, falling back to cp1251 on Windows if UTF-8 fails."""
        text = line.decode('utf-8', errors='replace')
        if '\ufffd' in text:
            try:
                text = line.decode('cp1251')
            except UnicodeDecodeError:
                pass
        return text.strip()

    def _set_error(self, message: str) -> None:
        """Store a fatal FFmpeg error and notify listeners."""
        if not self._error_message:
            self._error_message = message
            print(f"[FFmpeg] Error detected: {message}")
            if self._error_callback:
                try:
                    self._error_callback(message)
                except Exception as e:
                    print(f"Error callback failed: {e}")
            self._input_info_event.set()

    def _parse_stderr_line(self, text: str) -> bool:
        """Parse a single stderr line for metadata/technical updates.

        Returns True if metadata changed and should be broadcast.
        """
        updated = False
        stripped = text.strip()

        # Track whether we are inside the Input #0 section. This prevents
        # parsing Output #0 technical data (e.g. pcm_f32le) as input codec.
        if stripped.startswith('Input #0'):
            self._in_input_section = True
        elif stripped.startswith('Output #0') or stripped.startswith('Stream mapping:'):
            self._in_input_section = False
            # Signal that we have enough input info once we reach output section
            if not self._input_info_event.is_set():
                self._input_info_event.set()

        # Detect fatal errors (404, connection refused, unsupported format, etc.)
        if 'error opening input' in text.lower() or 'error opening input files' in text.lower():
            # Try to extract meaningful message after the colon
            if ':' in text:
                self._set_error(text.split(':', 1)[1].strip())
            else:
                self._set_error(text)
        elif 'server returned 404' in text.lower():
            self._set_error('Server returned 404 Not Found')
        elif 'server returned 403' in text.lower():
            self._set_error('Server returned 403 Forbidden')
        elif 'could not find codec parameters' in text.lower():
            self._set_error('Could not find codec parameters')

        # Parse input-section-specific data (container, codec, ID3 tags, tech data).
        # ICY metadata updates (StreamTitle/icy-*) can arrive later, outside input section.
        if self._in_input_section:
            # Detect container/format from Input #0 line (e.g. "Input #0, mp3, from ...")
            if not self._input_parsed:
                m = self._input_format_re.search(text)
                if m:
                    container = m.group(1).strip().upper()
                    # Normalize common names
                    container_map = {
                        'HLS,APPLEHTTP': 'HLS',
                        'APPLEHTTP': 'HLS',
                    }
                    self._metadata['container'] = container_map.get(container, container)
                    self._input_parsed = True
                    print(f"[FFmpeg] Detected container: {self._metadata['container']}")
                    updated = True
                    self._input_info_event.set()

            # Detect codec from Stream #0 line (fallback to audio_stream_re below)
            if not self._technical_parsed:
                m = self._codec_re.search(text)
                if m:
                    self._metadata['codec'] = m.group(1).upper()

            # Parse ID3 / container Metadata section (multi-line)
            if self._in_metadata_section:
                if stripped and not text.startswith(' '):
                    # Section ended
                    self._in_metadata_section = False
                else:
                    m = self._metadata_item_re.search(text)
                    if m:
                        key = m.group(1).lower().strip()
                        value = m.group(2).strip()
                        if key in ('title', 'artist', 'album', 'genre', 'date', 'comment', 'publisher'):
                            # Map standard ID3/container keys to our metadata schema
                            target_key = key
                            if key == 'date':
                                target_key = 'year'
                            if value and self._metadata.get(target_key) != value:
                                self._metadata[target_key] = value
                                print(f"[FFmpeg] Parsed metadata: {target_key}={value}")
                                updated = True

            if stripped == 'Metadata:':
                self._in_metadata_section = True

            # Parse technical data (only once)
            if not self._technical_parsed:
                m = self._audio_stream_re.search(text)
                if m:
                    self._metadata['codec'] = m.group(1).upper()
                    self._metadata['samplerate'] = f"{m.group(2)} Hz"
                    channels_raw = m.group(3).lower()
                    if channels_raw == 'stereo':
                        self._metadata['channels'] = '2 (Stereo)'
                    elif channels_raw == 'mono':
                        self._metadata['channels'] = '1 (Mono)'
                    else:
                        self._metadata['channels'] = channels_raw
                    self._technical_parsed = True
                    updated = True

                m = self._bitrate_re.search(text)
                if m:
                    kbps = float(m.group(1))
                    self._metadata['bitrate'] = f"{int(kbps)} kbps"
                    # ffmpeg's "kb/s" figure is an estimate (e.g. for ADTS);
                    # the icy-br header is authoritative, don't overwrite it
                    if not self._icy_br_seen:
                        self._media_bitrate = kbps
                    updated = True

        # Parse ICY metadata (handles "StreamTitle: ..." and "StreamTitle='...';" forms)
        m = self._stream_title_re.search(text)
        if m:
            title = m.group(1).strip()
            # Remove trailing quote/semi if regex captured it loosely
            title = title.rstrip(";'\"")
            if ' - ' in title:
                artist, song = title.split(' - ', 1)
                self._metadata['artist'] = artist.strip()
                self._metadata['title'] = song.strip()
            else:
                self._metadata['title'] = title
                self._metadata['artist'] = 'Unknown Artist'
            print(f"[FFmpeg] Parsed StreamTitle: {title}")
            updated = True

        m = self._icy_name_re.search(text)
        if m:
            self._metadata['station'] = m.group(1).strip()
            updated = True

        m = self._icy_genre_re.search(text)
        if m:
            self._metadata['genre'] = m.group(1).strip()
            updated = True

        m = self._icy_br_re.search(text)
        if m:
            self._metadata['bitrate'] = f"{m.group(1)} kbps"
            self._media_bitrate = float(m.group(1))
            self._icy_br_seen = True
            updated = True

        return updated

    async def _read_stderr(self) -> None:
        """Read stderr from ffmpeg, parse metadata and technical data."""
        if self._process is None or self._process.stderr is None:
            return

        print("[FFmpeg] stderr reader started")
        while self._running:
            try:
                line = await self._process.stderr.readline()
            except Exception as e:
                print(f"[FFmpeg] stderr read error: {e}")
                break
            if not line:
                rc = self._process.returncode if self._process else None
                print(f"[FFmpeg] stderr EOF (returncode={rc})")
                break

            text = self._decode_stderr_line(line)
            print(f"[FFmpeg stderr] {text}")

            updated = self._parse_stderr_line(text)
            if updated and self._metadata_callback:
                try:
                    self._metadata_callback(self._metadata.copy())
                    print(f"[FFmpeg] Metadata callback fired: {self._metadata}")
                except Exception as e:
                    print(f"Metadata callback error: {e}")

        rc = self._process.returncode if self._process else None
        print(f"[FFmpeg] stderr reader finished (running={self._running}, returncode={rc})")

    async def _read_loop(self) -> None:
        """Read stdout from ffmpeg and emit audio blocks."""
        if self._process is None or self._process.stdout is None:
            print("[FFmpeg] No stdout available")
            return

        chunk_size = self.config.chunk_bytes
        block_bytes = self.config.frame_size * self.config.channels * self.config.bytes_per_sample
        buffer = bytearray()

        while self._running:
            try:
                raw = await asyncio.wait_for(
                    self._process.stdout.read(chunk_size), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[FFmpeg] Read error: {e}")
                break

            if len(raw) == 0:
                rc = self._process.returncode if self._process else None
                print(f"[FFmpeg] EOF reached (returncode={rc})")
                break

            self._bytes_received += len(raw)
            buffer.extend(raw)

            # Ensure buffer contains whole stereo frames (each frame = channels * bytes_per_sample)
            frame_bytes = self.config.channels * self.config.bytes_per_sample
            valid_bytes = (len(buffer) // frame_bytes) * frame_bytes
            if valid_bytes < len(buffer):
                buffer = buffer[:valid_bytes]

            # Emit complete blocks
            while len(buffer) >= block_bytes:
                block_data = buffer[:block_bytes]
                buffer = buffer[block_bytes:]

                samples = np.frombuffer(block_data, dtype=np.float32)
                # FFmpeg outputs interleaved: [L0, R0, L1, R1, ...]
                samples = samples.reshape(-1, self.config.channels).T

                if self._callback:
                    try:
                        self._callback(samples)
                    except Exception as e:
                        print(f"Audio callback error: {e}")
                
                # Yield control to let broadcast tasks run
                await asyncio.sleep(0)

        # Process ended
        rc = self._process.returncode if self._process else None
        print(f"[FFmpeg] _read_loop ended (running={self._running}, returncode={rc})")
        if self._process.returncode is None:
            await self.stop()

    async def _bitrate_measure_loop(self) -> None:
        """Periodically broadcast media bitrate (e.g. MP3 payload bitrate).

        Uses the bitrate reported by FFmpeg in Stream #0:0 (e.g. 256 kb/s).
        This matches VLC's 'Input bitrate' — the actual audio payload rate
        without HTTP/TCP overhead.
        """
        measure_interval = 2.0  # seconds
        while self._running:
            try:
                await asyncio.sleep(measure_interval)
            except asyncio.CancelledError:
                break

            if self._media_bitrate and self._bitrate_callback:
                try:
                    self._bitrate_callback(self._media_bitrate)
                except Exception as e:
                    print(f"[FFmpeg] Bitrate callback error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None
