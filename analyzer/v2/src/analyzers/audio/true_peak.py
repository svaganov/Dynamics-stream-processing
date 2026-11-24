"""True Peak Detector — Sprint 1.

Uses 4x oversampling with a polyphase FIR low-pass filter to detect
inter-sample peaks per ITU-R BS.1770.
"""

import numpy as np
from scipy import signal
from typing import Dict, Any
from v2.src.core.constants import TRUE_PEAK_OVERSAMPLING, DBFS_FLOOR
from .base import AudioAnalyzerBase


class TruePeakDetector(AudioAnalyzerBase):
    """True Peak detector with 4x oversampling.
    
    Inter-sample peaks can exceed 0 dBFS even when all samples
    are below full scale. A polyphase FIR filter is used for proper
    band-limited upsampling as required by ITU-R BS.1770.
    """

    def __init__(self, sample_rate: int = 48000, oversampling: int = TRUE_PEAK_OVERSAMPLING):
        super().__init__(sample_rate)
        self.oversampling = oversampling
        self._max_true_peak = DBFS_FLOOR

    def _upsample(self, samples: np.ndarray) -> np.ndarray:
        """Upsample using a polyphase FIR low-pass filter.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Upsampled array with the same number of dimensions.
        """
        if samples.ndim > 1:
            channels = []
            for ch in range(samples.shape[0]):
                up = self._upsample_channel(samples[ch])
                channels.append(up)
            return np.array(channels)
        else:
            return self._upsample_channel(samples)

    def _upsample_channel(self, samples: np.ndarray) -> np.ndarray:
        """Upsample a single channel using scipy.signal.resample_poly.
        
        4x upsampling with a Kaiser-windowed FIR low-pass filter
        approximates the ITU-R BS.1770 true-peak oversampling stage.
        """
        # Pad with linear extension to reduce edge transients
        return signal.resample_poly(
            samples.astype(np.float64),
            self.oversampling,
            1,
            window=("kaiser", 5.0),
            padtype="line",
        )

    def process(self, samples: np.ndarray) -> Dict[str, Any]:
        """Process samples and return True Peak metrics.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Dict with current and max true peak in dBTP
        """
        if not self.enabled:
            return self.to_dict()

        # Upsample
        upsampled = self._upsample(samples)
        
        # Find peak in upsampled signal
        peak = float(np.max(np.abs(upsampled)))

        # Convert to dBTP
        if peak > 1e-10:
            dbtp = 20.0 * np.log10(peak)
        else:
            dbtp = DBFS_FLOOR

        dbtp = max(dbtp, DBFS_FLOOR)

        # Update max
        if dbtp > self._max_true_peak:
            self._max_true_peak = dbtp

        return {
            "current": float(round(dbtp, 1)),
            "max": float(round(self._max_true_peak, 1)),
            "oversample_ratio": self.oversampling,
        }

    def reset(self) -> None:
        """Reset max peak."""
        self._max_true_peak = DBFS_FLOOR

    def to_dict(self) -> Dict[str, Any]:
        """Return current state."""
        return {
            "current": DBFS_FLOOR,
            "max": float(round(self._max_true_peak, 1)),
            "oversample_ratio": self.oversampling,
        }
