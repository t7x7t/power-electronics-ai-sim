"""PID and feedforward controller behavior (UD-01).

The load-bearing property is that ``pi`` keeps its exact previous behaviour:
a zero derivative or feedforward gain must not perturb the output or the
recorded identity.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
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


def test_zero_gains_keep_the_pi_identity_unchanged():
    identity = FakePIController.from_config({"kp": 0.2, "ki": 5.0}).manifest_identity()

    assert "kd" not in identity["parameters"]
    assert "kd_filter_tau_s" not in identity["parameters"]
    assert "kff_v" not in identity["parameters"]


def test_pid_identity_records_only_the_active_terms():
    identity = FakePIController.from_config(
        {"kp": 0.2, "ki": 5.0, "kd": 0.01, "kd_filter_tau_s": 1e-4}
    ).manifest_identity()

    assert identity["parameters"]["kd"] == 0.01
    assert identity["parameters"]["kd_filter_tau_s"] == 1e-4
    assert "kff_v" not in identity["parameters"]


def test_derivative_term_opposes_a_falling_error():
    controller = FakePIController(kp=0.0, kd=1.0)
    controller.reset(None, 0)

    _observe(controller, measurement=1.0, vref=0.0, time_s=0.0)  # error = -1
    rising = _observe(controller, measurement=0.0, vref=0.0, time_s=0.1)  # error 0 -> +1

    # error rises from -1 to 0, so d(error)/dt = 10 and kd * derivative = 10
    assert rising.value == pytest.approx(10.0)
    assert rising.state_update["filtered_derivative"] == pytest.approx(10.0)


def test_derivative_filter_attenuates_the_response():
    def derivative_after_step(tau_s: float) -> float:
        controller = FakePIController(kp=0.0, kd=1.0, kd_filter_tau_s=tau_s)
        controller.reset(None, 0)
        _observe(controller, measurement=1.0, vref=0.0, time_s=0.0)
        return _observe(controller, measurement=0.0, vref=0.0, time_s=0.1).value

    unfiltered = derivative_after_step(0.0)
    filtered = derivative_after_step(0.1)

    assert filtered < unfiltered
    # alpha = dt / (tau + dt) = 0.1 / 0.2 = 0.5
    assert filtered == pytest.approx(unfiltered * 0.5)


def test_feedforward_shifts_the_output_by_the_reference():
    controller = FakePIController(kp=0.0, kff_v=0.5, output_min=-10.0, output_max=10.0)
    controller.reset(None, 0)

    request = _observe(controller, measurement=0.0, vref=2.0, time_s=0.0)

    assert request.value == pytest.approx(1.0)


def test_snapshot_restore_preserves_the_derivative_state():
    controller = FakePIController(kp=0.1, kd=0.5, kd_filter_tau_s=1e-3)
    controller.reset(None, 0)
    _observe(controller, measurement=1.0, vref=0.5, time_s=0.0)
    checkpoint = controller.snapshot()

    assert checkpoint["previous_error"] is not None

    restored = FakePIController(kp=0.1, kd=0.5, kd_filter_tau_s=1e-3)
    restored.restore(checkpoint)
    expected = _observe(controller, measurement=0.2, vref=0.5, time_s=0.1).value
    actual = _observe(restored, measurement=0.2, vref=0.5, time_s=0.1).value

    assert actual == pytest.approx(expected)


def test_negative_filter_time_constant_is_rejected():
    with pytest.raises(ValueError, match="kd_filter_tau_s"):
        FakePIController(kd=1.0, kd_filter_tau_s=-1.0)


def test_non_finite_gain_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        FakePIController(kd=float("nan"))


def _config(tmp_path: Path, controller_id: str, controller_config: dict) -> Path:
    document = {
        "schema_version": "0.1",
        "experiment_id": "pid_cli",
        "run_id": f"{controller_id}-cli",
        "plant_id": "fake",
        "controller_id": controller_id,
        "plant_config": {},
        "controller_config": controller_config,
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
    path = tmp_path / f"{controller_id}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_cli_runs_a_pid_controller_and_records_its_gains(tmp_path, capsys, monkeypatch):
    config = _config(
        tmp_path,
        "pid",
        {"kp": 0.5, "ki": 10.0, "kd": 0.01, "kd_filter_tau_s": 1e-3,
         "output_min": 0.0, "output_max": 1.0},
    )
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "QUALIFIED"

    manifest = json.loads((tmp_path / "runs" / "pid-cli" / "manifest.json").read_text(encoding="utf-8"))
    parameters = manifest["controller"]["identity"]["parameters"]
    assert parameters["kd"] == 0.01
    assert parameters["kd_filter_tau_s"] == 1e-3
    assert "kff_v" not in parameters


def test_cli_rejects_derivative_keys_under_pi(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path, "pi", {"kp": 0.5, "kd": 0.01})
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "kd" in payload["error"]


def test_cli_lists_supported_controllers(tmp_path, capsys, monkeypatch):
    config = _config(tmp_path, "lag-compensator", {"kp": 0.5})
    monkeypatch.chdir(tmp_path)

    assert main(["run", str(config)]) == 2
    payload = json.loads(capsys.readouterr().out)
    # The experiment schema now accepts this safe identifier; the controller
    # registry rejects it while building the controller and names the
    # registered controllers in the error.
    assert "controller_id" in payload["error"]
    assert "pid" in payload["error"]


def test_experiment_schema_accepts_pid_and_rejects_unknown_controller():
    schema = json.loads((REPOSITORY / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema["properties"]["controller_id"])

    validator.validate("pid")
    validator.validate("pi")
    # The schema no longer enumerates controller ids.  Every safe identifier is
    # accepted here; the controller registry rejects an unregistered id when
    # the controller is built.
    validator.validate("lag-compensator")

    config_schema = schema["properties"]["controller_config"]
    validator = jsonschema.Draft202012Validator(config_schema)
    validator.validate({"kp": 1.0, "kd": 0.01, "kd_filter_tau_s": 1e-4, "kff_v": 0.5})
    # Value validation belongs to the factory now, so the schema accepts the
    # negative constant and the factory is what rejects it.
    validator.validate({"kd_filter_tau_s": -1.0})
    with pytest.raises(ValueError, match="kd_filter_tau_s"):
        FakePIController.from_config({"kd_filter_tau_s": -1.0})
