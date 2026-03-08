"""Unit tests for ASR (Automatic Speech Recognition) module.

Tests for the Faster Whisper ASR engine implementation.
"""

from __future__ import annotations

import sys
from queue import Queue
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Mock faster-whisper before importing asr module
mock_faster_whisper = MagicMock()
mock_whisper_model = MagicMock()

# Pre-mock to avoid import errors
sys.modules["faster_whisper"] = mock_faster_whisper
mock_faster_whisper.WhisperModel = MagicMock(return_value=mock_whisper_model)

# Also mock torch for device detection
mock_torch = MagicMock()
mock_torch.cuda.is_available = MagicMock(return_value=False)
sys.modules["torch"] = mock_torch

from voicekey.audio.asr_faster_whisper import (
    ASREngine,
    MODEL_PROFILES,
    TranscriptionError,
    TranscriptEvent,
    create_asr_from_config,
    get_all_model_info,
    get_available_models,
    get_model_size_info,
    ModelLoadError,
)


class TestTranscriptEvent:
    """Tests for TranscriptEvent dataclass."""

    def test_transcript_event_creation(self):
        """Test creating a TranscriptEvent."""
        event = TranscriptEvent(
            text="Hello world",
            is_final=True,
            confidence=0.95,
            language="en",
            timestamp_start=0.5,
            timestamp_end=1.5,
        )

        assert event.text == "Hello world"
        assert event.is_final is True
        assert event.confidence == 0.95
        assert event.language == "en"
        assert event.timestamp_start == 0.5
        assert event.timestamp_end == 1.5

    def test_transcript_event_partial(self):
        """Test creating a partial transcript event."""
        event = TranscriptEvent(
            text="Hello",
            is_final=False,
            confidence=0.7,
        )

        assert event.text == "Hello"
        assert event.is_final is False
        assert event.confidence == 0.7
        assert event.language is None
        assert event.timestamp_start is None
        assert event.timestamp_end is None


class TestModelProfiles:
    """Tests for model profile configurations."""

    def test_model_profiles_exist(self):
        """Test that expected model profiles are defined."""
        profiles = ["tiny", "base", "small", "medium", "large"]

        for profile in profiles:
            assert profile in MODEL_PROFILES

    def test_model_profiles_have_required_fields(self):
        """Test that all model profiles have required fields."""
        required_fields = ["description", "speed", "accuracy", "size_mb"]

        for profile, info in MODEL_PROFILES.items():
            for field in required_fields:
                assert field in info, f"Profile {profile} missing {field}"

    def test_get_available_models(self):
        """Test getting list of available models."""
        models = get_available_models()

        assert isinstance(models, list)
        assert "tiny" in models
        assert "base" in models
        assert "small" in models

    def test_get_model_size_info(self):
        """Test getting info for a specific model."""
        info = get_model_size_info("base")

        assert "description" in info
        assert "speed" in info
        assert "accuracy" in info
        assert "size_mb" in info

    def test_get_model_size_info_invalid(self):
        """Test getting info for invalid model raises ValueError."""
        with pytest.raises(ValueError, match="Unknown model size"):
            get_model_size_info("invalid_model")

    def test_get_all_model_info(self):
        """Test getting all model info."""
        all_info = get_all_model_info()

        assert isinstance(all_info, dict)
        assert len(all_info) == 5
        assert "tiny" in all_info
        assert "base" in all_info


