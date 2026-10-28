"""Deterministic switching-level Buck adapters with model-side control.

This module fills the two ``control_location = model`` routes that the kernel
cannot express with a ``ControllerAdapter``:

- :class:`ModelControlBuckPlant` is a ``stepped`` adapter.  The kernel still
  owns the clock and observes every control window, but the compensator lives
  inside the model; the kernel-provided action is recorded and deliberately
  ignored.  The route is capped at ``limited`` because the kernel cannot shape
  the actuator.
- :class:`WholeRunSwitchingBuckPlant` is a ``whole_run`` engine.  It integrates
  the complete horizon in one call and returns a switching-level trace with
  ripple.  The route is post-hoc validation only, also capped at ``limited``.

Both adapters use the same ideal complementary-switch Buck equations:

``diL/dt = (duty * Vin - vout) / L`` during the high-side interval and
``diL/dt = (-vout) / L`` during the low-side interval, with
``dvout/dt = (iL - vout / R) / C`` throughout.

The model is deliberately small: ideal switches, no dead time, no diode, no
losses, no ESR/ESL, no thermal state, and no device or gate-drive model.  These
are declared assumptions, not hidden corrections.  The controller is a
model-side PI loop on ``vout``; it is not the kernel controller and does not
claim to be a tuned or deployable compensator.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from ..contracts import PlantObservation
from ..identity import (
    implementation_digest,
    implementation_sources,
    register_solve_path,
    review_status,
)
from ..model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
)
from ..safety import BoundedActionPolicy
from ..time_model import (
    TIME_MODEL_STEPPED,
    TIME_MODEL_WHOLE_RUN,
    TraceContract,
    WholeRunRequest,
    WholeRunResult,
)

_TOLERANCE_S = 1e-15
_TIME_EPSILON_S = 1e-12
_MISSING = object()


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class ModelControlBuckParameters:
    """Electrical and control parameters shared by the two adapters."""

    input_voltage_v: float
    inductance_h: float
    capacitance_f: float
    load_resistance_ohm: float
    switching_frequency_hz: float
    control_period_s: float
    integration_step_s: float = 0.5e-6
    kp: float = 0.05
    ki: float = 20.0
    default_vref_v: float = 5.0
    output_min: float = 0.0
    output_max: float = 1.0
    anti_windup: bool = True
    initial_inductor_current_a: float = 0.0
    initial_output_voltage_v: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_voltage_v", _positive("input_voltage_v", self.input_voltage_v))
        object.__setattr__(self, "inductance_h", _positive("inductance_h", self.inductance_h))
        object.__setattr__(self, "capacitance_f", _positive("capacitance_f", self.capacitance_f))
        object.__setattr__(self, "load_resistance_ohm", _positive("load_resistance_ohm", self.load_resistance_ohm))
        object.__setattr__(self, "switching_frequency_hz", _positive("switching_frequency_hz", self.switching_frequency_hz))
        object.__setattr__(self, "control_period_s", _positive("control_period_s", self.control_period_s))
        object.__setattr__(self, "integration_step_s", _positive("integration_step_s", self.integration_step_s))
        object.__setattr__(self, "kp", _finite("kp", self.kp))
        object.__setattr__(self, "ki", _finite("ki", self.ki))
        object.__setattr__(self, "default_vref_v", _non_negative("default_vref_v", self.default_vref_v))
        object.__setattr__(self, "output_min", _finite("output_min", self.output_min))
        object.__setattr__(self, "output_max", _finite("output_max", self.output_max))
        if not self.output_min < self.output_max:
            raise ValueError("output_min must be smaller than output_max")
        if self.output_min < 0.0 or self.output_max > 1.0:
            raise ValueError("duty output limits must lie within [0, 1]")
        if not isinstance(self.anti_windup, bool):
            raise ValueError("anti_windup must be a boolean")
        object.__setattr__(self, "initial_inductor_current_a", _finite("initial_inductor_current_a", self.initial_inductor_current_a))
        object.__setattr__(self, "initial_output_voltage_v", _finite("initial_output_voltage_v", self.initial_output_voltage_v))
        if self.integration_step_s > self.switching_period_s / 20.0:
            raise ValueError(
                "integration_step_s must be at most one twentieth of the switching period"
            )

    @property
    def switching_period_s(self) -> float:
        return 1.0 / self.switching_frequency_hz

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_voltage_v": self.input_voltage_v,
            "inductance_h": self.inductance_h,
            "capacitance_f": self.capacitance_f,
            "load_resistance_ohm": self.load_resistance_ohm,
            "switching_frequency_hz": self.switching_frequency_hz,
            "control_period_s": self.control_period_s,
            "integration_step_s": self.integration_step_s,
            "kp": self.kp,
            "ki": self.ki,
            "default_vref_v": self.default_vref_v,
            "output_min": self.output_min,
            "output_max": self.output_max,
            "anti_windup": self.anti_windup,
            "initial_inductor_current_a": self.initial_inductor_current_a,
            "initial_output_voltage_v": self.initial_output_voltage_v,
        }


class _PiController:
    """Small deterministic PI loop owned by the model, not by the kernel."""

    def __init__(
        self,
        *,
        kp: float,
        ki: float,
        output_min: float,
        output_max: float,
        anti_windup: bool,
        integral: float = 0.0,
    ) -> None:
        self.kp = float(kp)
        self.ki = float(ki)
        self.output_min = float(output_min)
        self.output_max = float(output_max)
        self.anti_windup = bool(anti_windup)
        self.integral = float(integral)
        self.saturation_updates = 0

    def update(self, *, error: float, dt_s: float) -> float:
        candidate = self.integral + self.ki * float(error) * float(dt_s)
        if self.anti_windup:
            candidate = min(max(candidate, self.output_min), self.output_max)
        output = self.kp * float(error) + candidate
        if output > self.output_max:
            output = self.output_max
            self.saturation_updates += 1
            if self.anti_windup:
                candidate = self.output_max
        elif output < self.output_min:
            output = self.output_min
            self.saturation_updates += 1
            if self.anti_windup:
                candidate = self.output_min
        self.integral = candidate
        return output


class _BuckCore:
    """State integration with exact switching-edge splitting and RK4 substeps."""

    def __init__(self, parameters: ModelControlBuckParameters) -> None:
        self.parameters = parameters
        self.switching_period_s = parameters.switching_period_s
        self.time_s = 0.0
        self.inductor_current_a = parameters.initial_inductor_current_a
        self.output_voltage_v = parameters.initial_output_voltage_v

    def reset(self, inductor_current_a: float, output_voltage_v: float) -> None:
        self.time_s = 0.0
        self.inductor_current_a = _finite("inductor_current_a", inductor_current_a)
        self.output_voltage_v = _finite("output_voltage_v", output_voltage_v)

    def _high_side_on(self, time_s: float, duty: float) -> bool:
        if duty <= 0.0:
            return False
        if duty >= 1.0:
            return True
        phase = float(time_s) % self.switching_period_s
        if self.switching_period_s - phase <= _TIME_EPSILON_S:
            phase = 0.0
        return phase < duty * self.switching_period_s - _TIME_EPSILON_S

    def _next_edge(self, time_s: float, duty: float) -> float | None:
        if duty <= 0.0 or duty >= 1.0:
            return None
        phase = float(time_s) % self.switching_period_s
        if self.switching_period_s - phase <= _TIME_EPSILON_S:
            phase = 0.0
        on_width = duty * self.switching_period_s
        if phase < on_width - _TIME_EPSILON_S:
            delta = on_width - phase
        else:
            delta = self.switching_period_s - phase
        if delta <= _TIME_EPSILON_S:
            delta += self.switching_period_s
        return float(time_s) + delta

    def _slopes(
        self,
        *,
        inductor_current_a: float,
        output_voltage_v: float,
        input_voltage_v: float,
        load_resistance_ohm: float,
        high_side_on: bool,
        diode_idle: bool = False,
    ) -> tuple[float, float]:
        if diode_idle:
            return 0.0, -output_voltage_v / (
                load_resistance_ohm * self.parameters.capacitance_f
            )
        applied = input_voltage_v if high_side_on else 0.0
        di = (applied - output_voltage_v) / self.parameters.inductance_h
        dv = (
            inductor_current_a - output_voltage_v / load_resistance_ohm
        ) / self.parameters.capacitance_f
        return di, dv

    def _rk4_step(
        self,
        step_s: float,
        *,
        input_voltage_v: float,
        load_resistance_ohm: float,
        high_side_on: bool,
        diode_idle: bool = False,
    ) -> None:
        i0 = self.inductor_current_a
        v0 = self.output_voltage_v
        k1i, k1v = self._slopes(
            inductor_current_a=i0,
            output_voltage_v=v0,
            input_voltage_v=input_voltage_v,
            load_resistance_ohm=load_resistance_ohm,
            high_side_on=high_side_on,
            diode_idle=diode_idle,
        )
        k2i, k2v = self._slopes(
            inductor_current_a=i0 + 0.5 * step_s * k1i,
            output_voltage_v=v0 + 0.5 * step_s * k1v,
            input_voltage_v=input_voltage_v,
            load_resistance_ohm=load_resistance_ohm,
            high_side_on=high_side_on,
            diode_idle=diode_idle,
        )
        k3i, k3v = self._slopes(
            inductor_current_a=i0 + 0.5 * step_s * k2i,
            output_voltage_v=v0 + 0.5 * step_s * k2v,
            input_voltage_v=input_voltage_v,
            load_resistance_ohm=load_resistance_ohm,
            high_side_on=high_side_on,
            diode_idle=diode_idle,
        )
        k4i, k4v = self._slopes(
            inductor_current_a=i0 + step_s * k3i,
            output_voltage_v=v0 + step_s * k3v,
            input_voltage_v=input_voltage_v,
            load_resistance_ohm=load_resistance_ohm,
            high_side_on=high_side_on,
            diode_idle=diode_idle,
        )
        self.inductor_current_a = i0 + step_s * (k1i + 2.0 * k2i + 2.0 * k3i + k4i) / 6.0
        self.output_voltage_v = v0 + step_s * (k1v + 2.0 * k2v + 2.0 * k3v + k4v) / 6.0

    def advance_to(
        self,
        end_time_s: float,
        *,
        duty: float,
        input_voltage_v: float,
        load_resistance_ohm: float,
        diode_idle: bool = False,
    ) -> None:
        if end_time_s < self.time_s - _TOLERANCE_S:
            raise ValueError("cannot advance the switching core backwards")
        while self.time_s < end_time_s - _TOLERANCE_S:
            edge = None if diode_idle else self._next_edge(self.time_s, duty)
            stop = end_time_s if edge is None else min(end_time_s, edge)
            remaining = stop - self.time_s
            while remaining > _TOLERANCE_S:
                step = min(self.parameters.integration_step_s, remaining)
                high_side_on = False if diode_idle else self._high_side_on(self.time_s, duty)
                self._rk4_step(
                    step,
                    input_voltage_v=input_voltage_v,
                    load_resistance_ohm=load_resistance_ohm,
                    high_side_on=high_side_on,
                    diode_idle=diode_idle,
                )
                self.time_s += step
                remaining -= step
            self.time_s = stop


class _ModelControlBase:
    """Shared declarations and lifecycle for the two model-controlled adapters."""

    topology = "buck"
    model_kind = "model_side_controlled_switching_reference"
    model_level = "L2"

    def __init__(self, parameters: ModelControlBuckParameters) -> None:
        self.parameters = parameters
        self._core = _BuckCore(parameters)
        self._controller = _PiController(
            kp=parameters.kp,
            ki=parameters.ki,
            output_min=parameters.output_min,
            output_max=parameters.output_max,
            anti_windup=parameters.anti_windup,
        )
        self.input_voltage_v = parameters.input_voltage_v
        self.load_resistance_ohm = parameters.load_resistance_ohm
        self.default_vref_v = parameters.default_vref_v
        self.last_duty = 0.0
        self.last_vref = parameters.default_vref_v
        self.last_kernel_action = 0.0
        self._identity_cache: dict[str, Any] | None = None

    # -- shared lifecycle -------------------------------------------------
    def capabilities(self) -> set[str]:
        return {"continuous_time", "deterministic", "engine_owned_control", "external_input", "snapshot"}

    def action_policy(self) -> BoundedActionPolicy:
        return BoundedActionPolicy(0.0, 1.0, name="duty")

    def observe(self) -> PlantObservation:
        self._validate_state()
        return PlantObservation(
            time_s=self._core.time_s,
            measurement={
                "vout": self._core.output_voltage_v,
                "inductor_current": self._core.inductor_current_a,
            },
            truth={
                "vout": self._core.output_voltage_v,
                "inductor_current": self._core.inductor_current_a,
                "duty": self.last_duty,
                "vref": self.last_vref,
                "controller_integral": self._controller.integral,
                "kernel_action_ignored": self.last_kernel_action,
            },
            age_steps=0,
            measurement_units={"vout": "V", "inductor_current": "A"},
        )

    def _validate_state(self) -> None:
        for name, value in (
            ("time_s", self._core.time_s),
            ("inductor_current_a", self._core.inductor_current_a),
            ("output_voltage_v", self._core.output_voltage_v),
            ("duty", self.last_duty),
            ("controller_integral", self._controller.integral),
        ):
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must remain finite")
        if self._core.time_s < 0.0:
            raise ValueError("time_s must remain non-negative")
        if not 0.0 <= self.last_duty <= 1.0:
            raise ValueError("duty must remain in [0, 1]")

    def _apply_external_input(self, external_input: Mapping[str, Any] | None) -> float:
        values = dict(external_input or {})
        if "vin_v" in values or "input_voltage_v" in values:
            raw = values["vin_v"] if "vin_v" in values else values["input_voltage_v"]
            self.input_voltage_v = _positive("input_voltage_v", raw)
        if "load_ohm" in values or "load_resistance_ohm" in values:
            raw = values["load_ohm"] if "load_ohm" in values else values["load_resistance_ohm"]
            self.load_resistance_ohm = _positive("load_resistance_ohm", raw)
        if "vref" in values:
            self.last_vref = _non_negative("vref", values["vref"])
        return self.last_vref

    def analytical_steady_state(self, duty: float) -> dict[str, float]:
        duty_value = float(duty)
        if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
            raise ValueError("duty must be finite and in [0, 1]")
        voltage = duty_value * self.input_voltage_v
        return {
            "output_voltage_v": voltage,
            "inductor_current_a": voltage / self.load_resistance_ohm,
        }

    def parameter_provenance(self) -> ParameterProvenance:
        values = (
            ("input_voltage_v", self.parameters.input_voltage_v, "V"),
            ("inductance_h", self.parameters.inductance_h, "H"),
            ("capacitance_f", self.parameters.capacitance_f, "F"),
            ("load_resistance_ohm", self.parameters.load_resistance_ohm, "ohm"),
            ("switching_frequency_hz", self.parameters.switching_frequency_hz, "Hz"),
            ("control_period_s", self.parameters.control_period_s, "s"),
            ("integration_step_s", self.parameters.integration_step_s, "s"),
            ("kp", self.parameters.kp, "1"),
            ("ki", self.parameters.ki, "1/s"),
            ("default_vref_v", self.parameters.default_vref_v, "V"),
            ("output_min", self.parameters.output_min, "1"),
            ("output_max", self.parameters.output_max, "1"),
        )
        records = tuple(
            ParameterRecord(
                name=name,
                value=value,
                unit=unit,
                source="assumption",
                reference="model_control_buck.py constructor/default",
                review_status="unreviewed",
                note="reference-model value; not a measured or calibrated parameter",
            )
            for name, value, unit in values
        )
        return ParameterProvenance(records=records, status="declared")

    def validity_envelope(self) -> ModelClaim:
        period = self.parameters.switching_period_s
        return ModelClaim(
            claim_level="L2-switching-reference",
            topology=self.topology,
            model_form="switching_state_space",
            assumptions=(
                "ideal complementary high-side and low-side switches with no dead time",
                "no freewheeling diode; the low-side switch carries reverse current",
                "linear inductor and capacitor with no ESR, ESL, or saturation",
                "no switch capacitance, reverse recovery, thermal state, or device self-heating",
                "the compensator is a model-side PI loop; the kernel-provided Controller is not driven",
                "duty is held over each declared control interval; switching edges split the integration",
            ),
            operating_ranges=(
                OperatingRange("duty", unit="1", minimum=0.0, maximum=1.0, basis="equation_domain"),
                OperatingRange("input_voltage_v", unit="V", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange("load_resistance_ohm", unit="ohm", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange("switching_frequency_hz", unit="Hz", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
            ),
            numerical_limits=(
                NumericalLimit(
                    "integration_step_s.max",
                    period / 20.0,
                    "s",
                    strength="advisory",
                    basis="resolution",
                    note="one twentieth of the switching period",
                ),
            ),
            limitations=(
                "model-side PI controller: the kernel cannot shape the actuator",
                "ideal complementary switches with no dead time, body diode, or reverse recovery",
                "no ESR/ESL, magnetic saturation, switch capacitance, or thermal model",
                "duty updates are applied at control instants, not reconstructed as a continuous analog compensator",
            ),
            uncheckable_rules=(
                {"rule_id": "envelope.actuator.kernel_projection", "reason": "actuator_control_in_model"},
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "input_voltage_v": self.parameters.input_voltage_v,
                "inductance_h": self.parameters.inductance_h,
                "capacitance_f": self.parameters.capacitance_f,
                "load_resistance_ohm": self.parameters.load_resistance_ohm,
                "switching_frequency_hz": self.parameters.switching_frequency_hz,
                "control_period_s": self.parameters.control_period_s,
                "integration_step_s": self.parameters.integration_step_s,
                "switching_period_s": period,
            },
        )

    def backend_provenance(self) -> dict[str, Any]:
        return {
            "status": "known",
            "name": "pe_sim.model_control_switching_buck",
            "version": "model-control-v1",
            "solver_settings": {
                "integrator": "rk4",
                "max_internal_step_s": self.parameters.integration_step_s,
                "switching_edge_splitting": "exact",
                "control": "model_side_pi",
                "floating_point": "IEEE-754-binary64",
            },
            "limitations": [
                "project-owned deterministic switching model; not an external circuit simulator",
                "the kernel-provided Controller is not driven on this route",
                "the model-side controller is a reference PI loop, not a tuned or deployable compensator",
            ],
        }

    def manifest_identity(self) -> dict[str, Any]:
        if self._identity_cache is None:
            implementation = type(self)
            identity: dict[str, Any] = {
                "level": self.model_level,
                "kind": self.model_kind,
                "topology": self.topology,
                "backend_id": self.backend_id,
                "time_model": self.time_model,
                "parameters": self.parameters.to_dict(),
                "controller": {
                    "kind": "model_side_pi",
                    "kp": self.parameters.kp,
                    "ki": self.parameters.ki,
                    "output_min": self.parameters.output_min,
                    "output_max": self.parameters.output_max,
                    "anti_windup": self.parameters.anti_windup,
                },
                "capabilities": sorted(self.capabilities()),
                "implementation": {
                    "module": implementation.__module__,
                    "class": implementation.__qualname__,
                    "solve_path_modules": sorted(implementation_sources(self.backend_id)),
                    "sha256": implementation_digest(self.backend_id),
                    "review": review_status(self.backend_id),
                },
            }
            identity["sha256"] = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            self._identity_cache = identity
        return dict(self._identity_cache)


class ModelControlBuckPlant(_ModelControlBase):
    """Stepped switching Buck whose PI loop closes inside the model.

    The kernel still calls ``advance`` once per (possibly quantized) segment and
    still drives its own controller, but the action is recorded and ignored.
    This is the ``stepped + model`` route: observation is per window, actuator
    control is not.
    """

    time_model = TIME_MODEL_STEPPED
    backend_id = "model-control-buck"

    def __init__(self, parameters: ModelControlBuckParameters) -> None:
        super().__init__(parameters)
        self.control_period_s = parameters.control_period_s
        self.next_control_time_s = 0.0
        self.last_control_time_s: float | None = None

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        allowed = {
            "mode",
            "snapshot_path",
            "snapshot_hash",
            "inductor_current_a",
            "current_a",
            "output_voltage_v",
            "vout",
            "controller_integral",
        }
        unknown = set(state) - allowed
        if unknown:
            raise ValueError(f"unsupported initial_state fields: {sorted(unknown)}")
        if state.get("snapshot_path") is not None or state.get("snapshot_hash") is not None:
            raise ValueError("model-control-buck does not read snapshot files; provide explicit state values")
        inductor_current = state.get(
            "inductor_current_a",
            state.get("current_a", self.parameters.initial_inductor_current_a),
        )
        output_voltage = state.get(
            "output_voltage_v",
            state.get("vout", self.parameters.initial_output_voltage_v),
        )
        if "inductor_current_a" in state and "current_a" in state:
            if float(state["inductor_current_a"]) != float(state["current_a"]):
                raise ValueError("initial_state.inductor_current_a and current_a disagree")
        if "output_voltage_v" in state and "vout" in state:
            if float(state["output_voltage_v"]) != float(state["vout"]):
                raise ValueError("initial_state.output_voltage_v and vout disagree")
        self._core.reset(float(inductor_current), float(output_voltage))
        self._controller = _PiController(
            kp=self.parameters.kp,
            ki=self.parameters.ki,
            output_min=self.parameters.output_min,
            output_max=self.parameters.output_max,
            anti_windup=self.parameters.anti_windup,
            integral=float(state.get("controller_integral", 0.0)),
        )
        self.last_duty = 0.0
        self.last_vref = self.parameters.default_vref_v
        self.last_kernel_action = 0.0
        self.next_control_time_s = 0.0
        self.last_control_time_s = None
        self._validate_state()
        return self.snapshot()

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ) -> PlantObservation:
        duration_s = float(duration)
        if not math.isfinite(duration_s) or duration_s <= 0.0:
            raise ValueError("duration must be finite and positive")
        self.last_kernel_action = _finite("action", action)
        vref = self._apply_external_input(external_input)
        remaining = duration_s
        while remaining > _TIME_EPSILON_S:
            if self._core.time_s >= self.next_control_time_s - _TIME_EPSILON_S:
                dt_s = (
                    0.0
                    if self.last_control_time_s is None
                    else self._core.time_s - self.last_control_time_s
                )
                self.last_duty = self._controller.update(
                    error=vref - self._core.output_voltage_v,
                    dt_s=dt_s,
                )
                self.last_vref = vref
                self.last_control_time_s = self._core.time_s
                self.next_control_time_s = self._core.time_s + self.control_period_s
            stop = min(self._core.time_s + remaining, self.next_control_time_s)
            if stop <= self._core.time_s:
                self.next_control_time_s = self._core.time_s + self.control_period_s
                continue
            start = self._core.time_s
            self._core.advance_to(
                stop,
                duty=self.last_duty,
                input_voltage_v=self.input_voltage_v,
                load_resistance_ohm=self.load_resistance_ohm,
            )
            remaining -= stop - start
        self._validate_state()
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            "topology": self.topology,
            "model_kind": self.model_kind,
            "time_s": self._core.time_s,
            "inductor_current_a": self._core.inductor_current_a,
            "output_voltage_v": self._core.output_voltage_v,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "last_duty": self.last_duty,
            "last_vref": self.last_vref,
            "controller_integral": self._controller.integral,
            "last_control_time_s": self.last_control_time_s,
            "next_control_time_s": self.next_control_time_s,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        if str(snapshot.get("model_kind", self.model_kind)) != self.model_kind:
            raise ValueError("snapshot model kind does not match plant")
        self._core.time_s = _non_negative("time_s", snapshot["time_s"])
        self._core.inductor_current_a = _finite("inductor_current_a", snapshot["inductor_current_a"])
        self._core.output_voltage_v = _finite("output_voltage_v", snapshot["output_voltage_v"])
        self.input_voltage_v = _positive("input_voltage_v", snapshot.get("input_voltage_v", self.input_voltage_v))
        self.load_resistance_ohm = _positive("load_resistance_ohm", snapshot.get("load_resistance_ohm", self.load_resistance_ohm))
        self.last_duty = float(snapshot.get("last_duty", 0.0))
        self.last_vref = float(snapshot.get("last_vref", self.parameters.default_vref_v))
        self._controller.integral = float(snapshot.get("controller_integral", 0.0))
        last_control = snapshot.get("last_control_time_s")
        self.last_control_time_s = None if last_control is None else float(last_control)
        self.next_control_time_s = float(snapshot.get("next_control_time_s", self._core.time_s))
        self._validate_state()
        return self.snapshot()


class WholeRunSwitchingBuckPlant(_ModelControlBase):
    """Whole-run switching Buck with a model-side PI loop.

    The engine owns the complete horizon: it integrates switching edges and
    PI updates internally, then returns one trace.  The kernel can only check
    the declared cadence after the fact; this is the ``whole_run + model``
    route and is priced at ``limited`` evidence.
    """

    time_model = TIME_MODEL_WHOLE_RUN
    backend_id = "whole-run-switching-buck"

    def __init__(self, parameters: ModelControlBuckParameters, *, cadence_s: float) -> None:
        super().__init__(parameters)
        self.cadence_s = _positive("cadence_s", cadence_s)

    def capabilities(self) -> set[str]:
        return {
            "continuous_time",
            "deterministic",
            "engine_owned_control",
            "external_input",
            "whole_run_trace",
        }

    def trace_contract(self) -> TraceContract:
        return TraceContract(cadence_s=self.cadence_s)

    def run_whole(self, request: WholeRunRequest) -> WholeRunResult:
        if not isinstance(request, WholeRunRequest):
            raise ValueError("request must be a WholeRunRequest")
        duration_s = float(request.duration_s)
        control_period_s = self._control_period_from(request.metadata)
        schedule = self._parse_schedule(request.metadata)
        inductor_current, output_voltage, integral = self._parse_initial_state(request.initial_state)
        self._core.reset(inductor_current, output_voltage)
        self._controller = _PiController(
            kp=self.parameters.kp,
            ki=self.parameters.ki,
            output_min=self.parameters.output_min,
            output_max=self.parameters.output_max,
            anti_windup=self.parameters.anti_windup,
            integral=integral,
        )
        sample_times = self._sample_times(duration_s, self.cadence_s)
        control_times = self._control_times(control_period_s, duration_s, schedule, sample_times)
        event_times = sorted(set(sample_times) | set(control_times))
        sample_index = 0
        previous_time = 0.0
        duty = 0.0
        vref = self.parameters.default_vref_v
        vin = self.parameters.input_voltage_v
        load = self.parameters.load_resistance_ohm
        samples_vout: list[float] = []
        samples_current: list[float] = []
        samples_duty: list[float] = []
        samples_vref: list[float] = []
        samples_switch: list[float] = []
        control_updates = 0
        last_control_time_s = 0.0
        duty_min = math.inf
        duty_max = -math.inf

        for event_time in event_times:
            if event_time > previous_time + _TOLERANCE_S:
                self._core.advance_to(
                    event_time,
                    duty=duty,
                    input_voltage_v=vin,
                    load_resistance_ohm=load,
                )
            vin, load, vref = self._schedule_at(schedule, event_time, vin, load, vref)
            is_control = self._is_control_time(event_time, control_times)
            if is_control:
                dt_s = 0.0 if control_updates == 0 else event_time - last_control_time_s
                duty = self._controller.update(
                    error=vref - self._core.output_voltage_v,
                    dt_s=dt_s,
                )
                control_updates += 1
                last_control_time_s = event_time
                duty_min = min(duty_min, duty)
                duty_max = max(duty_max, duty)
            if (
                sample_index < len(sample_times)
                and abs(event_time - sample_times[sample_index]) <= _TIME_EPSILON_S
            ):
                samples_vout.append(self._core.output_voltage_v)
                samples_current.append(self._core.inductor_current_a)
                samples_duty.append(duty)
                samples_vref.append(vref)
                samples_switch.append(1.0 if self._core._high_side_on(event_time, duty) else 0.0)
                sample_index += 1
            previous_time = event_time

        if sample_index != len(sample_times):
            raise RuntimeError(
                f"internal error: recorded {sample_index} of {len(sample_times)} samples"
            )

        self.last_duty = duty
        self.last_vref = vref
        result = WholeRunResult(
            time_s=tuple(sample_times),
            signals={
                "vout": tuple(samples_vout),
                "inductor_current_a": tuple(samples_current),
                "duty": tuple(samples_duty),
                "vref": tuple(samples_vref),
                "switch_node_voltage": tuple(
                    1.0 if value > 0.5 else 0.0 for value in samples_switch
                ),
            },
            final_state={
                "time_s": duration_s,
                "inductor_current_a": self._core.inductor_current_a,
                "output_voltage_v": self._core.output_voltage_v,
                "controller_integral": self._controller.integral,
                "duty": duty,
                "vref": vref,
            },
            metadata={
                "backend_id": self.backend_id,
                "time_model": self.time_model,
                "trace_contract": self.trace_contract().to_dict(),
                "control_law": "model_side_pi",
                "control_location": "model",
                "enforcement": "post_hoc_only",
                "control_updates": control_updates,
                "switching_frequency_hz": self.parameters.switching_frequency_hz,
                "switching_period_s": self.parameters.switching_period_s,
                "integration_step_s": self.parameters.integration_step_s,
                "duty": {
                    "minimum": duty_min if control_updates else None,
                    "maximum": duty_max if control_updates else None,
                    "saturated": bool(self._controller.saturation_updates),
                },
                "seed": request.seed,
            },
        )
        return result

    # -- request parsing --------------------------------------------------
    def _control_period_from(self, metadata: Mapping[str, Any]) -> float:
        raw = metadata.get("control_period_s", self.parameters.control_period_s)
        return _positive("control_period_s", raw)

    def _parse_schedule(self, metadata: Mapping[str, Any]) -> tuple[dict[str, float], ...]:
        if not isinstance(metadata, Mapping):
            raise ValueError("request.metadata must be a mapping")
        raw = metadata.get("input_schedule", _MISSING)
        if raw is _MISSING:
            return ()
        if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
            raise ValueError("metadata['input_schedule'] must be a list of objects")
        points: list[dict[str, float]] = []
        previous = -math.inf
        allowed = {
            "time_s",
            "vref",
            "vin_v",
            "input_voltage_v",
            "load_ohm",
            "load_resistance_ohm",
        }
        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                raise ValueError(f"input_schedule[{index}] must be an object")
            unknown = set(entry) - allowed
            if unknown:
                raise ValueError(f"input_schedule[{index}] has unsupported keys: {sorted(unknown)}")
            if "time_s" not in entry:
                raise ValueError(f"input_schedule[{index}] must declare time_s")
            time_s = _finite(f"input_schedule[{index}].time_s", entry["time_s"])
            if time_s < 0.0 or time_s < previous:
                raise ValueError("input_schedule must be non-decreasing and non-negative")
            point: dict[str, float] = {"time_s": time_s}
            if "vref" in entry:
                point["vref"] = _non_negative(f"input_schedule[{index}].vref", entry["vref"])
            if "vin_v" in entry or "input_voltage_v" in entry:
                raw_vin = entry["vin_v"] if "vin_v" in entry else entry["input_voltage_v"]
                point["input_voltage_v"] = _positive(
                    f"input_schedule[{index}].input_voltage_v", raw_vin
                )
            if "load_ohm" in entry or "load_resistance_ohm" in entry:
                raw_load = entry["load_ohm"] if "load_ohm" in entry else entry["load_resistance_ohm"]
                point["load_resistance_ohm"] = _positive(
                    f"input_schedule[{index}].load_resistance_ohm", raw_load
                )
            points.append(point)
            previous = time_s
        return tuple(points)

    def _parse_initial_state(self, initial_state: Mapping[str, Any]) -> tuple[float, float, float]:
        if not isinstance(initial_state, Mapping):
            raise ValueError("request.initial_state must be a mapping")
        allowed = {
            "mode",
            "snapshot_path",
            "snapshot_hash",
            "inductor_current_a",
            "current_a",
            "output_voltage_v",
            "vout",
            "controller_integral",
            "time_s",
        }
        unknown = set(initial_state) - allowed
        if unknown:
            raise ValueError(f"unsupported initial_state fields: {sorted(unknown)}")
        mode = initial_state.get("mode", "cold_start")
        if mode not in {"cold_start", "warm_start"}:
            raise ValueError(f"unsupported initial_state mode for a whole-run engine: {mode!r}")
        if initial_state.get("snapshot_path") is not None or initial_state.get("snapshot_hash") is not None:
            raise ValueError(
                "a whole-run engine cannot read a snapshot file; provide explicit state values instead"
            )
        if "time_s" in initial_state and float(initial_state["time_s"]) != 0.0:
            raise ValueError("initial_state.time_s must be 0.0 on the whole-run route")
        inductor_current = self._aliased(
            initial_state,
            "inductor_current_a",
            "current_a",
            self.parameters.initial_inductor_current_a,
        )
        output_voltage = self._aliased(
            initial_state,
            "output_voltage_v",
            "vout",
            self.parameters.initial_output_voltage_v,
        )
        integral = _finite(
            "initial_state.controller_integral",
            initial_state.get("controller_integral", 0.0),
        )
        return inductor_current, output_voltage, integral

    @staticmethod
    def _aliased(
        state: Mapping[str, Any], primary: str, alias: str, default: float
    ) -> float:
        if primary in state and alias in state:
            first = _finite(primary, state[primary])
            second = _finite(alias, state[alias])
            if first != second:
                raise ValueError(f"initial_state.{primary} and {alias} disagree")
            return first
        if primary in state:
            return _finite(primary, state[primary])
        if alias in state:
            return _finite(alias, state[alias])
        return default

    @staticmethod
    def _sample_times(duration_s: float, cadence_s: float) -> tuple[float, ...]:
        times: list[float] = [0.0]
        index = 1
        while True:
            candidate = index * cadence_s
            if candidate >= duration_s - _TOLERANCE_S:
                break
            if candidate <= times[-1]:
                raise ValueError("cadence_s is too small to produce strictly increasing sample times")
            times.append(candidate)
            index += 1
        if times[-1] != duration_s:
            times.append(duration_s)
        return tuple(times)

    @staticmethod
    def _control_times(
        control_period_s: float,
        duration_s: float,
        schedule: Sequence[Mapping[str, float]],
        sample_times: Sequence[float],
    ) -> tuple[float, ...]:
        control: list[float] = [0.0]
        index = 1
        while True:
            candidate = index * control_period_s
            if candidate >= duration_s - _TOLERANCE_S:
                break
            control.append(candidate)
            index += 1
        for point in schedule:
            time_s = float(point["time_s"])
            if 0.0 < time_s < duration_s - _TOLERANCE_S:
                control.append(time_s)
        # Samples are not control instants; the union is built by the caller.
        del sample_times
        control.sort()
        unique: list[float] = []
        for value in control:
            if not unique or value > unique[-1] + _TIME_EPSILON_S:
                unique.append(value)
        return tuple(unique)

    @staticmethod
    def _is_control_time(event_time: float, control_times: Sequence[float]) -> bool:
        return any(abs(event_time - point) <= _TIME_EPSILON_S for point in control_times)

    @staticmethod
    def _schedule_at(
        schedule: Sequence[Mapping[str, float]],
        time_s: float,
        vin: float,
        load: float,
        vref: float,
    ) -> tuple[float, float, float]:
        values = (vin, load, vref)
        for point in schedule:
            if float(point["time_s"]) <= time_s + _TIME_EPSILON_S:
                values = (
                    float(point.get("input_voltage_v", values[0])),
                    float(point.get("load_resistance_ohm", values[1])),
                    float(point.get("vref", values[2])),
                )
            else:
                break
        return values


@dataclass(frozen=True)
class PeakCurrentBuckParameters(ModelControlBuckParameters):
    """Peak-current-mode parameters: the base fields plus the comparator domain.

    ``kp`` and ``ki`` are the outer voltage-loop gains that produce a current
    reference in amperes.  The inner comparator operates on the instantaneous
    inductor current once per switching cycle.
    """

    current_reference_max_a: float = 5.0
    diode_emulation: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "current_reference_max_a",
            _positive("current_reference_max_a", self.current_reference_max_a),
        )
        if not isinstance(self.diode_emulation, bool):
            raise ValueError("diode_emulation must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "current_reference_max_a": self.current_reference_max_a,
            "diode_emulation": self.diode_emulation,
        }


class PeakCurrentBuckPlant(_ModelControlBase):
    """Stepped Buck with a fixed-frequency peak-current comparator in the model.

    The outer voltage loop updates a current reference at the declared control
    instants.  At the start of each switching cycle the model computes the
    high-side on-time needed to reach that reference from the present inductor
    current, records the comparator instant, and holds the resulting duty for
    the cycle.  The comparator is therefore inside the model: the kernel action
    and the kernel controller are not used to shape the actuator.

    This is intentionally a small reference model, not a device-level current
    loop.  It has no blanking time, propagation delay, slope compensation, or
    current-sense filter; the resulting limitation is declared rather than
    hidden.
    """

    time_model = TIME_MODEL_STEPPED
    backend_id = "peak-current-buck"
    model_kind = "model_side_peak_current_reference"

    def __init__(self, parameters: PeakCurrentBuckParameters) -> None:
        super().__init__(parameters)
        self.parameters: PeakCurrentBuckParameters = parameters
        self.control_period_s = parameters.control_period_s
        self.current_reference_max_a = parameters.current_reference_max_a
        self.diode_emulation = parameters.diode_emulation
        self.current_reference_a = 0.0
        self.comparator_event_s: float | None = None
        self.comparator_events: list[float] = []
        self.cycle_index = 0
        self.cycle_duty = 0.0
        self.next_cycle_start_s = 0.0
        self.cycle_start_s = 0.0
        self.cycle_end_s = 0.0
        self.hs_end_s = 0.0
        self.zero_crossing_s: float | None = None
        self.diode_zero_crossing_s: float | None = None
        self.phase = "idle"
        self.next_control_time_s = 0.0
        self.last_control_time_s: float | None = None
        self._reset_current_controller(0.0)

    def _reset_current_controller(self, integral: float) -> None:
        self._controller = _PiController(
            kp=self.parameters.kp,
            ki=self.parameters.ki,
            output_min=0.0,
            output_max=self.current_reference_max_a,
            anti_windup=self.parameters.anti_windup,
            integral=integral,
        )

    def capabilities(self) -> set[str]:
        return {
            "continuous_time",
            "deterministic",
            "engine_owned_control",
            "external_input",
            "snapshot",
            "cycle_by_cycle_comparator",
        }

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        allowed = {
            "mode",
            "snapshot_path",
            "snapshot_hash",
            "inductor_current_a",
            "current_a",
            "output_voltage_v",
            "vout",
            "controller_integral",
        }
        unknown = set(state) - allowed
        if unknown:
            raise ValueError(f"unsupported initial_state fields: {sorted(unknown)}")
        if state.get("snapshot_path") is not None or state.get("snapshot_hash") is not None:
            raise ValueError("peak-current-buck does not read snapshot files; provide explicit state values")
        inductor_current = state.get(
            "inductor_current_a",
            state.get("current_a", self.parameters.initial_inductor_current_a),
        )
        output_voltage = state.get(
            "output_voltage_v",
            state.get("vout", self.parameters.initial_output_voltage_v),
        )
        self._core.reset(float(inductor_current), float(output_voltage))
        self._reset_current_controller(float(state.get("controller_integral", 0.0)))
        self.current_reference_a = 0.0
        self.comparator_event_s = None
        self.comparator_events = []
        self.cycle_index = 0
        self.cycle_duty = 0.0
        self.next_cycle_start_s = 0.0
        self.cycle_start_s = 0.0
        self.cycle_end_s = 0.0
        self.hs_end_s = 0.0
        self.zero_crossing_s = None
        self.diode_zero_crossing_s = None
        self.phase = "idle"
        self.next_control_time_s = 0.0
        self.last_control_time_s = None
        self.last_duty = 0.0
        self.last_vref = self.parameters.default_vref_v
        self.last_kernel_action = 0.0
        self._validate_state()
        return self.snapshot()

    def _duty_for_peak(self, reference_a: float) -> float:
        if reference_a <= self._core.inductor_current_a + 1e-12:
            return 0.0
        denominator = self.input_voltage_v - self._core.output_voltage_v
        if denominator <= 0.0:
            return 0.0
        on_time_s = (
            (reference_a - self._core.inductor_current_a)
            * self.parameters.inductance_h
            / denominator
        )
        return min(max(on_time_s / self.parameters.switching_period_s, 0.0), 1.0)

    def _begin_cycle(self) -> None:
        self.cycle_start_s = self._core.time_s
        self.cycle_end_s = self.cycle_start_s + self.parameters.switching_period_s
        self.next_cycle_start_s = self.cycle_end_s
        self.cycle_duty = self._duty_for_peak(self.current_reference_a)
        self.hs_end_s = self.cycle_start_s + self.cycle_duty * self.parameters.switching_period_s
        self.zero_crossing_s = None
        self.diode_zero_crossing_s = None
        self.cycle_index += 1
        self.comparator_event_s = (
            self.hs_end_s
            if 0.0 < self.cycle_duty < 1.0
            else None
        )
        if self.comparator_event_s is not None:
            self.comparator_events.append(self.comparator_event_s)
        if self.cycle_duty <= 0.0:
            self.phase = "ls"
            self._begin_low_side()
        else:
            self.phase = "hs"

    def _begin_low_side(self) -> None:
        self.phase = "ls"
        if not self.diode_emulation:
            return
        if self._core.inductor_current_a <= 0.0:
            self._reach_zero_current(self._core.time_s)
            return
        if self._core.output_voltage_v <= 1e-9:
            return
        zero_delta = (
            self._core.inductor_current_a
            * self.parameters.inductance_h
            / self._core.output_voltage_v
        )
        candidate = self._core.time_s + zero_delta
        if candidate < self.cycle_end_s - _TIME_EPSILON_S:
            self.zero_crossing_s = candidate

    def _reach_zero_current(self, time_s: float) -> None:
        self._core.inductor_current_a = 0.0
        self.phase = "idle"
        self.diode_zero_crossing_s = float(time_s)

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ) -> PlantObservation:
        duration_s = float(duration)
        if not math.isfinite(duration_s) or duration_s <= 0.0:
            raise ValueError("duration must be finite and positive")
        self.last_kernel_action = _finite("action", action)
        vref = self._apply_external_input(external_input)
        remaining = duration_s
        while remaining > _TIME_EPSILON_S:
            if self._core.time_s >= self.next_control_time_s - _TIME_EPSILON_S:
                dt_s = (
                    0.0
                    if self.last_control_time_s is None
                    else self._core.time_s - self.last_control_time_s
                )
                self.current_reference_a = self._controller.update(
                    error=vref - self._core.output_voltage_v,
                    dt_s=dt_s,
                )
                self.last_vref = vref
                self.last_control_time_s = self._core.time_s
                self.next_control_time_s = self._core.time_s + self.control_period_s
            if self._core.time_s >= self.next_cycle_start_s - _TIME_EPSILON_S:
                self._begin_cycle()
            if self.phase == "hs" and self._core.time_s >= self.hs_end_s - _TIME_EPSILON_S:
                self._begin_low_side()
            if (
                self.phase == "ls"
                and self.zero_crossing_s is not None
                and self._core.time_s >= self.zero_crossing_s - _TIME_EPSILON_S
            ):
                self._reach_zero_current(self._core.time_s)
            stop = min(
                self._core.time_s + remaining,
                self.next_control_time_s,
                self.next_cycle_start_s,
            )
            if self.phase == "hs":
                stop = min(stop, self.hs_end_s)
            elif self.phase == "ls" and self.zero_crossing_s is not None:
                stop = min(stop, self.zero_crossing_s)
            if stop <= self._core.time_s:
                self.next_cycle_start_s = self._core.time_s + self.parameters.switching_period_s
                continue
            start = self._core.time_s
            self._core.advance_to(
                stop,
                duty=1.0 if self.phase == "hs" else 0.0,
                input_voltage_v=self.input_voltage_v,
                load_resistance_ohm=self.load_resistance_ohm,
                diode_idle=self.phase == "idle",
            )
            self.last_duty = self.cycle_duty
            remaining -= stop - start
        self._validate_state()
        return self.observe()

    def observe(self) -> PlantObservation:
        base = super().observe()
        truth = dict(base.truth)
        truth.update(
            {
                "current_reference_a": self.current_reference_a,
                "cycle_index": float(self.cycle_index),
                "comparator_event_s": (
                    -1.0 if self.comparator_event_s is None else self.comparator_event_s
                ),
                "diode_zero_crossing_s": (
                    -1.0
                    if self.diode_zero_crossing_s is None
                    else self.diode_zero_crossing_s
                ),
                "duty": self.cycle_duty,
            }
        )
        return PlantObservation(
            time_s=base.time_s,
            measurement=dict(base.measurement),
            truth=truth,
            age_steps=base.age_steps,
            measurement_units=dict(base.measurement_units),
        )

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            "topology": self.topology,
            "model_kind": self.model_kind,
            "time_s": self._core.time_s,
            "inductor_current_a": self._core.inductor_current_a,
            "output_voltage_v": self._core.output_voltage_v,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "last_duty": self.last_duty,
            "last_vref": self.last_vref,
            "controller_integral": self._controller.integral,
            "current_reference_a": self.current_reference_a,
            "cycle_duty": self.cycle_duty,
            "cycle_index": self.cycle_index,
            "next_cycle_start_s": self.next_cycle_start_s,
            "cycle_start_s": self.cycle_start_s,
            "cycle_end_s": self.cycle_end_s,
            "hs_end_s": self.hs_end_s,
            "zero_crossing_s": self.zero_crossing_s,
            "diode_zero_crossing_s": self.diode_zero_crossing_s,
            "phase": self.phase,
            "next_control_time_s": self.next_control_time_s,
            "last_control_time_s": self.last_control_time_s,
            "comparator_event_s": self.comparator_event_s,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        if str(snapshot.get("model_kind", self.model_kind)) != self.model_kind:
            raise ValueError("snapshot model kind does not match plant")
        self._core.time_s = _non_negative("time_s", snapshot["time_s"])
        self._core.inductor_current_a = _finite("inductor_current_a", snapshot["inductor_current_a"])
        self._core.output_voltage_v = _finite("output_voltage_v", snapshot["output_voltage_v"])
        self.input_voltage_v = _positive("input_voltage_v", snapshot.get("input_voltage_v", self.input_voltage_v))
        self.load_resistance_ohm = _positive("load_resistance_ohm", snapshot.get("load_resistance_ohm", self.load_resistance_ohm))
        self.last_duty = float(snapshot.get("last_duty", 0.0))
        self.last_vref = float(snapshot.get("last_vref", self.parameters.default_vref_v))
        self._reset_current_controller(float(snapshot.get("controller_integral", 0.0)))
        self.current_reference_a = float(snapshot.get("current_reference_a", 0.0))
        self.cycle_duty = float(snapshot.get("cycle_duty", 0.0))
        self.cycle_index = int(snapshot.get("cycle_index", 0))
        self.next_cycle_start_s = float(snapshot.get("next_cycle_start_s", self._core.time_s))
        self.cycle_start_s = float(snapshot.get("cycle_start_s", self._core.time_s))
        self.cycle_end_s = float(snapshot.get("cycle_end_s", self._core.time_s))
        self.hs_end_s = float(snapshot.get("hs_end_s", self._core.time_s))
        zero = snapshot.get("zero_crossing_s")
        self.zero_crossing_s = None if zero is None else float(zero)
        diode = snapshot.get("diode_zero_crossing_s")
        self.diode_zero_crossing_s = None if diode is None else float(diode)
        self.phase = str(snapshot.get("phase", "idle"))
        self.next_control_time_s = float(snapshot.get("next_control_time_s", self._core.time_s))
        last_control = snapshot.get("last_control_time_s")
        self.last_control_time_s = None if last_control is None else float(last_control)
        event = snapshot.get("comparator_event_s")
        self.comparator_event_s = None if event is None else float(event)
        self._validate_state()
        return self.snapshot()

    def validity_envelope(self) -> ModelClaim:
        period = self.parameters.switching_period_s
        return ModelClaim(
            claim_level="L2-switching-reference",
            topology=self.topology,
            model_form="switching_state_space_peak_current",
            assumptions=(
                "ideal complementary high-side and low-side switches with no dead time",
                "fixed-frequency peak-current comparator inside the model",
                "outer voltage PI produces the current reference in amperes",
                "the high-side on-time is calculated from the linear inductor slope at the cycle start",
                "no blanking time, propagation delay, slope compensation, or current-sense filter",
                "the kernel-provided Controller and action are not driven on this route",
            ),
            operating_ranges=(
                OperatingRange("duty", unit="1", minimum=0.0, maximum=1.0, basis="equation_domain"),
                OperatingRange("input_voltage_v", unit="V", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange("load_resistance_ohm", unit="ohm", minimum=0.0, minimum_exclusive=True, basis="equation_domain"),
                OperatingRange("current_reference_max_a", unit="A", minimum=0.0, minimum_exclusive=True, basis="constructor_domain"),
            ),
            numerical_limits=(
                NumericalLimit(
                    "integration_step_s.max",
                    period / 20.0,
                    "s",
                    strength="advisory",
                    basis="resolution",
                    note="one twentieth of the switching period",
                ),
            ),
            limitations=(
                "comparator and current loop are inside the model; the kernel cannot shape the actuator",
                "duty=1 cycles have no comparator event because the high-side switch is never turned off",
                "on-time uses the cycle-start linear slope; output-voltage movement during the on-time is not iterated",
                "no blanking time, propagation delay, slope compensation, or current-sense filter",
                "ideal switches with no dead time, body diode, ESR/ESL, loss, or thermal model",
            ),
            uncheckable_rules=(
                {"rule_id": "envelope.actuator.kernel_projection", "reason": "actuator_control_in_model"},
                {"rule_id": "envelope.comparator.cycle_by_cycle", "reason": "comparator_event_in_model"},
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "input_voltage_v": self.parameters.input_voltage_v,
                "inductance_h": self.parameters.inductance_h,
                "capacitance_f": self.parameters.capacitance_f,
                "load_resistance_ohm": self.parameters.load_resistance_ohm,
                "switching_frequency_hz": self.parameters.switching_frequency_hz,
                "control_period_s": self.parameters.control_period_s,
                "integration_step_s": self.parameters.integration_step_s,
                "switching_period_s": period,
                "current_reference_max_a": self.current_reference_max_a,
                "diode_emulation": self.diode_emulation,
            },
        )

    def backend_provenance(self) -> dict[str, Any]:
        return {
            "status": "known",
            "name": "pe_sim.peak_current_switching_buck",
            "version": "peak-current-v1",
            "solver_settings": {
                "integrator": "rk4",
                "max_internal_step_s": self.parameters.integration_step_s,
                "switching_edge_splitting": "cycle_start_duty",
                "control": "model_side_peak_current",
                "floating_point": "IEEE-754-binary64",
            },
            "limitations": [
                "project-owned deterministic peak-current model; not an external circuit simulator",
                "the kernel-provided Controller and action are not driven on this route",
                "comparator event time is computed from the cycle-start linear inductor slope",
            ],
        }


register_solve_path(
    "model-control-buck",
    ("backends/model_control_buck.py",),
)
register_solve_path(
    "whole-run-switching-buck",
    ("backends/model_control_buck.py",),
)
register_solve_path(
    "peak-current-buck",
    ("backends/model_control_buck.py",),
)


__all__ = [
    "ModelControlBuckParameters",
    "ModelControlBuckPlant",
    "PeakCurrentBuckParameters",
    "PeakCurrentBuckPlant",
    "WholeRunSwitchingBuckPlant",
]
