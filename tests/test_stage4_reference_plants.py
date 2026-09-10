import math
import json

import pytest

from pe_sim import (
    BoostPlant,
    BuckPlant,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    Timebase,
    check_controller_adapter,
    check_plant_adapter,
)
from pe_sim.contracts import InitialState, snapshot_digest
from pe_sim.runtime import Runner
from pe_sim.cli import main as cli_main


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


@pytest.mark.parametrize("factory", [_buck, _boost])
def test_reference_plant_conformance(factory):
    report = check_plant_adapter(factory, action_bounds=(0.0, 1.0))
    assert report.passed, report.failures
    assert {"capabilities", "reset_observe", "advance_monotonic", "snapshot_restore"}.issubset(report.checks)


def test_conformance_harness_does_not_assume_normalized_actions():
    report = check_plant_adapter(FakePlant, action=0.5)
    assert report.passed, report.failures
    assert "out_of_range_action_rejected" not in report.checks


def test_reference_controller_conformance():
    report = check_controller_adapter(FakePIController)
    assert report.passed, report.failures


@pytest.mark.parametrize("factory", [_buck, _boost])
def test_runner_accepts_reference_plants(factory, tmp_path):
    spec = ExperimentSpec(
        "stage4", f"{factory.__name__}-run", factory().topology, "pi",
        Timebase(duration_s=0.0001, control_period_s=0.00001),
        output_dir=str(tmp_path),
    )
    result = Runner().run(spec, factory(), FakePIController(kp=0.2))
    assert result.status == "QUALIFIED"
    assert (result.run_dir / "manifest.json").exists()


def test_reference_observation_has_units_and_truth_boundary():
    plant = _buck()
    plant.reset({"output_voltage_v": 1.0, "inductor_current_a": 0.2})
    obs = plant.observe()
    assert obs.measurement_units == {"vout": "V", "inductor_current": "A"}
    assert "input_voltage" in obs.truth
    assert "input_voltage" not in obs.controller_measurement()


def test_buck_and_boost_have_distinct_averaged_dynamics():
    buck, boost = _buck(), _boost()
    buck.reset({})
    boost.reset({})
    buck_obs = buck.advance(0.5, 1e-5)
    boost_obs = boost.advance(0.5, 1e-5)
    assert buck_obs.measurement != boost_obs.measurement
    assert buck_obs.measurement["inductor_current"] > 0
    assert boost_obs.measurement["inductor_current"] > 0


def test_reference_plant_external_inputs_and_snapshot_roundtrip():
    plant = _buck()
    plant.reset({})
    plant.advance(0.4, 1e-5, {"vin_v": 10.0, "load_ohm": 5.0})
    before = plant.snapshot()
    plant.advance(0.8, 1e-5)
    plant.restore(before)
    assert snapshot_digest(plant.snapshot()) == snapshot_digest(before)
    assert math.isclose(plant.input_voltage_v, 10.0)
    assert math.isclose(plant.load_resistance_ohm, 5.0)


def test_reference_plant_rejects_invalid_action_and_external_input():
    plant = _boost()
    plant.reset({})
    with pytest.raises(ValueError):
        plant.advance(float("nan"), 1e-5)
    with pytest.raises(ValueError):
        plant.advance(1.1, 1e-5)
    with pytest.raises(ValueError):
        plant.advance(0.5, 1e-5, {"vin_v": float("inf")})


@pytest.mark.parametrize("factory,duty", [(_buck, 0.4), (_boost, 0.4)])
def test_reference_plant_analytical_equilibrium(factory, duty):
    plant = factory()
    equilibrium = plant.analytical_steady_state(duty)
    plant.reset(equilibrium)
    derivatives = plant._derivatives(duty, plant.inductor_current_a, plant.output_voltage_v)
    assert math.isclose(derivatives[0], 0.0, abs_tol=1e-12)
    assert math.isclose(derivatives[1], 0.0, abs_tol=1e-12)


@pytest.mark.parametrize("factory", [_buck, _boost])
def test_reference_plant_integration_step_sensitivity(factory):
    coarse = factory()
    fine = factory()
    fine.integration_step_s = coarse.integration_step_s / 2.0
    coarse.reset({})
    fine.reset({})
    coarse.advance(0.4, 0.002)
    fine.advance(0.4, 0.002)
    assert math.isclose(
        coarse.observe().measurement["vout"],
        fine.observe().measurement["vout"],
        rel_tol=2e-8,
        abs_tol=2e-8,
    )


def test_reference_cli_config_and_manifest_identity(tmp_path, capsys):
    config = {
        "schema_version": "0.1",
        "experiment_id": "buck_cli",
        "run_id": "buck_cli_run",
        "plant_id": "buck",
        "controller_id": "pi",
        "plant_config": {"input_voltage_v": 12.0, "inductance_h": 1e-4, "capacitance_f": 4.7e-4, "load_resistance_ohm": 10.0, "integration_step_s": 1e-6},
        "timebase": {"unit": "s", "control_period_s": 1e-5, "duration_s": 1e-4},
        "initial_state": {"mode": "cold_start"},
        "input_schedule": [],
        "seed": 0,
        "contracts": {"safety": {"id": "s", "hash": "0" * 64}, "qualification": {"id": "q", "hash": "0" * 64}},
        "output": {"directory": str(tmp_path), "retention": "keep"},
    }
    config_path = tmp_path / "buck.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert cli_main(["run", str(config_path), "--backend", "buck"]) == 0
    output = json.loads(capsys.readouterr().out)
    manifest = json.loads((tmp_path / "buck_cli_run" / "manifest.json").read_text(encoding="utf-8"))
    assert output["status"] == "QUALIFIED"
    assert manifest["plant"]["identity"]["topology"] == "buck"
    assert manifest["plant"]["identity"]["level"] == "L1"
    assert manifest["plant"]["hash"] != "0" * 64
    assert "D:" not in json.dumps(manifest)


def test_experiment_spec_legacy_positional_fields_remain_compatible(tmp_path):
    spec = ExperimentSpec(
        "legacy", "legacy-run", "plant", "controller",
        Timebase(duration_s=0.001),
        InitialState(mode="warm_start"),
        ({"time_s": 0.0, "vref": 0.9},),
        7,
        {"safety": {"id": "s", "hash": "0" * 64}, "qualification": {"id": "q", "hash": "0" * 64}},
        str(tmp_path), "keep", "0.1", (),
    )
    assert spec.initial_state.mode == "warm_start"
    assert spec.input_schedule[0]["vref"] == 0.9
    assert spec.seed == 7
    assert spec.output_dir == str(tmp_path)
    assert spec.plant_config == {}