class TestASREngine:
    """Tests for ASREngine class."""

    def test_initialization_default(self):
        """Test initialization with default parameters."""
        engine = ASREngine()

        assert engine.model_size == "base"
        assert engine.device == "auto"
        assert engine.is_model_loaded is False
        assert engine.sample_rate == 16000

    def test_initialization_custom(self):
        """Test initialization with custom parameters."""
        engine = ASREngine(
            model_size="small",
            device="cpu",
            compute_type="float16",
            sample_rate=16000,
        )

        assert engine.model_size == "small"
        assert engine.device == "cpu"
        assert engine.sample_rate == 16000

    def test_initialization_invalid_model_size(self):
        """Test initialization with invalid model size raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported model size"):
            ASREngine(model_size="invalid")

    def test_initialization_invalid_device(self):
        """Test initialization with invalid device raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported device"):
            ASREngine(device="invalid")

    def test_initialization_invalid_sample_rate(self):
        """Test initialization with invalid sample rate raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported sample rate"):
            ASREngine(sample_rate=12345)

    def test_load_model(self):
        """Test loading the model."""
        engine = ASREngine()
        engine.load_model()

        assert engine.is_model_loaded is True
        mock_faster_whisper.WhisperModel.assert_called_once()

    def test_load_model_already_loaded(self):
        """Test that loading an already loaded model is idempotent."""
        # Reset the mock to start fresh
        mock_faster_whisper.WhisperModel.reset_mock()

        engine = ASREngine()
        engine.load_model()

        first_call_count = mock_faster_whisper.WhisperModel.call_count

        # Call load again
        engine.load_model()

        # Should not make additional calls
        assert mock_faster_whisper.WhisperModel.call_count == first_call_count

    def test_unload_model(self):
        """Test unloading the model."""
        engine = ASREngine()
        engine.load_model()

        assert engine.is_model_loaded is True

        engine.unload_model()

        assert engine.is_model_loaded is False

    def test_switch_model(self):
        """Test switching to a different model."""
        engine = ASREngine(model_size="tiny")
        engine.load_model()

        # Reset mock to track new call
        mock_faster_whisper.WhisperModel.reset_mock()

        engine.switch_model("small")

        assert engine.model_size == "small"
        assert engine.is_model_loaded is True
        mock_faster_whisper.WhisperModel.assert_called_once()

    def test_switch_model_same_size(self):
        """Test switching to the same model size."""
        engine = ASREngine(model_size="base")
        engine.load_model()

        call_count_before = mock_faster_whisper.WhisperModel.call_count

        engine.switch_model("base")

        # Should not reload
        assert mock_faster_whisper.WhisperModel.call_count == call_count_before

    def test_switch_model_invalid(self):
        """Test switching to invalid model raises ValueError."""
        engine = ASREngine()
        engine.load_model()

        with pytest.raises(ValueError, match="Unsupported model size"):
            engine.switch_model("invalid")

    def test_transcribe_empty_audio(self):
        """Test transcribing empty audio returns empty list."""
        engine = ASREngine()
        engine.load_model()

        result = engine.transcribe(np.array([], dtype=np.float32))

        assert result == []

    def test_transcribe_with_audio(self):
        """Test transcribing audio returns events."""
        engine = ASREngine()
        engine.load_model()

        # Mock the model's transcribe method
        mock_segment = MagicMock()
        mock_segment.text = "Hello world"
        mock_segment.avg_log_prob = 0.5
        mock_segment.start = 0.0
        mock_segment.end = 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.95

        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        # Generate test audio (1 second of random noise)
        audio = np.random.randn(16000).astype(np.float32) * 0.1

        result = engine.transcribe(audio)

        assert len(result) > 0
        assert isinstance(result[0], TranscriptEvent)

    def test_transcribe_resamples_audio_once_for_non_16khz_input(self):
        """Test non-16kHz input audio is resampled exactly once."""
        engine = ASREngine(sample_rate=8000, transcription_timeout=0)
        engine.load_model()

        mock_segment = MagicMock()
        mock_segment.text = "Resample test"
        mock_segment.avg_log_prob = 0.0
        mock_segment.start = 0.0
        mock_segment.end = 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.9
        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        audio = np.random.randn(8000).astype(np.float32) * 0.1

        with patch(
            "voicekey.audio.asr_faster_whisper.scipy_signal.resample_poly",
            side_effect=lambda data, up, down: np.zeros(
                int(len(data) * up / down), dtype=np.float32
            ),
        ) as resample_mock:
            result = engine.transcribe(audio)

        assert len(result) == 2
        assert resample_mock.call_count == 1

    def test_transcribe_preserves_partial_final_output_contract(self):
        """Test transcript output keeps partial-first, final-segment contract."""
        engine = ASREngine(transcription_timeout=0)
        engine.load_model()

        mock_segment = MagicMock()
        mock_segment.text = "Hello world"
        mock_segment.avg_log_prob = 0.5
        mock_segment.start = 0.0
        mock_segment.end = 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.95
        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        audio = np.random.randn(16000).astype(np.float32) * 0.1
        result = engine.transcribe(audio)

        assert len(result) == 2
        assert result[0].is_final is False
        assert result[0].text == "Hello world"
        assert result[0].language == "en"
        assert result[1].is_final is True
        assert result[1].text == "Hello world"
        assert result[1].language == "en"
        assert result[1].confidence == pytest.approx(0.625)

    def test_transcribe_model_not_loaded(self):
        """Test transcribe loads model automatically if not loaded."""
        engine = ASREngine()

        # Should auto-load when transcribe is called
        mock_segment = MagicMock()
        mock_segment.text = "Test"
        mock_segment.avg_log_prob = 0.0
        mock_segment.start = 0.0
        mock_segment.end = 0.5

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.9

        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        audio = np.random.randn(8000).astype(np.float32) * 0.1

        result = engine.transcribe(audio)

        assert engine.is_model_loaded is True


class TestASREngineErrors:
    """Tests for ASR engine error handling."""

    def test_transcribe_error(self):
        """Test that transcription errors are wrapped."""
        engine = ASREngine()
        engine.load_model()

        # Make the model transcribe raise an error
        mock_whisper_model.transcribe.side_effect = Exception("Test error")

        audio = np.random.randn(16000).astype(np.float32) * 0.1

        with pytest.raises(TranscriptionError, match="Transcription failed"):
            engine.transcribe(audio)


class TestCreateASRFromConfig:
    """Tests for create_asr_from_config helper."""

    def test_create_with_full_config(self):
        """Test creating ASR from complete config."""
        config = {
            "asr": {
                "model_size": "small",
                "device": "cpu",
                "compute_type": "int8",
                "sample_rate": 16000,
            }
        }

        engine = create_asr_from_config(config)

        assert engine.model_size == "small"
        assert engine.device == "cpu"
        assert engine.sample_rate == 16000

    def test_create_with_defaults(self):
        """Test creating ASR with default values."""
        config = {}

        engine = create_asr_from_config(config)

        assert engine.model_size == "base"
        assert engine.device == "auto"
        assert engine.sample_rate == 16000

    def test_create_with_partial_config(self):
        """Test creating ASR with partial config."""
        config = {
            "asr": {
                "model_size": "tiny",
            }
        }

        engine = create_asr_from_config(config)

        assert engine.model_size == "tiny"
        assert engine.device == "auto"  # default


class TestASREngineComputeType:
    """Tests for compute type selection."""

    def test_default_compute_type_tiny(self):
        """Test default compute type for tiny model."""
        engine = ASREngine(model_size="tiny")
        assert engine._compute_type == "int8"

    def test_default_compute_type_base(self):
        """Test default compute type for base model."""
        engine = ASREngine(model_size="base")
        assert engine._compute_type == "int8"

    def test_default_compute_type_small(self):
        """Test default compute type for small model."""
        engine = ASREngine(model_size="small")
        assert engine._compute_type == "int8_float16"

    def test_custom_compute_type(self):
        """Test custom compute type is respected."""
        engine = ASREngine(model_size="base", compute_type="float16")
        assert engine._compute_type == "float16"


class TestASRSampleRates:
    """Tests for sample rate handling."""

    def test_supported_sample_rates(self):
        """Test that expected sample rates are supported."""
        expected_rates = [8000, 16000, 22050, 32000, 44100, 48000]

        for rate in expected_rates:
            engine = ASREngine(sample_rate=rate)
            assert engine.sample_rate == rate


class TestASRStreaming:
    """Tests for streaming transcription."""

    def test_stream_transcribe_basic(self):
        """Test basic streaming transcription structure."""
        engine = ASREngine()
        engine.load_model()

        # Mock transcribe results
        mock_segment = MagicMock()
        mock_segment.text = "Streaming test"
        mock_segment.avg_log_prob = 0.5
        mock_segment.start = 0.0
        mock_segment.end = 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.95

        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        # Verify that stream_transcribe is an async generator
        import inspect

        result = engine.stream_transcribe(Queue())
        assert inspect.isasyncgen(result) or callable(result)


class TestASRTimeout:
    """Tests for transcription timeout functionality."""

    def test_default_timeout_is_30_seconds(self):
        """Test that default timeout is 30 seconds."""
        from voicekey.audio.asr_faster_whisper import ASREngine

        engine = ASREngine()
        assert engine.transcription_timeout == 30.0

    def test_custom_timeout_can_be_set(self):
        """Test that custom timeout can be configured."""
        from voicekey.audio.asr_faster_whisper import ASREngine

        engine = ASREngine(transcription_timeout=60.0)
        assert engine.transcription_timeout == 60.0

    def test_zero_timeout_disables_timeout(self):
        """Test that zero timeout disables the timeout mechanism."""
        from voicekey.audio.asr_faster_whisper import ASREngine

        engine = ASREngine(transcription_timeout=0)
        assert engine.transcription_timeout == 0

    def test_timeout_raises_transcription_timeout_error(self):
        """Test that transcription exceeding timeout raises TranscriptionTimeoutError."""
        import time
        from voicekey.audio.asr_faster_whisper import (
            ASREngine,
            TranscriptionTimeoutError,
        )

        engine = ASREngine(transcription_timeout=0.1)  # 100ms timeout
        engine.load_model()

        # Make transcribe hang for longer than timeout
        def slow_transcribe(*args, **kwargs):
            time.sleep(1.0)  # Sleep for 1 second, longer than 100ms timeout
            mock_segment = MagicMock()
            mock_segment.text = "Test"
            mock_segment.avg_log_prob = 0.0
            mock_segment.start = 0.0
            mock_segment.end = 0.5
            mock_info = MagicMock()
            mock_info.language = "en"
            mock_info.language_probability = 0.9
            return ([mock_segment], mock_info)

        mock_whisper_model.transcribe.side_effect = slow_transcribe

        audio = np.random.randn(16000).astype(np.float32) * 0.1

        with pytest.raises(TranscriptionTimeoutError) as exc_info:
            engine.transcribe(audio)

        assert exc_info.value.timeout_seconds == 0.1
        assert "timed out" in str(exc_info.value).lower()

    def test_fast_transcription_completes_within_timeout(self):
        """Test that fast transcription completes successfully."""
        from voicekey.audio.asr_faster_whisper import ASREngine

        engine = ASREngine(transcription_timeout=30.0)
        engine.load_model()

        # Mock fast transcribe
        mock_segment = MagicMock()
        mock_segment.text = "Quick test"
        mock_segment.avg_log_prob = 0.5
        mock_segment.start = 0.0
        mock_segment.end = 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.95

        mock_whisper_model.transcribe.return_value = ([mock_segment], mock_info)

        audio = np.random.randn(16000).astype(np.float32) * 0.1

        result = engine.transcribe(audio)

        assert len(result) > 0

    def test_create_asr_from_config_with_timeout(self):
        """Test that create_asr_from_config respects timeout config."""
        from voicekey.audio.asr_faster_whisper import (
            ASREngine,
            create_asr_from_config,
        )

        config = {
            "asr": {
                "model_size": "base",
                "transcription_timeout": 45.0,
            }
        }

        engine = create_asr_from_config(config)
        assert engine.transcription_timeout == 45.0

    def test_create_asr_from_config_default_timeout(self):
        """Test that create_asr_from_config uses default timeout when not specified."""
        from voicekey.audio.asr_faster_whisper import create_asr_from_config

        config = {"asr": {"model_size": "base"}}

        engine = create_asr_from_config(config)
        assert engine.transcription_timeout == 30.0
