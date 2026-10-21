"""Contract tests for the window-batched ngspice Buck plant.

The real simulator is injected as a deterministic callable so the lifecycle,
caching, snapshot and identity behavior can be verified without ngspice.
"""

from __future__ import annotations

import json
import re

import pytest

from pe_sim.backends.ngspice_plants import NgspiceBuckPlant
from pe_sim.backends.process import NgspiceRunResult
from pe_sim.backends.wrdata import TraceTable
from pe_sim.conformance import check_plant_adapter
from pe_sim.topology import export_topology_descriptor


_TRAN_RE = re.compile(r"^\.tran\s+(\S+)\s+(\S+)", re.MULTILINE)


class FakeSolver:
    """Deterministic stand-in for a real ngspice window solve."""

    def __init__(self) -> None:
        self.calls = 0
        self.netlists: list[str] = []

    def __call__(self, netlist: str) -> NgspiceRunResult:
        self.calls += 1
        self.netlists.append(netlist)
        match = _TRAN_RE.search(netlist)
        assert match is not None
        duration = float(match.group(2))
        points = 21
        times = tuple(duration * index / (points - 1) for index in range(points))
        vout = tuple(3.0 + 2.0 * (time / duration) for time in times)
        inductor_current = tuple(1.0 + 1.0 * (time / duration) for time in times)
        switch_node = tuple(12.0 if (time / duration) % 0.5 < 0.25 else 0.0 for time in times)
        table = TraceTable(
            times,
            {"v(vout)": vout, "i(l1)": inductor_current, "v(sw)": switch_node},
        )
        return NgspiceRunResult(
            returncode=0,
            stdout="",
            stderr="",
            log="",
            table=table,
            netlist_sha256="a" * 64,
            output_sha256="b" * 64,
            log_sha256="c" * 64,
            duration_s=0.0,
            command=("fake-ngspice",),
            timed_out=False,
        )


def _plant(solver: FakeSolver, **overrides) -> NgspiceBuckPlant:
    values = {"solver_callable": solver, "control_period_s": 10e-6}
    values.update(overrides)
    return NgspiceBuckPlant(**values)


def test_plant_passes_the_public_conformance_harness():
    solver = FakeSolver()

    report = check_plant_adapter(lambda: _plant(solver), action=0.5, duration_s=10e-6)

    assert report.passed, report.failures
    assert "advance_monotonic" in report.checks
    assert "snapshot_restore" in report.checks


def test_segments_inside_one_control_window_share_one_solver_call():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({})

    for _ in range(10):
        observation = plant.advance(0.5, 1e-6)

    assert solver.calls == 1
    assert observation.time_s == pytest.approx(10e-6)
    assert plant.time_s == pytest.approx(10e-6)


def test_duty_or_external_input_change_triggers_a_new_window_solve():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({})

    plant.advance(0.5, 1e-6)
    plant.advance(0.6, 1e-6)
    assert solver.calls == 2

    plant.advance(0.6, 1e-6, external_input={"vin_v": 15.0})
    assert solver.calls == 3
    assert plant.input_voltage_v == pytest.approx(15.0)


def test_snapshot_restore_is_deterministic_and_state_equivalent():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({"inductor_current_a": 0.5, "output_voltage_v": 2.0})
    plant.advance(0.5, 1e-6)

    before = plant.snapshot()
    plant.advance(0.5, 4e-6)
    after_advance = plant.snapshot()
    plant.restore(before)
    restored = plant.snapshot()

    assert restored == before
    assert after_advance["time_s"] > before["time_s"]


def test_snapshot_rejects_a_foreign_model_identity():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({})
    snapshot = dict(plant.snapshot())
    snapshot["model_kind"] = "ideal_averaged_reference"

    with pytest.raises(ValueError):
        plant.restore(snapshot)


def test_invalid_action_and_duration_fail_closed():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({})

    with pytest.raises(ValueError):
        plant.advance(float("nan"), 1e-6)
    with pytest.raises(ValueError):
        plant.advance(1.5, 1e-6)
    with pytest.raises(ValueError):
        plant.advance(0.5, 0.0)
    with pytest.raises(ValueError):
        plant.advance(0.5, float("inf"))


def test_initial_state_must_be_finite():
    solver = FakeSolver()
    plant = _plant(solver)

    with pytest.raises(ValueError):
        plant.reset({"vout": float("nan")})


def test_observation_separates_measurement_units_and_truth():
    solver = FakeSolver()
    plant = _plant(solver)
    plant.reset({})
    observation = plant.advance(0.5, 1e-6)

    assert set(observation.measurement) == {"vout", "inductor_current"}
    assert observation.measurement_units == {"vout": "V", "inductor_current": "A"}
    assert observation.controller_measurement() == dict(observation.measurement)
    assert "duty" in observation.truth
    assert "switch_node_voltage" in observation.truth


def test_manifest_identity_is_stable_json_and_includes_solver_settings():
    first = _plant(FakeSolver()).manifest_identity()
    second = _plant(FakeSolver()).manifest_identity()

    assert first["sha256"] == second["sha256"]
    assert first["kind"] == "ngspice_switching_reference"
    assert first["level"] == "L2"
    assert first["solver_settings"]["integration_method"] == "gear"
    assert first["backend"]["status"] == "not_declared"
    json.dumps(first, allow_nan=False)


def test_backend_provenance_is_not_declared_without_an_executable():
    plant = _plant(FakeSolver())

    evidence = plant.backend_provenance()

    assert evidence["status"] == "not_declared"
    assert evidence["limitations"]


def test_topology_descriptor_uses_the_switching_model_kind():
    plant = _plant(FakeSolver())

    descriptor = export_topology_descriptor(plant)

    assert descriptor.metadata["model_kind"] == "ngspice_switching_reference"
    kinds = {component.kind for component in descriptor.components}
    assert "voltage_controlled_switch" in kinds
    assert "freewheeling_diode" in kinds
    assert "pulse_source" in kinds


def test_analytical_steady_state_is_available_for_comparison():
    plant = _plant(FakeSolver())

    steady = plant.analytical_steady_state(0.5)

    assert steady["output_voltage_v"] == pytest.approx(6.0)
    assert steady["inductor_current_a"] == pytest.approx(0.6)


def test_runner_publishes_a_qualified_package_with_the_switching_identity(tmp_path):
    from pe_sim.contracts import ExperimentSpec, Timebase
    from pe_sim.runtime import FakePIController, Runner

    spec = ExperimentSpec(
        experiment_id="ngspice_contract",
        run_id="ngspice-contract",
        plant_id="ngspice-buck",
        controller_id="pi",
        timebase=Timebase(duration_s=20e-6, control_period_s=10e-6),
        output_dir=str(tmp_path),
        seed=0,
    )
    plant = _plant(FakeSolver(), control_period_s=10e-6)

    result = Runner().run(spec, plant, FakePIController(kp=0.1))

    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plant"]["identity"]["kind"] == "ngspice_switching_reference"
    assert manifest["plant"]["identity"]["level"] == "L2"
    assert manifest["plant"]["hash"]
    assert manifest["environment"]["backends"][0]["status"] == "not_declared"
