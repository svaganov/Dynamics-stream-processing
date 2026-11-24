"""Base class for all audio analyzers."""

from abc import ABC, abstractmethod
from typing import Any, Dict
import numpy as np


class AudioAnalyzerBase(ABC):
    """Abstract base class for audio analysis modules.
    
    All analyzers must implement:
    - process(samples: np.ndarray) -> Any
    - reset() -> None
    - to_dict() -> Dict[str, Any]
    """

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    @abstractmethod
    def process(self, samples: np.ndarray) -> Any:
        """Process audio samples and return metrics.
        
        Args:
            samples: Array of shape (channels, n_samples) or (n_samples,)
            
        Returns:
            Analysis result (type depends on analyzer)
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """Reset all internal state."""
        pass

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """Return current state as dictionary."""
        pass
