"""Pin what ``event_granularity=window`` still means after ADR 0004 step 2.

Step 2 ends every solve segment at the next declared input change point and
resolves the external input at the segment start.  A ``window`` registration
therefore does not quantise the plant input to control-window boundaries.  What
remains of ``window`` is the controller side: observation and actuation still
happen only at window boundaries, and the registration declares no cost for
in-window events.  The limitation code
``events_quantized_to_control_windows`` is consequently a controller-side
statement, not a plant statement.

This test pins that reading.  If a later change makes ``window`` really quantise
the plant input, the mid-window and boundary runs below become identical and
this test fails; the same change must then also update ``docs/58`` and ADR 0004.
The check uses the pure-Python ``buck`` registration, so it needs no ngspice.
"""

from __future__ import annotations

import json

import pytest

from pe_sim.builtin_plants import load_builtin_plants
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import FakePIController, Runner

_CONTROL_PERIOD_S = 1e-5
_DURATION_S = 4e-5


def _plant() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-7,
    )


def _spec(tmp_path, run_id: str, schedule) -> ExperimentSpec:
    return ExperimentSpec(
        "event-granularity",
        run_id,
        "buck",
        "pi",
        Timebase(duration_s=_DURATION_S, control_period_s=_CONTROL_PERIOD_S),
        input_schedule=tuple(schedule),
        output_dir=str(tmp_path),
        seed=17,
    )


def _run(tmp_path, run_id: str, schedule):
    result = Runner().run(
        _spec(tmp_path, run_id, schedule),
        _plant(),
        FakePIController(kp=0.0, ki=0.0, kff_v=0.4),
    )
    samples = json.loads(
        (result.run_dir / "samples.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (result.run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    return samples, manifest


def test_window_describes_the_controller_not_a_quantised_plant_input(tmp_path):
    """A mid-window load step is visible even though the route says ``window``.

    This pins ``window`` to "the controller observes and acts at window
    boundaries", not "the plant input is quantised to a window".  If a future
    change makes ``window`` really quantise the input, this test fails, and
    ``docs/58`` plus ADR 0004 must be updated in the same change.
    """

    load_builtin_plants()
    mid_schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": 0.5 * _CONTROL_PERIOD_S, "load_ohm": 5.0},
    )
    boundary_schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": _CONTROL_PERIOD_S, "load_ohm": 5.0},
    )

    mid_samples, mid_manifest = _run(tmp_path, "mid-window-step", mid_schedule)
    boundary_samples, _ = _run(tmp_path, "boundary-step", boundary_schedule)

    # (a) The controller is still sampled once per control window: the window
    # declaration describes the observation cadence, and that cadence does not
    # follow input change points.
    expected_samples = int(round(_DURATION_S / _CONTROL_PERIOD_S))
    assert expected_samples == 4
    assert len(mid_samples) == len(boundary_samples) == expected_samples

    # (b) Sample 0 is the cold-start observation at t=0, so it is identical.
    # Sample 1 is observed at the end of the first window (t=10 us): only the
    # mid-window run has already carried the 5 ohm load for 5 us.  A non-zero
    # difference proves the input was resolved at 5 us, not at 10 us; if events
    # were quantised to the window, the two runs would be identical.
    assert mid_samples[0]["vout"] == pytest.approx(boundary_samples[0]["vout"])
    delta = abs(mid_samples[1]["vout"] - boundary_samples[1]["vout"])
    assert delta > 0.0
    # A cold start towards 4.8 V over 5 us is microvolts, so the difference must
    # stay small.  This is a magnitude guard against reading the number as a
    # load-transient step, not a claim about how large a real transient is.
    assert delta < 1e-4
    assert mid_samples[1]["vout"] < boundary_samples[1]["vout"]

    # (c) and (d) The route still declares ``window`` and keeps the limitation
    # code.  Both now describe the controller side only.
    route = mid_manifest["route"]
    assert route["event_granularity"] == "window"
    assert "events_quantized_to_control_windows" in route["limitations"]
