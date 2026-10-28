"""Acceptance checks for the two ``control_location = model`` adapters.

- ``model-control-buck`` is a stepped adapter whose PI loop lives in the model.
- ``whole-run-switching-buck`` is a switching-level whole-run engine with the
  same model-side control loop and a declared trace cadence.

The tests also pin the evidence-pricing rules: both routes are capped at
``limited``, the kernel action is ignored, and the whole-run route cannot be
used for formal comparison or checkpoint recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.backends.model_control_buck import (
    ModelControlBuckParameters,
    ModelControlBuckPlant,
    PeakCurrentBuckParameters,
    PeakCurrentBuckPlant,
    WholeRunSwitchingBuckPlant,
)
from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.plant_registry import clear_registrations, get_plant
from pe_sim.runtime import Runner
from pe_sim.time_model import TIME_MODEL_STEPPED, TIME_MODEL_WHOLE_RUN, WholeRunRequest
from pe_sim.topology import export_topology_descriptor


_PLANT_CONFIG = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 5.0,
    "switching_frequency_hz": 100e3,
    "control_period_s": 10e-6,
    "integration_step_s": 0.5e-6,
    "kp": 0.05,
    "ki": 20.0,
    "default_vref_v": 5.0,
}


@pytest.fixture(autouse=True)
def _builtin_registry():
    clear_registrations()
    load_builtin_plants()
    yield
    clear_registrations()


def _parameters(**overrides) -> ModelControlBuckParameters:
    values = dict(_PLANT_CONFIG)
    values.update(overrides)
    return ModelControlBuckParameters(**values)


def _build(plant_id: str, **overrides):
    values = dict(_PLANT_CONFIG)
    values.update(overrides)
    context = PlantBuildContext(
        timebase=Timebase(duration_s=0.03, control_period_s=10e-6)
    )
    return get_plant(plant_id).factory(values, context)


def _run(
    tmp_path: Path,
    plant_id: str,
    *,
    run_id: str,
    duration_s: float,
    kp: float,
    plant_overrides: dict | None = None,
):
    plant_config = {**_PLANT_CONFIG, **(plant_overrides or {})}
    if plant_id.startswith("whole-run"):
        plant_config["cadence_s"] = 10e-6
    spec = ExperimentSpec(
        "s9-model-control",
        run_id,
        plant_id,
        "pi",
        Timebase(duration_s=duration_s, control_period_s=10e-6),
        output_dir=str(tmp_path / "runs"),
        plant_config=plant_config,
        controller_config={"kp": kp},
        input_schedule=(({"time_s": 0.0, "vref": 5.0}),),
        seed=0,
    )
    plant = _build(plant_id, **(plant_overrides or {}))
    return Runner().run(spec, plant, FakePIController())


def _samples(run_dir: Path) -> list[dict]:
    return json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Construction, declarations, and catalogue
# ---------------------------------------------------------------------------


def test_registration_declares_the_model_control_route():
    stepped = get_plant("model-control-buck")
    whole = get_plant("whole-run-switching-buck")

    assert stepped.time_model == TIME_MODEL_STEPPED
    assert stepped.control_location == "model"
    assert stepped.class_name == "ModelControlBuckPlant"
    assert whole.time_model == TIME_MODEL_WHOLE_RUN
    assert whole.control_location == "model"
    assert whole.class_name == "WholeRunSwitchingBuckPlant"


def test_topology_export_names_the_model_side_control():
    plant = _build("model-control-buck")
    descriptor = export_topology_descriptor(plant)

    assert descriptor.topology_id == "buck"
    assert descriptor.metadata["control_location"] == "model"
    assert descriptor.metadata["model_kind"] == "model_side_controlled_switching_reference"
    assert descriptor.metadata["dead_time_modelled"] is False


def test_topology_export_distinguishes_the_model_kind_and_backend():
    peak = export_topology_descriptor(_build("peak-current-buck"))
    whole = export_topology_descriptor(_build("whole-run-switching-buck"))

    assert peak.metadata["model_kind"] == "model_side_peak_current_reference"
    assert peak.metadata["backend"] == "peak-current-buck"
    assert whole.metadata["model_kind"] == "model_side_controlled_switching_reference"
    assert whole.metadata["backend"] == "whole-run-switching-buck"


def test_parameters_reject_a_step_larger_than_the_switching_resolution():
    with pytest.raises(ValueError, match="one twentieth"):
        _parameters(integration_step_s=10e-6)


# ---------------------------------------------------------------------------
# Stepped model-controlled adapter
# ---------------------------------------------------------------------------


def test_stepped_adapter_converges_and_exposes_switching_ripple():
    plant = ModelControlBuckPlant(_parameters())
    plant.reset({"mode": "cold_start"})

    for _ in range(6000):
        observation = plant.advance(0.0, 10e-6, {"time_s": 0.0, "vref": 5.0})

    assert observation.time_s == pytest.approx(0.06, abs=1e-12)
    assert observation.measurement["vout"] == pytest.approx(5.0, rel=0.01)
    assert observation.truth["duty"] == pytest.approx(0.4167, rel=0.02)
    assert observation.truth["kernel_action_ignored"] == 0.0
    assert observation.truth["controller_integral"] > 0.0

    # Sampling only at control-period boundaries lands on the same carrier
    # phase every cycle, so it hides the ripple.  Walk one switching period in
    # 1 us steps to expose the intra-cycle waveform.
    currents = []
    for _ in range(10):
        observation = plant.advance(0.0, 1e-6, {"time_s": 0.06, "vref": 5.0})
        currents.append(observation.measurement["inductor_current"])
    ripple = max(currents) - min(currents)
    assert 0.2 < ripple < 0.5


def test_stepped_adapter_ignores_the_kernel_action_value():
    def trajectory(actions: tuple[float, ...]) -> tuple[list[float], list[float]]:
        plant = ModelControlBuckPlant(_parameters())
        plant.reset({"mode": "cold_start"})
        voltages: list[float] = []
        seen: list[float] = []
        for action in actions:
            observation = plant.advance(action, 10e-6, {"vref": 5.0})
            voltages.append(observation.measurement["vout"])
            seen.append(observation.truth["kernel_action_ignored"])
        return voltages, seen

    voltage_a, seen_a = trajectory((0.0,) * 200)
    voltage_b, seen_b = trajectory((1.0,) * 200)

    assert seen_a == [0.0] * 200
    assert seen_b == [1.0] * 200
    # The state is driven by the model-side PI loop, so a different kernel
    # action must not create a different trajectory.
    assert voltage_a == voltage_b


def test_stepped_runner_records_model_route_and_ignores_controller_gain(tmp_path):
    zero_gain = _run(tmp_path, "model-control-buck", run_id="model-kp0", duration_s=0.03, kp=0.0)
    high_gain = _run(tmp_path, "model-control-buck", run_id="model-kp1", duration_s=0.03, kp=1.0)

    assert zero_gain.status == "QUALIFIED"
    assert high_gain.status == "QUALIFIED"
    zero_manifest = json.loads((zero_gain.run_dir / "manifest.json").read_text(encoding="utf-8"))
    high_manifest = json.loads((high_gain.run_dir / "manifest.json").read_text(encoding="utf-8"))
    for manifest in (zero_manifest, high_manifest):
        assert manifest["route"]["control_location"] == "model"
        assert manifest["route"]["evidence_cap"] == "limited"
        assert "actuator_control_in_model" in manifest["route"]["limitations"]

    zero_samples = _samples(zero_gain.run_dir)
    high_samples = _samples(high_gain.run_dir)
    assert [row["vout"] for row in zero_samples] == [row["vout"] for row in high_samples]
    assert zero_samples[-1]["vout"] == pytest.approx(5.0, rel=0.02)


# ---------------------------------------------------------------------------
# Whole-run switching adapter
# ---------------------------------------------------------------------------


def test_whole_run_adapter_returns_a_declared_cadence_and_switching_ripple():
    plant = WholeRunSwitchingBuckPlant(_parameters(), cadence_s=10e-6)
    result = plant.run_whole(
        WholeRunRequest(
            duration_s=0.03,
            metadata={
                "input_schedule": [{"time_s": 0.0, "vref": 5.0}],
                "control_period_s": 10e-6,
            },
        )
    )

    assert len(result.time_s) == 3001
    assert result.time_s[0] == 0.0
    assert result.time_s[-1] == pytest.approx(0.03, abs=1e-12)
    assert result.signals["vout"][-1] == pytest.approx(5.0, rel=0.02)
    assert result.metadata["control_location"] == "model"
    assert result.metadata["enforcement"] == "post_hoc_only"
    assert max(result.signals["vout"]) > min(result.signals["vout"])
    assert max(result.signals["inductor_current_a"]) - min(result.signals["inductor_current_a"]) > 0.1


def test_whole_run_adapter_can_sample_finer_than_the_control_period():
    plant = WholeRunSwitchingBuckPlant(_parameters(), cadence_s=1e-6)
    result = plant.run_whole(
        WholeRunRequest(
            duration_s=0.002,
            metadata={
                "input_schedule": [{"time_s": 0.0, "vref": 5.0}],
                "control_period_s": 10e-6,
            },
        )
    )

    assert len(result.time_s) == 2001
    assert result.time_s[1] - result.time_s[0] == pytest.approx(1e-6, abs=1e-12)
    assert max(result.signals["switch_node_voltage"]) == 1.0
    assert min(result.signals["switch_node_voltage"]) == 0.0


def test_whole_run_runner_records_post_hoc_route_and_cadence(tmp_path):
    result = _run(
        tmp_path,
        "whole-run-switching-buck",
        run_id="whole-switch",
        duration_s=0.01,
        kp=0.0,
    )

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route"]["time_model"] == TIME_MODEL_WHOLE_RUN
    assert manifest["route"]["control_location"] == "model"
    assert manifest["route"]["evidence_cap"] == "limited"
    assert "post_hoc_validation_only" in manifest["route"]["limitations"]
    samples = _samples(result.run_dir)
    assert len(samples) == 1001
    assert samples[-1]["vout"] == pytest.approx(4.386, rel=0.01)


def test_whole_run_adapter_tracks_a_vref_change_inside_the_horizon():
    plant = WholeRunSwitchingBuckPlant(_parameters(), cadence_s=10e-6)
    result = plant.run_whole(
        WholeRunRequest(
            duration_s=0.05,
            metadata={
                "input_schedule": [
                    {"time_s": 0.0, "vref": 3.0},
                    {"time_s": 0.025, "vref": 6.0},
                ],
                "control_period_s": 10e-6,
            },
        )
    )

    early_index = min(
        range(len(result.time_s)), key=lambda index: abs(result.time_s[index] - 0.02)
    )
    early = result.signals["vout"][early_index]
    late = result.signals["vout"][-1]
    assert early < 3.2
    assert late > 5.5


def test_peak_current_comparator_limits_the_inductor_current():
    values = {key: value for key, value in _PLANT_CONFIG.items() if key not in {"kp", "ki"}}
    parameters = PeakCurrentBuckParameters(
        **values,
        kp=0.5,
        ki=200.0,
        current_reference_max_a=2.0,
    )
    plant = PeakCurrentBuckPlant(parameters)
    plant.reset({"mode": "cold_start"})
    peaks = []
    for _ in range(1000):
        observation = plant.advance(0.0, 10e-6, {"vref": 5.0})
        peaks.append(observation.measurement["inductor_current"])

    assert max(peaks) <= 2.1
    assert observation.truth["current_reference_a"] > 0.0
    assert observation.truth["cycle_index"] == 1000
    assert observation.truth["comparator_event_s"] > 0.0
    assert observation.truth["comparator_event_s"] < observation.time_s


def test_peak_current_diode_emulation_records_a_zero_crossing_event():
    values = {key: value for key, value in _PLANT_CONFIG.items() if key not in {"kp", "ki"}}
    values["load_resistance_ohm"] = 50.0
    parameters = PeakCurrentBuckParameters(
        **values,
        kp=0.5,
        ki=200.0,
        current_reference_max_a=1.0,
        diode_emulation=True,
    )
    plant = PeakCurrentBuckPlant(parameters)
    plant.reset({"mode": "cold_start"})

    event = None
    for _ in range(5000):
        observation = plant.advance(0.0, 10e-6, {"vref": 5.0})
        if observation.truth["diode_zero_crossing_s"] >= 0.0:
            event = observation.truth["diode_zero_crossing_s"]

    assert event is not None
    assert event > 0.0
    assert observation.measurement["inductor_current"] >= 0.0
    assert observation.truth["comparator_event_s"] >= 0.0


def test_peak_current_runner_records_the_model_route(tmp_path):
    result = _run(
        tmp_path,
        "peak-current-buck",
        run_id="peak-current",
        duration_s=0.03,
        kp=0.0,
        plant_overrides={"kp": 0.5, "ki": 200.0},
    )

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route"]["control_location"] == "model"
    assert manifest["route"]["evidence_cap"] == "limited"
    assert "actuator_control_in_model" in manifest["route"]["limitations"]
    assert _samples(result.run_dir)[-1]["vout"] == pytest.approx(5.0, rel=0.02)


def test_whole_run_route_refuses_formal_comparison_before_writing(tmp_path):
    spec = ExperimentSpec(
        "s9-model-control",
        "formal-reject",
        "whole-run-switching-buck",
        "pi",
        Timebase(duration_s=0.01, control_period_s=10e-6),
        output_dir=str(tmp_path / "runs"),
        plant_config={**_PLANT_CONFIG, "cadence_s": 10e-6},
        controller_config={"kp": 0.0},
        seed=0,
    )
    plant = _build("whole-run-switching-buck")

    with pytest.raises(ValueError, match="whole-run"):
        Runner().run(spec, plant, FakePIController(), mode="formal_comparison")

    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())
