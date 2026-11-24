"""Silence Detector — Sprint 1.

Detects silence when level stays below threshold for specified duration.
"""

import numpy as np
from typing import Dict, Any
from v2.src.core.constants import DEFAULT_SILENCE_THRESHOLD_DB, DEFAULT_SILENCE_DURATION_SEC
from .base import AudioAnalyzerBase


class SilenceDetector(AudioAnalyzerBase):
    """Silence detector.
    
    Triggers when audio level remains below threshold_db
    for longer than duration_sec.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
        duration_sec: float = DEFAULT_SILENCE_DURATION_SEC,
    ):
        super().__init__(sample_rate)
        self.threshold_db = threshold_db
        self.duration_sec = duration_sec
        self._threshold_linear = 10 ** (threshold_db / 20.0)
        self._duration_samples = int(duration_sec * sample_rate)
        
        # State
        self._below_count = 0
        self._total_silence_samples = 0
        self._active = False

    def process(self, samples: np.ndarray) -> Dict[str, Any]:
        """Process samples and return silence metrics.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Dict with active, duration_sec, threshold_db
        """
        if not self.enabled:
            return self.to_dict()

        # Mix to mono
        if samples.ndim > 1:
            mono = np.mean(samples, axis=0)
        else:
            mono = samples

        # Check each sample
        below = np.abs(mono) < self._threshold_linear
        
        # Count consecutive below-threshold samples
        consecutive = 0
        max_consecutive = 0
        for is_below in below:
            if is_below:
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            else:
                consecutive = 0

        self._below_count = max_consecutive
        
        # Check if silence duration exceeded
        if max_consecutive >= self._duration_samples:
            self._active = True
            self._total_silence_samples = max_consecutive
        else:
            self._active = False
            self._total_silence_samples = 0

        duration_sec = self._total_silence_samples / self.sample_rate

        return {
            "active": self._active,
            "duration_sec": float(round(duration_sec, 2)),
            "threshold_db": float(self.threshold_db),
        }

    def reset(self) -> None:
        """Reset silence state."""
        self._below_count = 0
        self._total_silence_samples = 0
        self._active = False

    def to_dict(self) -> Dict[str, Any]:
        """Return current state."""
        return {
            "active": self._active,
            "duration_sec": float(round(self._total_silence_samples / self.sample_rate, 2)),
            "threshold_db": float(self.threshold_db),
        }
