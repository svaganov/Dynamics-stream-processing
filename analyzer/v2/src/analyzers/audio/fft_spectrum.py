"""FFT Spectrum Analyzer — Sprint 1.

Real-time FFT with configurable bands for visualization.
"""

import numpy as np
from typing import Dict, Any, List
from v2.src.core.constants import DEFAULT_FFT_SIZE, DEFAULT_SAMPLE_RATE
from .base import AudioAnalyzerBase


class FFTSpectrumAnalyzer(AudioAnalyzerBase):
    """FFT spectrum analyzer.
    
    Provides frequency spectrum divided into bands.
    Default: 31 bands (similar to professional analyzers).
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        fft_size: int = DEFAULT_FFT_SIZE,
        n_bands: int = 31,
    ):
        super().__init__(sample_rate)
        self.fft_size = fft_size
        self.n_bands = n_bands
        
        # Band edges (log-spaced)
        self.band_edges = self._compute_band_edges()
        
        # Window function (Hann)
        self._window = np.hanning(fft_size)

    def _compute_band_edges(self) -> List[float]:
        """Compute logarithmically spaced band edges."""
        f_min = 20.0   # Hz
        f_max = self.sample_rate / 2  # Nyquist
        
        edges = []
        for i in range(self.n_bands + 1):
            # Log spacing
            log_min = np.log10(f_min)
            log_max = np.log10(f_max)
            log_val = log_min + (log_max - log_min) * i / self.n_bands
            edges.append(10 ** log_val)
        
        return edges

    def process(self, samples: np.ndarray) -> Dict[str, Any]:
        """Process samples and return spectrum metrics.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Dict with bands, peak_freq, peak_db
        """
        if not self.enabled:
            return self.to_dict()

        # Mix to mono if stereo
        if samples.ndim > 1:
            mono = np.mean(samples, axis=0)
        else:
            mono = samples

        # Ensure enough samples
        if len(mono) < self.fft_size:
            mono = np.pad(mono, (0, self.fft_size - len(mono)))

        # Take latest fft_size samples
        frame = mono[-self.fft_size:]
        
        # Apply window
        windowed = frame * self._window
        
        # FFT
        fft = np.fft.rfft(windowed)
        magnitude = np.abs(fft)
        
        # Convert to dB
        power = magnitude ** 2
        power_db = 10.0 * np.log10(power + 1e-10)
        power_db = np.clip(power_db, -70.0, 0.0)
        
        # Frequency resolution
        freq_resolution = self.sample_rate / self.fft_size
        freqs = np.arange(len(power_db)) * freq_resolution
        
        # Aggregate into bands
        bands = []
        for i in range(self.n_bands):
            low = self.band_edges[i]
            high = self.band_edges[i + 1]
            
            # Find indices in range
            mask = (freqs >= low) & (freqs < high)
            if np.any(mask):
                band_power = np.mean(power_db[mask])
            else:
                band_power = -70.0
            
            bands.append(float(band_power))

        # Peak frequency
        peak_idx = np.argmax(power_db)
        peak_freq = freqs[peak_idx]
        peak_db = float(power_db[peak_idx])

        return {
            "bands": [float(round(b, 1)) for b in bands],
            "peak_freq_hz": int(peak_freq),
            "peak_db": float(round(peak_db, 1)),
        }

    def reset(self) -> None:
        """No state to reset."""
        pass

    def to_dict(self) -> Dict[str, Any]:
        """Return empty state."""
        return {
            "bands": [-70.0] * self.n_bands,
            "peak_freq_hz": 0,
            "peak_db": -70.0,
        }
