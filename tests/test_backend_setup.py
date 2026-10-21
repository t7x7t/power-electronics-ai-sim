"""Onboarding CLI tests: list, guide, setup, and user-config writing."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

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


def test_list_reports_registered_backends(capsys):
    assert main(["backend", "list"]) == 0

    payload = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in payload["backends"]}
    assert {"ngspice", "xyce", "ltspice", "pyspice"} <= names
    assert payload["platform"]


def test_guide_reports_platform_specific_recipes(capsys):
    assert main(["backend", "guide", "--name", "ngspice", "--platform", "windows"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "supported"
    methods = {recipe["method"] for recipe in payload["install_recipes"]}
    assert {"conda", "manual"} <= methods
    assert payload["config_template"].startswith('schema_version = "1.0"')


def test_guide_marks_planned_backends(capsys):
    assert main(["backend", "guide", "--name", "xyce"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["notes"]


def test_setup_detects_an_existing_installation(tmp_path, capsys):
    wrapper = _wrapper(tmp_path)

    assert main(["backend", "setup", "--name", "ngspice", "--executable", str(wrapper)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "known"
    assert payload["resolution"]["source"] == "explicit"
    assert payload["provenance"]["version"] == "44.2"
    assert "next_steps" in payload


def test_setup_writes_user_config_outside_the_repository(tmp_path, monkeypatch, capsys):
    wrapper = _wrapper(tmp_path)
    config_home = Path(tempfile.mkdtemp(prefix="pe-sim-user-config-"))
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(config_home))
    try:
        assert (
            main(
                [
                    "backend",
                    "setup",
                    "--name",
                    "ngspice",
                    "--executable",
                    str(wrapper),
                    "--write-config",
                ]
            )
            == 0
        )

        payload = json.loads(capsys.readouterr().out)
        written = Path(payload["config_written"])
        assert written.is_file()
        text = written.read_text(encoding="utf-8")
        assert "[ngspice]" in text
        assert "fake_ngspice" in text
    finally:
        shutil.rmtree(config_home, ignore_errors=True)


def test_setup_missing_binary_prints_install_recipes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.delenv("PE_SIM_NGSPICE", raising=False)

    assert main(["backend", "setup", "--name", "ngspice", "--executable", "definitely-missing"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NOT_FOUND"
    assert payload["install_recipes"]
    assert any("ngspice" in step for step in payload["next_steps"])


def test_setup_refuses_to_write_config_inside_the_repository(tmp_path, monkeypatch, capsys):
    wrapper = _wrapper(tmp_path)
    repository_root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("PE_SIM_CONFIG_HOME", str(repository_root / ".tmp" / "inside-repo-config"))

    exit_code = main(
        [
            "backend",
            "setup",
            "--name",
            "ngspice",
            "--executable",
            str(wrapper),
            "--write-config",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert "inside the repository" in payload["config_write_error"]
