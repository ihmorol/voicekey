"""Unit tests for app-layer wake/FSM runtime coordination (E02-S01)."""

from __future__ import annotations

import queue
from typing import Any, cast

import numpy as np
import pytest

from voicekey.actions.router import ActionRouter
from voicekey.app.main import RuntimeCoordinator
from voicekey.app.routing_policy import RuntimeRoutingPolicy
from voicekey.app.state_machine import (
    AppEvent,
    AppState,
    InvalidTransitionError,
    ListeningMode,
    VoiceKeyStateMachine,
)
from voicekey.app.watchdog import InactivityWatchdog, WatchdogTimerConfig
from voicekey.audio.asr_faster_whisper import TranscriptEvent
from voicekey.audio.wake import WakeWindowController
from voicekey.platform.hotkey_base import HotkeyRegistrationResult


class RecordingKeyboardBackend:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.combos: list[tuple[str, ...]] = []
        self.texts: list[str] = []

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def press_combo(self, keys: list[str]) -> None:
        self.combos.append(tuple(keys))

    def type_text(self, text: str, delay_ms: int = 0) -> None:
        del delay_ms
        self.texts.append(text)


class FakeClock:
    """Deterministic monotonic clock for timeout tests."""

    def __init__(self) -> None:
        self._now = 100.0

    def now(self) -> float:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now += seconds


class StubAudioCapture:
    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def get_audio_queue(self) -> queue.Queue[Any]:
        return self._queue


class DropAwareAudioCapture(StubAudioCapture):
    def __init__(self) -> None:
        super().__init__()
        self.drop_callback: Any = None

    def set_drop_callback(self, callback: Any) -> None:
        self.drop_callback = callback

    def emit_drop(self, dropped_frames: int) -> None:
        if callable(self.drop_callback):
            self.drop_callback(dropped_frames)


class RecordingHotkeyBackend:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.shutdown_calls = 0

    def register(self, hotkey: str, callback: Any) -> HotkeyRegistrationResult:
        self.registered.append(hotkey)
        self._callback = callback
        return HotkeyRegistrationResult(hotkey=hotkey, registered=True)

    def unregister(self, hotkey: str) -> None:
        self.unregistered.append(hotkey)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class StubASREngine:
    def __init__(self, events: list[TranscriptEvent]) -> None:
        self._events = events

    @property
    def is_model_loaded(self) -> bool:
        return True

    def transcribe(self, _audio: Any) -> list[TranscriptEvent]:
        return self._events


def test_wake_phrase_transitions_standby_to_listening_and_opens_window() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
    )

    update = coordinator.on_transcript("VOICE   key")

    assert update.wake_detected is True
    assert update.transition is not None
    assert update.transition.from_state is AppState.STANDBY
    assert update.transition.to_state is AppState.LISTENING
    assert update.routed_text is None
    assert coordinator.state is AppState.LISTENING
    assert coordinator.is_wake_window_open is True


def test_non_wake_transcript_in_standby_does_not_transition_or_open_window() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
    )

    update = coordinator.on_transcript("hello there")

    assert update.wake_detected is False
    assert update.transition is None
    assert update.routed_text is None
    assert coordinator.state is AppState.STANDBY
    assert coordinator.is_wake_window_open is False


def test_standby_wake_phrase_is_ignored_when_vad_is_inactive() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
    )

    update = coordinator.on_transcript("voice key", vad_active=False)

    assert update.wake_detected is False
    assert update.transition is None
    assert coordinator.state is AppState.STANDBY


def test_wake_window_timeout_transitions_listening_to_standby() -> None:
    clock = FakeClock()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        wake_window=WakeWindowController(timeout_seconds=5.0, time_provider=clock.now),
    )
    coordinator._watchdog = InactivityWatchdog(
        config=WatchdogTimerConfig(wake_window_timeout_seconds=5.0),
        clock=clock.now,
    )

    coordinator.on_transcript("voice key")
    clock.tick(5.01)
    update = coordinator.poll()

    assert update.transition is not None
    assert update.transition.from_state is AppState.LISTENING
    assert update.transition.to_state is AppState.STANDBY
    assert coordinator.state is AppState.STANDBY
    assert coordinator.is_wake_window_open is False


