"""Application configuration using Pydantic Settings."""

from pydantic import Field
from pydantic_settings import BaseSettings
from typing import Optional


class AudioConfig(BaseSettings):
    """Audio analysis configuration."""
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    channels: int = Field(default=2, ge=1, le=8)
    block_size_ms: int = Field(default=100, ge=50, le=1000)
    lufs_target: float = Field(default=-23.0, ge=-40.0, le=-10.0)
    silence_threshold_db: float = Field(default=-60.0, ge=-90.0, le=-20.0)
    silence_duration_sec: float = Field(default=1.0, ge=0.1, le=10.0)
    fft_size: int = Field(default=2048, ge=256, le=8192)


class WebSocketConfig(BaseSettings):
    """WebSocket server configuration."""
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1024, le=65535)
    message_rate_hz: int = Field(default=50, ge=1, le=100)
    max_clients: int = Field(default=10, ge=1, le=100)


class SRTConfig(BaseSettings):
    """SRT protocol configuration."""
    enabled: bool = True
    default_url: str = "srt://127.0.0.1:9000"
    default_mode: str = "caller"
    latency_ms: int = Field(default=120, ge=20, le=8000)


class AppConfig(BaseSettings):
    """Global application configuration."""
    app_name: str = "Media Stream Analyzer v2"
    version: str = "2.0.0"
    debug: bool = False
    log_level: str = "info"

    audio: AudioConfig = Field(default_factory=AudioConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    srt: SRTConfig = Field(default_factory=SRTConfig)

    class Config:
        env_prefix = "MSA_"
        env_nested_delimiter = "__"


# Singleton instance
config = AppConfig()
