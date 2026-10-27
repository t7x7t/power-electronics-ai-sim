"""Acceptance checks for the shared-duty averaged multiphase Buck reference.

These tests verify the declared averaged model only.  They do not claim
per-phase current regulation, current sharing, ripple cancellation, efficiency,
or device accuracy.
"""

from __future__ import annotations

import json

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.cli import main as cli_main
from pe_sim.multiphase_plants import MultiphaseBuckPlant, phase_currents, phase_offsets_deg
from pe_sim.plant_registry import clear_registrations, get_plant
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import Runner
from pe_sim.topology import export_topology_descriptor


_DEFAULTS = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "phases": 4,
    "switching_frequency_hz": 100e3,
    "integration_step_s": 1e-6,
}


@pytest.fixture(autouse=True)
def _builtin_registry():
    clear_registrations()
    load_builtin_plants()
    yield
    clear_registrations()


def _plant(**overrides):
    values = dict(_DEFAULTS)
    values.update(overrides)
    return MultiphaseBuckPlant(**values)


# ---------------------------------------------------------------------------
# Construction and pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phases", [1, 9])
def test_phase_count_outside_constructor_domain_is_rejected(phases):
    with pytest.raises(ValueError, match="phases"):
        _plant(phases=phases)


@pytest.mark.parametrize(
    "overrides",
    [
        {"inductance_h": -1e-6},
        {"capacitance_f": 0.0},
        {"load_resistance_ohm": -10.0},
    ],
)
def test_non_positive_electrical_parameters_are_rejected(overrides):
    with pytest.raises(ValueError, match="finite and positive"):
        _plant(**overrides)


def test_phase_offsets_are_evenly_spaced():
    assert phase_offsets_deg(4) == (0, 90, 180, 270)
    assert phase_offsets_deg(2) == (0, 180)


def test_phase_currents_are_an_equal_share_declaration():
    assert phase_currents(0.48, 4) == pytest.approx((0.12, 0.12, 0.12, 0.12))


# ---------------------------------------------------------------------------
# Averaged equilibrium, transient, and single-phase equivalence
# ---------------------------------------------------------------------------


def test_analytical_steady_state_and_long_run_converge_to_declared_values():
    plant = _plant()
    equilibrium = plant.analytical_steady_state(0.4)

    assert equilibrium["output_voltage_v"] == pytest.approx(4.8, rel=0.01)
    assert equilibrium["inductor_current_a"] == pytest.approx(0.48, rel=0.01)
    assert equilibrium["per_phase_current_a"] == pytest.approx(0.12, rel=0.01)

    plant.reset({})
    # The LC pair is lightly damped at these declared values, so the voltage
    # envelope needs several decay constants even though the current settles
    # quickly through L_eff/R.
    observation = plant.advance(0.4, 0.08)
    assert observation.measurement["vout"] == pytest.approx(4.8, rel=0.01)
    assert observation.measurement["inductor_current"] == pytest.approx(0.48, rel=0.01)
    assert observation.truth["phase_count"] == 4
    assert observation.truth["effective_inductance_h"] == pytest.approx(25e-6)


def test_transient_exposes_phase_metadata_and_consistent_total_current():
    plant = _plant(phases=3)
    plant.reset({})
    observation = plant.advance(0.35, 2e-4)

    total = observation.measurement["inductor_current"]
    phase_currents_observed = tuple(
        observation.truth[f"inductor_current_phase_{index}"] for index in range(3)
    )
    assert sum(phase_currents_observed) == pytest.approx(total, abs=1e-12)
    assert observation.truth["phase_count"] == 3
    assert tuple(
        observation.truth[f"phase_offset_deg_{index}"] for index in range(3)
    ) == pytest.approx((0.0, 120.0, 240.0))


