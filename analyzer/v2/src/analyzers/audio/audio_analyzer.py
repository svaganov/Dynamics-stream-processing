"""Audio Analyzer Orchestrator — Sprint 1.

Combines all audio analysis modules into a single pipeline:
- DBFS Peak Meter
- EBU R128 LUFS (M, S, I)
- True Peak Detector
- FFT Spectrum
- Silence Detector
- Loudness History Graph
"""

import time
import numpy as np
from typing import Dict, Any, Optional, Callable, List
from collections import deque

from v2.src.core.models import (
    AudioAnalysisResult,
    DBFSMetrics,
    LUFSMetrics,
    TruePeakMetrics,
    SpectrumMetrics,
    SilenceMetrics,
    LoudnessHistory,
)
from v2.src.core.constants import (
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHANNELS,
    LOUDNESS_HISTORY_WINDOW_SEC,
    LOUDNESS_HISTORY_UPDATE_INTERVAL_SEC,
    DBFS_FLOOR,
)

from .dbfs_meter import DBFSMeter
from .ebu_r128 import EBUR128Calculator
from .true_peak import TruePeakDetector
from .fft_spectrum import FFTSpectrumAnalyzer
from .silence_detector import SilenceDetector


class AudioAnalyzer:
    """Main audio analysis orchestrator.
    
    Processes audio frames and produces complete AudioAnalysisResult.
    Designed for real-time operation at 50 fps.
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ):
        self.sample_rate = sample_rate
        self.channels = channels

        # Sub-analyzers
        self.dbfs = DBFSMeter(sample_rate)
        self.lufs = EBUR128Calculator(sample_rate)
        self.true_peak = TruePeakDetector(sample_rate)
        self.spectrum = FFTSpectrumAnalyzer(sample_rate)
        self.silence = SilenceDetector(sample_rate)

        # Loudness history
        self._history_values: deque = deque(maxlen=LOUDNESS_HISTORY_WINDOW_SEC)
        self._history_interval = LOUDNESS_HISTORY_UPDATE_INTERVAL_SEC
        self._last_history_time: float = 0.0

        # LRA (Loudness Range) history
        self._lra_history: deque = deque(maxlen=75 * 60)  # 60 sec of blocks

        # Callbacks
        self._callbacks: List[Callable[[AudioAnalysisResult], None]] = []

    def on_analysis(self, callback: Callable[[AudioAnalysisResult], None]):
        """Register callback for analysis results."""
        self._callbacks.append(callback)

    def process(self, samples: np.ndarray) -> AudioAnalysisResult:
        """Process audio samples and return complete analysis.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            AudioAnalysisResult with all metrics
        """
        timestamp = time.time()

        # Ensure 2D array
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)

        # Run all analyzers
        dbfs_result = self.dbfs.process(samples)
        lufs_result = self.lufs.process(samples)
        tp_result = self.true_peak.process(samples)
        spec_result = self.spectrum.process(samples)
        silence_result = self.silence.process(samples)

        # Update loudness history
        lufs_s = lufs_result["short_term"]
        if timestamp - self._last_history_time >= self._history_interval:
            self._history_values.append(lufs_s)
            self._last_history_time = timestamp

        # Update LRA history
        self._lra_history.append(lufs_s)

        # Calculate LRA (Loudness Range)
        if len(self._lra_history) >= 10:
            hist = np.array(list(self._lra_history))
            p10 = np.percentile(hist, 10)
            p95 = np.percentile(hist, 95)
            lra = float(p95 - p10)
        else:
            lra = 0.0

        # Build result
        result = AudioAnalysisResult(
            timestamp=timestamp,
            dbfs=DBFSMetrics(
                left=dbfs_result["left"],
                right=dbfs_result["right"],
                peak=dbfs_result["peak"],
                peak_hold=dbfs_result["peak_hold"],
            ),
            lufs=LUFSMetrics(
                momentary=lufs_result["momentary"],
                short_term=lufs_result["short_term"],
                integrated=lufs_result["integrated"],
            ),
            true_peak=TruePeakMetrics(
                current=tp_result["current"],
                max=tp_result["max"],
                oversample_ratio=tp_result["oversample_ratio"],
            ),
            spectrum=SpectrumMetrics(
                bands=spec_result["bands"],
                peak_freq_hz=spec_result["peak_freq_hz"],
                peak_db=spec_result["peak_db"],
            ),
            silence=SilenceMetrics(
                active=silence_result["active"],
                duration_sec=silence_result["duration_sec"],
                threshold_db=silence_result["threshold_db"],
            ),
            loudness_history=LoudnessHistory(
                values=list(self._history_values),
                window_sec=LOUDNESS_HISTORY_WINDOW_SEC,
                interval_sec=self._history_interval,
            ),
            lra=lra,
        )

        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(result)
            except Exception as e:
                # Log but don't break pipeline
                print(f"Callback error: {e}")

        return result

    def reset(self) -> None:
        """Reset all analyzers."""
        self.dbfs.reset()
        self.lufs.reset()
        self.true_peak.reset()
        self.spectrum.reset()
        self.silence.reset()
        self._history_values.clear()
        self._lra_history.clear()
        self._last_history_time = 0.0

    def get_history(self) -> List[float]:
        """Get current loudness history values."""
        return list(self._history_values)

    def to_dict(self) -> Dict[str, Any]:
        """Return current state as dictionary."""
        return {
            "dbfs": self.dbfs.to_dict(),
            "lufs": self.lufs.to_dict(),
            "true_peak": self.true_peak.to_dict(),
            "spectrum": self.spectrum.to_dict(),
            "silence": self.silence.to_dict(),
            "loudness_history": {
                "values": list(self._history_values),
                "window_sec": LOUDNESS_HISTORY_WINDOW_SEC,
                "interval_sec": self._history_interval,
            },
            "lra": 0.0,
        }
