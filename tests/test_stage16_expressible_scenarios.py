"""Runner-level expressibility proofs for docs/60 rows A3, A5, B4, B5 and B6.

Each test demonstrates that an existing kernel mechanism can express the
scenario without a product-code change:

- A3: scheduled ``load_ohm`` at a window boundary.
- A5: a per-window ``vref`` ramp delivered through ``command``.
- B4: a non-zero ``sample_offset_s`` exposed through ``timing``.
- B5: a controller feedforward term isolated from the proportional term.
- B6: a cascaded voltage/current controller carrying its intermediate
  ``current_reference`` through ``ActionRequest.state_update``.

These are expressibility proofs, not accuracy, precision, switching-level,
hardware, or product claims.  The tests assert only observable runner-level
behaviour and use deterministic in-process plants.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from pe_sim import (
    ActionRequest,
    BoundedActionPolicy,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    PlantObservation,
    Timebase,
)
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import Runner


class _RecordingController(FakePIController):
    """FakePIController that records every controller-facing input."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._records: list[dict[str, Any]] = []

    def observe(
        self,
        measurement: Mapping[str, float],
        command: Mapping[str, Any],
        timing: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> ActionRequest:
        request = super().observe(measurement, command, timing, state)
        self._records.append(
            {
                "measurement": dict(measurement),
                "command": dict(command),
                "timing": dict(timing),
                "state": dict(state),
                "request": request,
            }
        )
        return request

    @property
    def records(self) -> list[dict[str, Any]]:
        return self._records


class _RecordingBuckPlant(BuckPlant):
    """BuckPlant that records every PlantObservation it returns."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observations: list[PlantObservation] = []

    def observe(self) -> PlantObservation:
        observation = super().observe()
        self._observations.append(observation)
        return observation

    @property
    def observations(self) -> list[PlantObservation]:
        return self._observations


class _PinnedMeasurementPlant(FakePlant):
    """FakePlant whose measured output is pinned to a declared value.

    B5 needs ``vref - vout == 0`` while ``vref`` is non-zero so that the only
    non-zero action term is the controller's feedforward gain.  Pinning a
    fixture measurement is an explicit controller-test condition, not a plant
    or converter claim.
    """

    def __init__(
        self,
        measured_output_v: float,
        *,
        gain: float = 1.0,
        tau_s: float = 1e-3,
    ) -> None:
        super().__init__(gain=gain, tau_s=tau_s)
        self._measured_output_v = float(measured_output_v)

    def observe(self) -> PlantObservation:
        observation = super().observe()
        value = self._measured_output_v
        return PlantObservation(
            observation.time_s,
            {"vout": value},
            {"vout": value},
            observation.age_steps,
            {"vout": "V"},
        )


class _CascadedController:
    """Voltage outer loop feeding a current inner loop through state_update."""

    def __init__(
        self,
        *,
        kp_v: float = 0.5,
        ki_v: float = 0.0,
        kp_i: float = 0.2,
    ) -> None:
        self.kp_v = float(kp_v)
        self.ki_v = float(ki_v)
        self.kp_i = float(kp_i)
        self._records: list[dict[str, Any]] = []

    def capabilities(self) -> set[str]:
        return {"cascade"}

    def reset(
        self,
        controller_state: Mapping[str, Any] | None,
        seed: int,
    ) -> Mapping[str, Any]:
        del seed
        state = dict(controller_state or {})
        self._records = []
        return {"integral_voltage": float(state.get("integral_voltage", 0.0))}

    def observe(
        self,
        measurement: Mapping[str, float],
        command: Mapping[str, Any],
        timing: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> ActionRequest:
        now = float(timing["sample_time_s"])
        dt = float(timing["control_period_s"])
        vref = float(command.get("vref", 1.0))
        vout = float(measurement["vout"])
        current = float(measurement["inductor_current"])

        # The integral state travels through the runner's ``state`` argument,
        # so the proof does not rely on hidden controller storage.
        integral = float(state.get("integral_voltage", 0.0))
        voltage_error = vref - vout
        integral += self.ki_v * voltage_error * dt
        current_reference = self.kp_v * voltage_error + integral
        duty = self.kp_i * (current_reference - current)
        duty = min(max(duty, 0.0), 1.0)

        state_update = {
            "integral_voltage": integral,
            "current_reference": current_reference,
            "duty": duty,
        }
        self._records.append(
            {
                "measurement": dict(measurement),
                "command": dict(command),
                "state": dict(state),
                "state_update": dict(state_update),
            }
        )
        return ActionRequest(
            value=duty,
            produced_time_s=now,
            target_time_s=now,
            state_update=state_update,
        )

    @property
    def records(self) -> list[dict[str, Any]]:
        return self._records


def _buck_plant(
    factory: type[BuckPlant] = BuckPlant,
    *,
    inductance_h: float = 100e-6,
    capacitance_f: float = 470e-6,
    integration_step_s: float = 1e-6,
    load_resistance_ohm: float = 10.0,
) -> BuckPlant:
    return factory(
        input_voltage_v=12.0,
        inductance_h=inductance_h,
        capacitance_f=capacitance_f,
        load_resistance_ohm=load_resistance_ohm,
        integration_step_s=integration_step_s,
    )


def _spec(
    tmp_path: Any,
    run_id: str,
    *,
    duration_s: float = 4e-5,
    control_period_s: float = 1e-5,
    sample_offset_s: float = 0.0,
    plant_id: str = "buck",
    controller_id: str = "pi",
    input_schedule: tuple[Mapping[str, Any], ...] = (),
) -> ExperimentSpec:
    return ExperimentSpec(
        "stage16-expressible",
        run_id,
        plant_id,
        controller_id,
        Timebase(
            control_period_s=control_period_s,
            duration_s=duration_s,
            sample_offset_s=sample_offset_s,
        ),
        input_schedule=tuple(input_schedule),
        output_dir=str(tmp_path),
        seed=17,
    )


def _observation_at(
    plant: _RecordingBuckPlant,
    time_s: float,
    *,
    tolerance_s: float = 1e-12,
) -> PlantObservation:
    """Return the last recorded observation at ``time_s`` within tolerance."""

    matches = [
        observation
        for observation in plant.observations
        if abs(observation.time_s - time_s) <= tolerance_s
    ]
    assert matches, (
        f"no observation at {time_s!r}; recorded "
        f"{[observation.time_s for observation in plant.observations]}"
    )
    return matches[-1]


def test_a3_load_step_at_window_boundary_records_new_command_and_truth(tmp_path):
    """A3: scheduled load step; command changes at the boundary, truth follows."""

    plant = _buck_plant(
        _RecordingBuckPlant,
        inductance_h=10e-6,
        capacitance_f=1e-6,
        integration_step_s=5e-8,
    )
    controller = _RecordingController(kp=0.1, ki=0.0)
    schedule = (
        {"time_s": 0.0, "load_ohm": 10.0},
        {"time_s": 3e-5, "load_ohm": 5.0},
    )
    result = Runner().run(
        _spec(
            tmp_path,
            "a3-load-step",
            duration_s=6e-5,
            input_schedule=schedule,
        ),
        plant,
        controller,
    )

    assert result.status == "QUALIFIED"
    assert [
        record["command"]["load_ohm"] for record in controller.records
    ] == [10.0, 10.0, 10.0, 5.0, 5.0, 5.0]

    # A schedule change is visible to the controller at the boundary window.
    # The plant still observes the pre-boundary load there because the new
    # input applies to the advance that follows the sample; the next sample
    # observes the new truth.
    assert _observation_at(plant, 3e-5).truth["load_resistance"] == 10.0
    assert _observation_at(plant, 4e-5).truth["load_resistance"] == 5.0

    vout = [record["measurement"]["vout"] for record in controller.records]
    assert vout[4] < vout[3]


def test_a5_soft_start_vref_ramp_reaches_every_planned_reference(tmp_path):
    """A5: four scheduled vref segments are delivered in order."""

    plant = _buck_plant()
    controller = _RecordingController(kp=0.2, ki=0.0)
    ramp = (0.2, 0.4, 0.6, 0.8)
    schedule = tuple(
        {"time_s": index * 1e-5, "vref": value}
        for index, value in enumerate(ramp)
    )
    result = Runner().run(
        _spec(tmp_path, "a5-soft-start", input_schedule=schedule),
        plant,
        controller,
    )

    assert result.status == "QUALIFIED"
    assert [
        record["command"]["vref"] for record in controller.records
    ] == list(ramp)

    duties = [record["request"].value for record in controller.records]
    assert all(
        later >= earlier for earlier, later in zip(duties, duties[1:])
    )
    assert duties[0] > 0.0
    assert duties[-1] > duties[0]


def test_b4_sample_offset_is_exposed_with_window_relative_observation_time(
    tmp_path,
):
    """B4: sample offset, control period and observation time are consistent."""

    control_period_s = 1e-5
    sample_offset_s = 0.4 * control_period_s
    plant = _buck_plant(_RecordingBuckPlant)
    controller = _RecordingController(kp=0.1, ki=0.0)
    spec = _spec(
        tmp_path,
        "b4-sample-offset",
        duration_s=3e-5,
        control_period_s=control_period_s,
        sample_offset_s=sample_offset_s,
    )
    result = Runner().run(spec, plant, controller)

    assert result.status == "QUALIFIED"
    assert len(controller.records) == 3
    tolerance_s = spec.timebase.event_tolerance_s
    for step_index, record in enumerate(controller.records):
        timing = record["timing"]
        assert timing["sample_offset_s"] == sample_offset_s
        assert timing["control_period_s"] == control_period_s
        assert isinstance(timing["age_steps"], int)
        assert not isinstance(timing["age_steps"], bool)

        expected_time_s = step_index * control_period_s + sample_offset_s
        assert abs(timing["sample_time_s"] - expected_time_s) <= tolerance_s
        observed = _observation_at(plant, expected_time_s)
        assert abs(observed.time_s - timing["sample_time_s"]) <= tolerance_s


def test_b5_feedforward_term_sets_duty_when_error_is_zero(tmp_path):
    """B5: feedforward alone sets duty; a non-zero kp with zero error does not."""

    schedule = ({"time_s": 0.0, "vref": 0.4},)
    action_policy = BoundedActionPolicy(0.0, 1.0)

    feedforward_controller = _RecordingController(kp=0.0, ki=0.0, kff_v=0.5)
    feedforward_plant = _PinnedMeasurementPlant(0.4)
    feedforward = Runner().run(
        _spec(
            tmp_path,
            "b5-feedforward",
            plant_id="fake",
            duration_s=2e-5,
            input_schedule=schedule,
        ),
        feedforward_plant,
        feedforward_controller,
        action_policy=action_policy,
    )

    assert feedforward.status == "QUALIFIED"
    first = feedforward_controller.records[0]
    assert first["timing"]["step_index"] == 0
    assert first["command"]["vref"] == pytest.approx(0.4)
    assert first["measurement"]["vout"] == pytest.approx(0.4)
    assert first["command"]["vref"] - first["measurement"]["vout"] == pytest.approx(
        0.0, abs=1e-15
    )
    assert first["request"].value == pytest.approx(0.5 * 0.4)

    feedforward_samples = json.loads(
        (feedforward.run_dir / "samples.json").read_text(encoding="utf-8")
    )
    assert feedforward_samples[0]["action_requested"] == pytest.approx(0.2)
    assert feedforward_samples[0]["action"] == pytest.approx(0.2)

    # Control: kp is non-zero, but the error is still zero, so the
    # proportional term contributes nothing.  The 0.2 above is feedforward.
    control_controller = _RecordingController(kp=2.0, ki=0.0, kff_v=0.0)
    control_plant = _PinnedMeasurementPlant(0.4)
    control = Runner().run(
        _spec(
            tmp_path,
            "b5-zero-error-control",
            plant_id="fake",
            duration_s=2e-5,
            input_schedule=schedule,
        ),
        control_plant,
        control_controller,
        action_policy=action_policy,
    )

    assert control.status == "QUALIFIED"
    assert control_controller.kp == 2.0
    control_first = control_controller.records[0]
    assert (
        control_first["command"]["vref"] - control_first["measurement"]["vout"]
    ) == pytest.approx(0.0, abs=1e-15)
    assert control_first["request"].value == pytest.approx(0.0)

    control_samples = json.loads(
        (control.run_dir / "samples.json").read_text(encoding="utf-8")
    )
    assert control_samples[0]["action"] == pytest.approx(0.0)


def test_b6_cascaded_voltage_and_current_loops_exchange_state_update(tmp_path):
    """B6: outer->inner reference survives in state_update and duty tracks vref."""

    plant = _buck_plant()
    controller = _CascadedController(kp_v=0.5, ki_v=0.0, kp_i=0.2)
    ramp = (0.2, 0.4, 0.6, 0.8)
    schedule = tuple(
        {"time_s": index * 1e-5, "vref": value}
        for index, value in enumerate(ramp)
    )
    result = Runner().run(
        _spec(tmp_path, "b6-cascade", input_schedule=schedule),
        plant,
        controller,
    )

    assert result.status == "QUALIFIED"
    records = controller.records
    assert len(records) == len(ramp)
    assert [
        record["command"]["vref"] for record in records
    ] == list(ramp)

    for record in records:
        assert "current_reference" in record["state_update"]

    # The previous step's state_update is the next step's state argument.
    for index in range(1, len(records)):
        assert records[index]["state"]["current_reference"] == pytest.approx(
            records[index - 1]["state_update"]["current_reference"]
        )
        assert records[index]["state"]["integral_voltage"] == pytest.approx(
            records[index - 1]["state_update"]["integral_voltage"]
        )

    duties = [record["state_update"]["duty"] for record in records]
    assert all(later > earlier for earlier, later in zip(duties, duties[1:]))
    assert duties[-1] > duties[0]
