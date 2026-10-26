"""Time ownership, trace cadence, and the whole-run engine boundary.

The kernel owns time for stepped adapters: it advances a model one window at a
time and can therefore enforce safety, validity, and applicability constraints
*per window*.  A whole-run engine owns time itself and returns an entire trace,
so the kernel can only inspect the result afterwards with post-hoc checks.

Whole-run engines are therefore admitted only at ``limited`` evidence.  The
contract and the executable route are implemented since 0.5.10
(:mod:`pe_sim.whole_run_route`); a whole-run adapter is still capped at
``limited``, and a request that cannot be served is refused rather than
silently mis-run.

The module is deliberately stdlib-only and names no engine, topology, or
vendor.  It states who owns time and what a trace must satisfy, not how any
particular engine works.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import Any, Protocol

TIME_MODEL_STEPPED = "stepped"
TIME_MODEL_WHOLE_RUN = "whole_run"
TIME_MODELS: tuple[str, ...] = (TIME_MODEL_STEPPED, TIME_MODEL_WHOLE_RUN)

#: Where the closed loop closes.  ``kernel`` means a kernel-owned
#: ``ControllerAdapter`` shapes the actuator; ``model`` means the compensator
#: lives inside the model or the netlist and the kernel cannot shape it.
CONTROL_LOCATION_KERNEL = "kernel"
CONTROL_LOCATION_MODEL = "model"
CONTROL_LOCATIONS: tuple[str, ...] = (CONTROL_LOCATION_KERNEL, CONTROL_LOCATION_MODEL)

#: Whether an external input may change between observation points.  ``window``
#: means inputs are resolved once per control window; ``continuous`` means the
#: solve honours every declared change point.
EVENT_GRANULARITY_WINDOW = "window"
EVENT_GRANULARITY_CONTINUOUS = "continuous"
EVENT_GRANULARITIES: tuple[str, ...] = (
    EVENT_GRANULARITY_WINDOW,
    EVENT_GRANULARITY_CONTINUOUS,
)

TRACE_CONTRACT_SCHEMA = "pe-sim.trace-contract.v1"

#: The runtime evidence vocabulary recorded in ``manifest["evidence_level"]``.
#: This is deliberately not the Stage 12 classification vocabulary
#: (``docs/22``), which has its own axes such as ``mechanism``.
RUNTIME_EVIDENCE_LEVELS = ("diagnostic_only", "functional", "limited", "comparable")

_EVIDENCE_RANK = {level: rank for rank, level in enumerate(RUNTIME_EVIDENCE_LEVELS)}

# The one and only evidence-cap table.  It is keyed by the two declarations
# that decide what the kernel can still enforce; ``whole_run + kernel`` is
# deliberately absent because :class:`RouteCapabilities` refuses it.  Every
# consumer must call :func:`evidence_cap_for` (directly or through a helper in
# this module) instead of restating a cap locally.
_ROUTE_EVIDENCE_CAP: dict[tuple[str, str], str] = {
    (TIME_MODEL_STEPPED, CONTROL_LOCATION_KERNEL): "comparable",
    (TIME_MODEL_STEPPED, CONTROL_LOCATION_MODEL): "limited",
    (TIME_MODEL_WHOLE_RUN, CONTROL_LOCATION_MODEL): "limited",
}

# Limitations that come from the time model itself, before the finer-grained
# declarations add their own codes.
_ROUTE_INHERENT_LIMITATIONS: dict[str, tuple[str, ...]] = {
    TIME_MODEL_STEPPED: (),
    TIME_MODEL_WHOLE_RUN: (
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    ),
}

__all__ = [
    "CONTROL_LOCATIONS",
    "CONTROL_LOCATION_KERNEL",
    "CONTROL_LOCATION_MODEL",
    "EVENT_GRANULARITIES",
    "EVENT_GRANULARITY_CONTINUOUS",
    "EVENT_GRANULARITY_WINDOW",
    "RUNTIME_EVIDENCE_LEVELS",
    "RouteCapabilities",
    "TIME_MODELS",
    "TIME_MODEL_STEPPED",
    "TIME_MODEL_WHOLE_RUN",
    "TRACE_CONTRACT_SCHEMA",
    "TraceContract",
    "WholeRunPlant",
    "WholeRunRequest",
    "WholeRunResult",
    "cap_evidence_level",
    "cap_evidence_level_for",
    "evidence_cap",
    "evidence_cap_for",
    "formal_comparison_allowed",
    "route_inherent_limitations_for",
    "route_limitations",
    "route_limitations_for",
    "validate_whole_run_result",
]


def _strict_float(value: Any, name: str) -> float:
    """Return ``value`` as a float, rejecting bool, non-numbers, and NaN/inf."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _number_problem(value: Any, label: str) -> str | None:
    """Return a problem description when ``value`` is not a finite real number."""

    if isinstance(value, bool):
        return f"{label} must be a real number, not bool"
    if not isinstance(value, (int, float)):
        return f"{label} must be a real number, got {type(value).__name__}"
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return f"{label} is not representable as a finite real number"
    if not math.isfinite(number):
        return f"{label} must be a finite real number"
    return None


