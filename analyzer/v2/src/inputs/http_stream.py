"""HTTP/Icecast audio stream input.

Manages connection to audio streams and emits decoded PCM blocks.
"""

from typing import Optional, Callable
import numpy as np

from v2.src.decoders.ffmpeg_decoder import FFmpegAudioDecoder, DecoderConfig
from v2.src.inputs.icy_metadata import ICYMetadataReader
from v2.src.core.config import config


class HTTPAudioInput:
    """Input layer for HTTP/Icecast audio streams.

    Usage:
        input = HTTPAudioInput()
        input.on_audio(callback)
        await input.connect("http://example.com/stream.mp3")
        # ... later ...
        await input.disconnect()
    """

    def __init__(self, decoder_config: Optional[DecoderConfig] = None):
        self._decoder = FFmpegAudioDecoder(decoder_config)
        self._decoder.on_audio(self._on_audio_block)
        self._decoder.on_metadata(self._on_decoder_metadata)
        self._decoder.on_error(self._on_decoder_error)
        self._icy_reader = ICYMetadataReader()
        self._icy_reader.on_metadata(self._on_icy_metadata)
        self._url: Optional[str] = None
        self._metadata: dict = {}
        self._audio_callback: Optional[Callable[[np.ndarray], None]] = None
        self._status_callback: Optional[Callable[[str, dict], None]] = None
        self._metadata_callback: Optional[Callable[[dict], None]] = None
        self._bitrate_callback: Optional[Callable[[float], None]] = None

    def on_audio(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register callback for decoded audio blocks."""
        self._audio_callback = callback

    def on_metadata(self, callback: Callable[[dict], None]) -> None:
        """Register callback for stream metadata."""
        self._metadata_callback = callback

    def on_status(self, callback: Callable[[str, dict], None]) -> None:
        """Register callback for status changes.

        Callback receives (status, info_dict) where status is one of:
        "connecting", "connected", "disconnected", "error".
        """
        self._status_callback = callback

    def on_bitrate(self, callback: Callable[[float], None]) -> None:
        """Register callback for input bitrate updates (kbps)."""
        self._bitrate_callback = callback
        self._decoder.on_bitrate(callback)

    async def connect(self, url: str) -> None:
        """Connect to stream URL."""
        self._url = url
        self._metadata = {}
        self._emit_status("connecting", {"url": url})
        await self._decoder.start(url)

        # Wait for FFmpeg to parse input info or report an error. This prevents
        # showing "connected" when the URL is 404 or unsupported.
        error = await self._decoder.wait_for_input_info(timeout=5.0)
        if error:
            self._emit_status("error", {"url": url, "message": error})
            await self._decoder.stop()
            return

        self._emit_status("connected", {"url": url})
        # Start direct ICY metadata reader in parallel. This reinforces FFmpeg's
        # stderr parsing, which some stations/Windows builds emit unreliably.
        await self._icy_reader.start(url)

    async def disconnect(self) -> None:
        """Disconnect from stream."""
        await self._icy_reader.stop()
        await self._decoder.stop()
        self._emit_status("disconnected", {})
        self._url = None
        self._metadata = {}

    def _on_audio_block(self, samples: np.ndarray) -> None:
        """Internal handler for decoder audio blocks."""
        if self._audio_callback:
            self._audio_callback(samples)

    def _on_decoder_metadata(self, metadata: dict) -> None:
        """Internal handler for decoder metadata (codec, station, etc)."""
        self._metadata.update(metadata)
        self._emit_metadata(self._metadata.copy())

    def _on_decoder_error(self, message: str) -> None:
        """Internal handler for fatal FFmpeg errors."""
        self._emit_status("error", {"message": message})

    def _on_icy_metadata(self, metadata: dict) -> None:
        """Internal handler for direct ICY metadata updates."""
        self._metadata.update(metadata)
        self._emit_metadata(self._metadata.copy())

    def _emit_metadata(self, metadata: dict) -> None:
        """Notify registered metadata callback."""
        if self._metadata_callback:
            try:
                self._metadata_callback(metadata)
            except Exception as e:
                print(f"Metadata callback error: {e}")

    def _emit_status(self, status: str, info: dict) -> None:
        if self._status_callback:
            try:
                self._status_callback(status, info)
            except Exception as e:
                print(f"Status callback error: {e}")

    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._decoder.is_running

    @property
    def metadata(self) -> dict:
        return self._metadata.copy()
