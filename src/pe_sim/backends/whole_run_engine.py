"""Deterministic whole-run engine and its ideal averaged Buck reference model.

This module is the engine half of the whole-run route.  Unlike a stepped
adapter, the engine here owns the time axis: it integrates an entire horizon
internally and returns one complete trace.  The kernel cannot inject the
project controller window by window on this route, so **the kernel-provided
Controller is not driven here**.  The engine owns a small discrete PI loop on
``vout`` against the ``vref`` schedule supplied in the request metadata.

The integration is a generic linear state-space RK4 stepper with a bounded
internal substep.  Input changes are integration boundaries: a step never
crosses a breakpoint, so a result cannot depend on whether a breakpoint happens
to land on the substep grid.

What this module does **not** claim:

- no in-run safety, validity, or applicability enforcement; the whole-run route
  is priced at post-hoc evidence only (``docs/43`` section 5, ``docs/46``);
- no switching ripple, CCM/DCM detection, losses, or thermal state;
- no control-loop stability or tuning claim; the PI gains are engine
  parameters, not a review of the kernel controller;
- no reviewed implementation identity.  The solve path is declared at runtime
  so the kernel can recompute a digest, but no reviewed pin exists, so
  :func:`pe_sim.identity.review_status` remains ``not_pinned``.

The module is project-owned and deterministic.  It names no external
simulator, vendor, or product, reads no files or environment variables, and
uses no clock or random source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from ..identity import (
    declared_solve_paths,
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
from ..time_model import (
    TIME_MODEL_WHOLE_RUN,
    TraceContract,
    WholeRunRequest,
    WholeRunResult,
    validate_whole_run_result,
)


BACKEND_ID = "whole-run-buck"
SOLVE_PATH: tuple[str, ...] = ("backends/whole_run_engine.py",)
ENGINE_NAME = "pe_sim.whole_run_linear_rk4"
ENGINE_VERSION = "reference-v1"
NUMERICAL_HEALTH_SCHEMA = "whole-run-numerical-health-v1"

# Timestamps are only compared to decide whether two events are the same
# instant.  It is not a numerical integration tolerance.
_TIME_TOLERANCE_S = 1e-12
_MISSING = object()


def _ensure_solve_path_declared() -> None:
    """Declare the solve path in this process without touching the review pin."""

    if BACKEND_ID not in declared_solve_paths():
        register_solve_path(BACKEND_ID, SOLVE_PATH)


_ensure_solve_path_declared()


def _finite(name: str, value: Any) -> float:
    """Return ``value`` as a finite float, rejecting bool and non-numbers."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _positive(name: str, value: Any) -> float:
    number = _finite(name, value)
    if number <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _non_negative(name: str, value: Any) -> float:
    number = _finite(name, value)
    if number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite_vector(name: str, values: Sequence[Any]) -> tuple[float, ...]:
    return tuple(_finite(f"{name}[{index}]", value) for index, value in enumerate(values))


@dataclass(frozen=True)
class LinearStateSpace:
    """A time-invariant linear system ``dx/dt = A x + B u``.

    ``matrix_a`` must be square ``n x n``; ``input_matrix`` must be ``n x m``
    with at least one input.  The object is a value object: it holds no state
    and performs no I/O.
    """

    matrix_a: tuple[tuple[float, ...], ...]
    input_matrix: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        matrix_a = tuple(_finite_vector(f"matrix_a[{row}]", values) for row, values in enumerate(self.matrix_a))
        row_count = len(matrix_a)
        if row_count == 0:
            raise ValueError("matrix_a must contain at least one row")
        if any(len(row) != row_count for row in matrix_a):
            raise ValueError("matrix_a must be square")
        input_matrix = tuple(
            _finite_vector(f"input_matrix[{row}]", values)
            for row, values in enumerate(self.input_matrix)
        )
        if len(input_matrix) != row_count:
            raise ValueError("input_matrix must have one row per state")
        input_width = len(input_matrix[0])
        if input_width == 0:
            raise ValueError("input_matrix must declare at least one input column")
        if any(len(row) != input_width for row in input_matrix):
            raise ValueError("input_matrix rows must all have the same length")
        object.__setattr__(self, "matrix_a", matrix_a)
        object.__setattr__(self, "input_matrix", input_matrix)

    @property
    def state_size(self) -> int:
        return len(self.matrix_a)

    @property
    def input_size(self) -> int:
        return len(self.input_matrix[0])

    def derivative(self, state: Sequence[float], inputs: Sequence[float]) -> tuple[float, ...]:
        """Return ``A x + B u`` as a tuple of floats."""

        if len(state) != self.state_size:
            raise ValueError(f"state must have {self.state_size} entries")
        if len(inputs) != self.input_size:
            raise ValueError(f"inputs must have {self.input_size} entries")
        result: list[float] = []
        for row_index, coefficients in enumerate(self.matrix_a):
            total = 0.0
            for column_index, coefficient in enumerate(coefficients):
                total += coefficient * float(state[column_index])
            input_coefficients = self.input_matrix[row_index]
            for column_index, coefficient in enumerate(input_coefficients):
                total += coefficient * float(inputs[column_index])
            result.append(total)
        return tuple(result)


