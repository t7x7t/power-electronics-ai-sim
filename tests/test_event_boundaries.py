"""Event-boundary acceptance for docs/60 rows A4, D2 and D3.

The reviewed switching adapter used to restart the PWM carrier at every control
window, which forced the control period to be an integer multiple of the
switching period and pushed any input change to the next window boundary.  ADR
0004 step 2 makes the carrier phase a parameter and ends a solve segment at the
next declared change point.

These tests are local infrastructure acceptance only.  They do not certify
device, thermal, hardware, or product behaviour, and the averaged models are not
involved.
"""

from __future__ import annotations

import json
import os

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.backends import NgspiceBuckPlant, NgspiceSolverSettings
from pe_sim.backends.netlist import BuckNetlistParameters, render_buck_netlist
from pe_sim.builtin_plants import load_builtin_plants
from pe_sim.plant_registry import get_plant
from pe_sim.runtime import Runner


NGSPICE = os.environ.get("PE_SIM_NGSPICE")
needs_ngspice = pytest.mark.skipif(
    not NGSPICE,
    reason="set PE_SIM_NGSPICE to run the real ngspice acceptance tests",
)

_SWITCHING_PERIOD_S = 10e-6
_BASE = {
    "input_voltage_v": 12.0,
    "inductance_h": 100e-6,
    "capacitance_f": 470e-6,
    "load_resistance_ohm": 10.0,
    "switching_frequency_hz": 1.0 / _SWITCHING_PERIOD_S,
    "ngspice_executable": NGSPICE,
    "timeout_s": 120.0,
}


def _plant(control_period_s: float, **overrides) -> NgspiceBuckPlant:
    values = dict(_BASE)
    values.update(overrides)
    return NgspiceBuckPlant(control_period_s=control_period_s, **values)


def _spec(tmp_path, run_id: str, *, duration_s: float, control_period_s: float, schedule):
    return ExperimentSpec(
        "event-boundaries",
        run_id,
        "ngspice-buck",
        "pi",
        Timebase(duration_s=duration_s, control_period_s=control_period_s),
        input_schedule=tuple(schedule),
        output_dir=str(tmp_path),
        seed=17,
    )


def _manifest(result) -> dict:
    return json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_the_netlist_carries_the_segment_start_carrier_phase():
    """The phase is a rendered parameter, not an implicit zero."""

    parameters = BuckNetlistParameters(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        switching_frequency_hz=1.0 / _SWITCHING_PERIOD_S,
    )
    solver = NgspiceSolverSettings()
    zero_phase = render_buck_netlist(
        parameters, duty=0.4, duration_s=15e-6, solver=solver
    )
    mid_phase = render_buck_netlist(
        parameters,
        duty=0.4,
        duration_s=15e-6,
        solver=solver,
        carrier_phase_s=3e-6,
    )

    assert ".param TD=0.000000000000e+00" in zero_phase
    # PULSE's delay is the time to the next carrier start, not the phase itself:
    # a segment starting at phase 3 us waits 7 us for the next on-interval.
    assert ".param TD=7.000000000000e-06" in mid_phase
    assert "PULSE(0 1 {TD} 1e-9 1e-9" in mid_phase
    # The segment starts inside the 4 us on-pulse, so the remaining 1 us must be
    # rendered as a finite tail and summed with the periodic source.
    assert "Vtail pwm_tail 0 PWL(0 1 1.000000000000e-06 1 1.001000000000e-06 0)" in mid_phase
    assert "Bpwm pwm 0 V = V(pwm_pulse) + V(pwm_tail)" in mid_phase
    assert zero_phase != mid_phase


@needs_ngspice
def test_control_period_not_an_integer_multiple_keeps_the_carrier_phase():
    """D2: a 1.5-period control window is phase-continuous across windows."""

    control_period_s = 1.5 * _SWITCHING_PERIOD_S
    windows = 8
    chained = _plant(control_period_s)
    chained.reset({})
    observation = None
    for _ in range(windows):
        observation = chained.advance(0.4, control_period_s)

    assert observation is not None
    assert observation.time_s == pytest.approx(windows * control_period_s, abs=1e-12)

    # One direct run over the same total interval is the phase-continuous
    # reference.  A carrier that restarted every 1.5 periods would change the
    # effective duty by roughly a third, so a few-percent tolerance still
    # distinguishes the two behaviours.
    reference = _plant(windows * control_period_s)
    reference.reset({})
    expected = reference.advance(0.4, windows * control_period_s)

    assert observation.measurement["vout"] == pytest.approx(
        expected.measurement["vout"], rel=0.05
    )
    assert observation.measurement["inductor_current"] == pytest.approx(
        expected.measurement["inductor_current"], rel=0.15
    )


@needs_ngspice
def test_a_window_boundary_inside_the_on_pulse_keeps_the_energy():
    """D2 with a ratio whose window boundaries fall inside the on-pulse.

    At 1.2 switching periods the boundaries alternate through phases
    0, 2, 4, 6, 8, 10 us.  Phase 2 us is inside the 4 us on-pulse, so a shifted
    rising edge alone loses 2 us of on-time per affected window.  This ratio
    is the case the 1.5-period test cannot see.
    """

    control_period_s = 1.2 * _SWITCHING_PERIOD_S
    windows = 8
    chained = _plant(control_period_s)
    chained.reset({})
    observation = None
    for _ in range(windows):
        observation = chained.advance(0.4, control_period_s)

    reference = _plant(windows * control_period_s)
    reference.reset({})
    expected = reference.advance(0.4, windows * control_period_s)

    assert observation is not None
    assert observation.measurement["vout"] == pytest.approx(
        expected.measurement["vout"], rel=0.01
    )
    assert observation.measurement["inductor_current"] == pytest.approx(
        expected.measurement["inductor_current"], rel=0.02
    )


