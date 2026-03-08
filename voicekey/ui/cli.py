"""Command-line interface for VoiceKey."""

from __future__ import annotations

import json
import importlib
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
import yaml

from voicekey.app.routing_policy import RuntimeRoutingPolicy
from voicekey.app.main import RuntimeCoordinator
from voicekey.app.state_machine import AppEvent, AppState, ListeningMode, VoiceKeyStateMachine
from voicekey.config.manager import (
    ConfigError,
    StartupEnvOverrides,
    backup_config,
    load_config,
    parse_startup_env_overrides,
    resolve_asr_runtime_policy,
    resolve_runtime_paths,
    save_config,
)
from voicekey.config.schema import default_config, validate_with_fallback
from voicekey.platform.keyboard_base import KeyboardBackend

# Platform-specific keyboard backend import
if sys.platform == "linux":
    from voicekey.platform.keyboard_linux import LinuxKeyboardBackend
elif sys.platform == "win32":
    from voicekey.platform.keyboard_windows import WindowsKeyboardBackend as LinuxKeyboardBackend
else:
    LinuxKeyboardBackend = None  # type: ignore

from voicekey.ui.daemon import resolve_daemon_start_behavior
from voicekey.ui.exit_codes import ExitCode
from voicekey.ui.onboarding import run_onboarding
from voicekey.ui.tray import TrayActionHandlers, TrayController, TrayIconBackend

REQUIRED_COMMANDS: tuple[str, ...] = (
    "setup",
    "start",
    "status",
    "devices",
    "commands",
    "config",
    "download",
    "calibrate",
    "diagnostics",
)


# Global coordinator reference for signal handling
_coordinator: RuntimeCoordinator | None = None

# Global tray references
_tray_controller: TrayController | None = None
_tray_backend: TrayIconBackend | None = None
_tray_update_thread: threading.Thread | None = None
_tray_stop_event: threading.Event | None = None


def _create_tray_handlers() -> TrayActionHandlers:
    """Create tray action handlers connected to the global coordinator."""

    def on_start() -> None:
        global _coordinator
        if _coordinator is not None and not _coordinator.is_running:
            _coordinator.start()

    def on_stop() -> None:
        global _coordinator
        if _coordinator is not None and _coordinator.is_running:
            _coordinator.stop()

    def on_pause() -> None:
        global _coordinator
        if _coordinator is not None:
            try:
                machine = _coordinator._state_machine
                if machine.state == AppState.STANDBY:
                    machine.transition(AppEvent.PAUSE_REQUESTED)
            except Exception:
                pass  # State transition may not be valid in current state

    def on_resume() -> None:
        global _coordinator
        if _coordinator is not None:
            try:
                machine = _coordinator._state_machine
                if machine.state == AppState.PAUSED:
                    machine.transition(AppEvent.RESUME_REQUESTED)
            except Exception:
                pass  # State transition may not be valid in current state

    def on_exit() -> None:
        global _coordinator, _tray_backend
        if _coordinator is not None and _coordinator.is_running:
            _coordinator.stop()
        if _tray_backend is not None:
            _tray_backend.stop()
        raise SystemExit(0)

    return TrayActionHandlers(
        on_start=on_start,
        on_stop=on_stop,
        on_pause=on_pause,
        on_resume=on_resume,
        on_open_dashboard=None,  # Dashboard not implemented yet
        on_open_settings=None,  # Settings UI not implemented yet
        on_exit=on_exit,
    )


def _start_tray_update_thread() -> None:
    """Start background thread to sync tray state with coordinator."""
    global _tray_controller, _tray_backend, _tray_stop_event, _tray_update_thread

    if _tray_controller is None or _tray_backend is None:
        return

    _tray_stop_event = threading.Event()

    def update_loop() -> None:
        while _tray_stop_event is not None and not _tray_stop_event.is_set():
            if _tray_controller is not None and _coordinator is not None:
                # Sync state from coordinator to tray
                try:
                    _tray_controller.set_runtime_state(_coordinator.state)
                    _tray_controller.set_runtime_active(_coordinator.is_running)
                    if _tray_backend is not None:
                        _tray_backend.update_icon()
                except Exception:
                    pass  # Ignore sync errors

            if _tray_stop_event is not None:
                _tray_stop_event.wait(0.5)  # Update every 500ms

    _tray_update_thread = threading.Thread(target=update_loop, daemon=True)
    _tray_update_thread.start()


