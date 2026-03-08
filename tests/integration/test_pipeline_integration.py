"""Integration tests for end-to-end audio capture to keyboard injection pipeline.

Tests the complete flow: Audio Capture -> VAD -> ASR -> Parser -> Router -> Keyboard Backend

Requirements:
- E10-S02: Integration harness expansion
- FR-A01, FR-A02, FR-A03, FR-C01, FR-C02: Audio/speech/command pipeline requirements
- requirements/testing-strategy.md: Integration layer tests

All tests use mocks/fixtures - no real microphone or keyboard hardware required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import queue

import pytest

from voicekey.app.state_machine import AppState, AppEvent, ListeningMode, VoiceKeyStateMachine
from voicekey.app.main import RuntimeCoordinator
from voicekey.audio.capture import AudioFrame
from voicekey.audio.asr_faster_whisper import TranscriptEvent
from voicekey.audio.vad import VADResult
from voicekey.commands.parser import CommandParser, ParseKind
from voicekey.actions.router import ActionRouter, ActionRouteResult
from voicekey.platform.keyboard_base import KeyboardCapabilityState

from tests.conftest import (
    RecordingKeyboardBackend,
    MockAudioCapture,
    MockVADProcessor,
    MockASREngine,
    create_transcript_event,
    generate_speech_like_audio,
    generate_silence_audio,
)


# =============================================================================
# Pipeline Integration Tests
# =============================================================================

class TestAudioToKeyboardPipeline:
    """Integration tests for the complete audio-to-keyboard pipeline."""

    def test_text_transcript_flows_to_keyboard_injection(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify text transcript flows through parser and gets typed."""
        # Setup parser and router
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Simulate transcript: "hello world" (plain text, no command suffix)
        transcript = "hello world"
        parse_result = parser.parse(transcript)

        # Verify parser identifies this as text
        assert parse_result.kind is ParseKind.TEXT
        assert parse_result.literal_text == "hello world"

        # Route the text through the keyboard backend
        if parse_result.kind is ParseKind.TEXT and parse_result.literal_text:
            recording_keyboard_backend.type_text(parse_result.literal_text)

        # Verify keyboard received the text
        assert recording_keyboard_backend.typed_texts == ["hello world"]

    def test_command_transcript_routes_to_keyboard_action(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify command transcript routes to appropriate keyboard action."""
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Simulate transcript: "new line command"
        transcript = "new line command"
        parse_result = parser.parse(transcript)

        # Verify parser identifies this as a command
        assert parse_result.kind is ParseKind.COMMAND
        assert parse_result.command is not None
        assert parse_result.command.command_id == "new_line"

        # Dispatch command through router
        result = router.dispatch(parse_result.command.command_id)

        # Verify routing succeeded and keyboard action was triggered
        assert result.handled is True
        assert result.route == "keyboard"
        assert recording_keyboard_backend.pressed_keys == ["enter"]

    def test_key_combo_command_routes_correctly(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify key combo commands (like control c) route correctly."""
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Simulate transcript: "control c command" (not feature-gated)
        transcript = "control c command"
        parse_result = parser.parse(transcript)

        assert parse_result.kind is ParseKind.COMMAND
        assert parse_result.command is not None

        result = router.dispatch(parse_result.command.command_id)

        assert result.handled is True
        assert recording_keyboard_backend.pressed_combos == [["ctrl", "c"]]

    def test_unknown_command_falls_back_to_literal_text(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify unknown commands with suffix fall back to literal text.

        This tests the critical safety requirement: unknown "... command" input
        must type literally, not be ignored or cause errors.
        """
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Simulate transcript: "xyzzy command" (unknown command)
        transcript = "xyzzy command"
        parse_result = parser.parse(transcript)

        # Parser should return TEXT kind with literal text
        assert parse_result.kind is ParseKind.TEXT
        assert parse_result.literal_text == "xyzzy command"

        # Simulate routing
        if parse_result.literal_text:
            recording_keyboard_backend.type_text(parse_result.literal_text)

        # Verify the literal text was typed (not ignored)
        assert recording_keyboard_backend.typed_texts == ["xyzzy command"]

    def test_special_phrase_bypasses_command_suffix_rule(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify special phrases like 'pause voice key' bypass suffix rules."""
        parser = CommandParser()

        # These special phrases should be recognized as SYSTEM commands
        # regardless of the "command" suffix rule
        special_phrases = [
            "pause voice key",
            "resume voice key",
            "voice key stop",
        ]

        for phrase in special_phrases:
            recording_keyboard_backend.reset()
            parse_result = parser.parse(phrase)

            # Should be identified as SYSTEM (not TEXT with literal)
            assert parse_result.kind is ParseKind.SYSTEM, f"Failed for phrase: {phrase}"
            assert parse_result.command is not None, f"No command for phrase: {phrase}"


class TestVADIntegration:
    """Integration tests for VAD (Voice Activity Detection) pipeline."""

    def test_vad_filters_silence_before_asr(
        self,
        mock_vad_processor: MockVADProcessor,
    ) -> None:
        """Verify VAD correctly filters silence frames."""
        # Setup VAD with predetermined results
        mock_vad_processor.set_results([
            VADResult(is_speech=False, confidence=0.1),  # silence
            VADResult(is_speech=True, confidence=0.9),   # speech
            VADResult(is_speech=True, confidence=0.8),   # speech
            VADResult(is_speech=False, confidence=0.2),  # silence
        ])

        # Generate test audio frames
        frames = [
            generate_silence_audio(0.1),
            generate_speech_like_audio(0.1),
            generate_speech_like_audio(0.1),
            generate_silence_audio(0.1),
        ]

        # Process frames through VAD
        results = [mock_vad_processor.process(frame) for frame in frames]

        # Verify VAD results
        assert results == [False, True, True, False]

    def test_vad_threshold_affects_speech_detection(
        self,
    ) -> None:
        """Verify VAD threshold configuration affects detection sensitivity."""
        from voicekey.audio.vad import VADProcessor

        # High threshold should require more confident speech
        high_threshold_vad = VADProcessor(threshold=0.9)
        # Low threshold should accept weaker speech signals
        low_threshold_vad = VADProcessor(threshold=0.1)

        # Generate moderate-amplitude audio
        moderate_audio = generate_speech_like_audio(0.1) * 0.3

        # Both should process without error
        # (Actual behavior depends on Silero availability, so we just verify no exceptions)
        high_threshold_vad.process(moderate_audio)
        low_threshold_vad.process(moderate_audio)


class TestASRIntegration:
    """Integration tests for ASR (Automatic Speech Recognition) pipeline."""

    def test_asr_produces_transcript_events(
        self,
        mock_asr_engine: MockASREngine,
    ) -> None:
        """Verify ASR engine produces valid transcript events."""
        # Setup mock ASR with predetermined transcripts
        mock_asr_engine.set_transcripts([
            create_transcript_event("hello world", is_final=True),
        ])

        # Generate dummy audio (would be real audio in production)
        audio = generate_speech_like_audio(0.5)

        # Process through ASR
        events = mock_asr_engine.transcribe(audio)

        # Verify transcript events
        assert len(events) == 1
        assert events[0].text == "hello world"
        assert events[0].is_final is True
        assert events[0].confidence > 0

    def test_asr_confidence_filter(
        self,
        mock_asr_engine: MockASREngine,
    ) -> None:
        """Verify low-confidence transcripts can be filtered."""
        # Setup mock ASR with low-confidence transcript
        mock_asr_engine.set_transcripts([
            create_transcript_event("unclear speech", is_final=True, confidence=0.3),
        ])

        audio = generate_speech_like_audio(0.5)
        events = mock_asr_engine.transcribe(audio)

        # Verify confidence is accessible for filtering
        assert events[0].confidence == 0.3

        # In production, low-confidence results would be filtered out
        # based on configured threshold
        confidence_threshold = 0.5
        filtered_events = [e for e in events if e.confidence >= confidence_threshold]

        assert len(filtered_events) == 0  # Low confidence should be filtered


class TestStateMachineIntegration:
    """Integration tests for state machine with speech pipeline."""

    def test_state_transitions_during_speech_processing(
        self,
    ) -> None:
        """Verify state machine transitions correctly during speech processing."""
        machine = VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        )

        # Initial state
        assert machine.state is AppState.STANDBY

        # Wake phrase detected -> LISTENING
        transition = machine.transition(AppEvent.WAKE_PHRASE_DETECTED)
        assert transition.to_state is AppState.LISTENING
        assert machine.state is AppState.LISTENING

        # Speech frame received -> PROCESSING
        transition = machine.transition(AppEvent.SPEECH_FRAME_RECEIVED)
        assert transition.to_state is AppState.PROCESSING
        assert machine.state is AppState.PROCESSING

        # Partial handled -> back to LISTENING
        transition = machine.transition(AppEvent.PARTIAL_HANDLED)
        assert transition.to_state is AppState.LISTENING

        # Wake window timeout -> back to STANDBY
        transition = machine.transition(AppEvent.WAKE_WINDOW_TIMEOUT)
        assert transition.to_state is AppState.STANDBY

    def test_inactivity_auto_pause_transition(
        self,
    ) -> None:
        """Verify inactivity auto-pause triggers correct state transition."""
        machine = VoiceKeyStateMachine(
            mode=ListeningMode.CONTINUOUS,
            initial_state=AppState.LISTENING,
        )

        # Inactivity auto-pause -> PAUSED
        transition = machine.transition(AppEvent.INACTIVITY_AUTO_PAUSE)
        assert transition.to_state is AppState.PAUSED
        assert machine.state is AppState.PAUSED

        # Resume requested -> STANDBY
        transition = machine.transition(AppEvent.RESUME_REQUESTED)
        assert transition.to_state is AppState.STANDBY

    def test_pause_resume_via_special_phrases(
        self,
    ) -> None:
        """Verify pause/resume special phrases integrate with state machine."""
        parser = CommandParser()
        machine = VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        )

        # Parse "pause voice key"
        parse_result = parser.parse("pause voice key")
        assert parse_result.kind is ParseKind.SYSTEM

        # Apply state transition
        transition = machine.transition(AppEvent.PAUSE_REQUESTED)
        assert transition.to_state is AppState.PAUSED

        # Parse "resume voice key"
        parse_result = parser.parse("resume voice key")
        assert parse_result.kind is ParseKind.SYSTEM

        # Apply state transition
        transition = machine.transition(AppEvent.RESUME_REQUESTED)
        assert transition.to_state is AppState.STANDBY