def test_activity_hooks_reset_wake_timeout() -> None:
    clock = FakeClock()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        wake_window=WakeWindowController(timeout_seconds=5.0, time_provider=clock.now),
    )
    coordinator._watchdog = InactivityWatchdog(
        config=WatchdogTimerConfig(wake_window_timeout_seconds=5.0),
        clock=clock.now,
    )

    coordinator.on_transcript("voice key")
    clock.tick(4.0)
    coordinator.on_activity()
    clock.tick(4.0)
    no_timeout = coordinator.poll()

    assert no_timeout.transition is None
    assert coordinator.state is AppState.LISTENING

    clock.tick(1.1)
    timeout_update = coordinator.poll()
    assert timeout_update.transition is not None
    assert timeout_update.transition.to_state is AppState.STANDBY


def test_transcript_activity_resets_wake_timeout() -> None:
    clock = FakeClock()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        wake_window=WakeWindowController(timeout_seconds=5.0, time_provider=clock.now),
    )
    coordinator._watchdog = InactivityWatchdog(
        config=WatchdogTimerConfig(wake_window_timeout_seconds=5.0),
        clock=clock.now,
    )

    coordinator.on_transcript("voice key")
    clock.tick(4.0)
    update = coordinator.on_transcript("dictation payload")
    clock.tick(4.0)
    no_timeout = coordinator.poll()

    assert update.transition is None
    assert no_timeout.transition is None
    assert coordinator.state is AppState.LISTENING

    clock.tick(1.1)
    timeout_update = coordinator.poll()
    assert timeout_update.transition is not None
    assert timeout_update.transition.to_state is AppState.STANDBY


def test_default_wake_window_timeout_is_five_seconds() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
    )

    assert coordinator.wake_window_timeout_seconds == 5.0


def test_paused_resume_phrase_transitions_to_standby() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.PAUSED,
        ),
    )

    update = coordinator.on_transcript("resume voice key")

    assert update.transition is not None
    assert update.transition.from_state is AppState.PAUSED
    assert update.transition.to_state is AppState.STANDBY
    assert coordinator.state is AppState.STANDBY


def test_paused_stop_phrase_transitions_to_shutting_down() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.PAUSED,
        ),
    )

    update = coordinator.on_transcript("voice key stop")

    assert update.transition is not None
    assert update.transition.from_state is AppState.PAUSED
    assert update.transition.to_state is AppState.SHUTTING_DOWN
    assert coordinator.state is AppState.SHUTTING_DOWN


def test_paused_text_and_non_system_command_are_suppressed() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.PAUSED,
        ),
    )

    text_update = coordinator.on_transcript("hello from paused")
    command_update = coordinator.on_transcript("new line command")

    assert text_update.transition is None
    assert command_update.transition is None
    assert coordinator.state is AppState.PAUSED


def test_paused_resume_phrase_channel_can_be_disabled() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.PAUSED,
        ),
        routing_policy=RuntimeRoutingPolicy(paused_resume_phrase_enabled=False),
    )

    resume_update = coordinator.on_transcript("resume voice key")
    stop_update = coordinator.on_transcript("voice key stop")

    assert resume_update.transition is None
    assert stop_update.transition is not None
    assert stop_update.transition.to_state is AppState.SHUTTING_DOWN


