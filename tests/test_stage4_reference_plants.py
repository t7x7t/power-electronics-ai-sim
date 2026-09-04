import math

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
from pe_sim.contracts import snapshot_digest
from pe_sim.runtime import Runner


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
    assert result.status == "RUN_OK"
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