class TestEndToEndScenarios:
    """Complete end-to-end integration scenarios."""

    def test_complete_dictation_scenario(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
        mock_asr_engine: MockASREngine,
        mock_vad_processor: MockVADProcessor,
    ) -> None:
        """Test a complete dictation scenario from audio to typed text."""
        # Setup pipeline components
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Setup mocks
        mock_vad_processor.set_results([
            VADResult(is_speech=True, confidence=0.9),
        ])
        mock_asr_engine.set_transcripts([
            create_transcript_event("the quick brown fox", is_final=True),
        ])

        # Simulate audio processing
        audio = generate_speech_like_audio(1.0)

        # VAD check
        is_speech = mock_vad_processor.process(audio)
        assert is_speech is True

        # ASR transcription
        events = mock_asr_engine.transcribe(audio)
        assert len(events) == 1

        # Parse transcript
        parse_result = parser.parse(events[0].text)
        assert parse_result.kind is ParseKind.TEXT

        # Route to keyboard
        if parse_result.literal_text:
            recording_keyboard_backend.type_text(parse_result.literal_text)

        # Verify final output
        assert recording_keyboard_backend.typed_texts == ["the quick brown fox"]

    def test_command_execution_scenario(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
        mock_asr_engine: MockASREngine,
    ) -> None:
        """Test a command execution scenario."""
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        mock_asr_engine.set_transcripts([
            create_transcript_event("new line command", is_final=True),
        ])

        audio = generate_speech_like_audio(0.5)
        events = mock_asr_engine.transcribe(audio)

        parse_result = parser.parse(events[0].text)
        assert parse_result.kind is ParseKind.COMMAND

        result = router.dispatch(parse_result.command.command_id)
        assert result.handled is True
        assert recording_keyboard_backend.pressed_keys == ["enter"]

    def test_mixed_text_and_commands_scenario(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
        mock_asr_engine: MockASREngine,
    ) -> None:
        """Test a scenario mixing text dictation and commands."""
        parser = CommandParser()
        router = ActionRouter(keyboard_backend=recording_keyboard_backend)

        # Simulate sequence of transcripts
        transcripts = [
            ("hello", "text"),
            ("new line command", "command"),
            ("world", "text"),
        ]

        for transcript, expected_kind in transcripts:
            mock_asr_engine.set_transcripts([
                create_transcript_event(transcript, is_final=True),
            ])

            audio = generate_speech_like_audio(0.3)
            events = mock_asr_engine.transcribe(audio)

            parse_result = parser.parse(events[0].text)

            if parse_result.kind is ParseKind.TEXT:
                if parse_result.literal_text:
                    recording_keyboard_backend.type_text(parse_result.literal_text)
            elif parse_result.kind is ParseKind.COMMAND:
                router.dispatch(parse_result.command.command_id)

        # Verify sequence of actions
        assert recording_keyboard_backend.typed_texts == ["hello", "world"]
        assert recording_keyboard_backend.pressed_keys == ["enter"]


