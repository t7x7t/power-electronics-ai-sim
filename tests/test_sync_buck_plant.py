"""Acceptance tests for the synchronous-Buck switching template (docs/60 A9).

The template is a new adapter, not a mode of the reviewed asynchronous Buck.
These tests prove the rendered gate timing, the dead-time validation, the
conduction-regime detector (A8), the adapter lifecycle, and one optional real
ngspice window.  They do not claim device, thermal, loss, or product accuracy.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.backends.process import NgspiceRunResult
from pe_sim.backends.settings import NgspiceSolverSettings
from pe_sim.backends.sync_buck import (
    SYNC_NETLIST_TEMPLATE_VERSION,
    NgspiceSyncBuckPlant,
    SyncBuckNetlistParameters,
    classify_conduction_regime,
    render_sync_buck_netlist,
)
from pe_sim.backends.wrdata import TraceTable
from pe_sim.runtime import Runner


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the real synchronous-Buck acceptance test",
)

_SWITCHING_PERIOD_S = 10e-6
_DEAD_TIME_S = 100e-9


def _parameters(**overrides) -> SyncBuckNetlistParameters:
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "switching_frequency_hz": 1.0 / _SWITCHING_PERIOD_S,
        "dead_time_s": _DEAD_TIME_S,
        "initial_inductor_current_a": 0.6,
        "initial_output_voltage_v": 6.0,
    }
    values.update(overrides)
    return SyncBuckNetlistParameters(**values)


def _pwl_pairs(netlist: str, source: str) -> list[tuple[float, float]]:
    """Return the flattened PWL pairs of one voltage source."""

    lines = netlist.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(source))
    collected = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("+"):
            break
        collected.append(line)
    text = " ".join(collected)
    match = re.search(r"PWL\((.*)\)", text, re.DOTALL)
    assert match is not None, text
    numbers = [float(value) for value in re.findall(r"[-+]?\d+\.\d+e[-+]\d+", match.group(1))]
    assert len(numbers) % 2 == 0
    pairs = list(zip(numbers[::2], numbers[1::2]))
    assert all(later[0] > earlier[0] for earlier, later in zip(pairs, pairs[1:]))
    return pairs


def _high_intervals(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for time_s, value in pairs:
        if value >= 0.5 and start is None:
            start = time_s
        elif value < 0.5 and start is not None:
            intervals.append((start, time_s))
            start = None
    if start is not None:
        intervals.append((start, pairs[-1][0]))
    return intervals


def test_template_has_two_gates_and_no_freewheeling_diode():
    netlist = render_sync_buck_netlist(
        _parameters(), duty=0.5, duration_s=20e-6, solver=NgspiceSolverSettings()
    )

    assert SYNC_NETLIST_TEMPLATE_VERSION in netlist
    assert "S1 vin sw hs_gate 0 SWMOD" in netlist
    assert "S2 sw 0 ls_gate 0 SWMOD" in netlist
    assert "D1 " not in netlist
    assert "Vghs hs_gate 0 PWL(" in netlist
    assert "Vgls ls_gate 0 PWL(" in netlist
    paths = _pwl_pairs(netlist, "Vghs")
    assert paths[0][1] == pytest.approx(0.0)
    assert any(value > 0.5 for _, value in paths)


def test_dead_time_is_visible_on_both_gate_transitions():
    netlist = render_sync_buck_netlist(
        _parameters(), duty=0.5, duration_s=20e-6, solver=NgspiceSolverSettings()
    )
    high = _high_intervals(_pwl_pairs(netlist, "Vghs"))
    low = _high_intervals(_pwl_pairs(netlist, "Vgls"))

    assert high, "high-side gate never turns on"
    assert low, "low-side gate never turns on"
    # One full period: HS [1ns, 4.9us], LS [5.0us, 9.9us].
    hs_start, hs_stop = high[0]
    ls_start, ls_stop = low[0]
    assert hs_start == pytest.approx(1e-9, abs=2e-9)
    assert hs_stop == pytest.approx(4.9e-6, abs=2e-9)
    assert ls_start == pytest.approx(5.0e-6, abs=2e-9)
    assert ls_stop == pytest.approx(9.9e-6, abs=2e-9)
    assert ls_start - hs_stop == pytest.approx(_DEAD_TIME_S, abs=2e-9)
    assert (hs_start + _SWITCHING_PERIOD_S) - ls_stop == pytest.approx(
        _DEAD_TIME_S, abs=2e-9
    )


def test_template_is_deterministic_and_phase_aware():
    solver = NgspiceSolverSettings()
    first = render_sync_buck_netlist(
        _parameters(), duty=0.4, duration_s=20e-6, solver=solver
    )
    second = render_sync_buck_netlist(
        _parameters(), duty=0.4, duration_s=20e-6, solver=solver
    )
    shifted = render_sync_buck_netlist(
        _parameters(),
        duty=0.4,
        duration_s=20e-6,
        solver=solver,
        carrier_phase_s=3e-6,
    )

    assert first == second
    assert first != shifted
    # At phase 3 us the high-side pulse has 1 us left of its 4 us width, so the
    # PWL source must start high rather than wait for the next carrier.
    assert _pwl_pairs(shifted, "Vghs")[0][1] >= 0.5


def test_dead_time_and_duty_are_validated():
    with pytest.raises(ValueError):
        _parameters(dead_time_s=0.0)
    with pytest.raises(ValueError):
        _parameters(dead_time_s=-1e-9)
    with pytest.raises(ValueError):
        _parameters(dead_time_s=_SWITCHING_PERIOD_S / 2.0)
    with pytest.raises(ValueError):
        render_sync_buck_netlist(
            _parameters(), duty=-0.1, duration_s=10e-6, solver=NgspiceSolverSettings()
        )
    with pytest.raises(ValueError):
        render_sync_buck_netlist(
            _parameters(), duty=1.1, duration_s=10e-6, solver=NgspiceSolverSettings()
        )


def _table(shape: str, *, periods: int = 1, samples: int = 200) -> TraceTable:
    total = _SWITCHING_PERIOD_S * periods
    times = tuple(total * index / (samples - 1) for index in range(samples))
    values: list[float] = []
    for time_s in times:
        phase = (time_s % _SWITCHING_PERIOD_S) / _SWITCHING_PERIOD_S
        if shape == "ccm":
            value = 0.5 + 0.1 * math.sin(2.0 * math.pi * phase)
        elif shape == "dcm":
            value = max(0.0, 0.6 * math.sin(2.0 * math.pi * phase))
        elif shape == "forced_ccm":
            value = 0.1 * math.sin(2.0 * math.pi * phase)
        elif shape == "idle":
            value = 0.0
        elif shape == "burst":
            value = 0.0 if time_s < total / 2.0 else 0.5
        else:
            value = 0.5
        values.append(value)
    return TraceTable(times, {"i(l1)": tuple(values)})


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("ccm", "ccm"),
        ("dcm", "dcm"),
        ("forced_ccm", "forced_ccm"),
        ("idle", "idle"),
        ("burst", "burst"),
    ],
)
def test_regime_classifier_shapes(shape: str, expected: str):
    periods = 2 if shape == "burst" else 1
    result = classify_conduction_regime(
        _table(shape, periods=periods),
        switching_period_s=_SWITCHING_PERIOD_S,
        duty=0.0 if shape == "idle" else 0.5,
        dead_time_s=_DEAD_TIME_S,
    )
    assert result == expected


def test_regime_classifier_returns_unknown_for_a_short_trace():
    table = TraceTable((0.0, 1e-6), {"i(l1)": (0.5, 0.6)})
    assert (
        classify_conduction_regime(
            table,
            switching_period_s=_SWITCHING_PERIOD_S,
            duty=0.5,
            dead_time_s=_DEAD_TIME_S,
        )
        == "unknown"
    )


class FakeSolver:
    """Deterministic switching waveform for adapter lifecycle tests."""

    def __init__(self, shape: str = "ccm") -> None:
        self.shape = shape
        self.calls = 0
        self.netlists: list[str] = []

    def __call__(self, netlist: str) -> NgspiceRunResult:
        self.calls += 1
        self.netlists.append(netlist)
        match = re.search(r"^\.tran\s+(\S+)\s+(\S+)", netlist, re.MULTILINE)
        assert match is not None
        duration = float(match.group(2))
        table = _table(self.shape, samples=101)
        if self.shape == "idle":
            values = tuple(0.0 for _ in table.time_s)
            table = TraceTable(table.time_s, {"i(l1)": values})
        if duration <= 0:
            raise AssertionError("duration must be positive")
        scaled = TraceTable(
            tuple(duration * index / (len(table.time_s) - 1) for index in range(len(table.time_s))),
            dict(table.signals),
        )
        vout = tuple(6.0 for _ in scaled.time_s)
        switch = tuple(12.0 if value > 0.55 else 0.0 for value in scaled.signals["i(l1)"])
        return NgspiceRunResult(
            returncode=0,
            stdout="",
            stderr="",
            log="",
            table=TraceTable(
                scaled.time_s,
                {"v(vout)": vout, "i(l1)": scaled.signals["i(l1)"], "v(sw)": switch},
            ),
            netlist_sha256="a" * 64,
            output_sha256="b" * 64,
            log_sha256="c" * 64,
            duration_s=0.0,
            command=("fake-ngspice",),
            timed_out=False,
        )


def _plant(solver: FakeSolver, **overrides) -> NgspiceSyncBuckPlant:
    values = {
        "dead_time_s": _DEAD_TIME_S,
        "control_period_s": _SWITCHING_PERIOD_S,
        "solver_callable": solver,
    }
    values.update(overrides)
    return NgspiceSyncBuckPlant(**values)


def test_adapter_exposes_regime_and_one_solver_call_per_window():
    solver = FakeSolver("dcm")
    plant = _plant(solver)
    plant.reset({})

    observation = plant.advance(0.5, _SWITCHING_PERIOD_S)

    assert solver.calls == 1
    assert observation.truth["conduction_regime"] == "dcm"
    evidence = plant.backend_evidence()
    assert evidence["conduction_regime"] == "dcm"
    assert evidence["dead_time_s"] == pytest.approx(_DEAD_TIME_S)
    assert evidence["solver_invocations"] == 1


def test_adapter_snapshot_restore_round_trips_the_dead_time_identity():
    solver = FakeSolver("ccm")
    plant = _plant(solver)
    plant.reset({"output_voltage_v": 6.0, "inductor_current_a": 0.6})
    plant.advance(0.5, _SWITCHING_PERIOD_S)
    before = plant.snapshot()

    plant.advance(0.5, _SWITCHING_PERIOD_S)
    plant.restore(before)

    assert plant.snapshot() == before
    assert before["dead_time_s"] == pytest.approx(_DEAD_TIME_S)
    assert before["netlist_template_version"] == SYNC_NETLIST_TEMPLATE_VERSION


def test_manifest_identity_names_the_new_solve_path():
    plant = _plant(FakeSolver())

    identity = plant.manifest_identity()

    assert identity["kind"] == "ngspice_sync_buck_reference"
    assert identity["backend_id"] == "ngspice-sync-buck"
    assert identity["netlist_template"] == SYNC_NETLIST_TEMPLATE_VERSION
    assert identity["parameters"]["dead_time_s"] == pytest.approx(_DEAD_TIME_S)
    assert identity["implementation"]["review"]["status"] == "not_pinned"
    json.dumps(identity, allow_nan=False)


def test_runner_publishes_the_declared_route_and_evidence(tmp_path):
    solver = FakeSolver("ccm")
    plant = _plant(solver)
    spec = ExperimentSpec(
        experiment_id="sync-buck-route",
        run_id="sync-buck-route",
        plant_id="ngspice-sync-buck",
        controller_id="pi",
        timebase=Timebase(duration_s=50e-6, control_period_s=10e-6),
        output_dir=str(tmp_path),
        seed=3,
    )

    result = Runner().run(spec, plant, FakePIController(kp=0.1))

    assert result.status in {"QUALIFIED", "RUN_OK"}
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route"] == {
        "time_model": "stepped",
        "control_location": "kernel",
        "event_granularity": "continuous",
        "evidence_cap": "comparable",
        "limitations": [],
    }
    assert manifest["plant"]["id"] == "ngspice-sync-buck"
    assert manifest["plant"]["evidence"]["solver_invocations"] == 5
    assert manifest["plant"]["evidence"]["dead_time_s"] == pytest.approx(_DEAD_TIME_S)


@needs_ngspice
def test_real_ngspice_window_keeps_the_output_near_the_precharged_value():
    plant = NgspiceSyncBuckPlant(
        dead_time_s=_DEAD_TIME_S,
        control_period_s=_SWITCHING_PERIOD_S,
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        switching_frequency_hz=1.0 / _SWITCHING_PERIOD_S,
        ngspice_executable=NGSPICE,
        timeout_s=120.0,
    )
    plant.reset({"output_voltage_v": 6.0, "inductor_current_a": 0.6})

    observation = plant.advance(0.5, 100e-6)

    assert 5.5 <= observation.measurement["vout"] <= 6.5
    assert math.isfinite(observation.measurement["inductor_current"])
    assert plant.backend_evidence()["conduction_regime"] in {"ccm", "forced_ccm"}