@dataclass(frozen=True)
class IntegrationOutcome:
    """The state after one integration call and how many RK4 steps it took."""

    state: tuple[float, ...]
    substeps: int
    segments: int


def _rk4_step(
    system: LinearStateSpace,
    state: tuple[float, ...],
    inputs: tuple[float, ...],
    step_s: float,
) -> tuple[float, ...]:
    k1 = system.derivative(state, inputs)
    k2_state = tuple(state[index] + 0.5 * step_s * k1[index] for index in range(len(state)))
    k2 = system.derivative(k2_state, inputs)
    k3_state = tuple(state[index] + 0.5 * step_s * k2[index] for index in range(len(state)))
    k3 = system.derivative(k3_state, inputs)
    k4_state = tuple(state[index] + step_s * k3[index] for index in range(len(state)))
    k4 = system.derivative(k4_state, inputs)
    return tuple(
        state[index] + step_s * (k1[index] + 2.0 * k2[index] + 2.0 * k3[index] + k4[index]) / 6.0
        for index in range(len(state))
    )


def _normalized_breakpoints(
    system: LinearStateSpace,
    input_breakpoints: Sequence[tuple[float, Sequence[float]]],
) -> tuple[tuple[float, tuple[float, ...]], ...]:
    """Validate breakpoints, keep them ordered, and let the last duplicate win."""

    normalized: list[tuple[float, tuple[float, ...]]] = []
    previous_time = -math.inf
    for index, item in enumerate(input_breakpoints):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"input_breakpoints[{index}] must be a (time_s, inputs) pair")
        time_s = _finite(f"input_breakpoints[{index}].time_s", item[0])
        if time_s < previous_time:
            raise ValueError("input_breakpoints must be non-decreasing in time")
        inputs = _finite_vector(f"input_breakpoints[{index}].inputs", item[1])
        if len(inputs) != system.input_size:
            raise ValueError(
                f"input_breakpoints[{index}] must have {system.input_size} input values"
            )
        if normalized and time_s == normalized[-1][0]:
            normalized[-1] = (time_s, inputs)
        else:
            normalized.append((time_s, inputs))
        previous_time = time_s
    return tuple(normalized)