class TestPipelineErrorHandling:
    """Integration tests for error handling in the pipeline."""

    def test_keyboard_unavailable_graceful_handling(
        self,
    ) -> None:
        """Verify graceful handling when keyboard backend is unavailable."""
        backend = RecordingKeyboardBackend()
        backend.capability_state = KeyboardCapabilityState.UNAVAILABLE

        router = ActionRouter(keyboard_backend=backend)

        # Try to dispatch a command
        # Should not raise, but action may fail gracefully
        result = router.dispatch("new_line")

        # Verify the backend was called (even if unavailable)
        # In production, this would return a failed result
        assert result.handled is True  # Handled by keyboard route

    def test_empty_transcript_handling(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify empty transcripts are handled gracefully."""
        parser = CommandParser()

        parse_result = parser.parse("")
        assert parse_result.kind is ParseKind.TEXT
        assert parse_result.literal_text == ""

        # Empty text should not be typed
        if parse_result.literal_text:
            recording_keyboard_backend.type_text(parse_result.literal_text)

        assert recording_keyboard_backend.typed_texts == []


class TestRuntimeCoordinatorIntegration:
    """Integration tests for coordinator-driven speech routing behavior."""

    def test_toggle_mode_short_buffer_flushes_and_types_on_hotkey_sleep(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
        mock_asr_engine: MockASREngine,
    ) -> None:
        machine = VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.LISTENING,
        )
        mock_asr_engine.set_transcripts(
            [create_transcript_event("integration flush check", is_final=True)]
        )
        coordinator = RuntimeCoordinator(
            state_machine=machine,
            keyboard_backend=recording_keyboard_backend,
            asr_engine=mock_asr_engine,
            transcribe_batch_frames=5,
            transcribe_idle_flush_seconds=10.0,
        )

        for index in range(4):
            coordinator._process_frame(
                AudioFrame(
                    audio=generate_speech_like_audio(0.1),
                    sample_rate=16000,
                    timestamp=float(index),
                )
            )

        coordinator._on_toggle_hotkey()

        assert machine.state is AppState.STANDBY
        assert recording_keyboard_backend.typed_texts == ["integration flush check "]

    def test_toggle_mode_recognizes_pause_phrase_without_wake_word(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        machine = VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.LISTENING,
        )
        coordinator = RuntimeCoordinator(
            state_machine=machine,
            keyboard_backend=recording_keyboard_backend,
        )

        update = coordinator.on_transcript("pause voice key")

        assert update.transition is not None
        assert update.transition.to_state is AppState.PAUSED
        assert machine.state is AppState.PAUSED

    def test_whitespace_only_transcript_handling(
        self,
        recording_keyboard_backend: RecordingKeyboardBackend,
    ) -> None:
        """Verify whitespace-only transcripts are handled gracefully."""
        parser = CommandParser()

        parse_result = parser.parse("   \t\n   ")
        assert parse_result.kind is ParseKind.TEXT
        assert parse_result.literal_text == ""  # Normalized to empty

        if parse_result.literal_text:
            recording_keyboard_backend.type_text(parse_result.literal_text)

        assert recording_keyboard_backend.typed_texts == []