def test_rapid_pause_resume_sequence_remains_deterministic() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.WAKE_WORD,
        initial_state=AppState.STANDBY,
    )
    coordinator = RuntimeCoordinator(state_machine=machine)

    machine.transition(AppEvent.PAUSE_REQUESTED)
    resume_update = coordinator.on_transcript("resume voice key")
    pause_transition = machine.transition(AppEvent.PAUSE_REQUESTED)
    stop_update = coordinator.on_transcript("voice key stop")

    assert resume_update.transition is not None
    assert resume_update.transition.from_state is AppState.PAUSED
    assert resume_update.transition.to_state is AppState.STANDBY
    assert pause_transition.from_state is AppState.STANDBY
    assert pause_transition.to_state is AppState.PAUSED
    assert stop_update.transition is not None
    assert stop_update.transition.from_state is AppState.PAUSED
    assert stop_update.transition.to_state is AppState.SHUTTING_DOWN


def test_race_style_resume_phrase_disabled_still_allows_hotkey_resume() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.WAKE_WORD,
        initial_state=AppState.PAUSED,
    )
    coordinator = RuntimeCoordinator(
        state_machine=machine,
        routing_policy=RuntimeRoutingPolicy(paused_resume_phrase_enabled=False),
    )

    phrase_resume = coordinator.on_transcript("resume voice key")
    hotkey_resume = machine.transition(AppEvent.RESUME_REQUESTED)
    repause = machine.transition(AppEvent.PAUSE_REQUESTED)
    stop_update = coordinator.on_transcript("voice key stop")

    assert phrase_resume.transition is None
    assert hotkey_resume.from_state is AppState.PAUSED
    assert hotkey_resume.to_state is AppState.STANDBY
    assert repause.from_state is AppState.STANDBY
    assert repause.to_state is AppState.PAUSED
    assert stop_update.transition is not None
    assert stop_update.transition.to_state is AppState.SHUTTING_DOWN


def test_race_style_invalid_interleaving_is_state_machine_guarded() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.WAKE_WORD,
        initial_state=AppState.PAUSED,
    )
    coordinator = RuntimeCoordinator(state_machine=machine)

    coordinator.on_transcript("resume voice key")

    with pytest.raises(InvalidTransitionError):
        machine.transition(AppEvent.RESUME_REQUESTED)


def test_listening_state_routes_literal_text_output() -> None:
    routed_texts: list[str] = []
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        text_output=routed_texts.append,
    )

    coordinator.on_transcript("voice key")
    update = coordinator.on_transcript("hello from runtime")

    assert update.transition is None
    assert update.routed_text == "hello from runtime"
    assert routed_texts == ["hello from runtime"]


def test_listening_state_routes_command_to_action_router() -> None:
    keyboard = RecordingKeyboardBackend()
    router = ActionRouter(keyboard_backend=cast(Any, keyboard))  # type: ignore[arg-type]
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        action_router=router,
    )

    coordinator.on_transcript("voice key")
    update = coordinator.on_transcript("new line command")

    assert update.executed_command_id == "new_line"
    assert keyboard.keys == ["enter"]


def test_listening_pause_system_phrase_transitions_to_paused() -> None:
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
    )

    coordinator.on_transcript("voice key")
    update = coordinator.on_transcript("pause voice key")

    assert update.transition is not None
    assert update.transition.to_state is AppState.PAUSED
    assert coordinator.state is AppState.PAUSED


def test_low_confidence_transcript_event_is_dropped_before_routing() -> None:
    routed_texts: list[str] = []
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.WAKE_WORD,
            initial_state=AppState.STANDBY,
        ),
        text_output=routed_texts.append,
    )

    coordinator.on_transcript("voice key")
    update = coordinator.on_transcript_event(
        TranscriptEvent(text="low confidence text", is_final=True, confidence=0.1)
    )

    assert update.transition is None
    assert update.routed_text is None
    assert routed_texts == []


def test_toggle_hotkey_transitions_between_standby_and_listening_in_toggle_mode() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.TOGGLE,
        initial_state=AppState.STANDBY,
    )
    coordinator = RuntimeCoordinator(state_machine=machine)

    coordinator._on_toggle_hotkey()
    assert coordinator.state is AppState.LISTENING

    coordinator._on_toggle_hotkey()
    assert coordinator.state is AppState.STANDBY


