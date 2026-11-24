"""Core data models for Media Stream Analyzer v2.

All dataclasses are JSON-serializable via to_dict().
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
from enum import Enum
import time


class DBFSZone(Enum):
    """DBFS level zones for color coding."""
    DANGER = "danger"      # -6 .. 0 dBFS
    WARNING = "warning"    # -9 .. -6 dBFS
    CAUTION = "caution"    # -18 .. -9 dBFS
    SAFE = "safe"          # -60 .. -18 dBFS
    QUIET = "quiet"        # -70 .. -60 dBFS
    SILENCE = "silence"    # < -70 dBFS


class LUFSZone(Enum):
    """LUFS level zones for color coding."""
    DANGER = "danger"      # > -14 LUFS (too loud)
    TARGET = "target"      # -24 .. -22 LUFS (EBU R128)
    SAFE = "safe"          # -30 .. -24 LUFS
    QUIET = "quiet"        # -40 .. -30 LUFS
    SILENCE = "silence"    # < -70 LUFS


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DBFSMetrics:
    """Digital Peak Level Meter metrics."""
    left: float = -70.0
    right: float = -70.0
    peak: float = -70.0
    peak_hold: float = -70.0
    zone: DBFSZone = DBFSZone.SILENCE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left": float(round(self.left, 1)),
            "right": float(round(self.right, 1)),
            "peak": float(round(self.peak, 1)),
            "peak_hold": float(round(self.peak_hold, 1)),
            "zone": self.zone.value,
        }


@dataclass
class LUFSMetrics:
    """EBU R128 loudness metrics."""
    momentary: float = -70.0    # M - 400ms
    short_term: float = -70.0   # S - 3s
    integrated: float = -70.0   # I - entire program
    zone: LUFSZone = LUFSZone.SILENCE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "momentary": float(round(self.momentary, 1)),
            "short_term": float(round(self.short_term, 1)),
            "integrated": float(round(self.integrated, 1)),
            "zone": self.zone.value,
        }


@dataclass
class TruePeakMetrics:
    """True Peak detector metrics."""
    current: float = -70.0
    max: float = -70.0
    oversample_ratio: int = 4

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current": float(round(self.current, 1)),
            "max": float(round(self.max, 1)),
            "oversample_ratio": self.oversample_ratio,
        }


@dataclass
class SpectrumMetrics:
    """FFT spectrum metrics."""
    bands: List[float] = field(default_factory=list)
    peak_freq_hz: float = 0.0
    peak_db: float = -70.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bands": [float(round(b, 1)) for b in self.bands],
            "peak_freq_hz": int(self.peak_freq_hz),
            "peak_db": float(round(self.peak_db, 1)),
        }


@dataclass
class SilenceMetrics:
    """Silence detection metrics."""
    active: bool = False
    duration_sec: float = 0.0
    threshold_db: float = -60.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "duration_sec": float(round(self.duration_sec, 2)),
            "threshold_db": float(self.threshold_db),
        }


@dataclass
class LoudnessHistory:
    """Loudness history for graphing."""
    values: List[float] = field(default_factory=list)
    window_sec: int = 60
    interval_sec: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "values": [float(round(v, 1)) for v in self.values],
            "window_sec": self.window_sec,
            "interval_sec": self.interval_sec,
        }


@dataclass
class AudioAnalysisResult:
    """Complete audio analysis result."""
    timestamp: float = field(default_factory=time.time)
    dbfs: DBFSMetrics = field(default_factory=DBFSMetrics)
    lufs: LUFSMetrics = field(default_factory=LUFSMetrics)
    true_peak: TruePeakMetrics = field(default_factory=TruePeakMetrics)
    spectrum: SpectrumMetrics = field(default_factory=SpectrumMetrics)
    silence: SilenceMetrics = field(default_factory=SilenceMetrics)
    loudness_history: LoudnessHistory = field(default_factory=LoudnessHistory)
    lra: float = 0.0  # Loudness Range in LU

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": float(self.timestamp),
            "dbfs": self.dbfs.to_dict(),
            "lufs": self.lufs.to_dict(),
            "true_peak": self.true_peak.to_dict(),
            "spectrum": self.spectrum.to_dict(),
            "silence": self.silence.to_dict(),
            "loudness_history": self.loudness_history.to_dict(),
            "lra": float(round(self.lra, 1)),
        }


@dataclass
class Alert:
    """Alert event."""
    id: str
    timestamp: float
    severity: AlertSeverity
    metric: str
    message: str
    value: float
    threshold: float
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "metric": self.metric,
            "message": self.message,
            "value": round(self.value, 2),
            "threshold": self.threshold,
            "acknowledged": self.acknowledged,
        }
