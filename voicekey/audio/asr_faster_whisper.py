"""Automatic Speech Recognition using Faster Whisper.

This module provides ASR (Automatic Speech Recognition) capabilities for VoiceKey
using the Faster Whisper library with CTranslate2 backend for optimal performance.

Faster Whisper provides:
- Faster inference speed compared to original Whisper
- Multiple precision options (int8, float16, int8_float16)
- Streaming transcription support
- Model profile selection (tiny, base, small, medium, large)

Requirements: FR-A03, FR-A04, FR-A06
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import AsyncGenerator, Dict, List, Optional

import numpy as np
from scipy import signal as scipy_signal

logger = logging.getLogger(__name__)

# Try to import faster-whisper, provide graceful fallback.
# Keep class binding mutable so runtime/test environments can patch availability.
try:
    from faster_whisper import WhisperModel

    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False
    logger.warning("faster-whisper not available, ASR functionality will be limited")
    WhisperModel = None


# Model profile configurations
MODEL_PROFILES: Dict[str, Dict[str, str]] = {
    "tiny": {
        "description": "Fastest, lowest memory (~1GB RAM)",
        "speed": "fastest",
        "accuracy": "lowest",
        "size_mb": "75",
    },
    "base": {
        "description": "Balanced speed and accuracy (~1.5GB RAM)",
        "speed": "fast",
        "accuracy": "basic",
        "size_mb": "140",
    },
    "small": {
        "description": "Better accuracy, moderate speed (~2.5GB RAM)",
        "speed": "medium",
        "accuracy": "good",
        "size_mb": "490",
    },
    "medium": {
        "description": "High accuracy, slower (~5GB RAM)",
        "speed": "slow",
        "accuracy": "very good",
        "size_mb": "1500",
    },
    "large": {
        "description": "Best accuracy, highest resources (~6GB RAM)",
        "speed": "slowest",
        "accuracy": "best",
        "size_mb": "3000",
    },
}

# Default compute types based on model size
DEFAULT_COMPUTE_TYPES: Dict[str, str] = {
    "tiny": "int8",
    "base": "int8",
    "small": "int8_float16",
    "medium": "float16",
    "large": "float16",
}


@dataclass
class TranscriptEvent:
    """Result of speech recognition on an audio segment.

    Attributes:
        text: The recognized text content
        is_final: True if this is a final transcription, False if partial/interim
        confidence: Confidence score from 0.0 to 1.0
        language: Detected language code (e.g., 'en') or None
        timestamp_start: Start time of the audio segment in seconds, or None
        timestamp_end: End time of the audio segment in seconds, or None
    """

    text: str
    is_final: bool
    confidence: float
    language: Optional[str] = None
    timestamp_start: Optional[float] = None
    timestamp_end: Optional[float] = None


class ModelLoadError(Exception):
    """Raised when the ASR model fails to load.

    This exception indicates that the Faster Whisper model could not be
    initialized, possibly due to:
    - Missing model files
    - Unsupported model size
    - Insufficient memory
    - Invalid device specification
    """

    pass


class TranscriptionError(Exception):
    """Raised when speech transcription fails at runtime.

    This exception indicates that the ASR engine encountered an error
    during transcription processing, possibly due to:
    - Invalid audio format
    - Audio processing errors
    - Model inference failures
    """

    pass


class TranscriptionTimeoutError(TranscriptionError):
    """Raised when speech transcription exceeds the configured timeout.

    This exception indicates that the ASR engine took too long to process
    the audio and was terminated to prevent hanging.

    Attributes:
        timeout_seconds: The timeout duration that was exceeded.
    """

    def __init__(self, timeout_seconds: float, message: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        if message is None:
            message = f"Transcription timed out after {timeout_seconds} seconds"
        super().__init__(message)


class ASREngine:
    """Automatic Speech Recognition engine using Faster Whisper.

    This class provides streaming speech recognition capabilities using the
    Faster Whisper library with CTranslate2 backend. It supports:
    - Multiple model sizes (tiny, base, small, medium, large)
    - Automatic device selection (CPU, CUDA)
    - Partial and final transcript events
    - Runtime model switching
    - Configurable compute precision

    Example:
        >>> engine = ASREngine(model_size="base", device="auto")
        >>> engine.load_model()
        >>> # Transcribe audio
        >>> events = engine.transcribe(audio_samples)
        >>> for event in events:
        ...     print(f"{'[FINAL]' if event.is_final else '[PARTIAL]'} {event.text}")
    """

    SUPPORTED_SAMPLE_RATES = [8000, 16000, 22050, 32000, 44100, 48000]

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        compute_type: Optional[str] = None,
        sample_rate: int = 16000,
        transcription_timeout: float = 30.0,
    ):
        """Initialize ASR engine.

        Args:
            model_size: Model size to use (tiny, base, small, medium, large).
                       Default is "base" for balanced performance.
            device: Device to use for inference ("auto", "cpu", "cuda").
                   Default "auto" automatically selects best available.
            compute_type: Compute precision type (int8, float16, int8_float16).
                          Default None uses model-appropriate default.
            sample_rate: Audio sample rate in Hz. Default 16000.
            transcription_timeout: Maximum seconds to wait for transcription.
                                   Default 30 seconds. Set to 0 to disable timeout.

        Raises:
            ValueError: If model_size or device is not supported
        """
        if model_size not in MODEL_PROFILES:
            raise ValueError(
                f"Unsupported model size: {model_size}. "
                f"Supported: {list(MODEL_PROFILES.keys())}"
            )

        if device not in ("auto", "cpu", "cuda"):
            raise ValueError(
                f"Unsupported device: {device}. "
                f"Supported: auto, cpu, cuda"
            )

        if sample_rate not in self.SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"Unsupported sample rate: {sample_rate}. "
                f"Supported: {self.SUPPORTED_SAMPLE_RATES}"
            )

        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type or DEFAULT_COMPUTE_TYPES.get(model_size, "int8")
        self._sample_rate = sample_rate
        self._model: Optional[WhisperModel] = None
        self._model_loaded = False
        self._transcription_timeout = transcription_timeout

        # Lazy load model on first use
        logger.info(
            f"ASR engine initialized: model={model_size}, "
            f"device={device}, compute_type={self._compute_type}, "
            f"timeout={transcription_timeout}s"
        )

    @property
    def model_size(self) -> str:
        """Get current model size."""
        return self._model_size

    @property
    def device(self) -> str:
        """Get current device."""
        return self._device

    @property
    def is_model_loaded(self) -> bool:
        """Check if model is loaded and ready."""
        return self._model_loaded

    @property
    def sample_rate(self) -> int:
        """Get audio sample rate."""
        return self._sample_rate

    @property
    def transcription_timeout(self) -> float:
        """Get transcription timeout in seconds."""
        return self._transcription_timeout

    def load_model(self) -> None:
        """Load the Faster Whisper model.

        This method loads the model into memory. Call this before
        transcribing if you want to eager-load the model.

        Raises:
            ModelLoadError: If model fails to load
        """
        if self._model_loaded and self._model is not None:
            logger.debug("Model already loaded")
            return

        model_cls = self._resolve_model_class()
        if model_cls is None:
            raise ModelLoadError(
                "faster-whisper is not installed. "
                "Install with: pip install faster-whisper"
            )

        # Determine actual device
        actual_device = self._get_device()

        logger.info(
            f"Loading Faster Whisper model: {self._model_size} "
            f"on {actual_device} with {self._compute_type}"
        )

        try:
            self._model = model_cls(
                self._model_size,
                device=actual_device,
                compute_type=self._compute_type,
            )
        except Exception as e:
            # CPU-only environments may not support int8_float16 efficiently.
            if self._compute_type == "int8_float16" and "int8_float16" in str(e):
                fallback_compute_type = "int8"
                logger.warning(
                    "Compute type %s unsupported on %s; retrying with %s",
                    self._compute_type,
                    actual_device,
                    fallback_compute_type,
                )
                self._compute_type = fallback_compute_type
                self._model = model_cls(
                    self._model_size,
                    device=actual_device,
                    compute_type=self._compute_type,
                )
            else:
                logger.error(f"Failed to load model: {e}")
                raise ModelLoadError(f"Failed to load model {self._model_size}: {e}")

        self._model_loaded = True
        logger.info(f"Model {self._model_size} loaded successfully")

    def _resolve_model_class(self):
        """Resolve faster-whisper model class at runtime.

        Returns:
            Whisper model class when available, otherwise None.
        """
        global WhisperModel, FASTER_WHISPER_AVAILABLE

        runtime_module = sys.modules.get("faster_whisper")
        runtime_model_class = None
        if runtime_module is not None:
            runtime_model_class = getattr(runtime_module, "WhisperModel", None)

        if runtime_model_class is not None:
            WhisperModel = runtime_model_class
            FASTER_WHISPER_AVAILABLE = True
            return runtime_model_class

        if WhisperModel is not None:
            return WhisperModel

        try:
            from faster_whisper import WhisperModel as runtime_model_class

            WhisperModel = runtime_model_class
            FASTER_WHISPER_AVAILABLE = True
            return runtime_model_class
        except ImportError:
            FASTER_WHISPER_AVAILABLE = False
            return None

    def unload_model(self) -> None:
        """Unload the model from memory.

        Call this to free memory when ASR is not needed.
        """
        if self._model is not None:
            logger.info("Unloading ASR model")
            self._model = None
            self._model_loaded = False

    def switch_model(self, model_size: str) -> None:
        """Switch to a different model size.

        Args:
            model_size: New model size to use

        Raises:
            ValueError: If model_size is not supported
            ModelLoadError: If new model fails to load
        """
        if model_size not in MODEL_PROFILES:
            raise ValueError(
                f"Unsupported model size: {model_size}. "
                f"Supported: {list(MODEL_PROFILES.keys())}"
            )

        if model_size == self._model_size:
            logger.debug("Already using requested model size")
            return

        logger.info(f"Switching model from {self._model_size} to {model_size}")

        # Unload current model
        self.unload_model()

        # Update configuration
        self._model_size = model_size
        self._compute_type = DEFAULT_COMPUTE_TYPES.get(model_size, "int8")

        # Load new model
        self.load_model()

    def transcribe(self, audio: np.ndarray) -> List[TranscriptEvent]:
        """Transcribe audio data and return transcript events.

        This method processes the entire audio buffer at once and returns
        both partial and final transcription results.

        Args:
            audio: Audio samples as float32 numpy array with values in range [-1, 1].
                  Can be mono or stereo (will be converted to mono).

        Returns:
            List of TranscriptEvent objects, starting with partial results
            and ending with final transcription

        Raises:
            TranscriptionError: If transcription fails
            TranscriptionTimeoutError: If transcription exceeds the configured timeout
        """
        # Ensure model is loaded
        if not self._model_loaded:
            self.load_model()

        if self._model is None:
            raise TranscriptionError("Model not loaded")

        audio = self._prepare_audio(audio)
        if len(audio) == 0:
            return []

        # If timeout is disabled (0), run directly
        if self._transcription_timeout <= 0:
            return self._transcribe_internal(audio)

        # Run transcription with timeout using a thread
        result_container: dict[str, object] = {}
        exception_container: list[Exception] = []

        def _transcribe_worker() -> None:
            try:
                result_container["result"] = self._transcribe_internal(audio)
            except Exception as e:
                exception_container.append(e)

        worker_thread = threading.Thread(target=_transcribe_worker, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=self._transcription_timeout)

        if worker_thread.is_alive():
            # Thread is still running - timeout occurred
            logger.error(
                f"Transcription timed out after {self._transcription_timeout} seconds"
            )
            raise TranscriptionTimeoutError(self._transcription_timeout)

        # Thread completed - check for exceptions
        if exception_container:
            exc = exception_container[0]
            logger.error(f"Transcription error: {exc}")
            if isinstance(exc, TranscriptionTimeoutError):
                raise exc
            raise TranscriptionError(f"Transcription failed: {exc}")

        return result_container.get("result", [])  # type: ignore[return-value]

    def _transcribe_internal(self, audio: np.ndarray) -> List[TranscriptEvent]:
        """Internal transcription implementation without timeout wrapping.

        Args:
            audio: Audio samples as float32 numpy array

        Returns:
            List of TranscriptEvent objects

        Raises:
            TranscriptionError: If transcription fails
        """
        try:
            # Run transcription
            segments, info = self._model.transcribe(
                audio,
                beam_size=5,
                vad_filter=False,
                vad_parameters=dict(min_silence_duration_ms=500),
            )

            events: List[TranscriptEvent] = []

            # Get language info
            language = info.language if hasattr(info, "language") else None
            language_probability = (
                info.language_probability if hasattr(info, "language_probability") else 1.0
            )

            # Process segments
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue

                # Determine if this is a final segment
                # Faster Whisper segments are always "final" in this context
                # but we can detect partial by checking if there's more coming
                is_final = True

                # Get confidence from segment
                confidence = segment.avg_log_prob if hasattr(segment, "avg_log_prob") else 0.0
                # Convert log probability to confidence-like score
                confidence = max(0.0, min(1.0, (confidence + 2.0) / 4.0))

                # Get timestamps
                start = segment.start if hasattr(segment, "start") else None
                end = segment.end if hasattr(segment, "end") else None

                events.append(
                    TranscriptEvent(
                        text=text,
                        is_final=is_final,
                        confidence=confidence,
                        language=language,
                        timestamp_start=start,
                        timestamp_end=end,
                    )
                )

            # If we have results, also emit a "partial" event at the start
            # to match the expected partial/final flow
            if events:
                # Create a partial event with the combined text
                # This helps with real-time feedback
                partial_text = " ".join(e.text for e in events)
                events.insert(
                    0,
                    TranscriptEvent(
                        text=partial_text,
                        is_final=False,
                        confidence=language_probability,
                        language=language,
                        timestamp_start=events[0].timestamp_start if events else None,
                        timestamp_end=None,
                    ),
                )

            return events

        except TranscriptionTimeoutError:
            raise
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise TranscriptionError(f"Transcription failed: {e}")

    def _prepare_audio(self, audio: np.ndarray) -> np.ndarray:
        """Normalize and resample audio to match Faster-Whisper input expectations."""
        if not isinstance(audio, np.ndarray):
            audio = np.array(audio, dtype=np.float32)
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)

        if len(audio) == 0:
            return np.array([], dtype=np.float32)

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        target_sample_rate = 16000
        if self._sample_rate != target_sample_rate:
            original_length = len(audio)
            target_length = int(original_length * target_sample_rate / self._sample_rate)
            audio = scipy_signal.resample_poly(audio, target_length, original_length).astype(np.float32)

        return audio

    async def stream_transcribe(
        self, audio_queue: Queue, chunk_duration: float = 1.0
    ) -> AsyncGenerator[TranscriptEvent, None]:
        """Compatibility streaming wrapper for queued chunk transcription.

        This interface is currently unused by runtime code paths but is retained
        to avoid breaking external callers that may still use it.

        Args:
            audio_queue: Queue containing audio chunks (numpy arrays)
            chunk_duration: Deprecated and ignored; retained for API compatibility

        Yields:
            TranscriptEvent objects as audio is processed

        Raises:
            TranscriptionError: If streaming transcription fails
        """
        while True:
            try:
                audio_chunk = audio_queue.get(timeout=1.0)
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Stream transcription error: {e}")
                raise TranscriptionError(f"Streaming transcription failed: {e}")

            try:
                if audio_chunk is None:
                    break

                for event in self.transcribe(audio_chunk):
                    yield event
            except Exception as e:
                logger.error(f"Stream transcription error: {e}")
                raise TranscriptionError(f"Streaming transcription failed: {e}")

    def _get_device(self) -> str:
        """Determine the actual device to use.

        Returns:
            Device string: "cuda" or "cpu"
        """
        if self._device == "auto":
            # Try to detect CUDA availability
            try:
                import torch

                if torch.cuda.is_available():
                    return "cuda"
            except ImportError:
                pass
            return "cpu"
        return self._device


def get_available_models() -> List[str]:
    """Get list of available model sizes.

    Returns:
        List of model size identifiers
    """
    return list(MODEL_PROFILES.keys())


def get_model_size_info(model_size: str) -> Dict[str, str]:
    """Get information about a specific model size.

    Args:
        model_size: The model size to query

    Returns:
        Dictionary with model information (description, speed, accuracy, size)

    Raises:
        ValueError: If model_size is not recognized
    """
    if model_size not in MODEL_PROFILES:
        raise ValueError(
            f"Unknown model size: {model_size}. "
            f"Available: {list(MODEL_PROFILES.keys())}"
        )

    return MODEL_PROFILES[model_size].copy()


def get_all_model_info() -> Dict[str, Dict[str, str]]:
    """Get information about all available models.

    Returns:
        Dictionary mapping model sizes to their information
    """
    return MODEL_PROFILES.copy()


def create_asr_from_config(config: dict) -> ASREngine:
    """Create ASR engine from configuration dictionary.

    Args:
        config: Configuration dictionary with ASR settings

    Returns:
        Configured ASREngine instance
    """
    asr_config = config.get("asr", {})

    model_size = asr_config.get("model_size", "base")
    device = asr_config.get("device", "auto")
    compute_type = asr_config.get("compute_type")
    sample_rate = asr_config.get("sample_rate", 16000)
    transcription_timeout = asr_config.get("transcription_timeout", 30.0)

    return ASREngine(
        model_size=model_size,
        device=device,
        compute_type=compute_type,
        sample_rate=sample_rate,
        transcription_timeout=transcription_timeout,
    )
