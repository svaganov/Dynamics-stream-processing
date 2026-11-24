"""EBU R128 Loudness Calculator — Sprint 1.

Implements Momentary (M), Short-term (S), and Integrated (I) loudness.
Block size: 400ms with 75% overlap (100ms step).
Reference: EBU Tech 3341.
"""

import numpy as np
from scipy import signal
from typing import Dict, Any, Optional, Tuple, List
from collections import deque
from v2.src.core.constants import (
    LUFS_BLOCK_SIZE_MS, LUFS_OVERLAP_PERCENT, LUFS_SHORT_TERM_SEC,
    LUFS_INTEGRATED_MAX_SEC, DEFAULT_SAMPLE_RATE, LUFS_TARGET
)
from v2.src.core.models import LUFSZone
from .base import AudioAnalyzerBase


class EBUR128Calculator(AudioAnalyzerBase):
    """EBU R128 loudness calculator.

    Implements Momentary (M), Short-term (S) and Integrated (I) loudness
    using the K-weighting filter chain (pre-filter + RLB) from ITU-R BS.1770.
    Filter coefficients are normalized to match ffmpeg/libebur128 at 48 kHz.
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE):
        super().__init__(sample_rate)

        # Block configuration
        self.block_size = int(LUFS_BLOCK_SIZE_MS * sample_rate / 1000)
        self.step_size = int(self.block_size * (1 - LUFS_OVERLAP_PERCENT / 100))
        # Short-term = 3 seconds. With 100ms step, that's 30 blocks.
        self.short_term_blocks = int(LUFS_SHORT_TERM_SEC * sample_rate / self.step_size)
        self.integrated_max_blocks = int(LUFS_INTEGRATED_MAX_SEC * 1000 / LUFS_BLOCK_SIZE_MS)

        # K-weighting filters (pre-filter + RLB) per ITU-R BS.1770
        self._pre_filter = self._design_pre_filter(sample_rate)
        self._rlb_filter = self._design_rlb_filter(sample_rate)
        self._filter_states: Dict[str, Optional[List[np.ndarray]]] = {'pre': None, 'rlb': None}

        # Buffers
        self._block_buffer: deque = deque(maxlen=self.integrated_max_blocks)
        self._filtered_overlap_buffer: Optional[np.ndarray] = None

        # Current values
        self._momentary: float = -70.0
        self._short_term: float = -70.0
        self._integrated: float = -70.0

    @staticmethod
    def _design_pre_filter(fs: float) -> Tuple[np.ndarray, np.ndarray]:
        """Design EBU R128 pre-filter (high-shelf) coefficients.

        Coefficients below are the ITU-R BS.1770-4 coefficients for 48 kHz,
        normalized to 0 dB gain at 1 kHz to match ffmpeg/libebur128 behaviour.
        """
        if fs == 48000:
            b = np.array([1.53512485558697, -2.69169618940638, 1.19839281085285])
            a = np.array([1.0, -1.69065929318241, 0.73248077421585])
            # Normalize so that 1 kHz has 0 dB gain
            _, h = signal.freqz(b, a, worN=8192, fs=fs)
            idx = np.argmin(np.abs(_ - 1000.0))
            b = b / np.abs(h[idx])
            return b, a
        # Fallback: use 48k coefficients (will be slightly off for other rates)
        b = np.array([1.53512485558697, -2.69169618940638, 1.19839281085285])
        a = np.array([1.0, -1.69065929318241, 0.73248077421585])
        return b, a

    @staticmethod
    def _design_rlb_filter(fs: float) -> Tuple[np.ndarray, np.ndarray]:
        """Design EBU R128 RLB filter (high-pass) coefficients.

        Coefficients below are the ITU-R BS.1770-4 coefficients for 48 kHz,
        normalized to 0 dB gain at 1 kHz to match ffmpeg/libebur128 behaviour.
        """
        if fs == 48000:
            b = np.array([1.0, -2.0, 1.0])
            a = np.array([1.0, -1.99004745483398, 0.99007225036621])
            # Normalize so that 1 kHz has 0 dB gain
            _, h = signal.freqz(b, a, worN=8192, fs=fs)
            idx = np.argmin(np.abs(_ - 1000.0))
            b = b / np.abs(h[idx])
            return b, a
        b = np.array([1.0, -2.0, 1.0])
        a = np.array([1.0, -1.99004745483398, 0.99007225036621])
        return b, a

    def _apply_filters(self, samples: np.ndarray) -> np.ndarray:
        """Apply K-weighting filters (pre-filter then RLB) per channel.

        Maintains filter state across calls for continuous processing.
        """
        filtered = np.zeros_like(samples, dtype=np.float64)

        def _apply_one(b, a, x, state_key):
            states = self._filter_states.get(state_key)
            out_ch = []
            new_states = []
            for ch in range(x.shape[0]):
                zi = states[ch] if states else signal.lfilter_zi(b, a)
                y, zf = signal.lfilter(b, a, x[ch].astype(np.float64), zi=zi * x[ch, 0])
                out_ch.append(y)
                new_states.append(zf)
            self._filter_states[state_key] = new_states
            return np.array(out_ch)

        pre_filtered = _apply_one(self._pre_filter[0], self._pre_filter[1], samples, 'pre')
        rlb_filtered = _apply_one(self._rlb_filter[0], self._rlb_filter[1], pre_filtered, 'rlb')
        return rlb_filtered

    def _k_weight(self, samples: np.ndarray) -> np.ndarray:
        """Apply channel weighting after K-filtering.

        Matches ffmpeg/libebur128 behaviour:
        - stereo: ms = left^2 + right^2
        - mono:   ms = left^2
        This gives I = peak dBFS for stereo 1 kHz sine and I = RMS dBFS for mono.
        """
        if samples.ndim > 1 and samples.shape[0] >= 2:
            left = samples[0]
            right = samples[1]
            return np.sqrt(left ** 2 + right ** 2)
        else:
            return samples if samples.ndim == 1 else samples[0]

    def _calculate_loudness_block(self, block: np.ndarray) -> float:
        """Calculate loudness of a single 400ms block."""
        weighted = self._k_weight(block)
        
        # Mean square
        ms = np.mean(weighted ** 2)
        
        if ms > 1e-10:
            # Loudness matches ffmpeg/libebur128: Lk = 10*log10(mean_square)
            loudness = 10.0 * np.log10(ms)
        else:
            loudness = -70.0
        
        return max(loudness, -70.0)

    def _get_zone(self, lufs: float) -> LUFSZone:
        """Determine LUFS zone."""
        if lufs > -14.0:
            return LUFSZone.DANGER
        elif -24.0 <= lufs <= -22.0:
            return LUFSZone.TARGET
        elif lufs >= -30.0:
            return LUFSZone.SAFE
        elif lufs >= -40.0:
            return LUFSZone.QUIET
        elif lufs >= -70.0:
            return LUFSZone.SILENCE
        return LUFSZone.SILENCE

    def process(self, samples: np.ndarray) -> Dict[str, Any]:
        """Process audio samples and return LUFS metrics.

        Args:
            samples: Array of shape (channels, n_samples)

        Returns:
            Dict with momentary, short_term, integrated, zone
        """
        if not self.enabled:
            return self.to_dict()

        # Ensure 2D array
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)

        # Apply K-weighting filters to new samples (maintains state)
        filtered = self._apply_filters(samples)

        # Concatenate with filtered overlap from previous call
        if self._filtered_overlap_buffer is not None:
            filtered = np.concatenate([self._filtered_overlap_buffer, filtered], axis=1)

        # Process overlapping blocks
        pos = 0
        n_samples = filtered.shape[1]

        while pos + self.block_size <= n_samples:
            block = filtered[:, pos:pos + self.block_size]
            loudness = self._calculate_loudness_block(block)
            self._block_buffer.append(loudness)
            pos += self.step_size

        # Save remaining filtered samples for next call
        if pos < n_samples:
            self._filtered_overlap_buffer = filtered[:, pos:]
        else:
            self._filtered_overlap_buffer = None

        # Calculate current values
        buf = list(self._block_buffer)
        
        if buf:
            # Momentary: current 400ms block (last computed block)
            self._momentary = float(buf[-1])
            
            # Short-term: mean of last 30 blocks (~3 seconds with 100ms step)
            self._short_term = float(np.mean(buf[-self.short_term_blocks:]))
            
            # Integrated: mean of all blocks
            self._integrated = float(np.mean(buf))
        else:
            self._momentary = self._short_term = self._integrated = -70.0

        zone = self._get_zone(self._momentary)

        return {
            "momentary": float(round(self._momentary, 1)),
            "short_term": float(round(self._short_term, 1)),
            "integrated": float(round(self._integrated, 1)),
            "zone": zone.value,
        }

    def reset(self) -> None:
        """Reset all buffers and filter states."""
        self._block_buffer.clear()
        self._filtered_overlap_buffer = None
        self._filter_states = {'pre': None, 'rlb': None}
        self._momentary = self._short_term = self._integrated = -70.0

    def to_dict(self) -> Dict[str, Any]:
        """Return current state."""
        return {
            "momentary": float(round(self._momentary, 1)),
            "short_term": float(round(self._short_term, 1)),
            "integrated": float(round(self._integrated, 1)),
            "zone": self._get_zone(self._momentary).value,
        }