def integrate_rk4(
    system: LinearStateSpace,
    state: Sequence[float],
    *,
    start_s: float,
    end_s: float,
    input_breakpoints: Sequence[tuple[float, Sequence[float]]],
    max_step_s: float,
) -> IntegrationOutcome:
    """Integrate a linear system from ``start_s`` to ``end_s`` with RK4.

    ``input_breakpoints`` declares the piecewise-constant input as
    ``(time_s, inputs)`` pairs in non-decreasing time.  The value of a pair
    holds until the next pair.  At least one pair must be at or before
    ``start_s``; the last such pair is the initial input.

    Every breakpoint strictly inside the horizon is an integration boundary.
    Within one constant-input segment, the span is divided into
    ``ceil(span / max_step_s)`` equal RK4 substeps, so no step crosses a
    breakpoint and no result depends on substep alignment with one.  The final
    step of a segment is therefore always the same size as the others; it is
    not a leftover sliver that changes when a breakpoint moves.

    The function is deterministic and stdlib-only.  It raises ``ValueError``
    for malformed arguments and for a state that becomes non-finite.
    """

    if not isinstance(system, LinearStateSpace):
        raise ValueError("system must be a LinearStateSpace")
    start = _finite("start_s", start_s)
    end = _finite("end_s", end_s)
    if end <= start:
        raise ValueError("end_s must be greater than start_s")
    step_limit = _positive("max_step_s", max_step_s)
    initial = _finite_vector("state", state)
    if len(initial) != system.state_size:
        raise ValueError(f"state must have {system.state_size} entries")

    breakpoints = _normalized_breakpoints(system, input_breakpoints)
    if not breakpoints:
        raise ValueError("input_breakpoints must declare the input at start_s")

    initial_index = -1
    for index, (time_s, _inputs) in enumerate(breakpoints):
        if time_s <= start:
            initial_index = index
        else:
            break
    if initial_index < 0:
        raise ValueError("input_breakpoints must declare the input at start_s")

    boundaries: list[float] = [start]
    for time_s, _inputs in breakpoints:
        if start < time_s < end:
            boundaries.append(time_s)
    boundaries.append(end)

    current = initial
    current_index = initial_index
    substeps = 0
    for left, right in zip(boundaries, boundaries[1:]):
        while current_index + 1 < len(breakpoints) and breakpoints[current_index + 1][0] <= left:
            current_index += 1
        inputs = breakpoints[current_index][1]
        span = right - left
        step_count = max(1, int(math.ceil(span / step_limit)))
        step = span / step_count
        for _ in range(step_count):
            current = _rk4_step(system, current, inputs, step)
            if not all(math.isfinite(value) for value in current):
                raise ValueError("integration produced a non-finite state")
        substeps += step_count

    return IntegrationOutcome(state=current, substeps=substeps, segments=len(boundaries) - 1)


@dataclass(frozen=True)
class _SchedulePoint:
    time_s: float
    vref_v: float


class _PiController:
    """Small deterministic PI controller owned by the engine.

    The integral term is conditionally frozen when the actuator is saturated in
    the direction of the error, and is bounded by the actuator limits.  The
    output is always clamped.  This is a reference control law, not a tuning
    claim and not the kernel-provided controller.
    """

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
        self.kp = kp
        self.ki = ki
        self.output_min = output_min
        self.output_max = output_max
        self.anti_windup = anti_windup
        self.integral = integral
        self.saturation_updates = 0

    def update(self, *, error: float, dt_s: float) -> tuple[float, bool]:
        raw_integral = self.integral + self.ki * error * dt_s
        unclamped = self.kp * error + raw_integral
        saturated_high = unclamped > self.output_max
        saturated_low = unclamped < self.output_min
        candidate = raw_integral
        if self.anti_windup:
            if (saturated_high and error > 0.0) or (saturated_low and error < 0.0):
                candidate = self.integral
            candidate = min(max(candidate, self.output_min), self.output_max)
        self.integral = candidate
        output = self.kp * error + candidate
        if output > self.output_max:
            output = self.output_max
            saturated = True
        elif output < self.output_min:
            output = self.output_min
            saturated = True
        else:
            saturated = False
        if saturated:
            self.saturation_updates += 1
        return output, saturated