def _sequence_or_problem(
    value: Any, label: str, problems: list[str]
) -> tuple[Any, int] | None:
    """Return ``(value, length)`` when sample-like, else record a problem.

    The check is duck-typed (``len`` and indexed access) so an engine may return
    a list, tuple, or array without this stdlib-only module importing it.  A
    mapping is rejected: iterating a mapping yields keys, not samples.
    """

    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        problems.append(f"{label} must be a sequence of numbers")
        return None
    if not hasattr(value, "__len__") or not hasattr(value, "__getitem__"):
        problems.append(f"{label} must be a sequence of numbers")
        return None
    try:
        length = len(value)
    except (TypeError, OverflowError):
        problems.append(f"{label} must be a sequence of numbers")
        return None
    return value, length


def _finite_values(
    values: Sequence[Any], label: str, problems: list[str]
) -> list[float] | None:
    """Return all values as floats, or ``None`` when any value is invalid."""

    numbers: list[float] = []
    for index, value in enumerate(values):
        problem = _number_problem(value, f"{label}[{index}]")
        if problem is None:
            numbers.append(float(value))
        else:
            problems.append(problem)
    if len(numbers) != len(values):
        return None
    return numbers


def _check_time_axis(
    times: Sequence[float],
    contract: "TraceContract",
    duration_s: float,
    problems: list[str],
) -> None:
    """Check monotonicity, zero start, coverage, and cadence on finite times."""

    cadence = contract.cadence_s
    tolerance = contract.coverage_tolerance_s

    for index in range(1, len(times)):
        if times[index] <= times[index - 1]:
            problems.append(
                f"time_s is not strictly increasing at index {index}: "
                f"{times[index]!r} does not advance past {times[index - 1]!r}"
            )

    first = times[0]
    if first < -tolerance:
        problems.append(
            f"trace starts at time_s[0]={first!r}, earlier than t=0 by more than "
            f"the coverage tolerance {tolerance!r}"
        )
    elif first > tolerance:
        problems.append(
            f"trace starts at time_s[0]={first!r}, later than t=0 by more than "
            f"the coverage tolerance {tolerance!r} (late start)"
        )

    last = times[-1]
    if last < duration_s - tolerance:
        problems.append(
            f"trace ends at time_s[-1]={last!r}, short of duration_s={duration_s!r} "
            f"by more than the coverage tolerance {tolerance!r} (short end)"
        )
    elif last > duration_s + tolerance:
        problems.append(
            f"trace ends at time_s[-1]={last!r}, beyond duration_s={duration_s!r} "
            f"by more than the coverage tolerance {tolerance!r} (overrun)"
        )

    budget = cadence + tolerance
    for index in range(1, len(times)):
        gap = times[index] - times[index - 1]
        if gap > budget:
            problems.append(
                f"time_s gap between index {index - 1} ({times[index - 1]!r}) and "
                f"index {index} ({times[index]!r}) is {gap!r}, which exceeds "
                f"cadence_s={cadence!r} plus tolerance {tolerance!r}"
            )


@dataclass(frozen=True)
class TraceContract:
    """The trace resolution an engine promises before it runs.

    ``cadence_s`` is the nominal spacing between consecutive samples.
    ``coverage_tolerance_s`` is the slack allowed on the first sample (must be
    zero within tolerance), the last sample (must reach ``duration_s`` within
    tolerance), and on each consecutive gap.  The contract is a promise about
    the produced trace, not a guarantee that the engine honours it: a whole-run
    trace is checked afterwards with :func:`validate_whole_run_result`.
    """

    cadence_s: float
    coverage_tolerance_s: float = 1e-9
    schema: str = TRACE_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        cadence = _strict_float(self.cadence_s, "cadence_s")
        if cadence <= 0.0:
            raise ValueError("cadence_s must be positive")
        tolerance = _strict_float(self.coverage_tolerance_s, "coverage_tolerance_s")
        if tolerance < 0.0:
            raise ValueError("coverage_tolerance_s must be non-negative")
        if self.schema != TRACE_CONTRACT_SCHEMA:
            raise ValueError(
                f"unsupported trace contract schema {self.schema!r}; "
                f"expected {TRACE_CONTRACT_SCHEMA!r}"
            )
        object.__setattr__(self, "cadence_s", cadence)
        object.__setattr__(self, "coverage_tolerance_s", tolerance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cadence_s": self.cadence_s,
            "coverage_tolerance_s": self.coverage_tolerance_s,
        }