def _stop_tray_update_thread() -> None:
    """Stop the tray state update thread."""
    global _tray_stop_event, _tray_update_thread
    if _tray_stop_event is not None:
        _tray_stop_event.set()
    if _tray_update_thread is not None:
        _tray_update_thread.join(timeout=2.0)


def _create_runtime_coordinator(
    config: Any,
    runtime_paths: Any,
    device_index: int | None = None,
    keyboard_backend: KeyboardBackend | None = None,
) -> RuntimeCoordinator:
    """Create and configure a RuntimeCoordinator instance."""
    del runtime_paths
    config_mode = getattr(getattr(config, "modes", None), "default", ListeningMode.WAKE_WORD.value)
    if config_mode == ListeningMode.TOGGLE.value:
        listening_mode = ListeningMode.TOGGLE
    elif config_mode == ListeningMode.CONTINUOUS.value:
        listening_mode = ListeningMode.CONTINUOUS
    else:
        listening_mode = ListeningMode.WAKE_WORD

    vad_threshold = getattr(getattr(config, "vad", None), "speech_threshold", 0.5)
    sample_rate = getattr(getattr(config, "audio", None), "sample_rate_hz", 16000)
    chunk_ms = getattr(getattr(config, "audio", None), "chunk_ms", 100)
    chunk_duration_seconds = max(0.08, float(chunk_ms) / 1000.0)
    transcribe_batch_frames = max(3, int(round(0.5 / chunk_duration_seconds)))
    wake_window_timeout_seconds = float(
        getattr(getattr(config, "wake_word", None), "wake_window_timeout_seconds", 5)
    )
    inactivity_auto_pause_seconds = float(
        getattr(getattr(config, "modes", None), "inactivity_auto_pause_seconds", 30)
    )
    paused_resume_phrase_enabled = bool(
        getattr(getattr(config, "modes", None), "paused_resume_phrase_enabled", True)
    )
    toggle_hotkey = getattr(
        getattr(config, "hotkeys", None),
        "toggle_listening",
        "ctrl+shift+`",
    )
    asr_engine_factory = _build_asr_engine_factory(config=config, sample_rate=sample_rate)

    state_machine = VoiceKeyStateMachine(
        mode=listening_mode,
        initial_state=AppState.INITIALIZING,
    )

    # Create coordinator - sample rate will be auto-detected by AudioCapture if backend overrides it.
    coordinator = RuntimeCoordinator(
        state_machine=state_machine,
        routing_policy=RuntimeRoutingPolicy(paused_resume_phrase_enabled=paused_resume_phrase_enabled),
        keyboard_backend=keyboard_backend,
        device_index=device_index,
        sample_rate=sample_rate,
        vad_threshold=vad_threshold,
        wake_window_timeout_seconds=wake_window_timeout_seconds,
        inactivity_auto_pause_seconds=inactivity_auto_pause_seconds,
        chunk_duration_seconds=chunk_duration_seconds,
        transcribe_batch_frames=transcribe_batch_frames,
        transcribe_idle_flush_seconds=0.5,
        toggle_hotkey=toggle_hotkey,
        asr_engine_factory=asr_engine_factory,
    )

    return coordinator


def _build_asr_engine_factory(config: Any, sample_rate: int) -> Any:
    """Build lazy ASR engine factory derived from engine config."""
    engine_config = getattr(config, "engine", None)
    asr_backend = getattr(engine_config, "asr_backend", "faster-whisper")
    network_fallback_enabled = bool(getattr(engine_config, "network_fallback_enabled", False))
    model_profile = getattr(engine_config, "model_profile", "base")
    compute_type = getattr(engine_config, "compute_type", None)
    cloud_timeout_seconds = float(getattr(engine_config, "cloud_timeout_seconds", 30))

    def _factory() -> Any:
        if asr_backend == "faster-whisper" and not network_fallback_enabled:
            from voicekey.audio.asr_faster_whisper import ASREngine

            return ASREngine(
                model_size=model_profile,
                device="auto",
                compute_type=compute_type,
                sample_rate=sample_rate,
                transcription_timeout=cloud_timeout_seconds,
            )
        return _build_router_asr_engine(config=config, sample_rate=sample_rate)

    return _factory


