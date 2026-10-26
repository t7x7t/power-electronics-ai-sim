"""Protection state machine and short-circuit recovery (docs/60 rows B9, A7).

These tests cover the composable controller wrapper in
``pe_sim.protection``:

- B9: OVP, OCP, UVLO (with a cold-start mask), hiccup retry, latch, merged
  ``state_update`` audit keys, constructor validation, and checkpoint
  round-tripping.
- A7: a runner-level short circuit injected through ``input_schedule`` on the
  reference ``BuckPlant``; the trip action is executed no later than the next
  control period and the hiccup retry is observable.

The tests only assert observable behaviour of the wrapper and the Runner.
They are not switching-level, hardware, or accuracy claims; A7 remains capped
by ``protection_latency_is_one_control_period`` (``docs/61`` §2.3).
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

import pytest

from pe_sim import ActionRequest, ExperimentSpec, FakePIController, Timebase
from pe_sim.protection import (
    PROTECTION_LATCHED,
    PROTECTION_NORMAL,
    PROTECTION_TRIPPED,
    ProtectedController,
    ProtectionPolicy,
)
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import Runner


class _Driver:
    """Feed samples to a controller and thread ``state_update`` like the Runner."""

    def __init__(
        self,
        controller: ProtectedController,
        *,
        control_period_s: float = 1e-3,
        vref: float = 8.0,
        start_index: int = 0,
    ) -> None:
        self.controller = controller
        self.control_period_s = control_period_s
        self.vref = vref
        self.step_index = start_index
        self.state: Mapping[str, Any] = {}

    def step(self, measurement: Mapping[str, float]) -> ActionRequest:
        timing = {
            "sample_time_s": self.step_index * self.control_period_s,
            "control_period_s": self.control_period_s,
            "plant_step_s": self.control_period_s,
            "sample_offset_s": 0.0,
            "age_steps": 0,
            "step_index": self.step_index,
        }
        request = self.controller.observe(
            measurement, {"vref": self.vref}, timing, self.state
        )
        self.state = dict(request.state_update or {})
        self.step_index += 1
        return request


# ----------------------------------------------------------------------
# B9: individual protections
# ----------------------------------------------------------------------


def test_ovp_trips_on_overvoltage_and_applies_trip_duty():
    """B9/OVP: vout above the threshold returns the trip duty, not the inner law.

    A finite ``hiccup_s`` makes the trip recoverable, so the reported state is
    ``tripped``; ``hiccup_s=None`` latches and is covered separately.
    """

    controller = ProtectedController(
        FakePIController(kp=1.0),
        ProtectionPolicy(ovp_v=10.0, trip_duty=0.25, hiccup_s=1e-3),
    )
    request = _Driver(controller).step({"vout": 11.0, "inductor_current": 0.1})

    assert request.value == pytest.approx(0.25)
    assert request.state_update["protection_state"] == PROTECTION_TRIPPED
    assert request.state_update["protection_trips"] == 1
    assert request.state_update["protection_elapsed_s"] == 0.0
    # The trip action is time-stamped at the observed sample.
    assert request.produced_time_s == pytest.approx(0.0)
    assert request.target_time_s == pytest.approx(request.produced_time_s)


def test_ocp_trips_on_overcurrent_and_applies_trip_duty():
    """B9/OCP: inductor current above the threshold returns the trip duty."""

    controller = ProtectedController(
        FakePIController(kp=0.2),
        ProtectionPolicy(ocp_a=1.5, trip_duty=0.1, hiccup_s=1e-3),
    )
    request = _Driver(controller).step({"vout": 1.0, "inductor_current": 2.0})

    assert request.value == pytest.approx(0.1)
    assert request.state_update["protection_state"] == PROTECTION_TRIPPED
    assert request.state_update["protection_trips"] == 1


def test_uvlo_startup_mask_suppresses_the_cold_start_trip():
    """B9/UVLO: a cold start is not a brownout until the rail has started once."""

    controller = ProtectedController(
        FakePIController(kp=0.1), ProtectionPolicy(uvlo_v=5.0, hiccup_s=1e-3)
    )
    driver = _Driver(controller)

    cold_start = driver.step({"vout": 0.0, "inductor_current": 0.0})
    assert cold_start.state_update["protection_state"] == PROTECTION_NORMAL
    assert cold_start.state_update["protection_started"] is False
    assert cold_start.value == pytest.approx(0.1 * (8.0 - 0.0))

    armed = driver.step({"vout": 6.0, "inductor_current": 0.0})
    assert armed.state_update["protection_state"] == PROTECTION_NORMAL
    assert armed.state_update["protection_started"] is True

    brownout = driver.step({"vout": 4.0, "inductor_current": 0.0})
    assert brownout.state_update["protection_state"] == PROTECTION_TRIPPED
    assert brownout.state_update["protection_trips"] == 1


def test_uvlo_without_startup_mask_trips_on_the_first_sample():
    """B9/UVLO: disabling the mask arms UVLO for a pre-biased rail."""

    controller = ProtectedController(
        FakePIController(kp=0.1),
        ProtectionPolicy(uvlo_v=5.0, uvlo_startup_mask=False, hiccup_s=1e-3),
    )
    request = _Driver(controller).step({"vout": 0.0, "inductor_current": 0.0})

    assert request.value == pytest.approx(0.0)
    assert request.state_update["protection_state"] == PROTECTION_TRIPPED
    assert request.state_update["protection_trips"] == 1


# ----------------------------------------------------------------------
# B9: hiccup and latch
# ----------------------------------------------------------------------


def test_hiccup_holds_two_steps_then_retries_the_inner_controller():
    """B9/hiccup: trip, two held periods, retry, and re-trip while the fault lasts."""

    control_period_s = 1e-3
    controller = ProtectedController(
        FakePIController(kp=0.2),
        ProtectionPolicy(
            ocp_a=1.5, hiccup_s=3 * control_period_s, trip_duty=0.0
        ),
    )
    driver = _Driver(controller, control_period_s=control_period_s)
    fault = {"vout": 1.0, "inductor_current": 2.0}

    first = driver.step(fault)
    assert first.value == pytest.approx(0.0)
    assert first.state_update["protection_state"] == PROTECTION_TRIPPED
    assert first.state_update["protection_trips"] == 1

    for elapsed_steps in (1, 2):
        held = driver.step(fault)
        assert held.value == pytest.approx(0.0)
        assert held.state_update["protection_state"] == PROTECTION_TRIPPED
        assert held.state_update["protection_elapsed_s"] == pytest.approx(
            elapsed_steps * control_period_s
        )
        assert held.state_update["protection_trips"] == 1

    retry = driver.step(fault)
    assert retry.state_update["protection_state"] == PROTECTION_NORMAL
    assert retry.state_update["protection_elapsed_s"] == 0.0
    assert retry.state_update["protection_trips"] == 1
    # The inner law is executed on the retry step, not the trip duty.
    assert retry.value == pytest.approx(0.2 * (8.0 - 1.0))

    again = driver.step(fault)
    assert again.state_update["protection_state"] == PROTECTION_TRIPPED
    assert again.state_update["protection_trips"] == 2


def test_latched_policy_never_recovers_and_always_returns_trip_duty():
    """B9/latch: ``hiccup_s=None`` stays latched for every following sample."""

    controller = ProtectedController(
        FakePIController(kp=0.2),
        ProtectionPolicy(ocp_a=1.5, trip_duty=0.05),
    )
    driver = _Driver(controller)
    fault = {"vout": 1.0, "inductor_current": 2.0}

    for _ in range(10):
        request = driver.step(fault)
        assert request.value == pytest.approx(0.05)
        assert request.state_update["protection_state"] == PROTECTION_LATCHED

    assert request.state_update["protection_trips"] == 1
    assert controller.snapshot()["protection"]["trips"] == 1
    assert controller.snapshot()["protection"]["state"] == PROTECTION_LATCHED


# ----------------------------------------------------------------------
# B9: audit trail and declaration
# ----------------------------------------------------------------------


def test_delegated_state_update_keeps_inner_and_protection_keys():
    """B9: a healthy sample merges the inner keys with the protection keys."""

    inner = FakePIController(kp=0.2)
    controller = ProtectedController(
        inner, ProtectionPolicy(ovp_v=100.0, trip_duty=0.0)
    )
    driver = _Driver(controller)
    request = driver.step({"vout": 1.0, "inductor_current": 0.1})

    state_update = request.state_update
    assert set(inner.snapshot()) <= set(state_update)
    assert {
        "protection_state",
        "protection_trips",
        "protection_elapsed_s",
    } <= set(state_update)
    assert state_update["protection_state"] == PROTECTION_NORMAL
    assert driver.state["protection_state"] == PROTECTION_NORMAL
    assert driver.state["integral"] == pytest.approx(0.0)
    assert request.value == pytest.approx(0.2 * (8.0 - 1.0))


def test_capabilities_and_declared_limitations_are_published():
    """B9: the wrapper advertises ``protection`` and its A7 latency ceiling."""

    controller = ProtectedController(
        FakePIController(kp=0.2), ProtectionPolicy(ocp_a=1.5)
    )
    assert "protection" in controller.capabilities()
    assert controller.declared_limitations == (
        "protection_latency_is_one_control_period",
    )
    assert controller.policy.ocp_a == pytest.approx(1.5)
    identity = controller.manifest_identity()
    assert identity["parameters"]["ocp_a"] == pytest.approx(1.5)
    assert identity["declared_limitations"] == [
        "protection_latency_is_one_control_period"
    ]


# ----------------------------------------------------------------------
# B9: constructor validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ovp_v": -1.0},
        {"ovp_v": 0.0},
        {"ovp_v": math.nan},
        {"ovp_v": math.inf},
        {"ocp_a": "not-a-number"},
        {"uvlo_v": -5.0},
        {"uvlo_v": math.inf},
        {"uvlo_v": math.nan},
        {"ocp_a": 0.0},
        {"ocp_a": -2.0},
        {"ocp_a": math.nan},
        {"ocp_a": math.inf},
        {"trip_duty": -0.1},
        {"trip_duty": 1.1},
        {"trip_duty": math.nan},
        {"trip_duty": math.inf},
        {"hiccup_s": 0.0},
        {"hiccup_s": -1e-3},
        {"hiccup_s": math.nan},
        {"hiccup_s": math.inf},
        {"ovp_v": True},
        {"uvlo_v": True},
        {"ocp_a": False},
        {"trip_duty": True},
        {"hiccup_s": True},
        {"uvlo_startup_mask": "yes"},
        {"voltage_key": ""},
        {"current_key": ""},
    ],
)
def test_policy_rejects_invalid_declarations(kwargs):
    """B9: every malformed threshold is a construction-time error."""

    with pytest.raises(ValueError):
        ProtectionPolicy(**kwargs)


def test_policy_accepts_disabled_and_boundary_values():
    """B9: ``None`` disables a protection; duty bounds 0 and 1 are legal."""

    disabled = ProtectionPolicy()
    assert disabled.to_dict() == {
        "ovp_v": None,
        "uvlo_v": None,
        "ocp_a": None,
        "trip_duty": 0.0,
        "hiccup_s": None,
        "voltage_key": "vout",
        "current_key": "inductor_current",
        "uvlo_startup_mask": True,
    }
    assert ProtectionPolicy(trip_duty=1.0).trip_duty == 1.0
    declared = ProtectionPolicy(
        ovp_v=10, uvlo_v=5, ocp_a=1.2, hiccup_s=1e-3, trip_duty=0
    )
    assert declared.ocp_a == pytest.approx(1.2)
    assert declared.ovp_v == pytest.approx(10.0)


# ----------------------------------------------------------------------
# B9: checkpoint round trip
# ----------------------------------------------------------------------


def _fault_sequence(step_index: int) -> Mapping[str, float]:
    """Over-current for the first six samples, healthy afterwards."""

    return {
        "vout": 1.0,
        "inductor_current": 2.0 if step_index < 6 else 0.5,
    }


def test_snapshot_restore_reproduces_the_continuation_and_rejects_other_policies():
    """B9: a mid-hiccup snapshot continues bit-identically; other policies fail."""

    policy = ProtectionPolicy(ocp_a=1.5, hiccup_s=3e-3, trip_duty=0.0)
    reference = ProtectedController(FakePIController(kp=0.2, ki=1.0), policy)
    reference_driver = _Driver(reference, control_period_s=1e-3)

    records: list[tuple[str, float]] = []
    snapshot: dict[str, Any] | None = None
    for index in range(17):
        request = reference_driver.step(_fault_sequence(index))
        if index == 6:
            snapshot = reference.snapshot()
        records.append((reference_driver.state["protection_state"], request.value))

    assert snapshot is not None
    # Snapshot is taken while the hiccup timer is running.
    assert snapshot["protection"]["state"] == PROTECTION_TRIPPED
    assert snapshot["protection"]["elapsed_s"] == pytest.approx(2e-3)
    assert snapshot["protection"]["trips"] == 2

    restored = ProtectedController(FakePIController(kp=0.2, ki=1.0), policy)
    restored.restore(snapshot)
    restored_driver = _Driver(restored, control_period_s=1e-3, start_index=7)
    for index in range(7, 17):
        request = restored_driver.step(_fault_sequence(index))
        assert (
            restored_driver.state["protection_state"],
            request.value,
        ) == records[index]

    other = ProtectedController(
        FakePIController(kp=0.2, ki=1.0),
        ProtectionPolicy(ocp_a=1.0, hiccup_s=2e-3, trip_duty=0.0),
    )
    with pytest.raises(ValueError):
        other.restore(snapshot)


# ----------------------------------------------------------------------
# A7: runner-level short circuit on the reference buck
# ----------------------------------------------------------------------

# Fixture used by the A7 runner test.  With kp=0.2, L=100 uH, C=470 uF,
# 12 V input, and the load stepping 10 ohm -> 0.2 ohm at 25 us and back at
# 75 us, the unprotected run first exceeds OCP_A=1.2 A at sample index 6
# (t = 60 us, iL = 1.3938 A); sample index 5 (iL = 1.1733 A) is below it.
CONTROL_PERIOD_S = 1e-5
DURATION_S = 1.2e-4
FAULT_ON_S = 2.5e-5
FAULT_OFF_S = 7.5e-5
SHORT_LOAD_OHM = 0.2
NOMINAL_LOAD_OHM = 10.0
OCP_A = 1.2
HICCUP_S = 2e-5
PROPORTIONAL_GAIN = 0.2
REFERENCE_VREF = 1.0


def _a7_plant() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=NOMINAL_LOAD_OHM,
        integration_step_s=1e-6,
    )


def _a7_spec(tmp_path: Any, run_id: str, controller_id: str) -> ExperimentSpec:
    return ExperimentSpec(
        "protection-a7",
        run_id,
        "buck",
        controller_id,
        Timebase(control_period_s=CONTROL_PERIOD_S, duration_s=DURATION_S),
        input_schedule=(
            {"time_s": 0.0, "load_ohm": NOMINAL_LOAD_OHM},
            {"time_s": FAULT_ON_S, "load_ohm": SHORT_LOAD_OHM},
            {"time_s": FAULT_OFF_S, "load_ohm": NOMINAL_LOAD_OHM},
        ),
        output_dir=str(tmp_path),
        seed=17,
    )


def _samples(result: Any) -> list[dict[str, Any]]:
    return json.loads((result.run_dir / "samples.json").read_text(encoding="utf-8"))


def test_a7_short_circuit_trips_within_one_control_period_and_recovers(tmp_path):
    """A7: a schedule-injected short is tripped after one sample and recovers.

    Actual fixture (documented so the recorded indices are reproducible):
    ``control_period_s=1e-5``, ``duration_s=1.2e-4``, load 10 ohm -> 0.2 ohm
    at 25 us and back to 10 ohm at 75 us, ``kp=0.2``, ``ocp_a=1.2`` A,
    ``hiccup_s=2e-5``.  The unprotected run first crosses the threshold at
    sample index 6 (iL = 1.3938 A); protected, that sample requests the trip
    duty and sample 7 still does, so the executed response is never later
    than one control period.  The hiccup retry is visible at sample 8 and the
    inner law is executed again there.
    """

    plain = Runner().run(
        _a7_spec(tmp_path, "a7-unprotected", "pi"),
        _a7_plant(),
        FakePIController(kp=PROPORTIONAL_GAIN),
    )
    assert plain.status == "QUALIFIED"
    plain_samples = _samples(plain)

    first_violation = next(
        index
        for index, row in enumerate(plain_samples)
        if row["inductor_current"] >= OCP_A
    )
    assert first_violation == 6
    assert plain_samples[first_violation - 1]["inductor_current"] < OCP_A

    controller = ProtectedController(
        FakePIController(kp=PROPORTIONAL_GAIN),
        ProtectionPolicy(ocp_a=OCP_A, hiccup_s=HICCUP_S, trip_duty=0.0),
    )
    protected = Runner().run(
        _a7_spec(tmp_path, "a7-protected", "protected-pi"),
        _a7_plant(),
        controller,
    )
    assert protected.status == "QUALIFIED"
    samples = _samples(protected)
    assert len(samples) == len(plain_samples)

    # The trajectory is unchanged until the detection sample, which proves
    # the trip is caused by this fault and nothing else.
    for index in range(first_violation):
        assert samples[index]["action"] == pytest.approx(plain_samples[index]["action"])
        assert samples[index]["inductor_current"] == pytest.approx(
            plain_samples[index]["inductor_current"]
        )
    assert samples[first_violation]["inductor_current"] == pytest.approx(
        plain_samples[first_violation]["inductor_current"]
    )

    # Trip at the detection sample and still held at the following sample:
    # the fault response is executed no later than one control period.
    assert samples[first_violation]["action"] == pytest.approx(0.0)
    assert samples[first_violation + 1]["action"] == pytest.approx(0.0)

    # After the fault is removed, the hiccup timer expires and the inner
    # controller is executed again (positive duty).
    after_fault_cleared = [
        row for row in samples if row["time_s"] >= FAULT_OFF_S and row["action"] > 0.0
    ]
    assert after_fault_cleared, "protection never released the trip action"
    retry = after_fault_cleared[0]
    assert retry["action"] == pytest.approx(
        PROPORTIONAL_GAIN * (REFERENCE_VREF - retry["vout"])
    )

    snapshot = controller.snapshot()
    assert snapshot["protection"]["trips"] >= 1
    assert "protection_latency_is_one_control_period" in controller.declared_limitations
