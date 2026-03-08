"""Linux keyboard backend with X11-primary and fallback capability model."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from voicekey.platform.compatibility import detect_display_session
from voicekey.platform.keyboard_base import (
    KeyboardBackend,
    KeyboardBackendError,
    KeyboardCapabilityReport,
    KeyboardCapabilityState,
    KeyboardErrorCode,
)


class KeyboardInjector(Protocol):
    """Minimal injection surface used by this adapter."""

    def type_text(self, text: str, delay_ms: int) -> None: ...

    def press_key(self, key: str) -> None: ...

    def press_combo(self, keys: list[str]) -> None: ...


@dataclass(frozen=True)
class _NoOpInjector:
    """No-op injector used in unit scope to avoid OS side effects."""

    def type_text(self, text: str, delay_ms: int) -> None:
        del text, delay_ms

    def press_key(self, key: str) -> None:
        del key

    def press_combo(self, keys: list[str]) -> None:
        del keys


class _PynputInjector:
    """Concrete injector backed by pynput keyboard controller."""

    _ALIASES: dict[str, str] = {
        "control": "ctrl",
        "return": "enter",
        "esc": "esc",
        "super": "cmd",
        "meta": "cmd",
        "win": "cmd",
        "pageup": "page_up",
        "pagedown": "page_down",
        "pgup": "page_up",
        "pgdn": "page_down",
    }

    def __init__(
        self,
        *,
        controller: Any,
        key_namespace: Any,
        default_delay_ms: int = 0,
    ) -> None:
        self._controller = controller
        self._key_namespace = key_namespace
        self._default_delay_ms = max(0, default_delay_ms)

    def type_text(self, text: str, delay_ms: int) -> None:
        effective_delay_ms = delay_ms if delay_ms > 0 else self._default_delay_ms
        if effective_delay_ms <= 0:
            self._controller.type(text)
            return

        delay_s = effective_delay_ms / 1000
        for char in text:
            self._controller.type(char)
            time.sleep(delay_s)

    def press_key(self, key: str) -> None:
        resolved = self._resolve_key(key)
        self._controller.press(resolved)
        self._controller.release(resolved)

    def press_combo(self, keys: list[str]) -> None:
        resolved = [self._resolve_key(key) for key in keys]
        for key in resolved:
            self._controller.press(key)
        for key in reversed(resolved):
            self._controller.release(key)

    def _resolve_key(self, key: str) -> Any:
        normalized = key.strip().lower()
        if not normalized:
            return key

        mapped = self._ALIASES.get(normalized, normalized)
        key_value = getattr(self._key_namespace, mapped, None)
        if key_value is not None:
            return key_value
        if len(mapped) == 1:
            return mapped
        return mapped


def _create_pynput_injector(*, default_delay_ms: int = 0) -> KeyboardInjector | None:
    """Return pynput-backed injector when dependency/runtime are available."""

    try:
        from pynput.keyboard import Controller, Key
    except Exception:
        return None

    try:
        return _PynputInjector(
            controller=Controller(),
            key_namespace=Key,
            default_delay_ms=default_delay_ms,
        )
    except Exception:
        return None


class LinuxKeyboardBackend(KeyboardBackend):
    """Linux keyboard adapter with deterministic capability self-check."""

    _PRIMARY_ADAPTER = "x11_pynput"
    _FALLBACK_ADAPTER = "evdev_uinput"

    def __init__(
        self,
        *,
        session_type: str | None = None,
        primary_available: bool | None = None,
        fallback_available: bool = False,
        fallback_permitted: bool = False,
        primary_injector: KeyboardInjector | None = None,
        fallback_injector: KeyboardInjector | None = None,
        char_delay_ms: int = 8,
    ) -> None:
        self._session_type = self._detect_session_type(session_type)
        self._fallback_available = fallback_available
        self._fallback_permitted = fallback_permitted
        self._char_delay_ms = max(0, char_delay_ms)

        if primary_injector is not None:
            self._primary_injector = primary_injector
            self._primary_available = True if primary_available is None else primary_available
        elif primary_available is None:
            discovered = _create_pynput_injector(default_delay_ms=self._char_delay_ms)
            self._primary_injector = discovered or _NoOpInjector()
            self._primary_available = discovered is not None
        elif primary_available:
            self._primary_injector = (
                _create_pynput_injector(default_delay_ms=self._char_delay_ms) or _NoOpInjector()
            )
            self._primary_available = True
        else:
            self._primary_injector = _NoOpInjector()
            self._primary_available = False

        self._fallback_injector = fallback_injector or _NoOpInjector()
        self._active_adapter = self._select_active_adapter()

    def type_text(self, text: str, delay_ms: int = 0) -> None:
        if not text:
            raise KeyboardBackendError(KeyboardErrorCode.EMPTY_TEXT, "Cannot type empty text.")
        injector = self._resolve_injector()
        self._invoke(lambda: injector.type_text(text, delay_ms))

    def press_key(self, key: str) -> None:
        injector = self._resolve_injector()
        self._invoke(lambda: injector.press_key(key))

    def press_combo(self, keys: list[str]) -> None:
        if not keys:
            raise KeyboardBackendError(KeyboardErrorCode.INVALID_COMBO, "Key combo cannot be empty.")
        injector = self._resolve_injector()
        self._invoke(lambda: injector.press_combo(keys))

    def self_check(self) -> KeyboardCapabilityReport:
        available = self._available_adapters()
        codes: list[KeyboardErrorCode] = []
        warnings: list[str] = []
        remediation: list[str] = []

        if self._session_type not in {"x11", "wayland"}:
            codes.append(KeyboardErrorCode.DISPLAY_SERVER_UNSUPPORTED)
            warnings.append(
                f"Linux session type '{self._session_type}' is not an officially supported keyboard target."
            )
            remediation.append("Use an X11 session for full keyboard compatibility.")

        if self._session_type == "wayland":
            codes.append(KeyboardErrorCode.WAYLAND_BEST_EFFORT)
            warnings.append(
                "Wayland keyboard injection is best-effort and may fail in some applications."
            )
            remediation.append("Use an X11 session for full keyboard compatibility.")

        active_adapter = self._active_adapter
        if active_adapter is None:
            if not self._primary_available:
                codes.append(KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE)
            if not self._fallback_available:
                codes.append(KeyboardErrorCode.FALLBACK_BACKEND_UNAVAILABLE)
            if self._fallback_available and not self._fallback_permitted:
                codes.append(KeyboardErrorCode.INPUT_PERMISSION_REQUIRED)
                remediation.append(
                    "Grant evdev/uinput permissions or run with required privileges for fallback input."
                )

            return KeyboardCapabilityReport(
                backend="linux_keyboard",
                platform="linux",
                state=KeyboardCapabilityState.UNAVAILABLE,
                active_adapter=None,
                available_adapters=available,
                codes=tuple(dict.fromkeys(codes)),
                warnings=tuple(dict.fromkeys(warnings)),
                remediation=tuple(dict.fromkeys(remediation)),
            )

        if active_adapter == self._FALLBACK_ADAPTER:
            codes.append(KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE)
            warnings.append("Using evdev/uinput fallback path because X11 primary path is unavailable.")

        state = KeyboardCapabilityState.DEGRADED if codes else KeyboardCapabilityState.READY

        return KeyboardCapabilityReport(
            backend="linux_keyboard",
            platform="linux",
            state=state,
            active_adapter=active_adapter,
            available_adapters=available,
            codes=tuple(dict.fromkeys(codes)),
            warnings=tuple(dict.fromkeys(warnings)),
            remediation=tuple(dict.fromkeys(remediation)),
        )

    @staticmethod
    def _detect_session_type(session_type: str | None) -> str:
        if session_type is not None:
            return session_type.strip().lower() or "unknown"
        env_value = os.environ.get("XDG_SESSION_TYPE")
        if env_value is not None:
            return env_value.strip().lower() or "unknown"
        return detect_display_session(platform_name="linux", env=os.environ).value

    def _available_adapters(self) -> tuple[str, ...]:
        available: list[str] = []
        if self._primary_available:
            available.append(self._PRIMARY_ADAPTER)
        if self._fallback_available:
            available.append(self._FALLBACK_ADAPTER)
        return tuple(available)

    def _select_active_adapter(self) -> str | None:
        if self._primary_available:
            return self._PRIMARY_ADAPTER
        if self._fallback_available and self._fallback_permitted:
            return self._FALLBACK_ADAPTER
        return None

    def _resolve_injector(self) -> KeyboardInjector:
        if self._active_adapter is None:
            raise KeyboardBackendError(
                self._unavailable_error_code(),
                "Linux keyboard backend is unavailable. Run capability self-check for remediation.",
            )

        if self._active_adapter == self._PRIMARY_ADAPTER:
            return self._primary_injector
        if self._active_adapter == self._FALLBACK_ADAPTER:
            return self._fallback_injector
        raise KeyboardBackendError(
            KeyboardErrorCode.INJECTION_FAILED,
            "Linux keyboard backend has no active injector despite non-unavailable state.",
        )

    def _unavailable_error_code(self) -> KeyboardErrorCode:
        if not self._primary_available:
            return KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE
        if self._fallback_available and not self._fallback_permitted:
            return KeyboardErrorCode.INPUT_PERMISSION_REQUIRED
        if not self._fallback_available:
            return KeyboardErrorCode.FALLBACK_BACKEND_UNAVAILABLE
        return KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE

    @staticmethod
    def _invoke(action: Callable[[], None]) -> None:
        try:
            action()
        except KeyboardBackendError:
            raise
        except Exception as exc:  # pragma: no cover - defensive conversion
            raise KeyboardBackendError(
                KeyboardErrorCode.INJECTION_FAILED,
                f"Linux keyboard injection failed: {exc}",
            ) from exc


__all__ = ["LinuxKeyboardBackend"]
