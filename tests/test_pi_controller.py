"""Regression tests for true PI behavior and CLI controller wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim.cli import main
from pe_sim.runtime import FakePIController

REPOSITORY = Path(__file__).resolve().parents[1]


def _timing(time_s: float, period_s: float = 0.1) -> dict[str, float]:
    return {"sample_time_s": time_s, "control_period_s": period_s}


def _observe(
    controller: FakePIController,
    measurement: float,
    vref: float,
    time_s: float,
    period_s: float = 0.1,
):
    return controller.observe(
        {"vout": measurement},
        {"vref": vref},
        _timing(time_s, period_s),
        {},
    )


def test_ki_accumulates_an_integral_state():
    controller = FakePIController(kp=0.0, ki=100.0)
    controller.reset(None, 0)

    first = _observe(controller, measurement=0.0, vref=1.0, time_s=0.0)
    second = _observe(controller, measurement=0.0, vref=1.0, time_s=0.1)

    assert first.value == pytest.approx(10.0)
    assert second.value == pytest.approx(20.0)
    assert second.state_update["integral"] == pytest.approx(20.0)


def test_integral_clamping_prevents_unbounded_windup():
    controller = FakePIController(
        kp=0.0,
        ki=100.0,
        output_min=0.0,
        output_max=1.0,
        anti_windup=True,
    )
    controller.reset(None, 0)

    for index in range(10):
        request = _observe(controller, measurement=0.0, vref=1.0, time_s=index * 0.1)

    assert request.value == pytest.approx(1.0)
    assert request.state_update["integral"] == pytest.approx(1.0)


def test_snapshot_restore_preserves_the_integral_state():
    controller = FakePIController(kp=0.0, ki=10.0)
    controller.reset(None, 0)
    _observe(controller, measurement=0.0, vref=1.0, time_s=0.0)
    checkpoint = controller.snapshot()

    restored = FakePIController(kp=0.0, ki=10.0)
    restored.restore(checkpoint)
    expected = _observe(controller, measurement=0.0, vref=1.0, time_s=0.1).value
    actual = _observe(restored, measurement=0.0, vref=1.0, time_s=0.1).value

    assert actual == pytest.approx(expected)


def test_manifest_identity_records_ki_and_limits():
    identity = FakePIController.from_config(
        {"kp": 0.2, "ki": 5.0, "output_min": 0.0, "output_max": 1.0}
    ).manifest_identity()

    assert identity["parameters"]["ki"] == 5.0
    assert identity["parameters"]["output_min"] == 0.0
    assert identity["parameters"]["output_max"] == 1.0


def _fake_config(tmp_path: Path) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "pi_cli_integral",
        "run_id": "pi-cli-integral",
        "plant_id": "fake",
        "controller_id": "pi",
        "plant_config": {},
        "controller_config": {
            "kp": 0.0,
            "ki": 100.0,
            "output_min": 0.0,
            "output_max": 1.0,
        },
        "timebase": {"unit": "s", "control_period_s": 0.01, "duration_s": 0.1},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [{"time_s": 0.0, "vref": 1.0}],
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
    path = tmp_path / "pi.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_cli_uses_the_configured_integral_term(tmp_path, capsys, monkeypatch):
    config = _fake_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "QUALIFIED"
    assert summary["backend"] == "fake"
    assert summary["plant_class"] == "FakePlant"

    manifest = json.loads(
        (tmp_path / "runs" / "pi-cli-integral" / "manifest.json").read_text(encoding="utf-8")
    )
    samples = json.loads(
        (tmp_path / "runs" / "pi-cli-integral" / "samples.json").read_text(encoding="utf-8")
    )
    assert manifest["controller"]["identity"]["parameters"]["ki"] == 100.0
    assert samples[-1]["vout"] > 0.5