def _build_router_asr_engine(config: Any, sample_rate: int) -> Any:
    """Build hybrid/cloud ASR router from available runtime module."""
    try:
        router_module = importlib.import_module("voicekey.audio.asr_router")
    except ImportError as exc:
        raise RuntimeError(
            "Hybrid/cloud ASR is configured but `voicekey.audio.asr_router` is unavailable. "
            "Switch to local-only mode (`engine.asr_backend=faster-whisper` and "
            "`engine.network_fallback_enabled=false`) or install hybrid ASR components."
        ) from exc

    engine_config = getattr(config, "engine", None)
    if hasattr(engine_config, "model_dump"):
        engine_payload = engine_config.model_dump(mode="python")
    elif hasattr(engine_config, "dict"):
        engine_payload = engine_config.dict()  # pragma: no cover - compatibility path
    else:
        engine_payload = {}

    factory = getattr(router_module, "create_asr_router_from_config", None)
    if callable(factory):
        try:
            return factory(engine_payload, sample_rate=sample_rate, environ=os.environ)
        except TypeError:
            try:
                return factory(engine_payload, sample_rate=sample_rate)
            except TypeError:
                return factory(engine_payload)

    router_class = getattr(router_module, "ASRRouter", None)
    if callable(router_class):
        try:
            return router_class(config=engine_payload, sample_rate=sample_rate)
        except TypeError:
            try:
                return router_class(engine_config=engine_payload, sample_rate=sample_rate)
            except TypeError:
                return router_class(engine_payload)

    raise RuntimeError(
        "Hybrid/cloud ASR is configured but no compatible ASR router factory was found. "
        "Expected `create_asr_router_from_config(...)` or `ASRRouter`."
    )


def _signal_handler(signum, frame) -> None:
    """Handle Ctrl+C gracefully."""
    global _coordinator, _tray_backend
    if _coordinator is not None:
        click.echo("\nReceived interrupt signal, shutting down...")
        _coordinator.stop()
        _coordinator = None
    if _tray_backend is not None:
        _tray_backend.stop()
        _tray_backend = None
    _stop_tray_update_thread()
    sys.exit(0)

def _emit_output(ctx: click.Context, command: str, result: dict[str, Any]) -> None:
    output_mode = ctx.obj["output"]
    payload = {
        "ok": True,
        "command": command,
        "result": result,
    }
    if output_mode == "json":
        click.echo(json.dumps(payload, sort_keys=True))
        return

    click.echo(f"{command}: ok")
    for key in sorted(result):
        value = result[key]
        if isinstance(value, (dict, list)):
            rendered_value = json.dumps(value, sort_keys=True)
        else:
            rendered_value = str(value)
        click.echo(f"{key}={rendered_value}")


def _validate_single_config_operation(
    get_key: str | None,
    set_value: str | None,
    reset_flag: bool,
    edit_flag: bool,
) -> str:
    selected = {
        "get": bool(get_key),
        "set": bool(set_value),
        "reset": reset_flag,
        "edit": edit_flag,
    }
    enabled = [name for name, is_enabled in selected.items() if is_enabled]
    if len(enabled) > 1:
        raise click.UsageError("Use only one config operation: --get, --set, --reset, or --edit.")
    return enabled[0] if enabled else "show"


def _split_key_value(raw_set_value: str) -> tuple[str, str]:
    if "=" not in raw_set_value:
        raise click.UsageError("--set expects KEY=VALUE format.")
    key, value = raw_set_value.split("=", 1)
    key = key.strip()
    if not key:
        raise click.UsageError("--set expects non-empty key in KEY=VALUE format.")
    return key, value


