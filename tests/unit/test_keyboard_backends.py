"""Unit tests for keyboard backend abstraction and capability self-check (E04-S01)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voicekey.platform.keyboard_base import (
    KeyboardBackend,
    KeyboardBackendError,
    KeyboardCapabilityState,
    KeyboardErrorCode,
)
from voicekey.platform.keyboard_linux import LinuxKeyboardBackend
from voicekey.platform.keyboard_windows import WindowsKeyboardBackend


@dataclass
class RecordingInjector:
    """Test injector that records calls without OS side effects."""

    calls: list[str] = field(default_factory=list)

    def type_text(self, text: str, delay_ms: int) -> None:
        self.calls.append(f"type:{text}:{delay_ms}")

    def press_key(self, key: str) -> None:
        self.calls.append(f"key:{key}")

    def press_combo(self, keys: list[str]) -> None:
        self.calls.append(f"combo:{'+'.join(keys)}")


def test_linux_and_windows_backends_share_same_contract() -> None:
    linux = LinuxKeyboardBackend(session_type="x11", primary_available=True)
    windows = WindowsKeyboardBackend(is_admin=True, primary_available=True)

    assert isinstance(linux, KeyboardBackend)
    assert isinstance(windows, KeyboardBackend)

    contract_methods = ("type_text", "press_key", "press_combo", "self_check")
    for method_name in contract_methods:
        assert callable(getattr(linux, method_name))
        assert callable(getattr(windows, method_name))


def test_linux_x11_primary_reports_ready_state() -> None:
    backend = LinuxKeyboardBackend(
        session_type="x11",
        primary_available=True,
        fallback_available=True,
        fallback_permitted=True,
    )

    report = backend.self_check()

    assert report.platform == "linux"
    assert report.state is KeyboardCapabilityState.READY
    assert report.active_adapter == "x11_pynput"
    assert report.codes == ()


def test_linux_wayland_reports_degraded_best_effort_state() -> None:
    backend = LinuxKeyboardBackend(session_type="wayland", primary_available=True)

    report = backend.self_check()

    assert report.state is KeyboardCapabilityState.DEGRADED
    assert report.active_adapter == "x11_pynput"
    assert KeyboardErrorCode.WAYLAND_BEST_EFFORT in report.codes
    assert any("best-effort" in warning.lower() for warning in report.warnings)


def test_linux_fallback_without_permission_is_unavailable() -> None:
    backend = LinuxKeyboardBackend(
        session_type="x11",
        primary_available=False,
        fallback_available=True,
        fallback_permitted=False,
    )

    report = backend.self_check()

    assert report.state is KeyboardCapabilityState.UNAVAILABLE
    assert report.active_adapter is None
    assert KeyboardErrorCode.INPUT_PERMISSION_REQUIRED in report.codes


def test_windows_standard_user_reports_degraded_admin_recommended_state() -> None:
    backend = WindowsKeyboardBackend(is_admin=False, primary_available=True, fallback_available=True)

    report = backend.self_check()

    assert report.platform == "windows"
    assert report.state is KeyboardCapabilityState.DEGRADED
    assert report.active_adapter == "pynput_win32"
    assert KeyboardErrorCode.ADMIN_RECOMMENDED in report.codes


def test_windows_admin_reports_ready_state() -> None:
    backend = WindowsKeyboardBackend(is_admin=True, primary_available=True)

    report = backend.self_check()

    assert report.state is KeyboardCapabilityState.READY
    assert report.active_adapter == "pynput_win32"
    assert report.codes == ()


def test_self_check_result_is_deterministic_for_same_backend_state() -> None:
    backend = WindowsKeyboardBackend(is_admin=False, primary_available=False, fallback_available=True)

    first = backend.self_check()
    second = backend.self_check()

    assert first == second


def test_keyboard_operations_route_to_active_injector_without_side_effects() -> None:
    injector = RecordingInjector()
    backend = LinuxKeyboardBackend(
        session_type="x11",
        primary_available=True,
        primary_injector=injector,
    )

    backend.type_text("hello", delay_ms=5)
    backend.press_key("enter")
    backend.press_combo(["ctrl", "a"])

    assert injector.calls == ["type:hello:5", "key:enter", "combo:ctrl+a"]


def test_keyboard_backend_raises_typed_error_when_unavailable() -> None:
    backend = LinuxKeyboardBackend(
        session_type="x11",
        primary_available=False,
        fallback_available=False,
    )

    with pytest.raises(KeyboardBackendError) as exc_info:
        backend.press_key("enter")

    assert exc_info.value.code is KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE


def test_keyboard_backend_validates_invalid_inputs() -> None:
    backend = WindowsKeyboardBackend(is_admin=True, primary_available=True)

    with pytest.raises(KeyboardBackendError) as empty_text_exc:
        backend.type_text("")
    with pytest.raises(KeyboardBackendError) as combo_exc:
        backend.press_combo([])

    assert empty_text_exc.value.code is KeyboardErrorCode.EMPTY_TEXT
    assert combo_exc.value.code is KeyboardErrorCode.INVALID_COMBO


def test_linux_backend_uses_detected_primary_injector_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = RecordingInjector()
    monkeypatch.setattr(
        "voicekey.platform.keyboard_linux._create_pynput_injector",
        lambda **_: injector,
    )
    backend = LinuxKeyboardBackend(session_type="x11")

    report = backend.self_check()
    assert report.state is KeyboardCapabilityState.READY
    assert report.active_adapter == "x11_pynput"

    backend.press_key("enter")
    assert injector.calls == ["key:enter"]


def test_linux_backend_defaults_to_deterministic_unavailable_when_primary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("voicekey.platform.keyboard_linux._create_pynput_injector", lambda **_: None)
    backend = LinuxKeyboardBackend(session_type="x11")

    first = backend.self_check()
    second = backend.self_check()
    assert first == second
    assert first.state is KeyboardCapabilityState.UNAVAILABLE
    assert first.active_adapter is None
    assert KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE in first.codes

    with pytest.raises(KeyboardBackendError) as exc_info:
        backend.press_key("enter")
    assert exc_info.value.code is KeyboardErrorCode.PRIMARY_BACKEND_UNAVAILABLE


def test_linux_injector_resolution_avoids_self_check_hot_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = RecordingInjector()
    backend = LinuxKeyboardBackend(
        session_type="x11",
        primary_available=True,
        primary_injector=injector,
    )

    def _fail_self_check() -> None:
        raise AssertionError("self_check should not be called on the injection hot path")

    monkeypatch.setattr(backend, "self_check", _fail_self_check)

    backend.type_text("hello")
    backend.press_key("enter")
    backend.press_combo(["ctrl", "a"])
    assert injector.calls == ["type:hello:0", "key:enter", "combo:ctrl+a"]
