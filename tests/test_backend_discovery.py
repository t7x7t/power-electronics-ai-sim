"""Discovery and user-level configuration tests; no simulator is executed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pe_sim.backends.discovery import (
    BackendConfigError,
    BackendNotFoundError,
    config_template,
    load_backend_config,
    resolve_ngspice_executable,
    scan_for_ngspice,
    user_config_path,
)


def _executable(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text(
        "@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    return path


def test_user_config_path_uses_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(tmp_path))

    assert user_config_path() == tmp_path / "backends.toml"


def test_explicit_argument_wins_over_environment_and_config(tmp_path, monkeypatch):
    executable = _executable(tmp_path, "ngspice_con.exe")
    monkeypatch.setenv("PE_SIM_NGSPICE", "not-used")
    config = tmp_path / "backends.toml"
    config.write_text('[ngspice]\nexecutable = "also-not-used"\n', encoding="utf-8")

    resolution = resolve_ngspice_executable(str(executable), config_path=config)

    assert resolution.source == "explicit"
    assert resolution.executable == str(executable.resolve())


def test_environment_variable_is_used_when_no_explicit_argument(tmp_path, monkeypatch):
    executable = _executable(tmp_path, "ngspice_con.exe")
    monkeypatch.setenv("PE_SIM_NGSPICE", str(executable))

    resolution = resolve_ngspice_executable()

    assert resolution.source == "environment"
    assert resolution.executable == str(executable.resolve())


def test_user_config_is_used_after_explicit_and_environment(tmp_path, monkeypatch):
    executable = _executable(tmp_path, "ngspice_con.exe")
    config = tmp_path / "backends.toml"
    config.write_text(
        f'schema_version = "1.0"\n\n[ngspice]\nexecutable = "{executable.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("PE_SIM_NGSPICE", raising=False)

    resolution = resolve_ngspice_executable(config_path=config)

    assert resolution.source == "config"
    assert resolution.executable == str(executable.resolve())
    assert resolution.config_path == config


def test_gui_variant_is_replaced_by_console_variant(tmp_path):
    gui = _executable(tmp_path, "ngspice.exe")
    console = _executable(tmp_path, "ngspice_con.exe")

    resolution = resolve_ngspice_executable(str(gui))

    assert resolution.executable == str(console.resolve())
    assert resolution.executable_name == "ngspice_con.exe"
    assert any("console" in note for note in resolution.notes)


def test_gui_variant_without_console_records_a_limitation(tmp_path):
    gui = _executable(tmp_path, "ngspice.exe")

    resolution = resolve_ngspice_executable(str(gui))

    assert resolution.executable == str(gui.resolve())
    assert any("GUI" in note for note in resolution.notes)


def test_missing_executable_reports_every_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("PE_SIM_NGSPICE", raising=False)

    with pytest.raises(BackendNotFoundError) as caught:
        resolve_ngspice_executable("definitely-missing-ngspice")

    assert caught.value.attempts
    assert any("explicit argument" in attempt for attempt in caught.value.attempts)


def test_malformed_configuration_is_rejected(tmp_path):
    config = tmp_path / "backends.toml"
    config.write_text('schema_version = "9.9"\n', encoding="utf-8")

    with pytest.raises(BackendConfigError):
        load_backend_config(config)


def test_invalid_executable_field_is_rejected(tmp_path, monkeypatch):
    config = tmp_path / "backends.toml"
    config.write_text("[ngspice]\nexecutable = 42\n", encoding="utf-8")
    monkeypatch.delenv("PE_SIM_NGSPICE", raising=False)

    with pytest.raises(BackendConfigError):
        resolve_ngspice_executable(config_path=config)


def test_scan_prefers_console_and_skips_heavy_directories(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    _executable(package, "ngspice.exe")
    _executable(package, "ngspice_con.exe")
    skipped = package / "node_modules"
    skipped.mkdir()
    _executable(skipped, "ngspice.exe")

    found = scan_for_ngspice([tmp_path], max_depth=4)

    assert found[0].name == "ngspice_con.exe"
    assert all("node_modules" not in str(path) for path in found)


def test_config_template_is_valid_toml(tmp_path):
    text = config_template("<path-to-executable>")
    written = tmp_path / "backends.toml"
    written.write_text(text, encoding="utf-8")

    loaded = load_backend_config(written)

    assert loaded["schema_version"] == "1.0"
    assert loaded["ngspice"]["executable"] == "<path-to-executable>"
    assert "[ngspice]" in text