def _get_nested_value(data: dict[str, Any], dotted_key: str) -> tuple[bool, Any]:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_nested_value(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    current = data
    for part in parts[:-1]:
        next_item = current.get(part)
        if not isinstance(next_item, dict):
            raise click.ClickException(
                f"Unsupported config key '{dotted_key}'. Use a valid dotted key path."
            )
        current = next_item

    final_key = parts[-1]
    if final_key not in current:
        raise click.ClickException(
            f"Unsupported config key '{dotted_key}'. Use a valid dotted key path."
        )
    current[final_key] = value


def _config_payload_for_show(config_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": config_data.get("version"),
        "engine.model_profile": config_data.get("engine", {}).get("model_profile"),
        "wake_word.phrase": config_data.get("wake_word", {}).get("phrase"),
        "wake_word.sensitivity": config_data.get("wake_word", {}).get("sensitivity"),
        "modes.inactivity_auto_pause_seconds": config_data.get("modes", {}).get(
            "inactivity_auto_pause_seconds"
        ),
        "typing.confidence_threshold": config_data.get("typing", {}).get("confidence_threshold"),
        "system.autostart_enabled": config_data.get("system", {}).get("autostart_enabled"),
        "privacy.telemetry_enabled": config_data.get("privacy", {}).get("telemetry_enabled"),
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--output",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output mode for command responses.",
)
@click.pass_context
def cli(ctx: click.Context, output: str) -> None:
    """VoiceKey command-line interface."""
    ctx.ensure_object(dict)
    ctx.obj["output"] = output.lower()


@cli.command("start")
@click.option("--daemon", is_flag=True, help="Start without terminal dashboard.")
@click.option(
    "--foreground",
    is_flag=True,
    help="Keep runtime attached to the terminal until interrupted.",
)
@click.option("--config", "config_path", type=click.Path(), default=None)
@click.option("--portable", is_flag=True, help="Use local portable config/data paths.")
@click.option(
    "--portable-root",
    type=click.Path(file_okay=False),
    default=None,
    help="Root directory used by portable mode.",
)
@click.pass_context
def start_command(
    ctx: click.Context,
    daemon: bool,
    foreground: bool,
    config_path: str | None,
    portable: bool,
    portable_root: str | None,
) -> None:
    """Start VoiceKey runtime."""
    global _coordinator, _tray_controller, _tray_backend

    output_mode = ctx.obj.get("output", "text")

    try:
        startup_overrides = parse_startup_env_overrides()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    # Resolve daemon behavior to determine if tray should be enabled
    behavior = resolve_daemon_start_behavior(
        daemon=daemon,
        environment={key: str(value) for key, value in os.environ.items() if key in (
            "XDG_SESSION_TYPE", "DISPLAY", "WAYLAND_DISPLAY"
        )}
    )
    tray_enabled = behavior.tray_enabled and not startup_overrides.disable_tray

    effective_config_path = config_path or startup_overrides.config_path
    runtime_paths = resolve_runtime_paths(
        explicit_config_path=effective_config_path,
        model_dir_override=startup_overrides.model_dir,
        portable_mode=portable,
        portable_root=Path(portable_root).expanduser() if portable_root is not None else None,
    )

    # Load configuration
    try:
        load_result = load_config(explicit_path=runtime_paths.config_path)
        config = load_result.config
    except ConfigError as exc:
        raise click.ClickException(f"Failed to load config: {exc}") from exc

    try:
        asr_runtime_policy = resolve_asr_runtime_policy(config, env=os.environ)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if asr_runtime_policy.warning and output_mode != "json":
        click.echo(f"Warning: {asr_runtime_policy.warning}", err=True)

    # Create keyboard backend
    keyboard_backend: KeyboardBackend | None = None
    if LinuxKeyboardBackend is not None:
        try:
            char_delay_ms = int(getattr(getattr(config, "typing", None), "char_delay_ms", 8))
            keyboard_backend = LinuxKeyboardBackend(char_delay_ms=char_delay_ms)
        except TypeError:
            keyboard_backend = LinuxKeyboardBackend()
        except Exception as e:
            click.echo(f"Warning: Could not initialize keyboard backend: {e}", err=True)
    else:
        click.echo(f"Warning: Keyboard backend not available for platform: {sys.platform}", err=True)

    # Create and start coordinator
    runtime_state = "stub"
    try:
        _coordinator = _create_runtime_coordinator(
            config=config,
            runtime_paths=runtime_paths,
            keyboard_backend=keyboard_backend,
        )

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        # Initialize tray if enabled
        if tray_enabled:
            _tray_controller = TrayController(
                handlers=_create_tray_handlers(),
                initial_runtime_state=_coordinator.state,
                runtime_active=True,
            )
            _tray_backend = TrayIconBackend(controller=_tray_controller)
            if _tray_backend.start():
                # Start the tray state update thread
                _start_tray_update_thread()
            else:
                # Tray failed to start - clean up
                _tray_controller = None
                _tray_backend = None

        # Start the coordinator
        _coordinator.start()
        runtime_state = _coordinator.state.value if _coordinator else "standby"

    except RuntimeError as exc:
        # Audio not available - this is OK in test environments
        if "Audio capture not available" in str(exc):
            if output_mode != "json":
                click.echo(f"Warning: {exc}", err=True)
            runtime_state = "standby"
            _coordinator = None
        else:
            raise click.ClickException(f"Failed to start VoiceKey: {exc}") from exc
    except Exception as exc:
        exc_text = str(exc).lower()
        if (
            "no microphone device found" in exc_text
            or "no audio device found" in exc_text
            or "device is busy" in exc_text
            or "device details unavailable" in exc_text
            or "failed to start audio capture" in exc_text
            or "audio device" in exc_text
        ):
            if output_mode != "json":
                click.echo(f"Warning: {exc}", err=True)
            runtime_state = "standby"
            _coordinator = None
        else:
            raise click.ClickException(f"Failed to start VoiceKey: {exc}") from exc

    # Output startup information
    # Always use machine-readable one-shot output in JSON mode,
    # or when coordinator couldn't start.
    if output_mode == "json" or daemon or _coordinator is None or not foreground:
        payload = {
            "accepted": True,
            "daemon": daemon,
            "foreground": foreground,
            "tray_enabled": tray_enabled,
            "config_path": config_path,
            "runtime_paths": {
                "config_path": str(runtime_paths.config_path),
                "data_dir": str(runtime_paths.data_dir),
                "model_dir": str(runtime_paths.model_dir),
                "portable_mode": runtime_paths.portable_mode,
            },
            "env_overrides": {
                "config_path": str(startup_overrides.config_path)
                if startup_overrides.config_path is not None
                else None,
                "model_dir": str(startup_overrides.model_dir)
                if startup_overrides.model_dir is not None
                else None,
                "log_level": startup_overrides.log_level,
                "disable_tray": startup_overrides.disable_tray,
            },
            "runtime": "started" if _coordinator else "stub",
            "state": _coordinator.state.value if _coordinator and _coordinator.is_running else "standby",
            "listening_mode": _coordinator.listening_mode.value if _coordinator else None,
            "toggle_hotkey": _coordinator.toggle_hotkey if _coordinator else None,
        }

        if _coordinator is not None and not foreground:
            _coordinator.stop()
            _coordinator = None
        if _tray_backend is not None and not foreground:
            _tray_backend.stop()
            _tray_backend = None
            _stop_tray_update_thread()
            _tray_controller = None

        # In daemon mode or when coordinator couldn't start, just output success and exit
        _emit_output(
            ctx,
            command="start",
            result=payload,
        )
        return

    # In foreground mode, run until interrupted
    active_hotkey = (
        _coordinator.toggle_hotkey
        if _coordinator is not None
        else "ctrl+shift+`"
    )
    listening_mode = (
        _coordinator.listening_mode
        if _coordinator is not None
        else ListeningMode.WAKE_WORD
    )
    click.echo("VoiceKey started. Press Ctrl+C to stop.")
    if listening_mode is ListeningMode.TOGGLE:
        click.echo(f"Press {active_hotkey} to toggle listening mode.")

    # Wait in a loop, checking status
    try:
        while _coordinator is not None and _coordinator.is_running:
            state = _coordinator.state
            if state == AppState.STANDBY:
                if listening_mode is ListeningMode.TOGGLE:
                    click.echo(f"\rSleeping... (press {active_hotkey} to wake)   ", nl=False)
                else:
                    click.echo("\rSleeping...                                     ", nl=False)
            elif state == AppState.LISTENING:
                click.echo("\rListening... (speak now)                 ", nl=False)
            elif state == AppState.PAUSED:
                click.echo("\rPaused                                     ", nl=False)
            else:
                click.echo(f"\rState: {state.value}                       ", nl=False)

            import time
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        if _coordinator is not None:
            _coordinator.stop()
            _coordinator = None
        if _tray_backend is not None:
            _tray_backend.stop()
            _tray_backend = None
        _stop_tray_update_thread()
        _tray_controller = None

    click.echo("\nVoiceKey stopped.")


@cli.command("setup")
@click.option("--config", "config_path", type=click.Path(), default=None)
@click.option("--skip", is_flag=True, help="Skip onboarding and write safe defaults.")
@click.option("--device-id", type=int, default=None, help="Selected microphone device id.")
@click.option(
    "--wake-test-success/--wake-test-fail",
    "wake_test_success",
    default=True,
    help="Result of wake phrase verification step.",
)
@click.option("--hotkey", default="ctrl+shift+`", show_default=True)
@click.option("--autostart/--no-autostart", "autostart_enabled", default=False, show_default=True)
@click.pass_context
def setup_command(
    ctx: click.Context,
    config_path: str | None,
    skip: bool,
    device_id: int | None,
    wake_test_success: bool,
    hotkey: str,
    autostart_enabled: bool,
) -> None:
    """Run onboarding setup flow and persist selected values."""
    result = run_onboarding(
        config_path=config_path,
        skip=skip,
        selected_device_id=device_id,
        wake_phrase_verified=wake_test_success,
        toggle_hotkey=hotkey,
        autostart_enabled=autostart_enabled,
    )
    _emit_output(
        ctx,
        command="setup",
        result={
            "completed": result.completed,
            "skipped": result.skipped,
            "persisted": result.persisted,
            "config_path": str(result.config_path),
            "selected_device_id": result.selected_device_id,
            "wake_phrase_verified": result.wake_phrase_verified,
            "toggle_hotkey": result.toggle_hotkey,
            "autostart_enabled": result.autostart_enabled,
            "completed_steps": list(result.completed_steps),
            "skipped_steps": list(result.skipped_steps),
            "tutorial_script": list(result.tutorial_script),
            "keyboard_interaction_map": {
                key: list(value) for key, value in result.keyboard_interaction_map.items()
            },
            "duration_seconds": result.duration_seconds,
            "within_target": result.within_target,
            "errors": list(result.errors),
        },
    )


@cli.command("status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Show runtime status including state, config, devices, and model status."""
    from voicekey.app.single_instance import SingleInstanceGuard
    from voicekey.config.manager import resolve_runtime_paths
    from voicekey.models import ModelDownloadManager

    # Try to detect if runtime is running by checking instance lock
    runtime_running = False
    runtime_state = "stopped"
    try:
        guard = SingleInstanceGuard()
        if guard.try_acquire():
            guard.release()
        else:
            runtime_running = True
            runtime_state = "running"
    except OSError:
        runtime_running = False
        runtime_state = "unknown"

    # Get config summary
    config_summary: dict[str, Any] = {}
    selected_device = None
    try:
        load_result = load_config()
        cfg = load_result.config
        config_summary = {
            "wake_word.phrase": cfg.wake_word.phrase if hasattr(cfg, 'wake_word') else "voice key",
            "wake_word.sensitivity": cfg.wake_word.sensitivity if hasattr(cfg, 'wake_word') else 0.55,
            "engine.model_profile": cfg.engine.model_profile if hasattr(cfg, 'engine') else "base",
            "vad.speech_threshold": cfg.vad.speech_threshold if hasattr(cfg, 'vad') else 0.5,
            "modes.inactivity_auto_pause_seconds": cfg.modes.inactivity_auto_pause_seconds
            if hasattr(cfg, 'modes') else 300,
        }
        selected_device = cfg.audio.device_id if hasattr(cfg, 'audio') and hasattr(cfg.audio, 'device_id') else None
    except ConfigError:
        config_summary = {"error": "config_not_available"}

    # Get audio device info
    device_info: dict[str, Any] = {"available": False, "devices": [], "selected": None}
    try:
        from voicekey.audio import list_devices as get_devices
        devices = get_devices()
        device_info = {
            "available": True,
            "count": len(devices),
            "devices": [
                {"index": d["index"], "name": d["name"], "channels": d["channels"]}
                for d in devices
            ],
            "selected": selected_device,
        }
    except Exception:
        device_info = {"available": False, "error": "audio_library_not_available"}

    # Get model download status
    model_status: dict[str, Any] = {}
    try:
        runtime_paths = resolve_runtime_paths()
        manager = ModelDownloadManager(model_dir=runtime_paths.model_dir)
        all_status = manager.get_all_status()
        model_status = {
            name: {
                "installed": s.installed,
                "profile": s.profile,
                "checksum_valid": s.checksum_valid,
            }
            for name, s in all_status.items()
        }
    except Exception:
        model_status = {"error": "model_status_unavailable"}

    # Build the result
    result: dict[str, Any] = {
        "runtime": {
            "running": runtime_running,
            "state": runtime_state,
        },
        "config": config_summary,
        "audio": device_info,
        "models": model_status,
    }

    _emit_output(ctx, command="status", result=result)


@cli.command("devices")
@click.pass_context
def devices_command(ctx: click.Context) -> None:
    """List available microphone devices."""
    from voicekey.audio import get_default_device, list_devices

    # Get list of available devices
    devices = list_devices()

    # Get the default device info (may fail on some systems)
    default_index = None
    try:
        default_device = get_default_device()
        if default_device:
            default_index = default_device["index"]
    except Exception:
        pass  # Default device detection not available

    # Try to get configured device from config
    selected_device_id = None
    try:
        load_result = load_config()
        selected_device_id = load_result.config.audio.device_id
    except ConfigError:
        pass  # Config not available, ignore

    # Build result with proper device info
    result_devices = []
    for dev in devices:
        result_devices.append({
            "index": dev["index"],
            "name": dev["name"],
            "channels": dev["channels"],
            "sample_rate": dev["sample_rates"],
            "is_default": dev["default"],
        })

    _emit_output(
        ctx,
        command="devices",
        result={
            "devices": result_devices,
            "selected_device_id": selected_device_id,
            "default_device_id": default_index,
            "probe": "sounddevice",
        },
    )


@cli.command("commands")
@click.pass_context
def commands_command(ctx: click.Context) -> None:
    """List supported CLI commands for contract verification."""
    _emit_output(
        ctx,
        command="commands",
        result={
            "supported": list(REQUIRED_COMMANDS),
        },
    )


@cli.command("download")
@click.option("--asr", "asr_profile", type=str, default=None, help="Download ASR model profile (tiny, base, small).")
@click.option("--vad", "download_vad", is_flag=True, help="Download/verify VAD model.")
@click.option("--all", "download_all_models", is_flag=True, help="Download all models.")
@click.option("--force", is_flag=True, help="Force model redownload.")
@click.pass_context
def download_command(
    ctx: click.Context,
    asr_profile: str | None,
    download_vad: bool,
    download_all_models: bool,
    force: bool,
) -> None:
    """Download ASR/VAD models for offline use."""
    from voicekey.config.manager import resolve_runtime_paths
    from voicekey.models import ModelDownloadManager

    # Resolve model directory
    runtime_paths = resolve_runtime_paths()
    manager = ModelDownloadManager(model_dir=runtime_paths.model_dir)

    # If no specific option is selected, show status
    if not asr_profile and not download_vad and not download_all_models:
        # Default behavior: show status
        status = manager.get_all_status()
        result = {
            "requested": True,
            "force": force,
            "status": "showing_status",
            "models": {
                name: {
                    "installed": s.installed,
                    "profile": s.profile,
                    "path": str(s.path) if s.path else None,
                    "checksum_valid": s.checksum_valid,
                }
                for name, s in status.items()
            },
        }
        _emit_output(ctx, command="download", result=result)
        return

    # Download requested models
    results: list[dict[str, object]] = []

    if download_all_models:
        download_results = manager.download_all(force=force)
        for r in download_results:
            results.append({
                "name": r.name,
                "success": r.success,
                "profile": r.profile,
                "path": str(r.path) if r.path else None,
                "reused": r.reused,
                "error": r.error,
            })
    else:
        if asr_profile:
            r = manager.download_asr(asr_profile, force=force)
            results.append({
                "name": r.name,
                "success": r.success,
                "profile": r.profile,
                "path": str(r.path) if r.path else None,
                "reused": r.reused,
                "error": r.error,
            })

        if download_vad:
            r = manager.download_vad()
            results.append({
                "name": r.name,
                "success": r.success,
                "profile": r.profile,
                "error": r.error,
            })

    # Determine overall status
    all_success = all(r.get("success", False) for r in results)
    status_value = "completed" if all_success else "failed"

    _emit_output(
        ctx,
        command="download",
        result={
            "requested": True,
            "force": force,
            "status": status_value,
            "downloads": results,
        },
    )


@cli.command("calibrate")
@click.pass_context
def calibrate_command(ctx: click.Context) -> None:
    """Run calibration contract (stub)."""
    _emit_output(
        ctx,
        command="calibrate",
        result={
            "requested": True,
            "status": "not_implemented",
        },
    )


@cli.command("diagnostics")
@click.option("--export", "export_path", type=click.Path(), default=None, help="Export diagnostics to file.")
@click.option(
    "--full",
    "full_export",
    is_flag=True,
    default=False,
    help="WARNING: Include full config (may contain sensitive data). Use with caution.",
)
@click.pass_context
def diagnostics_command(ctx: click.Context, export_path: str | None, full_export: bool) -> None:
    """Collect and export VoiceKey diagnostics.
    
    By default, exports are REDACTED for privacy. Use --full only if you
    understand the security implications and consent to including potentially
    sensitive data.
    
    If unexpected typing is observed, follow the incident response procedure:
    1. Pause voice input immediately
    2. Export redacted diagnostics (this command without --full)
    3. Disable autostart until resolved
    """
    from pathlib import Path
    
    from voicekey.diagnostics import (
        collect_diagnostics,
        export_diagnostics,
        get_export_warning_for_full_mode,
        validate_diagnostics_safety,
    )
    
    if full_export:
        click.echo(get_export_warning_for_full_mode(), err=True)
        if not click.confirm("Continue with full export?", default=False):
            raise click.ClickException("Full export cancelled.")
    
    if export_path:
        diagnostics = export_diagnostics(
            export_path=Path(export_path),
            include_full_config=full_export,
        )
        result = {
            "exported": True,
            "export_path": export_path,
            "export_mode": "full" if full_export else "redacted",
            "safety_check": "passed" if validate_diagnostics_safety(diagnostics)[0] else "warning",
        }
    else:
        diagnostics = collect_diagnostics(include_full_config=full_export)
        result = {
            "exported": False,
            "export_mode": "full" if full_export else "redacted",
            "diagnostics": diagnostics,
            "safety_check": "passed" if validate_diagnostics_safety(diagnostics)[0] else "warning",
        }
    
    _emit_output(ctx, command="diagnostics", result=result)


@cli.command("config")
@click.option("--get", "get_key", type=str, default=None)
@click.option("--set", "set_value", type=str, default=None)
@click.option("--reset", "reset_flag", is_flag=True)
@click.option("--edit", "edit_flag", is_flag=True)
@click.pass_context
def config_command(
    ctx: click.Context,
    get_key: str | None,
    set_value: str | None,
    reset_flag: bool,
    edit_flag: bool,
) -> None:
    """Config command for get/set/reset/edit operations."""
    operation = _validate_single_config_operation(get_key, set_value, reset_flag, edit_flag)
    try:
        load_result = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    config_data = load_result.config.model_dump(mode="python")
    config_path = load_result.path
    load_warnings = list(load_result.warnings)

    if operation == "get":
        assert get_key is not None
        found, value = _get_nested_value(config_data, get_key)
        _emit_output(
            ctx,
            command="config",
            result={
                "operation": "get",
                "key": get_key,
                "value": value,
                "found": found,
                "path": str(config_path),
                "source": "file",
                "warnings": load_warnings,
            },
        )
        return

    if operation == "set":
        assert set_value is not None
        key, value_raw = _split_key_value(set_value)
        value = yaml.safe_load(value_raw)
        updated_data = load_result.config.model_dump(mode="python")
        _set_nested_value(updated_data, key, value)
        validated, validation_warnings = validate_with_fallback(updated_data)
        save_config(validated, config_path)

        found, persisted_value = _get_nested_value(validated.model_dump(mode="python"), key)
        _emit_output(
            ctx,
            command="config",
            result={
                "operation": "set",
                "key": key,
                "value": persisted_value if found else None,
                "persisted": True,
                "path": str(config_path),
                "warnings": load_warnings + list(validation_warnings),
            },
        )
        return

    if operation == "reset":
        backup_path = backup_config(config_path) if config_path.exists() else None
        defaults = default_config()
        save_config(defaults, config_path)
        _emit_output(
            ctx,
            command="config",
            result={
                "operation": "reset",
                "persisted": True,
                "path": str(config_path),
                "backup_path": str(backup_path) if backup_path is not None else None,
                "warnings": load_warnings,
            },
        )
        return

    if operation == "edit":
        editor = os.getenv("VISUAL") or os.getenv("EDITOR")
        click.edit(filename=str(config_path), editor=editor, require_save=False)
        _emit_output(
            ctx,
            command="config",
            result={
                "operation": "edit",
                "editor_spawned": True,
                "path": str(config_path),
                "editor": editor or "system_default",
                "warnings": load_warnings,
            },
        )
        return

    _emit_output(
        ctx,
        command="config",
        result={
            "operation": "show",
            "path": str(config_path),
            "values": _config_payload_for_show(config_data),
            "warnings": load_warnings,
        },
    )


def run(argv: Sequence[str] | None = None) -> int:
    """Execute CLI and return deterministic process exit code."""
    args = list(argv) if argv is not None else None
    try:
        cli.main(args=args, prog_name="voicekey", standalone_mode=False)
        return int(ExitCode.SUCCESS)
    except click.UsageError as exc:
        exc.show()
        return int(ExitCode.USAGE_ERROR)
    except click.ClickException as exc:
        exc.show()
        return int(ExitCode.COMMAND_ERROR)
    except click.Abort:
        click.echo("Aborted.", err=True)
        return int(ExitCode.RUNTIME_ERROR)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)


def main() -> None:
    """Script entrypoint used by packaging metadata."""
    raise SystemExit(run())
