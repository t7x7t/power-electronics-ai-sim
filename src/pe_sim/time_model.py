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

TRACE_CONTRACT_SCHEMA = "pe-sim.trace-contract.v1"

#: The runtime evidence vocabulary recorded in ``manifest["evidence_level"]``.
#: This is deliberately not the Stage 12 classification vocabulary
#: (``docs/22``), which has its own axes such as ``mechanism``.
RUNTIME_EVIDENCE_LEVELS = ("diagnostic_only", "functional", "limited", "comparable")

_EVIDENCE_RANK = {level: rank for rank, level in enumerate(RUNTIME_EVIDENCE_LEVELS)}

_ROUTE_EVIDENCE_CAP = {
    TIME_MODEL_STEPPED: "comparable",
    TIME_MODEL_WHOLE_RUN: "limited",
}

_ROUTE_LIMITATIONS = {
    TIME_MODEL_STEPPED: (),
    TIME_MODEL_WHOLE_RUN: (
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    ),
}

__all__ = [
    "RUNTIME_EVIDENCE_LEVELS",
    "TIME_MODELS",
    "TIME_MODEL_STEPPED",
    "TIME_MODEL_WHOLE_RUN",
    "TRACE_CONTRACT_SCHEMA",
    "TraceContract",
    "WholeRunPlant",
    "WholeRunRequest",
    "WholeRunResult",
    "cap_evidence_level",
    "evidence_cap",
    "formal_comparison_allowed",
    "route_limitations",
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


def evidence_cap(time_model: str) -> str:
    """Return the strongest evidence level a route can reach."""

    try:
        return _ROUTE_EVIDENCE_CAP[time_model]
    except (KeyError, TypeError):
        raise ValueError(f"unknown time model: {time_model!r}") from None


def cap_evidence_level(level: str, time_model: str) -> str:
    """Return the weaker of ``level`` and the cap of ``time_model``."""

    try:
        rank = _EVIDENCE_RANK[level]
    except (KeyError, TypeError):
        raise ValueError(f"unknown evidence level: {level!r}") from None
    cap = evidence_cap(time_model)
    if rank <= _EVIDENCE_RANK[cap]:
        return level
    return cap


def formal_comparison_allowed(time_model: str) -> bool:
    """Return True only for the kernel-stepped route.

    Unknown routes are not allowed to claim formal comparison; the predicate is
    total and fail-closed rather than raising.
    """

    return isinstance(time_model, str) and time_model == TIME_MODEL_STEPPED


def route_limitations(time_model: str) -> tuple[str, ...]:
    """Return the limitations that come with a route, empty for stepped."""

    try:
        return _ROUTE_LIMITATIONS[time_model]
    except (KeyError, TypeError):
        raise ValueError(f"unknown time model: {time_model!r}") from None