def test_toggle_hotkey_transitions_in_wake_word_mode() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.WAKE_WORD,
        initial_state=AppState.STANDBY,
    )
    coordinator = RuntimeCoordinator(state_machine=machine)

    coordinator._on_toggle_hotkey()
    assert coordinator.state is AppState.LISTENING
    assert coordinator.is_wake_window_open is True

    coordinator._on_toggle_hotkey()
    assert coordinator.state is AppState.STANDBY
    assert coordinator.is_wake_window_open is False


def test_toggle_hotkey_resumes_when_paused() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.WAKE_WORD,
        initial_state=AppState.PAUSED,
    )
    coordinator = RuntimeCoordinator(state_machine=machine)

    coordinator._on_toggle_hotkey()
    assert coordinator.state is AppState.STANDBY


def test_start_registers_and_stop_unregisters_injected_hotkey_backend() -> None:
    machine = VoiceKeyStateMachine(
        mode=ListeningMode.TOGGLE,
        initial_state=AppState.INITIALIZING,
    )
    hotkey_backend = RecordingHotkeyBackend()
    coordinator = RuntimeCoordinator(
        state_machine=machine,
        audio_capture=StubAudioCapture(),
        vad_processor=object(),
        asr_engine=object(),
        hotkey_backend=hotkey_backend,
    )

    coordinator.start()
    coordinator.stop()

    assert hotkey_backend.registered == ["ctrl+shift+`"]
    assert hotkey_backend.unregistered == ["ctrl+shift+`"]
    assert hotkey_backend.shutdown_calls == 1


def test_toggle_mode_listening_transcript_routes_text_to_keyboard() -> None:
    keyboard = RecordingKeyboardBackend()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.LISTENING,
        ),
        keyboard_backend=cast(Any, keyboard),
    )

    update = coordinator.on_transcript("hello from toggle")

    assert update.routed_text == "hello from toggle"
    assert keyboard.texts == ["hello from toggle "]


def test_toggle_hotkey_sleep_flushes_buffered_audio_before_standby() -> None:
    keyboard = RecordingKeyboardBackend()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.LISTENING,
        ),
        keyboard_backend=cast(Any, keyboard),
        asr_engine=StubASREngine(
            [TranscriptEvent(text="flush me", is_final=True, confidence=0.9)]
        ),
        transcribe_batch_frames=5,
    )
    coordinator._audio_buffer = [np.ones(100, dtype=np.float32), np.ones(100, dtype=np.float32)]

    coordinator._on_toggle_hotkey()

    assert coordinator.state is AppState.STANDBY
    assert keyboard.texts == ["flush me "]


def test_toggle_mode_inactivity_timeout_transitions_to_paused() -> None:
    clock = FakeClock()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.LISTENING,
        ),
    )
    coordinator._watchdog = InactivityWatchdog(
        config=WatchdogTimerConfig(inactivity_auto_pause_seconds=2.0),
        clock=clock.now,
    )

    assert coordinator.poll().transition is None
    clock.tick(2.1)
    update = coordinator.poll()

    assert update.transition is not None
    assert update.transition.to_state is AppState.PAUSED
    assert coordinator.state is AppState.PAUSED


def test_start_installs_audio_drop_callback_and_updates_runtime_counter() -> None:
    capture = DropAwareAudioCapture()
    coordinator = RuntimeCoordinator(
        state_machine=VoiceKeyStateMachine(
            mode=ListeningMode.TOGGLE,
            initial_state=AppState.INITIALIZING,
        ),
        audio_capture=capture,
        vad_processor=object(),
        asr_engine=object(),
    )

    coordinator.start()
    capture.emit_drop(4)
    coordinator.stop()

    assert coordinator.dropped_audio_frames == 4
