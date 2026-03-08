"""Application-layer runtime coordination for VoiceKey listening modes."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from voicekey.actions.router import ActionRouter
from voicekey.app.routing_policy import RuntimeRoutingPolicy
from voicekey.app.state_machine import (
    AppEvent,
    AppState,
    ListeningMode,
    TransitionResult,
    VoiceKeyStateMachine,
)
from voicekey.app.watchdog import InactivityWatchdog, WatchdogTimerConfig, WatchdogTimeoutType
from voicekey.audio.asr_faster_whisper import TranscriptEvent
from voicekey.audio.threshold import ConfidenceFilter
from voicekey.audio.wake import WakePhraseDetector, WakeWindowController
from voicekey.commands.parser import CommandParser, ParseKind, create_parser
from voicekey.platform.keyboard_base import KeyboardBackend

logger = logging.getLogger(__name__)

# Hotkey backend - platform specific
HotkeyBackend = None
try:
    import sys
    if sys.platform == "linux":
        from voicekey.platform.hotkey_linux import LinuxHotkeyBackend as HotkeyBackend
    elif sys.platform == "win32":
        from voicekey.platform.hotkey_windows import WindowsHotkeyBackend as HotkeyBackend
except ImportError:
    pass

# Audio components - will be imported conditionally
AudioCapture = None
AudioFrame = None
VADProcessor = None
_streaming_vad = None

# Try to import audio components - may fail if PortAudio is missing
_audio_import_error = None
try:
    from voicekey.audio.capture import AudioCapture, AudioFrame
except Exception as e:
    _audio_import_error = e
    logger.debug(f"Audio capture not available: {e}")

try:
    from voicekey.audio.vad import VADProcessor
except Exception as e:
    logger.debug(f"VAD processor not available: {e}")


@dataclass(frozen=True)
class RuntimeUpdate:
    """Deterministic runtime update emitted by coordinator operations."""

    transition: TransitionResult | None = None
    wake_detected: bool = False
    routed_text: str | None = None
    executed_command_id: str | None = None


class RuntimeCoordinator:
    """Binds wake detection/window control to FSM transitions."""

    def __init__(
        self,
        state_machine: VoiceKeyStateMachine,
        wake_detector: WakePhraseDetector | None = None,
        wake_window: WakeWindowController | None = None,
        parser: CommandParser | None = None,
        routing_policy: RuntimeRoutingPolicy | None = None,
        action_router: ActionRouter | None = None,
        text_output: Callable[[str], None] | None = None,
        confidence_filter: ConfidenceFilter | None = None,
        # Audio components
        audio_capture=None,  # Type: AudioCapture | None
        vad_processor=None,  # Type: VADProcessor | None
        asr_engine=None,
        asr_engine_factory: Callable[[], object] | None = None,
        keyboard_backend: KeyboardBackend | None = None,
        # Hotkey
        hotkey_backend=None,
        toggle_hotkey: str = "ctrl+shift+`",
        # Configuration
        device_index: int | None = None,
        sample_rate: int = 16000,
        vad_threshold: float = 0.5,
        wake_window_timeout_seconds: float = 5.0,
        inactivity_auto_pause_seconds: float = 30.0,
        chunk_duration_seconds: float = 0.1,
        transcribe_batch_frames: int = 5,
        transcribe_idle_flush_seconds: float = 0.5,
    ) -> None:
        self._state_machine = state_machine
        self._wake_detector = wake_detector or WakePhraseDetector()
        self._wake_window = wake_window or WakeWindowController(timeout_seconds=wake_window_timeout_seconds)
        self._parser = parser or create_parser()
        self._routing_policy = routing_policy or RuntimeRoutingPolicy()
        self._action_router = action_router
        self._text_output = text_output
        self._confidence_filter = confidence_filter or ConfidenceFilter(log_dropped=False)

        # Audio components
        self._audio_capture = audio_capture
        self._vad_processor = vad_processor
        self._asr_engine = asr_engine
        self._asr_engine_factory = asr_engine_factory
        self._keyboard_backend = keyboard_backend

        # Hotkey
        self._hotkey_backend = hotkey_backend
        self._toggle_hotkey = toggle_hotkey
        self._is_manual_wake = False  # Track if manually woken by hotkey

        # Audio configuration
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._vad_threshold = vad_threshold
        self._chunk_duration_seconds = chunk_duration_seconds
        self._transcribe_batch_frames = max(3, transcribe_batch_frames)
        self._transcribe_idle_flush_seconds = max(0.0, transcribe_idle_flush_seconds)

        # Runtime state
        self._lock = threading.Lock()
        self._is_running = False
        self._processing_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._watchdog = InactivityWatchdog(
            config=WatchdogTimerConfig(
                wake_window_timeout_seconds=wake_window_timeout_seconds,
                inactivity_auto_pause_seconds=inactivity_auto_pause_seconds,
            )
        )

        # Audio buffers for ASR
        self._audio_buffer: list = []
        self._buffer_lock = threading.Lock()
        self._last_audio_frame_at: float | None = None
        self._dropped_audio_frames = 0

        # Text output callback for runtime
        self._runtime_text_output: Callable[[str], None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the runtime coordinator is currently running."""
        with self._lock:
            return self._is_running

    @property
    def dropped_audio_frames(self) -> int:
        """Total dropped audio frames reported by audio capture."""
        return self._dropped_audio_frames

    @property
    def toggle_hotkey(self) -> str:
        """Configured toggle hotkey binding."""
        return self._toggle_hotkey

    @property
    def listening_mode(self) -> ListeningMode:
        """Configured listening mode."""
        return self._state_machine.mode

    def start(self) -> None:
        """Start the audio pipeline and begin voice recognition."""
        with self._lock:
            if self._is_running:
                logger.warning("RuntimeCoordinator already running")
                return

            logger.info("Starting RuntimeCoordinator")

            # Initialize audio capture if not provided
            if self._audio_capture is None:
                if AudioCapture is None:
                    logger.warning("Audio capture not available (sounddevice/PortAudio missing)")
                    self._is_running = False
                    return
                try:
                    self._audio_capture = AudioCapture(
                        device_index=self._device_index,
                        sample_rate=self._sample_rate,
                        chunk_duration=self._chunk_duration_seconds,
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize audio capture: {e}")
                    self._is_running = False
                    return

            # Initialize VAD processor if not provided
            if self._vad_processor is None:
                if VADProcessor is None:
                    logger.warning("VAD processor not available")
                    self._is_running = False
                    return
                try:
                    # Use lower threshold to be more sensitive to speech
                    self._vad_processor = VADProcessor(threshold=0.3)
                except Exception as e:
                    logger.error(f"Failed to initialize VAD processor: {e}")
                    self._is_running = False
                    return

            # Initialize ASR engine if not provided and available
            if self._asr_engine is None and self._asr_engine_factory is not None:
                try:
                    self._asr_engine = self._asr_engine_factory()
                except Exception as e:
                    logger.error(f"Failed to initialize ASR engine from config: {e}")
                    self._is_running = False
                    return

            if self._asr_engine is None:
                try:
                    from voicekey.audio.asr_faster_whisper import ASREngine

                    self._asr_engine = ASREngine(
                        model_size="tiny",  # Use tiny for faster download
                        device="auto",
                        sample_rate=self._sample_rate,
                    )
                except ImportError:
                    logger.warning("ASR engine not available, speech recognition disabled")

            # Initialize keyboard backend and action router if not provided
            if self._action_router is None and self._keyboard_backend is not None:
                self._action_router = ActionRouter(keyboard_backend=self._keyboard_backend)

            self._install_audio_drop_callback()

            # Initialize hotkey backend
            if self._hotkey_backend is None and HotkeyBackend is not None:
                try:
                    self._hotkey_backend = HotkeyBackend()
                except Exception as e:
                    logger.warning(f"Failed to initialize hotkey backend: {e}")
                    self._hotkey_backend = None
            if self._hotkey_backend is not None:
                try:
                    result = self._hotkey_backend.register(
                        self._toggle_hotkey,
                        self._on_toggle_hotkey,
                    )
                    if result.registered:
                        logger.info(f"Toggle hotkey registered: {self._toggle_hotkey}")
                    elif result.alternatives:
                        logger.warning(
                            "Failed to register hotkey %s. Suggested alternatives: %s",
                            self._toggle_hotkey,
                            ", ".join(result.alternatives),
                        )
                    else:
                        logger.warning(f"Failed to register hotkey: {self._toggle_hotkey}")
                except Exception as e:
                    logger.warning(f"Failed to register toggle hotkey: {e}")

            # Mark as running
            self._is_running = True
            self._stop_event.clear()

            # Start audio capture
            self._audio_capture.start()

            # Start processing thread
            self._processing_thread = threading.Thread(
                target=self._audio_processing_loop,
                daemon=True,
            )
            self._processing_thread.start()

            # Transition state machine to STANDBY
            try:
                self._state_machine.transition(AppEvent.INITIALIZATION_SUCCEEDED)
            except Exception as e:
                logger.warning(f"Could not transition to STANDBY: {e}")

            logger.info("RuntimeCoordinator started successfully")

    def stop(self) -> None:
        """Stop the audio pipeline and shutdown gracefully."""
        with self._lock:
            if not self._is_running:
                logger.warning("RuntimeCoordinator not running")
                return

            logger.info("Stopping RuntimeCoordinator")
            self._watchdog.disarm()
            self._transcribe_and_route()

            # Signal stop event
            self._stop_event.set()

            # Stop audio capture
            if self._audio_capture is not None:
                self._audio_capture.stop()

            # Wait for processing thread to finish
            if self._processing_thread is not None:
                self._processing_thread.join(timeout=2.0)
                self._processing_thread = None

            # Shutdown hotkey backend
            if self._hotkey_backend is not None:
                try:
                    self._hotkey_backend.unregister(self._toggle_hotkey)
                except Exception:
                    pass
                try:
                    self._hotkey_backend.shutdown()
                except Exception:
                    pass

            # Transition to shutting down
            try:
                self._state_machine.transition(AppEvent.STOP_REQUESTED)
            except Exception:
                pass  # State machine might already be in terminal state

            self._is_running = False
            logger.info("RuntimeCoordinator stopped")

    def _on_toggle_hotkey(self) -> None:
        """Handle toggle hotkey press."""
        current_state = self._state_machine.state

        if current_state == AppState.PAUSED:
            logger.info("Toggle hotkey pressed - resuming from pause")
            try:
                self._state_machine.transition(AppEvent.RESUME_REQUESTED)
            except Exception as e:
                logger.warning(f"Failed to resume: {e}")
            return

        if current_state == AppState.STANDBY:
            # Wake up - transition to LISTENING
            logger.info("Toggle hotkey pressed - waking up")
            self._is_manual_wake = True
            try:
                wake_event = self._hotkey_wake_event()
                self._state_machine.transition(wake_event)
                self._arm_listening_window()
            except Exception as e:
                logger.warning(f"Failed to wake: {e}")
        elif current_state == AppState.LISTENING:
            # Go back to sleep - transition to STANDBY
            logger.info("Toggle hotkey pressed - going to sleep")
            self._is_manual_wake = False
            try:
                self._transcribe_and_route()
                self._state_machine.transition(AppEvent.WAKE_WINDOW_TIMEOUT)
                self._disarm_listening_window()
            except Exception as e:
                logger.warning(f"Failed to sleep: {e}")

    def _hotkey_wake_event(self) -> AppEvent:
        """Resolve wake transition event for the configured listening mode."""
        if self._state_machine.mode is ListeningMode.WAKE_WORD:
            return AppEvent.WAKE_PHRASE_DETECTED
        if self._state_machine.mode is ListeningMode.CONTINUOUS:
            return AppEvent.CONTINUOUS_START
        return AppEvent.TOGGLE_LISTENING_ON

    def set_text_output(self, callback: Callable[[str], None] | None) -> None:
        """Set the text output callback for dictation."""
        self._runtime_text_output = callback

    def _audio_processing_loop(self) -> None:
        """Main audio processing loop running in a separate thread."""
        while not self._stop_event.is_set():
            try:
                # Get audio frame from queue with timeout
                if self._audio_capture is None:
                    break

                audio_queue = self._audio_capture.get_audio_queue()
                try:
                    frame: AudioFrame = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    self._flush_if_idle()
                    if self._state_machine.state == AppState.LISTENING:
                        self._check_timeout()
                    continue

                # Skip VAD entirely for now - ASR has built-in VAD
                # Just pass frame to coordinator
                self._process_frame(frame)

            except Exception as e:
                logger.error(f"Error in audio processing loop: {e}")
                time.sleep(0.1)  # Brief sleep to avoid busy loop on errors

    def _process_frame(self, frame: AudioFrame) -> None:
        """Process a single audio frame through the coordinator."""
        self._check_timeout()

        # Get current state and handle accordingly
        state = self._state_machine.state

        if state == AppState.STANDBY:
            # In TOGGLE mode, don't transcribe in standby
            # User needs to press hotkey to wake
            pass

        elif state == AppState.LISTENING:
            # Accumulate audio when in LISTENING mode
            with self._buffer_lock:
                self._audio_buffer.append(frame.audio)
                self._last_audio_frame_at = time.monotonic()

            if getattr(frame, "is_speech", None) is True:
                self._watchdog.on_vad_activity()

            if len(self._audio_buffer) >= self._transcribe_batch_frames:
                self._transcribe_and_route()

    def _transcribe_and_route(self) -> None:
        """Transcribe accumulated audio and route transcript through the normal policy path."""
        with self._buffer_lock:
            if not self._audio_buffer:
                return
            audio_data = np.concatenate(self._audio_buffer)
            self._audio_buffer.clear()
            self._last_audio_frame_at = None
        
        if self._asr_engine is None:
            return
        
        try:
            is_model_loaded = bool(getattr(self._asr_engine, "is_model_loaded", True))
            if not is_model_loaded:
                model_loader = getattr(self._asr_engine, "load_model", None)
                if callable(model_loader):
                    model_loader()

            asr_output = self._asr_engine.transcribe(audio_data)
            if hasattr(asr_output, "events"):
                events = asr_output.events
                if getattr(asr_output, "fallback_used", False):
                    logger.info(
                        "ASR fallback active: backend=%s reason=%s",
                        getattr(asr_output, "backend_used", "unknown"),
                        getattr(asr_output, "fallback_reason", None),
                    )
            else:
                events = asr_output
            
            for event in events:
                if not event.is_final:
                    continue
                update = self.on_transcript_event(event, vad_active=True)
                if update.transition is not None:
                    logger.info(
                        "Transcript-driven transition: %s -> %s",
                        update.transition.from_state,
                        update.transition.to_state,
                    )
        except Exception as e:
            logger.warning(f"Transcription failed: {e}")

    def _check_timeout(self) -> None:
        """Check for wake window timeout."""
        update = self.poll()
        if update.transition is not None:
            logger.info("Timeout transition applied: %s", update.transition.to_state)

    @property
    def state(self) -> AppState:
        """Current application state."""
        return self._state_machine.state

    @property
    def wake_window_timeout_seconds(self) -> float:
        """Configured wake window timeout."""
        return self._wake_window.timeout_seconds

    @property
    def is_wake_window_open(self) -> bool:
        """Whether wake listening window is currently open."""
        return self._wake_window.is_open()

    def on_transcript(self, transcript: str, *, vad_active: bool = True) -> RuntimeUpdate:
        """Handle transcript as wake detection input/activity signal."""
        if self.state == AppState.PAUSED:
            return self._handle_paused_transcript(transcript)

        if self.state == AppState.STANDBY:
            if self._state_machine.mode is not ListeningMode.WAKE_WORD:
                return RuntimeUpdate()
            if not vad_active:
                return RuntimeUpdate()
            match = self._wake_detector.detect(transcript)
            if not match.matched:
                return RuntimeUpdate()

            transition = self._state_machine.transition(AppEvent.WAKE_PHRASE_DETECTED)
            self._arm_listening_window()
            return RuntimeUpdate(transition=transition, wake_detected=True)

        if self.state == AppState.LISTENING:
            if self._state_machine.mode is ListeningMode.WAKE_WORD and not self._wake_window.is_open():
                return RuntimeUpdate()
            if self._state_machine.mode is ListeningMode.WAKE_WORD and self._wake_window.is_open():
                self._wake_window.on_activity()
            self._watchdog.on_transcript_activity()
            return self._handle_listening_transcript(transcript)

        return RuntimeUpdate()

    def on_transcript_event(self, transcript: TranscriptEvent, *, vad_active: bool = True) -> RuntimeUpdate:
        """Handle ASR transcript event with confidence-threshold filtering."""
        filtered = self._confidence_filter.filter(transcript)
        if filtered is None:
            return RuntimeUpdate()
        return self.on_transcript(filtered.text, vad_active=vad_active)

    def on_activity(self) -> RuntimeUpdate:
        """Handle generic activity hooks (for example VAD/speech activity)."""
        if self.state == AppState.LISTENING and self._wake_window.is_open():
            self._wake_window.on_activity()
        if self.state == AppState.LISTENING:
            self._watchdog.on_vad_activity()

        return RuntimeUpdate()

    def poll(self) -> RuntimeUpdate:
        """Advance timeout logic and emit FSM transition updates."""
        if self.state is not AppState.LISTENING:
            return RuntimeUpdate()
        if not self._watchdog.is_armed:
            self._watchdog.arm_for_mode(self._state_machine.mode)
        timeout_event = self._watchdog.poll_timeout()
        if timeout_event is None:
            return RuntimeUpdate()
        if timeout_event.timeout_type is WatchdogTimeoutType.WAKE_WINDOW_TIMEOUT:
            self._wake_window.close_window()
            transition = self._state_machine.transition(AppEvent.WAKE_WINDOW_TIMEOUT)
            return RuntimeUpdate(transition=transition)
        transition = self._state_machine.transition(AppEvent.INACTIVITY_AUTO_PAUSE)
        return RuntimeUpdate(transition=transition)

    def _handle_paused_transcript(self, transcript: str) -> RuntimeUpdate:
        parsed = self._parser.parse(transcript)
        policy = self._routing_policy.evaluate(self.state, parsed)
        if not policy.allowed:
            return RuntimeUpdate()

        if parsed.kind is not ParseKind.SYSTEM or parsed.command is None:
            return RuntimeUpdate()

        if parsed.command.command_id == "resume_voice_key":
            transition = self._state_machine.transition(AppEvent.RESUME_REQUESTED)
            self._disarm_listening_window()
            return RuntimeUpdate(transition=transition)

        if parsed.command.command_id == "voice_key_stop":
            transition = self._state_machine.transition(AppEvent.STOP_REQUESTED)
            self._disarm_listening_window()
            return RuntimeUpdate(transition=transition)

        return RuntimeUpdate()

    def _handle_listening_transcript(self, transcript: str) -> RuntimeUpdate:
        parsed = self._parser.parse(transcript)
        policy = self._routing_policy.evaluate(self.state, parsed)
        if not policy.allowed:
            return RuntimeUpdate()

        if parsed.kind is ParseKind.TEXT:
            literal = parsed.literal_text or ""
            if not literal:
                return RuntimeUpdate()
            self._emit_text(literal)
            return RuntimeUpdate(routed_text=literal)

        if parsed.command is None:
            return RuntimeUpdate()

        command_id = parsed.command.command_id
        if command_id == "pause_voice_key":
            transition = self._state_machine.transition(AppEvent.INACTIVITY_AUTO_PAUSE)
            self._disarm_listening_window()
            return RuntimeUpdate(transition=transition, executed_command_id=command_id)

        if command_id == "voice_key_stop":
            transition = self._state_machine.transition(AppEvent.STOP_REQUESTED)
            self._disarm_listening_window()
            return RuntimeUpdate(transition=transition, executed_command_id=command_id)

        if self._action_router is None:
            return RuntimeUpdate(executed_command_id=command_id)

        route_result = self._action_router.dispatch(command_id)
        if not route_result.handled:
            return RuntimeUpdate()

        return RuntimeUpdate(executed_command_id=command_id)

    def _emit_text(self, literal: str) -> None:
        if self._keyboard_backend is not None:
            self._keyboard_backend.type_text(f"{literal} ")
        if self._text_output is not None:
            self._text_output(literal)
        if self._runtime_text_output is not None:
            self._runtime_text_output(literal)

    def _arm_listening_window(self) -> None:
        if self._state_machine.mode is ListeningMode.WAKE_WORD:
            self._wake_window.open_window()
        self._watchdog.arm_for_mode(self._state_machine.mode)

    def _disarm_listening_window(self) -> None:
        self._watchdog.disarm()
        if self._state_machine.mode is ListeningMode.WAKE_WORD:
            self._wake_window.close_window()

    def _flush_if_idle(self) -> None:
        if self._transcribe_idle_flush_seconds <= 0:
            return
        if self.state is not AppState.LISTENING:
            return
        with self._buffer_lock:
            has_buffered_audio = bool(self._audio_buffer)
            last_audio_frame_at = self._last_audio_frame_at
        if not has_buffered_audio or last_audio_frame_at is None:
            return
        if (time.monotonic() - last_audio_frame_at) < self._transcribe_idle_flush_seconds:
            return
        self._transcribe_and_route()

    def _install_audio_drop_callback(self) -> None:
        if self._audio_capture is None:
            return
        setter = getattr(self._audio_capture, "set_drop_callback", None)
        if callable(setter):
            setter(self._on_audio_drop)

    def _on_audio_drop(self, total_dropped_frames: int) -> None:
        self._dropped_audio_frames = total_dropped_frames
        logger.warning("Audio queue drop detected: total_dropped_frames=%d", total_dropped_frames)


__all__ = ["RuntimeCoordinator", "RuntimeUpdate"]
