"""Unit tests for CLI command contract (E05-S01)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from voicekey.app.state_machine import ListeningMode
from voicekey.config.schema import default_config
from voicekey.ui.cli import _create_runtime_coordinator, cli
from voicekey.ui.exit_codes import ExitCode


def test_help_includes_required_commands() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == ExitCode.SUCCESS
    for command_name in [
        "setup",
        "start",
        "status",
        "devices",
        "commands",
        "config",
        "download",
        "calibrate",
        "diagnostics",
    ]:
        assert command_name in result.output


def test_required_commands_smoke(tmp_path) -> None:
    runner = CliRunner()

    setup_config = tmp_path / "voicekey-test-config.yaml"

    command_args = [
        ["setup", "--skip", "--config", str(setup_config)],
        ["start"],
        ["status"],
        ["devices"],
        ["commands"],
        ["config"],
        ["download"],
        ["calibrate"],
        ["diagnostics"],
    ]

    for args in command_args:
        result = runner.invoke(cli, args)
        assert result.exit_code == ExitCode.SUCCESS


def test_json_output_shape_is_deterministic() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--output", "json", "status"])

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "status"
    # Check the structure - runtime, config, audio, models sections
    assert "runtime" in payload["result"]
    assert "config" in payload["result"]
    assert "audio" in payload["result"]
    assert "models" in payload["result"]


def test_status_reports_running_when_instance_lock_is_held(monkeypatch) -> None:
    runner = CliRunner()

    class GuardLockHeld:
        def try_acquire(self) -> bool:
            return False

        def release(self) -> None:
            return

    monkeypatch.setattr("voicekey.app.single_instance.SingleInstanceGuard", GuardLockHeld)

    result = runner.invoke(cli, ["--output", "json", "status"])

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    assert payload["result"]["runtime"]["running"] is True
    assert payload["result"]["runtime"]["state"] == "running"


def test_status_reports_stopped_when_instance_lock_is_available(monkeypatch) -> None:
    runner = CliRunner()

    class GuardLockAvailable:
        def __init__(self) -> None:
            self.released = False

        def try_acquire(self) -> bool:
            return True

        def release(self) -> None:
            self.released = True

    monkeypatch.setattr("voicekey.app.single_instance.SingleInstanceGuard", GuardLockAvailable)

    result = runner.invoke(cli, ["--output", "json", "status"])

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    assert payload["result"]["runtime"]["running"] is False
    assert payload["result"]["runtime"]["state"] == "stopped"


def test_unknown_command_returns_deterministic_usage_error_exit_code() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["does-not-exist"])

    assert result.exit_code == ExitCode.USAGE_ERROR


def test_invalid_arguments_return_deterministic_usage_error_exit_code() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["config", "--set", "missing_equals_sign"])

    assert result.exit_code == ExitCode.USAGE_ERROR


def test_setup_json_output_includes_required_onboarding_fields(tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"

    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "setup",
            "--config",
            str(config_path),
            "--device-id",
            "3",
            "--wake-test-success",
            "--autostart",
        ],
    )

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "setup"
    assert payload["result"]["completed"] is True
    assert payload["result"]["persisted"] is True
    assert payload["result"]["selected_device_id"] == 3
    assert payload["result"]["skipped_steps"] == []
    assert "welcome_privacy" in payload["result"]["keyboard_interaction_map"]


def test_start_command_returns_command_error_for_invalid_env_override() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["start"], env={"VOICEKEY_LOG_LEVEL": "verbose"})

    assert result.exit_code == ExitCode.COMMAND_ERROR


def test_start_command_supports_portable_mode_runtime_paths(tmp_path) -> None:
    runner = CliRunner()
    portable_root = tmp_path / "portable-root"

    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "start",
            "--portable",
            "--portable-root",
            str(portable_root),
        ],
    )

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    runtime_paths = payload["result"]["runtime_paths"]
    assert runtime_paths["portable_mode"] is True
    assert runtime_paths["config_path"] == str(portable_root / "config" / "config.yaml")
    assert runtime_paths["data_dir"] == str(portable_root / "data")
    assert runtime_paths["model_dir"] == str(portable_root / "data" / "models")


def test_start_command_uses_env_config_override_for_runtime_paths(tmp_path) -> None:
    runner = CliRunner()
    env_config = tmp_path / "env" / "config.yaml"

    result = runner.invoke(
        cli,
        ["--output", "json", "start"],
        env={"VOICEKEY_CONFIG": str(env_config)},
    )

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    runtime_paths = payload["result"]["runtime_paths"]
    assert runtime_paths["portable_mode"] is False
    assert runtime_paths["config_path"] == str(env_config)
    assert runtime_paths["data_dir"] == str(env_config.parent)


def test_config_set_get_reset_persists_to_config_file(tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"
    env = {"VOICEKEY_CONFIG": str(config_path)}

    set_result = runner.invoke(
        cli,
        ["--output", "json", "config", "--set", "system.autostart_enabled=true"],
        env=env,
    )
    assert set_result.exit_code == ExitCode.SUCCESS
    set_payload = json.loads(set_result.output)
    assert set_payload["result"]["persisted"] is True
    assert set_payload["result"]["value"] is True

    get_result = runner.invoke(
        cli,
        ["--output", "json", "config", "--get", "system.autostart_enabled"],
        env=env,
    )
    assert get_result.exit_code == ExitCode.SUCCESS
    get_payload = json.loads(get_result.output)
    assert get_payload["result"]["found"] is True
    assert get_payload["result"]["value"] is True

    reset_result = runner.invoke(
        cli,
        ["--output", "json", "config", "--reset"],
        env=env,
    )
    assert reset_result.exit_code == ExitCode.SUCCESS
    reset_payload = json.loads(reset_result.output)
    assert reset_payload["result"]["persisted"] is True

    get_after_reset = runner.invoke(
        cli,
        ["--output", "json", "config", "--get", "system.autostart_enabled"],
        env=env,
    )
    assert get_after_reset.exit_code == ExitCode.SUCCESS
    get_after_reset_payload = json.loads(get_after_reset.output)
    assert get_after_reset_payload["result"]["value"] is False


def test_config_set_rejects_unknown_key(tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"

    result = runner.invoke(
        cli,
        ["config", "--set", "unknown.key=1"],
        env={"VOICEKEY_CONFIG": str(config_path)},
    )

    assert result.exit_code == ExitCode.COMMAND_ERROR


def test_config_edit_spawns_editor(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"
    captured: dict[str, str | bool | None] = {}

    def fake_edit(*, filename: str, editor: str | None = None, require_save: bool = False) -> str | None:
        captured["filename"] = filename
        captured["editor"] = editor
        captured["require_save"] = require_save
        return None

    monkeypatch.setattr("voicekey.ui.cli.click.edit", fake_edit)
    result = runner.invoke(
        cli,
        ["--output", "json", "config", "--edit"],
        env={"VOICEKEY_CONFIG": str(config_path), "EDITOR": "nano"},
    )

    assert result.exit_code == ExitCode.SUCCESS
    payload = json.loads(result.output)
    assert payload["result"]["editor_spawned"] is True
    assert payload["result"]["editor"] == "nano"
    assert captured["filename"] == str(config_path)
    assert captured["editor"] == "nano"
    assert captured["require_save"] is False


def test_runtime_coordinator_uses_configured_mode_and_hotkey() -> None:
    config = default_config()
    config.modes.default = "toggle"
    config.hotkeys.toggle_listening = "ctrl+alt+k"
    config.audio.sample_rate_hz = 48000

    coordinator = _create_runtime_coordinator(
        config=config,
        runtime_paths=object(),
        keyboard_backend=None,
    )

    assert coordinator.listening_mode is ListeningMode.TOGGLE
    assert coordinator.toggle_hotkey == "ctrl+alt+k"


def test_start_command_warns_and_runs_local_only_for_hybrid_without_api_key(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = default_config().model_dump(mode="python")
    config["engine"]["network_fallback_enabled"] = True
    config["engine"]["cloud_api_base"] = "https://api.openai.com/v1"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = runner.invoke(cli, ["start", "--config", str(config_path)])

    assert result.exit_code == ExitCode.SUCCESS
    assert "Warning:" in result.output
    assert "VOICEKEY_OPENAI_API_KEY" in result.output
    assert "engine.network_fallback_enabled=true" in result.output


def test_start_command_fails_closed_for_cloud_primary_without_api_base(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "voicekey" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = default_config().model_dump(mode="python")
    config["engine"]["asr_backend"] = "openai-api-compatible"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = runner.invoke(
        cli,
        ["start", "--config", str(config_path)],
        env={"VOICEKEY_OPENAI_API_KEY": "sk-test"},
    )

    assert result.exit_code == ExitCode.COMMAND_ERROR
    assert "engine.cloud_api_base" in result.output
    assert "voicekey config --set engine.cloud_api_base=https://api.openai.com/v1" in result.output
