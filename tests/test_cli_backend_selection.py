"""CLI backend selection and strict configuration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from pe_sim.cli import main

REPOSITORY = Path(__file__).resolve().parents[1]
NGSPICE_FIXTURE = REPOSITORY / "tests" / "fixtures" / "ngspice" / "fake_ngspice.py"


def _wrapper(tmp_path: Path) -> Path:
    if os.name == "nt":
        wrapper = tmp_path / "fake_ngspice.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{NGSPICE_FIXTURE}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = tmp_path / "fake_ngspice.sh"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{NGSPICE_FIXTURE}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return wrapper


def _document(name: str, plant_id: str, tmp_path: Path) -> dict:
    return {
        "schema_version": "0.1",
        "experiment_id": f"{name}-experiment",
        "run_id": name,
        "plant_id": plant_id,
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {"kp": 0.2},
        "timebase": {"unit": "s", "control_period_s": 0.01, "duration_s": 0.01},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [],
        "seed": 0,
        "contracts": {
            "safety": {
                "id": "default-safety",
                "hash": "c8af0de8cfeec979bf5fd17dbf1c819df90d6fc5a95fc3a2f53d84271ba0ef58",
            },
            "qualification": {
                "id": "default-qualification",
                "hash": "a3ef375ff21b886d76977854178c77a5bd397e551170fe13e4db9e606f9f121e",
            },
        },
        "output": {"directory": str(tmp_path / "runs"), "retention": "keep"},
    }


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / f"{document['run_id']}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_run_infers_the_backend_from_plant_id(tmp_path, monkeypatch, capsys):
    document = _document("inferred-ngspice", "ngspice-buck", tmp_path)
    document["plant_config"] = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "switching_frequency_hz": 100e3,
        "control_period_s": 10e-6,
    }
    document["timebase"]["duration_s"] = 1e-4
    config = _write(tmp_path, document)
    wrapper = _wrapper(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config), "--ngspice", str(wrapper)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "ngspice-buck"
    assert payload["plant_class"] == "NgspiceBuckPlant"


def test_backend_mismatch_is_rejected_before_running(tmp_path, monkeypatch, capsys):
    document = _document("mismatch", "buck", tmp_path)
    document["plant_config"] = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
    }
    config = _write(tmp_path, document)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config), "--backend", "boost"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "requires --backend" in payload["error"]


def test_unknown_controller_field_is_rejected(tmp_path, monkeypatch, capsys):
    document = _document("unknown-controller", "fake", tmp_path)
    document["controller_config"]["ki_misspelled"] = 1.0
    config = _write(tmp_path, document)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "controller_config" in payload["error"]


def test_unknown_schedule_key_is_rejected(tmp_path, monkeypatch, capsys):
    document = _document("unknown-schedule", "fake", tmp_path)
    document["input_schedule"] = [{"time_s": 0.0, "v_ref": 1.0}]
    config = _write(tmp_path, document)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "input_schedule" in payload["error"]


def test_schedule_key_must_be_supported_by_the_selected_backend(tmp_path, monkeypatch, capsys):
    document = _document("backend-schedule", "buck", tmp_path)
    document["plant_config"] = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
    }
    # `load` is valid for fake-load but not for the reviewed averaged Buck.
    document["input_schedule"] = [{"time_s": 0.0, "load": 1.0}]
    config = _write(tmp_path, document)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config), "--backend", "buck"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "not supported by backend" in payload["error"]
