"""Core constants for Media Stream Analyzer v2."""

# Audio
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
LUFS_TARGET = -23.0  # EBU R128 target
DBFS_FLOOR = -70.0   # Minimum display value
DBFS_CEIL = 0.0      # Maximum display value

# Block sizes (EBU R128)
LUFS_BLOCK_SIZE_MS = 400
LUFS_OVERLAP_PERCENT = 75
LUFS_SHORT_TERM_SEC = 3
LUFS_INTEGRATED_MAX_SEC = 600  # 10 minutes for integrated

# True Peak
TRUE_PEAK_OVERSAMPLING = 4

# FFT
DEFAULT_FFT_SIZE = 2048
FFT_UPDATE_RATE_HZ = 50

# Silence detection
DEFAULT_SILENCE_THRESHOLD_DB = -60.0
DEFAULT_SILENCE_DURATION_SEC = 1.0

# Loudness history
LOUDNESS_HISTORY_WINDOW_SEC = 3600
LOUDNESS_HISTORY_UPDATE_INTERVAL_SEC = 1.0

# WebSocket
WS_MESSAGE_RATE_HZ = 50

# Time windows for charts
TIME_WINDOWS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
}
