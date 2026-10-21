"""CLI tests for external simulator discovery; no real simulator required."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from pe_sim.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "ngspice" / "fake_ngspice.py"


def _wrapper(tmp_path: Path) -> Path:
    if os.name == "nt":
        wrapper = tmp_path / "fake_ngspice.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{FIXTURE}" %*\r\n',
            encoding="utf-8",
        )
        return wrapper
    wrapper = tmp_path / "fake_ngspice.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def test_config_path_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(tmp_path))

    assert main(["backend", "config-path"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(tmp_path / "backends.toml")
    assert payload["exists"] is False


def test_template_command_prints_copyable_toml(capsys):
    assert main(["backend", "template", "--executable", "D:/tools/ngspice_con.exe"]) == 0

    output = capsys.readouterr().out
    assert "[ngspice]" in output
    assert "D:/tools/ngspice_con.exe" in output


def test_scan_command_reports_candidates(tmp_path, capsys):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ngspice_con.exe").write_text("stub", encoding="utf-8")

    assert main(["backend", "scan", "--root", str(tmp_path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FOUND"
    assert payload["candidates"][0]["preferred"] is True
    assert "config_template" in payload


def test_doctor_command_reports_known_identity(tmp_path, capsys):
    wrapper = _wrapper(tmp_path)

    assert main(["backend", "doctor", "--executable", str(wrapper)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "known"
    assert payload["resolution"]["source"] == "explicit"
    assert payload["provenance"]["version"] == "44.2"
    assert payload["provenance"]["executable_sha256"]


def test_doctor_command_reports_unavailable_for_missing_binary(capsys):
    assert main(["backend", "doctor", "--executable", "definitely-missing-ngspice"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NOT_FOUND"
    assert "attempted" in payload["error"]
    assert payload["install_recipes"]


def test_pyspice_model_builder_requires_the_optional_dependency(tmp_path, monkeypatch, capsys):
    if importlib.util.find_spec("PySpice") is not None:
        pytest.skip("PySpice is installed in this environment")
    wrapper = _wrapper(tmp_path)
    config = Path(__file__).resolve().parent.parent / "examples" / "ngspice_buck_step" / "config.json"
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            str(config),
            "--backend",
            "ngspice-buck",
            "--ngspice",
            str(wrapper),
            "--model-builder",
            "pyspice",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "CONFIG_REJECTED"
    assert "PySpice" in payload["error"]