@dataclass(frozen=True)
class WholeRunRequest:
    """Everything a whole-run engine is asked for in one call.

    ``duration_s`` is the only required field: the engine owns the internal
    time axis and decides how it is integrated.  ``seed`` is ``None`` for
    deterministic engines and a non-negative integer otherwise, so a stochastic
    engine can still be reproduced from the request alone.
    """

    duration_s: float
    initial_state: Mapping[str, Any] = field(default_factory=dict)
    seed: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        duration = _strict_float(self.duration_s, "duration_s")
        if duration <= 0.0:
            raise ValueError("duration_s must be positive")
        object.__setattr__(self, "duration_s", duration)
        if self.seed is not None:
            if isinstance(self.seed, bool) or not isinstance(self.seed, int):
                raise ValueError("seed must be a non-negative integer or None")
            if self.seed < 0:
                raise ValueError("seed must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "initial_state": dict(self.initial_state),
            "seed": self.seed,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WholeRunResult:
    """A complete trace returned by an engine that owns its own time axis.

    The result is the engine's claim, not a kernel guarantee.  Because a
    whole-run engine cannot be constrained while it runs, a caller must pass
    this object through :func:`validate_whole_run_result` before the values are
    used as evidence.
    """

    time_s: Sequence[float]
    signals: Mapping[str, Sequence[float]]
    final_state: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class WholeRunPlant(Protocol):
    """Structural boundary for an engine that owns its own time axis.

    ``time_model`` is :data:`TIME_MODEL_WHOLE_RUN`.  ``trace_contract`` declares
    the cadence the engine promises before it runs; ``run_whole`` returns a
    complete trace that the kernel can only check afterwards.  Declaring this
    protocol does not make the route executable: the Runner refuses
    ``whole_run`` until a real whole-run engine exists.
    """

    time_model: str

    def trace_contract(self) -> TraceContract:
        ...

    def run_whole(self, request: WholeRunRequest) -> WholeRunResult:
        ...


def validate_whole_run_result(
    result: WholeRunResult,
    contract: TraceContract,
    *,
    duration_s: float,
) -> tuple[str, ...]:
    """Return post-hoc problems in ``result``; an empty tuple means valid.

    The checks are deliberately limited to what can be established after the
    fact: sample presence, signal presence, alignment, finite values, strictly
    increasing time, zero start, duration coverage, and cadence.  They cannot
    replace in-run constraint enforcement, which is why the whole-run route is
    capped at ``limited`` evidence.

    ``ValueError`` is reserved for invalid arguments; problems in the trace are
    returned as text so a caller can report all of them at once.
    """

    if not isinstance(result, WholeRunResult):
        raise ValueError("result must be a WholeRunResult")
    if not isinstance(contract, TraceContract):
        raise ValueError("contract must be a TraceContract")
    duration = _strict_float(duration_s, "duration_s")
    if duration <= 0.0:
        raise ValueError("duration_s must be positive")

    problems: list[str] = []

    time_axis = _sequence_or_problem(result.time_s, "time_s", problems)
    if time_axis is not None:
        times, time_count = time_axis
        if time_count == 0:
            problems.append("time_s must contain at least one sample")
        else:
            finite_times = _finite_values(times, "time_s", problems)
            if finite_times is not None:
                _check_time_axis(finite_times, contract, duration, problems)
    else:
        time_count = None

    signals = result.signals
    if not isinstance(signals, Mapping):
        problems.append("signals must be a mapping of signal name to samples")
        return tuple(problems)
    if len(signals) == 0:
        problems.append("signals must declare at least one signal")
        return tuple(problems)

    expected = time_count
    for name, samples_value in signals.items():
        label = f"signals[{name!r}]"
        sample_axis = _sequence_or_problem(samples_value, label, problems)
        if sample_axis is None:
            continue
        samples, sample_count = sample_axis
        if expected is not None and sample_count != expected:
            problems.append(
                f"{label} has {sample_count} samples but time_s has {expected}"
            )
        _finite_values(samples, label, problems)

    return tuple(problems)


@dataclass(frozen=True)
class RouteCapabilities:
    """The three independent declarations that describe a solve route.

    ``time_model`` says who owns the clock, ``control_location`` says where the
    closed loop closes, and ``event_granularity`` says whether an external input
    may change between observation points.  The three facts are independent;
    the evidence cap is *derived* from the first two and never declared.

    ``whole_run`` with ``control_location="kernel"`` is contradictory: when the
    model owns the clock there is no per-window executor for the kernel to
    drive.  The combination is refused here, at construction, rather than being
    discovered at run time.
    """

    time_model: str
    control_location: str = CONTROL_LOCATION_KERNEL
    event_granularity: str = EVENT_GRANULARITY_WINDOW

    def __post_init__(self) -> None:
        if self.time_model not in TIME_MODELS:
            raise ValueError(
                f"unknown time_model {self.time_model!r}; "
                f"expected one of {list(TIME_MODELS)}"
            )
        if self.control_location not in CONTROL_LOCATIONS:
            raise ValueError(
                f"unknown control_location {self.control_location!r}; "
                f"expected one of {list(CONTROL_LOCATIONS)}"
            )
        if self.event_granularity not in EVENT_GRANULARITIES:
            raise ValueError(
                f"unknown event_granularity {self.event_granularity!r}; "
                f"expected one of {list(EVENT_GRANULARITIES)}"
            )
        if (
            self.time_model == TIME_MODEL_WHOLE_RUN
            and self.control_location == CONTROL_LOCATION_KERNEL
        ):
            raise ValueError(
                "contradictory route: whole_run with control_location='kernel' "
                "is impossible because the model owns the clock, so there is "
                "no per-window executor for the kernel to drive; declare "
                "control_location='model'"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "time_model": self.time_model,
            "control_location": self.control_location,
            "event_granularity": self.event_granularity,
        }


def _legacy_capabilities(time_model: str) -> RouteCapabilities:
    """Map the one-field legacy form onto a complete record.

    A time model alone cannot say where the loop closes, so the historical
    reading is kept: the kernel drives the actuator when it owns the clock, and
    the engine holds the compensator when it owns the clock itself.
    """

    control_location = (
        CONTROL_LOCATION_MODEL
        if time_model == TIME_MODEL_WHOLE_RUN
        else CONTROL_LOCATION_KERNEL
    )
    return RouteCapabilities(time_model=time_model, control_location=control_location)


def evidence_cap_for(capabilities: RouteCapabilities) -> str:
    """Return the strongest evidence level a declared route can reach."""

    if not isinstance(capabilities, RouteCapabilities):
        raise ValueError("capabilities must be a RouteCapabilities record")
    try:
        return _ROUTE_EVIDENCE_CAP[
            (capabilities.time_model, capabilities.control_location)
        ]
    except KeyError:
        raise ValueError(
            "no evidence cap is declared for time_model "
            f"{capabilities.time_model!r} with control_location "
            f"{capabilities.control_location!r}"
        ) from None


def cap_evidence_level_for(level: str, capabilities: RouteCapabilities) -> str:
    """Return the weaker of ``level`` and the cap of ``capabilities``."""

    try:
        rank = _EVIDENCE_RANK[level]
    except (KeyError, TypeError):
        raise ValueError(f"unknown evidence level: {level!r}") from None
    cap = evidence_cap_for(capabilities)
    if rank <= _EVIDENCE_RANK[cap]:
        return level
    return cap


def route_inherent_limitations_for(capabilities: RouteCapabilities) -> tuple[str, ...]:
    """Return the limitations that come from the time model alone.

    This is the compatibility subset reported under the historical
    ``route_limitations`` manifest key.  :func:`route_limitations_for` adds the
    codes contributed by the other two declarations.
    """

    if not isinstance(capabilities, RouteCapabilities):
        raise ValueError("capabilities must be a RouteCapabilities record")
    return _ROUTE_INHERENT_LIMITATIONS[capabilities.time_model]


def route_limitations_for(capabilities: RouteCapabilities) -> tuple[str, ...]:
    """Return every limitation a declared route reports, in stable order.

    The order is route-inherent first, then the control declaration, then the
    event declaration, so two reports can be compared positionally.
    """

    limitations = list(route_inherent_limitations_for(capabilities))
    if capabilities.control_location == CONTROL_LOCATION_MODEL:
        limitations.append("actuator_control_in_model")
    if capabilities.event_granularity == EVENT_GRANULARITY_WINDOW:
        limitations.append("events_quantized_to_control_windows")
    return tuple(limitations)


def evidence_cap(time_model: str) -> str:
    """Return the strongest evidence level a route can reach.

    Compatibility wrapper for the one-field form; the derivation lives in
    :func:`evidence_cap_for`.
    """

    return evidence_cap_for(_legacy_capabilities(time_model))


def cap_evidence_level(level: str, time_model: str) -> str:
    """Return the weaker of ``level`` and the cap of ``time_model``."""

    return cap_evidence_level_for(level, _legacy_capabilities(time_model))


def formal_comparison_allowed(time_model: str) -> bool:
    """Return True only for the kernel-stepped route.

    Unknown routes are not allowed to claim formal comparison; the predicate is
    total and fail-closed rather than raising.
    """

    return isinstance(time_model, str) and time_model == TIME_MODEL_STEPPED


def route_limitations(time_model: str) -> tuple[str, ...]:
    """Return the limitations that come with a route, empty for stepped.

    Compatibility wrapper: it reports the route-inherent subset only, because
    the one-field form has no control or event declaration to report.
    """

    return route_inherent_limitations_for(_legacy_capabilities(time_model))