def test_total_state_trajectory_matches_single_phase_buck_with_effective_inductance():
    multiphase = _plant(phases=4)
    single_phase = BuckPlant(
        input_voltage_v=12.0,
        inductance_h=25e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )
    initial = {"inductor_current_a": 0.1, "output_voltage_v": 1.0}
    multiphase.reset(initial)
    single_phase.reset(initial)

    multiphase_observation = multiphase.advance(0.4, 2e-4)
    single_phase_observation = single_phase.advance(0.4, 2e-4)

    assert multiphase_observation.measurement["vout"] == pytest.approx(
        single_phase_observation.measurement["vout"], rel=1e-12, abs=1e-12
    )
    assert multiphase_observation.measurement["inductor_current"] == pytest.approx(
        single_phase_observation.measurement["inductor_current"], rel=1e-12, abs=1e-12
    )


# ---------------------------------------------------------------------------
# Registry, Runner, topology, and identity
# ---------------------------------------------------------------------------


def test_runner_end_to_end_uses_the_registered_multiphase_plant(tmp_path):
    spec = ExperimentSpec(
        experiment_id="multiphase-e2e",
        run_id="multiphase-e2e",
        plant_id="multiphase-buck",
        controller_id="pi",
        timebase=Timebase(duration_s=1e-3, control_period_s=10e-6),
        plant_config={
            "input_voltage_v": 12.0,
            "inductance_h": 100e-6,
            "capacitance_f": 470e-6,
            "load_resistance_ohm": 10.0,
            "phases": 4,
            "switching_frequency_hz": 100e3,
            "integration_step_s": 1e-6,
        },
        output_dir=str(tmp_path),
        seed=17,
    )
    plant = get_plant("multiphase-buck").factory(
        dict(spec.plant_config), PlantBuildContext(timebase=spec.timebase)
    )
    assert isinstance(plant, MultiphaseBuckPlant)

    result = Runner().run(spec, plant, FakePIController(kp=0.5))
    assert result.status in {"QUALIFIED", "RUN_OK"}

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route"]["time_model"] == "stepped"
    assert manifest["route"]["control_location"] == "kernel"
    assert manifest["route"]["event_granularity"] == "window"
    assert manifest["plant"]["id"] == "multiphase-buck"
    assert (
        manifest["plant"]["identity"]["kind"]
        == "ideal_averaged_multiphase_reference"
    )


def test_topology_descriptor_declares_one_branch_per_phase():
    plant = _plant(phases=4)
    descriptor = export_topology_descriptor(plant)

    kinds = [component.kind for component in descriptor.components]
    assert kinds.count("averaged_switch") == 4
    assert kinds.count("inductor") == 4
    assert descriptor.metadata["phases"] == 4
    assert descriptor.metadata["model_kind"] == "ideal_averaged_multiphase_reference"
    assert descriptor.metadata["effective_inductance_h"] == pytest.approx(25e-6)


def test_manifest_identity_is_unpinned_and_distinct_from_single_phase_buck():
    plant = _plant(phases=4)
    identity = plant.manifest_identity()

    assert identity["kind"] == "ideal_averaged_multiphase_reference"
    assert identity["topology"] == "multiphase_buck"
    assert identity["parameters"]["phases"] == 4
    assert identity["parameters"]["inductance_h"] == pytest.approx(100e-6)
    assert identity["parameters"]["switching_frequency_hz"] == pytest.approx(100e3)
    assert identity["parameters"]["effective_inductance_h"] == pytest.approx(25e-6)
    assert identity["implementation"]["solve_path_modules"] == ["multiphase_plants.py"]
    assert identity["implementation"]["review"]["status"] == "not_pinned"

    buck_identity = BuckPlant(
        input_voltage_v=12.0,
        inductance_h=25e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    ).manifest_identity()
    assert identity["implementation"]["sha256"] != buck_identity["implementation"]["sha256"]


def test_cli_lists_multiphase_buck(capsys):
    assert cli_main(["list-plants"]) == 0
    payload = json.loads(capsys.readouterr().out)
    entries = {item["plant_id"]: item for item in payload["plants"]}

    assert entries["multiphase-buck"]["trust_level"] == "reference"
    assert entries["multiphase-buck"]["topology_export"] is True
    assert entries["multiphase-buck"]["config_keys"] == sorted(_DEFAULTS)