class WholeRunBuckPlant:
    """Whole-run adapter for the ideal averaged Buck converter.

    State is inductor current ``iL`` and capacitor voltage ``vout``::

        diL/dt  = (duty * Vin - vout) / L
        dvout/dt = (iL - vout / R) / C

    The engine integrates that state internally with RK4 and runs its own PI
    controller on ``vout`` against the ``vref`` schedule.  The kernel-provided
    Controller is deliberately not driven: a whole-run engine cannot be
    constrained window by window, and the whole-run route is capped at post-hoc
    evidence.  :meth:`backend_evidence` reports numerical health after the run;
    it does not claim in-run enforcement.
    """

    topology = "buck"
    model_kind = "whole_run_averaged_reference"
    model_level = "L1"
    time_model = TIME_MODEL_WHOLE_RUN
    backend_id = BACKEND_ID

    def __init__(
        self,
        *,
        input_voltage_v: float = 12.0,
        inductance_h: float = 100e-6,
        capacitance_f: float = 470e-6,
        load_resistance_ohm: float = 1.0,
        integration_step_s: float = 1e-6,
        cadence_s: float = 1e-4,
        default_vref_v: float = 5.0,
        kp: float = 0.1,
        ki: float = 60.0,
        output_min: float = 0.0,
        output_max: float = 1.0,
        anti_windup: bool = True,
    ) -> None:
        self.input_voltage_v = _positive("input_voltage_v", input_voltage_v)
        self.inductance_h = _positive("inductance_h", inductance_h)
        self.capacitance_f = _positive("capacitance_f", capacitance_f)
        self.load_resistance_ohm = _positive("load_resistance_ohm", load_resistance_ohm)
        self.integration_step_s = _positive("integration_step_s", integration_step_s)
        self.cadence_s = _positive("cadence_s", cadence_s)
        self.default_vref_v = _non_negative("default_vref_v", default_vref_v)
        self.kp = _finite("kp", kp)
        self.ki = _finite("ki", ki)
        self.output_min = _finite("output_min", output_min)
        self.output_max = _finite("output_max", output_max)
        if not self.output_min < self.output_max:
            raise ValueError("output_min must be smaller than output_max")
        if self.output_min < 0.0 or self.output_max > 1.0:
            raise ValueError("duty output limits must lie within [0, 1]")
        if not isinstance(anti_windup, bool):
            raise ValueError("anti_windup must be a boolean")
        self.anti_windup = anti_windup
        self._system = LinearStateSpace(
            matrix_a=(
                (0.0, -1.0 / self.inductance_h),
                (1.0 / self.capacitance_f, -1.0 / (self.load_resistance_ohm * self.capacitance_f)),
            ),
            input_matrix=((self.input_voltage_v / self.inductance_h,), (0.0,)),
        )
        self._last_run_evidence: dict[str, Any] | None = None
        self._identity_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # WholeRunPlant boundary
    # ------------------------------------------------------------------
    def trace_contract(self) -> TraceContract:
        return TraceContract(cadence_s=self.cadence_s)

    def run_whole(self, request: WholeRunRequest) -> WholeRunResult:
        """Integrate the complete horizon and return one trace.

        Configuration is read only from the request (``duration_s``,
        ``initial_state``, ``seed``, and ``metadata['input_schedule']``) and
        from this object's constructor parameters.  The method touches no
        global state, environment variable, file, or clock.
        """

        if not isinstance(request, WholeRunRequest):
            raise ValueError("request must be a WholeRunRequest")
        contract = self.trace_contract()
        duration_s = float(request.duration_s)
        schedule = self._parse_schedule(request.metadata)
        inductor_current_a, output_voltage_v, integral = self._parse_initial_state(
            request.initial_state
        )
        sample_times = self._sample_times(duration_s, contract.cadence_s)
        control_times = self._control_times(sample_times, schedule, duration_s)
        controller = _PiController(
            kp=self.kp,
            ki=self.ki,
            output_min=self.output_min,
            output_max=self.output_max,
            anti_windup=self.anti_windup,
            integral=integral,
        )

        vout_samples: list[float] = []
        current_samples: list[float] = []
        duty_samples: list[float] = []
        vref_samples: list[float] = []

        sample_index = 0
        substeps = 0
        segments = 0
        control_updates = 0
        duty_min = math.inf
        duty_max = -math.inf
        previous_time_s = 0.0
        duty = 0.0
        vref = self.default_vref_v

        for index, time_s in enumerate(control_times):
            dt_s = 0.0 if index == 0 else time_s - previous_time_s
            if dt_s < 0.0:
                raise RuntimeError("control instants must be non-decreasing")
            vref = self._vref_at(schedule, time_s)
            duty, _saturated = controller.update(error=vref - output_voltage_v, dt_s=dt_s)
            control_updates += 1
            duty_min = min(duty_min, duty)
            duty_max = max(duty_max, duty)

            if (
                sample_index < len(sample_times)
                and abs(time_s - sample_times[sample_index]) <= _TIME_TOLERANCE_S
            ):
                # Record the declared sample time, not the merged event time,
                # so the first sample is exactly 0.0 and the last is exactly
                # the requested duration.
                sample_time = sample_times[sample_index]
                vout_samples.append(output_voltage_v)
                current_samples.append(inductor_current_a)
                duty_samples.append(duty)
                vref_samples.append(vref)
                if sample_time != time_s:
                    raise RuntimeError("sample bookkeeping lost the declared sample time")
                sample_index += 1

            if index + 1 < len(control_times):
                next_time_s = control_times[index + 1]
                outcome = integrate_rk4(
                    self._system,
                    (inductor_current_a, output_voltage_v),
                    start_s=time_s,
                    end_s=next_time_s,
                    input_breakpoints=((time_s, (duty,)),),
                    max_step_s=self.integration_step_s,
                )
                inductor_current_a, output_voltage_v = outcome.state
                substeps += outcome.substeps
                segments += outcome.segments
            previous_time_s = time_s

        if sample_index != len(sample_times):
            raise RuntimeError(
                f"internal error: recorded {sample_index} of {len(sample_times)} samples"
            )

        result = WholeRunResult(
            time_s=tuple(sample_times),
            signals={
                "vout": tuple(vout_samples),
                "inductor_current_a": tuple(current_samples),
                "duty": tuple(duty_samples),
                "vref": tuple(vref_samples),
            },
            final_state={
                "time_s": duration_s,
                "inductor_current_a": inductor_current_a,
                "output_voltage_v": output_voltage_v,
                "controller_integral": controller.integral,
                "duty": duty,
                "vref": vref,
            },
            metadata={
                "backend_id": self.backend_id,
                "time_model": self.time_model,
                "trace_contract": contract.to_dict(),
                "control_law": "engine_owned_pi",
                "enforcement": "post_hoc_only",
                "seed": request.seed,
                "samples": len(sample_times),
                "integration": {
                    "integrator": "rk4",
                    "max_internal_step_s": self.integration_step_s,
                    "substeps": substeps,
                    "segments": segments,
                },
                "duty": {
                    "minimum": duty_min if control_updates else None,
                    "maximum": duty_max if control_updates else None,
                    "saturated": bool(controller.saturation_updates),
                    "saturation_updates": controller.saturation_updates,
                },
            },
        )

        problems = validate_whole_run_result(result, contract, duration_s=duration_s)
        if problems:
            raise RuntimeError("whole-run trace failed its own post-hoc validation: " + "; ".join(problems))

        self._last_run_evidence = {
            "schema": NUMERICAL_HEALTH_SCHEMA,
            "severity": "clean",
            "severity_basis": (
                "no non-finite state or step-size failure was observed; actuator "
                "saturation is reported separately"
            ),
            "integrator": "rk4",
            "internal_step_s": self.integration_step_s,
            "substeps": substeps,
            "segments": segments,
            "control_updates": control_updates,
            "sample_count": len(sample_times),
            "minimum_duty": duty_min if control_updates else None,
            "maximum_duty": duty_max if control_updates else None,
            "duty_saturated": bool(controller.saturation_updates),
            "saturation_updates": controller.saturation_updates,
            "state_finite": True,
        }
        return result

    # ------------------------------------------------------------------
    # Declarations used by the composition root and qualification rules
    # ------------------------------------------------------------------
    def capabilities(self) -> set[str]:
        return {
            "continuous_time",
            "deterministic",
            "engine_owned_control",
            "external_input",
            "whole_run_trace",
        }

    def analytical_steady_state(self, duty: float) -> dict[str, float]:
        """Return the ideal CCM equilibrium for a constant duty ratio."""

        duty_value = _finite("duty", duty)
        if not 0.0 <= duty_value <= 1.0:
            raise ValueError("duty must be finite and in [0, 1]")
        voltage = duty_value * self.input_voltage_v
        return {
            "output_voltage_v": voltage,
            "inductor_current_a": voltage / self.load_resistance_ohm,
        }

    def backend_provenance(self) -> dict[str, Any]:
        """Identify the deterministic, project-owned Python engine.

        This is not a claim about an external circuit simulator.  The solver
        settings are complete enough for the provenance collector to report
        ``known``; the implementation review status is a separate question and
        remains ``not_pinned``.
        """

        return {
            "status": "known",
            "name": ENGINE_NAME,
            "version": ENGINE_VERSION,
            "solver_settings": {
                "integrator": "rk4",
                "max_internal_step_s": self.integration_step_s,
                "trace_cadence_s": self.cadence_s,
                "control": "engine_owned_pi",
                "floating_point": "IEEE-754-binary64",
            },
            "limitations": [
                "project-owned deterministic Python engine; not an external circuit simulator",
                "the engine owns its time axis, so the kernel cannot enforce safety or validity in-run",
                "the kernel-provided Controller is not driven on the whole-run route",
            ],
        }

    def manifest_identity(self) -> dict[str, Any]:
        """Return a portable identity without claiming a reviewed pin."""

        if self._identity_cache is not None:
            return dict(self._identity_cache)
        _ensure_solve_path_declared()
        implementation = type(self)
        sources = implementation_sources(self.backend_id)
        digest = implementation_digest(self.backend_id)
        identity: dict[str, Any] = {
            "level": self.model_level,
            "kind": self.model_kind,
            "topology": self.topology,
            "backend_id": self.backend_id,
            "time_model": self.time_model,
            "trace_contract": self.trace_contract().to_dict(),
            "parameters": {
                "input_voltage_v": self.input_voltage_v,
                "inductance_h": self.inductance_h,
                "capacitance_f": self.capacitance_f,
                "load_resistance_ohm": self.load_resistance_ohm,
                "integration_step_s": self.integration_step_s,
                "cadence_s": self.cadence_s,
                "default_vref_v": self.default_vref_v,
            },
            "controller": {
                "kind": "engine_owned_pi",
                "kp": self.kp,
                "ki": self.ki,
                "output_min": self.output_min,
                "output_max": self.output_max,
                "anti_windup": self.anti_windup,
            },
            "capabilities": sorted(self.capabilities()),
            "implementation": {
                "module": implementation.__module__,
                "class": implementation.__qualname__,
                "solve_path_modules": sorted(sources),
                "sha256": digest,
                "review": review_status(self.backend_id, digest),
            },
        }
        identity["sha256"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        self._identity_cache = identity
        return dict(identity)

    def backend_evidence(self) -> dict[str, Any]:
        """Report post-hoc numerical health for the most recent run.

        The evidence is descriptive only.  It records what the engine observed
        after integrating, not a constraint that was enforced during the run.
        """

        limitations = [
            "post-hoc evidence only: no in-run safety, validity, or applicability enforcement",
            "actuator saturation is reported as a fact, not classified as a numerical failure",
        ]
        base: dict[str, Any] = {
            "status": "post_hoc_only",
            "time_model": self.time_model,
            "enforcement": "none",
            "limitations": limitations,
        }
        if self._last_run_evidence is None:
            return {
                **base,
                "status": "not_run",
                "numerical_health": {
                    "schema": NUMERICAL_HEALTH_SCHEMA,
                    "severity": "unknown",
                    "reason": "engine_has_not_run",
                },
            }
        return {**base, "numerical_health": dict(self._last_run_evidence)}

    def validity_envelope(self) -> ModelClaim:
        """Declare the averaged model limits and the missing in-run enforcement."""

        tau_current = self.inductance_h / self.load_resistance_ohm
        tau_voltage = self.load_resistance_ohm * self.capacitance_f
        tau_lc = 2.0 * math.pi * math.sqrt(self.inductance_h * self.capacitance_f)
        recommended_step = min(tau_current, tau_voltage, tau_lc / 2.0) / 10.0
        return ModelClaim(
            claim_level="L1-averaged-reference",
            topology=self.topology,
            model_form="state_space_average",
            assumptions=(
                "continuous conduction mode (CCM) is assumed, not detected",
                "ideal switch and diode",
                "lossless inductor and capacitor (no ESR, ESL, or saturation)",
                "averaged state-space model: switching ripple is not represented",
                "the engine owns a discrete PI control loop on vout",
                "duty is constant between control instants",
                "no thermal state or temperature dependence",
            ),
            operating_ranges=(
                OperatingRange(
                    "duty",
                    unit="1",
                    minimum=0.0,
                    maximum=1.0,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "input_voltage_v",
                    unit="V",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "load_resistance_ohm",
                    unit="ohm",
                    minimum=0.0,
                    minimum_exclusive=True,
                    basis="equation_domain",
                ),
                OperatingRange(
                    "default_vref_v",
                    unit="V",
                    minimum=0.0,
                    basis="declared",
                    note="setpoints above input_voltage_v cannot be tracked and saturate duty",
                ),
            ),
            numerical_limits=(
                NumericalLimit(
                    "integration_step_s.max_recommended",
                    recommended_step,
                    "s",
                    strength="advisory",
                    basis="structural",
                    note=(
                        "one tenth of min(L/R, R*C, T_LC/2); guidance only, "
                        "not enforced in-run"
                    ),
                ),
                NumericalLimit(
                    "cadence_s.min_internal_substeps",
                    1.0,
                    "1",
                    strength="advisory",
                    basis="structural",
                    note="the integrator subdivides any cadence span as needed",
                ),
            ),
            limitations=(
                "whole-run route: post-hoc validation only, no in-run safety or validity enforcement",
                "averaged state-space model: switching ripple and peak inductor current are not represented",
                "continuous conduction is assumed, not detected",
                "ideal switch/diode and lossless L/C: no conduction, switching, ESR, or ESL losses",
                "no control-loop stability or tuning claim; PI gains are engine parameters",
                "no thermal state or magnetic saturation",
                "parameter values are constructor-declared, not measured or calibrated",
            ),
            uncheckable_rules=(
                {"rule_id": "envelope.ccm.boundary", "reason": "switching_frequency_unavailable"},
                {
                    "rule_id": "envelope.in_run_enforcement",
                    "reason": "whole_run_engine_owns_the_time_axis",
                },
                {
                    "rule_id": "envelope.controller.stability",
                    "reason": "no_stability_analysis_is_declared",
                },
            ),
            actuator={"name": "duty", "unit": "normalized"},
            static_parameters={
                "input_voltage_v": self.input_voltage_v,
                "inductance_h": self.inductance_h,
                "capacitance_f": self.capacitance_f,
                "load_resistance_ohm": self.load_resistance_ohm,
                "integration_step_s": self.integration_step_s,
                "cadence_s": self.cadence_s,
                "default_vref_v": self.default_vref_v,
                "kp": self.kp,
                "ki": self.ki,
                "output_min": self.output_min,
                "output_max": self.output_max,
            },
        )

    def parameter_provenance(self) -> ParameterProvenance:
        """Declare that every reference parameter is a constructor assumption."""

        values = (
            ("input_voltage_v", self.input_voltage_v, "V"),
            ("inductance_h", self.inductance_h, "H"),
            ("capacitance_f", self.capacitance_f, "F"),
            ("load_resistance_ohm", self.load_resistance_ohm, "ohm"),
            ("integration_step_s", self.integration_step_s, "s"),
            ("cadence_s", self.cadence_s, "s"),
            ("default_vref_v", self.default_vref_v, "V"),
            ("kp", self.kp, "1/V"),
            ("ki", self.ki, "1/(V*s)"),
            ("output_min", self.output_min, "1"),
            ("output_max", self.output_max, "1"),
        )
        records = tuple(
            ParameterRecord(
                name=name,
                value=value,
                unit=unit,
                source="assumption",
                reference="backends/whole_run_engine.py constructor/default",
                review_status="unreviewed",
                note="reference-engine value; not a measured or calibrated parameter",
            )
            for name, value, unit in values
        )
        return ParameterProvenance(records=records, status="declared")

    def topology_descriptor(self):
        from ..topology import export_topology_descriptor

        return export_topology_descriptor(self)

    # ------------------------------------------------------------------
    # Request parsing
    # ------------------------------------------------------------------
    def _parse_schedule(self, metadata: Mapping[str, Any]) -> tuple[_SchedulePoint, ...]:
        if not isinstance(metadata, Mapping):
            raise ValueError("request.metadata must be a mapping")
        raw = metadata.get("input_schedule", _MISSING)
        if raw is _MISSING:
            return ()
        if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
            raise ValueError("metadata['input_schedule'] must be a list of {time_s, vref} objects")
        points: list[_SchedulePoint] = []
        previous_time = -math.inf
        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                raise ValueError(f"input_schedule[{index}] must be an object")
            unknown = set(entry) - {"time_s", "vref"}
            if unknown:
                raise ValueError(
                    f"input_schedule[{index}] has unsupported keys: {sorted(unknown)}; "
                    "allowed: ['time_s', 'vref']"
                )
            if "time_s" not in entry or "vref" not in entry:
                raise ValueError(f"input_schedule[{index}] must declare both time_s and vref")
            time_s = _finite(f"input_schedule[{index}].time_s", entry["time_s"])
            vref = _finite(f"input_schedule[{index}].vref", entry["vref"])
            if time_s < 0.0:
                raise ValueError(f"input_schedule[{index}].time_s must be non-negative")
            if time_s < previous_time:
                raise ValueError(
                    "input_schedule must be non-decreasing in time; "
                    f"entry {index} goes backwards from {previous_time!r} to {time_s!r}"
                )
            points.append(_SchedulePoint(time_s=time_s, vref_v=vref))
            previous_time = time_s
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
            raise ValueError(
                f"unsupported initial_state fields: {sorted(unknown)}; allowed: {sorted(allowed)}"
            )
        mode = initial_state.get("mode", "cold_start")
        if mode not in {"cold_start", "warm_start"}:
            raise ValueError(
                "unsupported initial_state mode for a whole-run engine: "
                f"{mode!r}; snapshot restore is not implemented on this route"
            )
        if initial_state.get("snapshot_path") is not None or initial_state.get("snapshot_hash") is not None:
            raise ValueError(
                "a whole-run engine cannot read a snapshot file; provide explicit "
                "inductor_current_a/output_voltage_v values instead"
            )
        if "time_s" in initial_state:
            initial_time = _finite("initial_state.time_s", initial_state["time_s"])
            if initial_time != 0.0:
                raise ValueError("initial_state.time_s must be 0.0 on the whole-run route")
        inductor_current_a = self._aliased_value(
            initial_state, "inductor_current_a", "current_a", 0.0
        )
        output_voltage_v = self._aliased_value(
            initial_state, "output_voltage_v", "vout", 0.0
        )
        integral = (
            0.0
            if "controller_integral" not in initial_state
            else _finite("initial_state.controller_integral", initial_state["controller_integral"])
        )
        return inductor_current_a, output_voltage_v, integral

    @staticmethod
    def _aliased_value(
        state: Mapping[str, Any], primary: str, alias: str, default: float
    ) -> float:
        if primary in state and alias in state:
            first = _finite(f"initial_state.{primary}", state[primary])
            second = _finite(f"initial_state.{alias}", state[alias])
            if first != second:
                raise ValueError(
                    f"initial_state.{primary} and initial_state.{alias} disagree"
                )
            return first
        if primary in state:
            return _finite(f"initial_state.{primary}", state[primary])
        if alias in state:
            return _finite(f"initial_state.{alias}", state[alias])
        return default

    # ------------------------------------------------------------------
    # Time grid
    # ------------------------------------------------------------------
    @staticmethod
    def _sample_times(duration_s: float, cadence_s: float) -> tuple[float, ...]:
        """Return 0.0, cadence multiples below the horizon, and the exact end.

        The endpoint is appended even when the cadence does not divide the
        duration; the resulting final gap is therefore never larger than the
        cadence.
        """

        times: list[float] = [0.0]
        index = 1
        while True:
            candidate = index * cadence_s
            if candidate >= duration_s - _TIME_TOLERANCE_S:
                break
            if candidate <= times[-1]:
                raise ValueError(
                    "cadence_s is too small to produce strictly increasing sample times"
                )
            times.append(candidate)
            index += 1
        if times[-1] != duration_s:
            times.append(duration_s)
        return tuple(times)

    @staticmethod
    def _control_times(
        sample_times: Sequence[float],
        schedule: Sequence[_SchedulePoint],
        duration_s: float,
    ) -> tuple[float, ...]:
        """Union of declared samples and schedule breakpoints inside the run."""

        control = list(sample_times)
        for point in schedule:
            if point.time_s <= 0.0 or point.time_s >= duration_s:
                continue
            if any(abs(point.time_s - sample) <= _TIME_TOLERANCE_S for sample in sample_times):
                continue
            control.append(point.time_s)
        control.sort()
        return tuple(control)

    def _vref_at(self, schedule: Sequence[_SchedulePoint], time_s: float) -> float:
        value = self.default_vref_v
        for point in schedule:
            if point.time_s <= time_s + _TIME_TOLERANCE_S:
                value = point.vref_v
            else:
                break
        return value


__all__ = [
    "BACKEND_ID",
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "IntegrationOutcome",
    "LinearStateSpace",
    "NUMERICAL_HEALTH_SCHEMA",
    "SOLVE_PATH",
    "WholeRunBuckPlant",
    "integrate_rk4",
]
