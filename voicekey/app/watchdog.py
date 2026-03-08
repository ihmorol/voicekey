"""Inactivity watchdog timers for listening safety behavior."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from voicekey.app.state_machine import ListeningMode


@dataclass(frozen=True)
class WatchdogTimerConfig:
    """Config for wake and inactivity timeout timers."""

    wake_window_timeout_seconds: float = 5.0
    inactivity_auto_pause_seconds: float = 30.0


@dataclass(frozen=True)
class WatchdogTelemetryCounters:
    """Timeout telemetry counters emitted by the watchdog."""

    wake_window_timeouts: int = 0
    inactivity_auto_pauses: int = 0


class WatchdogTimeoutType(str, Enum):
    """Timeout outcomes that can be emitted by the watchdog."""

    WAKE_WINDOW_TIMEOUT = "wake_window_timeout"
    INACTIVITY_AUTO_PAUSE = "inactivity_auto_pause"


@dataclass(frozen=True)
class WatchdogTimeoutEvent:
    """A single timeout event emitted by the watchdog."""

    timeout_type: WatchdogTimeoutType
    occurred_at: float


class InactivityWatchdog:
    """Tracks mode-specific listening inactivity and emits timeout events."""

    def __init__(
        self,
        config: WatchdogTimerConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_config = config or WatchdogTimerConfig()
        if resolved_config.wake_window_timeout_seconds <= 0:
            raise ValueError("wake_window_timeout_seconds must be > 0")
        if resolved_config.inactivity_auto_pause_seconds <= 0:
            raise ValueError("inactivity_auto_pause_seconds must be > 0")

        self._config = resolved_config
        self._clock = clock
        self._mode: ListeningMode | None = None
        self._last_activity_at: float | None = None
        self._wake_window_timeouts = 0
        self._inactivity_auto_pauses = 0

    @property
    def config(self) -> WatchdogTimerConfig:
        """Watchdog timeout configuration."""
        return self._config

    @property
    def is_armed(self) -> bool:
        """Whether timeout polling is currently active."""
        return self._mode is not None and self._last_activity_at is not None

    def arm_for_mode(self, mode: ListeningMode) -> None:
        """Arm the watchdog for an active listening session in the given mode."""
        self._mode = mode
        self._last_activity_at = self._clock()

    def disarm(self) -> None:
        """Disarm the watchdog when no listening session is active."""
        self._mode = None
        self._last_activity_at = None

    def on_vad_activity(self) -> None:
        """Reset inactivity timer from VAD speech activity."""
        self._reset_on_activity()

    def on_transcript_activity(self) -> None:
        """Reset inactivity timer from transcript activity."""
        self._reset_on_activity()

    def poll_timeout(self) -> WatchdogTimeoutEvent | None:
        """Emit and disarm timeout event if current timer has expired."""
        if self._mode is None or self._last_activity_at is None:
            return None

        timeout_seconds = self._timeout_for_mode(self._mode)
        now = self._clock()
        if (now - self._last_activity_at) < timeout_seconds:
            return None

        timeout_type = self._timeout_type_for_mode(self._mode)
        if timeout_type is WatchdogTimeoutType.WAKE_WINDOW_TIMEOUT:
            self._wake_window_timeouts += 1
        else:
            self._inactivity_auto_pauses += 1

        self.disarm()
        return WatchdogTimeoutEvent(timeout_type=timeout_type, occurred_at=now)

    def telemetry_counters(self) -> WatchdogTelemetryCounters:
        """Return timeout telemetry counters snapshot."""
        return WatchdogTelemetryCounters(
            wake_window_timeouts=self._wake_window_timeouts,
            inactivity_auto_pauses=self._inactivity_auto_pauses,
        )

    def _reset_on_activity(self) -> None:
        if self._mode is None or self._last_activity_at is None:
            return
        self._last_activity_at = self._clock()

    def _timeout_for_mode(self, mode: ListeningMode) -> float:
        if mode == ListeningMode.WAKE_WORD:
            return self._config.wake_window_timeout_seconds
        return self._config.inactivity_auto_pause_seconds

    @staticmethod
    def _timeout_type_for_mode(mode: ListeningMode) -> WatchdogTimeoutType:
        if mode == ListeningMode.WAKE_WORD:
            return WatchdogTimeoutType.WAKE_WINDOW_TIMEOUT
        return WatchdogTimeoutType.INACTIVITY_AUTO_PAUSE


__all__ = [
    "InactivityWatchdog",
    "WatchdogTelemetryCounters",
    "WatchdogTimerConfig",
    "WatchdogTimeoutEvent",
    "WatchdogTimeoutType",
]