@needs_ngspice
def test_a_change_inside_the_on_pulse_is_applied_at_its_declared_time(tmp_path):
    """A4/D3 at a phase inside the on-pulse, with a change too small to matter.

    A 0.01% load change cannot move the final state by a measurable amount.  If
    a segment that starts inside the on-pulse loses its remaining on-time, the
    same tiny change moves the output by tens of percent.  The comparison is
    therefore a direct test of the carrier reconstruction, not of the load.
    """

    load_builtin_plants()
    control_period_s = 1.5 * _SWITCHING_PERIOD_S
    duration_s = 2 * control_period_s

    unchanged_schedule = (({"time_s": 0.0, "load_ohm": 10.0}),)
    on_pulse_schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": 0.25 * control_period_s, "load_ohm": 10.001},
    )

    def run(run_id, schedule):
        plant = _plant(control_period_s, sample_step_s=5e-9)
        Runner().run(
            _spec(
                tmp_path,
                run_id,
                duration_s=duration_s,
                control_period_s=control_period_s,
                schedule=schedule,
            ),
            plant,
            FakePIController(kp=0.0, ki=0.0, kff_v=0.4),
        )
        return plant.observe().measurement

    unchanged = run("no-change", unchanged_schedule)
    changed = run("on-pulse-change", on_pulse_schedule)

    # 2.5 us of a 10 us period is inside the 4 us on-pulse.
    assert changed["vout"] == pytest.approx(unchanged["vout"], rel=0.01)
    assert changed["inductor_current"] == pytest.approx(
        unchanged["inductor_current"], rel=0.01
    )


@needs_ngspice
def test_a_load_step_inside_a_window_is_solved_where_it_is_declared(tmp_path):
    """A4/D3: a mid-window step is applied at its declared time."""

    load_builtin_plants()
    control_period_s = 1.5 * _SWITCHING_PERIOD_S
    duration_s = 2 * control_period_s
    mid_window_s = 0.5 * control_period_s

    mid_schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": mid_window_s, "load_ohm": 5.0},
    )
    mid_plant = _plant(control_period_s, sample_step_s=5e-9)
    mid = Runner().run(
        _spec(
            tmp_path,
            "mid-window-step",
            duration_s=duration_s,
            control_period_s=control_period_s,
            schedule=mid_schedule,
        ),
        mid_plant,
        FakePIController(kp=0.0, ki=0.0, kff_v=0.4),
    )

    # The same step moved to the window boundary is the counterfactual: if the
    # kernel quantised events to windows, both runs would be identical.
    boundary_schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": control_period_s, "load_ohm": 5.0},
    )
    boundary_plant = _plant(control_period_s, sample_step_s=5e-9)
    boundary = Runner().run(
        _spec(
            tmp_path,
            "boundary-step",
            duration_s=duration_s,
            control_period_s=control_period_s,
            schedule=boundary_schedule,
        ),
        boundary_plant,
        FakePIController(kp=0.0, ki=0.0, kff_v=0.4),
    )

    mid_samples = json.loads(
        (mid.run_dir / "samples.json").read_text(encoding="utf-8")
    )
    boundary_samples = json.loads(
        (boundary.run_dir / "samples.json").read_text(encoding="utf-8")
    )
    assert len(mid_samples) == len(boundary_samples) == 2

    # Both runs start cold, so the first boundary sample is identical.  The
    # second sample is taken after the first window has been advanced: only the
    # mid-window run has already experienced the 5 ohm load, so its output is
    # lower.  If events were quantised to the window boundary, the two runs
    # would be identical.
    assert mid_samples[0]["vout"] == pytest.approx(boundary_samples[0]["vout"])
    assert mid_samples[1]["vout"] < boundary_samples[1]["vout"]
    mid_final = mid_plant.observe().measurement["vout"]
    boundary_final = boundary_plant.observe().measurement["vout"]
    assert mid_final < boundary_final
    # The output is still small at 30 us, so the accumulated difference from
    # 7.5 us of extra load is microvolts; the point is that it is non-zero and
    # in the expected direction, not that it is large.
    assert boundary_final - mid_final > 1e-6

    # The same change point resolved inside one long control window must agree
    # with the two-window run: segmentation must not change the physics.
    single_plant = _plant(duration_s, sample_step_s=5e-9)
    Runner().run(
        _spec(
            tmp_path,
            "single-window-step",
            duration_s=duration_s,
            control_period_s=duration_s,
            schedule=mid_schedule,
        ),
        single_plant,
        FakePIController(kp=0.0, ki=0.0, kff_v=0.4),
    )
    assert single_plant.observe().measurement["vout"] == pytest.approx(
        mid_final, rel=0.03
    )

    route = _manifest(mid)["route"]
    assert route["event_granularity"] == "continuous"
    assert "events_quantized_to_control_windows" not in route["limitations"]


def test_the_registration_declares_continuous_events():
    load_builtin_plants()
    registration = get_plant("ngspice-buck")

    assert registration.event_granularity == "continuous"
    assert registration.control_location == "kernel"