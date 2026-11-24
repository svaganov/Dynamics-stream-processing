"""DBFS Peak Meter — Sprint 1.

Scale: 0 dBFS at top, -70 dBFS at bottom.
Solid colors, no gradients.
"""

import numpy as np
from typing import Dict, Any
from v2.src.core.models import DBFSZone
from v2.src.core.constants import DBFS_FLOOR, DBFS_CEIL
from .base import AudioAnalyzerBase


class DBFSMeter(AudioAnalyzerBase):
    """Digital Peak Level Meter.
    
    Measures peak amplitude in dBFS (decibels relative to full scale).
    0 dBFS = maximum digital level (sample value = 1.0).
    """

    def __init__(self, sample_rate: int = 48000, hold_time_sec: float = 2.0):
        super().__init__(sample_rate)
        self.hold_time_sec = hold_time_sec
        self.peak_hold = DBFS_FLOOR
        self._hold_samples = 0
        self._hold_sample_count = int(hold_time_sec * sample_rate)

    def _get_zone(self, dbfs: float) -> DBFSZone:
        """Determine color zone for a dBFS value."""
        if dbfs >= -6.0:
            return DBFSZone.DANGER
        elif dbfs >= -9.0:
            return DBFSZone.WARNING
        elif dbfs >= -18.0:
            return DBFSZone.CAUTION
        elif dbfs >= -60.0:
            return DBFSZone.SAFE
        elif dbfs > DBFS_FLOOR:
            return DBFSZone.QUIET
        return DBFSZone.SILENCE

    def process(self, samples: np.ndarray) -> Dict[str, Any]:
        """Process samples and return DBFS metrics.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Dict with left, right, peak, peak_hold, zone
        """
        if not self.enabled:
            return self.to_dict()

        # Handle mono/stereo
        if samples.ndim == 1:
            left = right = samples
        else:
            left = samples[0] if samples.shape[0] > 0 else np.array([])
            right = samples[1] if samples.shape[0] > 1 else left

        # Calculate peaks
        left_peak = float(np.max(np.abs(left))) if len(left) > 0 else 0.0
        right_peak = float(np.max(np.abs(right))) if len(right) > 0 else 0.0
        overall_peak = max(left_peak, right_peak)

        # Convert to dBFS
        def to_dbfs(peak: float) -> float:
            if peak > 0:
                db = 20.0 * np.log10(peak)
                return max(db, DBFS_FLOOR)
            return DBFS_FLOOR

        left_dbfs = float(to_dbfs(left_peak))
        right_dbfs = float(to_dbfs(right_peak))
        peak_dbfs = float(to_dbfs(overall_peak))

        # Peak hold logic
        if peak_dbfs >= self.peak_hold:
            self.peak_hold = peak_dbfs
            self._hold_samples = 0
        else:
            self._hold_samples += len(left) if len(left) > 0 else len(samples)
            if self._hold_samples >= self._hold_sample_count:
                self.peak_hold = peak_dbfs
                self._hold_samples = 0

        zone = self._get_zone(peak_dbfs)

        return {
            "left": float(round(left_dbfs, 1)),
            "right": float(round(right_dbfs, 1)),
            "peak": float(round(peak_dbfs, 1)),
            "peak_hold": float(round(self.peak_hold, 1)),
            "zone": zone.value,
        }

    def reset(self) -> None:
        """Reset peak hold."""
        self.peak_hold = DBFS_FLOOR
        self._hold_samples = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return current state."""
        return {
            "left": DBFS_FLOOR,
            "right": DBFS_FLOOR,
            "peak": DBFS_FLOOR,
            "peak_hold": float(round(self.peak_hold, 1)),
            "zone": DBFSZone.SILENCE.value,
        }
