"""Standard post-run qualification rules.

The baseline qualification plugin answers only "are the samples structurally
usable?".  These rules add three independent questions:

- did the trajectory reach a settled window?
- does the settled mean agree with the plant's own analytical equilibrium?
- did the solver report recovered numerical difficulties?

A rule never turns a warning into a blocking failure unless it is explicitly
configured to do so.  Warning-only evidence produces ``level="partial"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any, Callable, Mapping, Protocol, Sequence

from .checks import CheckContext, CheckFinding, CheckReport
from .jsonutil import json_safe


@dataclass(frozen=True)
class PlantOracle:
    """A narrow read-only view of a Plant for post-run rules.

    The bound methods are captured at construction time.  Rules never hold the
    Plant object itself, so evidence cannot leak a live object and a rule
    cannot accidentally advance the model or invoke the solver.
    """

    plant_id: str | None = None
    topology: str | None = None
    identity_sha256: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    envelope: Mapping[str, Any] = field(default_factory=dict)
    _equilibrium: Callable[[float], Mapping[str, float]] | None = field(
        default=None, repr=False, compare=False
    )
    _health: Callable[[], Mapping[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def from_plant(cls, plant: Any, *, plant_id: str | None = None) -> "PlantOracle":
        identity: Mapping[str, Any] = {}
        identity_fn = getattr(plant, "manifest_identity", None)
        if callable(identity_fn):
            try:
                value = identity_fn()
                if isinstance(value, Mapping):
                    identity = value
            except Exception:
                identity = {}
        envelope: Mapping[str, Any] = {}
        envelope_fn = getattr(plant, "validity_envelope", None)
        if callable(envelope_fn):
            try:
                value = envelope_fn()
                if isinstance(value, Mapping):
                    envelope = value
                elif hasattr(value, "to_dict"):
                    converted = value.to_dict()
                    if isinstance(converted, Mapping):
                        envelope = converted
            except Exception:
                envelope = {}
        parameters = identity.get("parameters")
        parameters_map = dict(parameters) if isinstance(parameters, Mapping) else {}
        static = envelope.get("static_parameters")
        if isinstance(static, Mapping):
            for key, value in static.items():
                parameters_map.setdefault(str(key), value)
        equilibrium = getattr(plant, "analytical_steady_state", None)
        health = getattr(plant, "backend_evidence", None)
        return cls(
            plant_id=plant_id or identity.get("id"),
            topology=identity.get("topology") or envelope.get("topology"),
            identity_sha256=identity.get("sha256"),
            parameters=json_safe(parameters_map),
            envelope=json_safe(envelope),
            _equilibrium=equilibrium if callable(equilibrium) else None,
            _health=health if callable(health) else None,
        )

    @property
    def has_equilibrium(self) -> bool:
        return self._equilibrium is not None

    def analytical_steady_state(self, duty: float) -> Mapping[str, float] | None:
        if self._equilibrium is None:
            return None
        value = self._equilibrium(float(duty))
        return value if isinstance(value, Mapping) else None

    def numerical_health_evidence(self) -> Mapping[str, Any] | None:
        if self._health is None:
            return None
        try:
            value = self._health()
        except Exception:
            return None
        if not isinstance(value, Mapping):
            return None
        health = value.get("numerical_health")
        return health if isinstance(health, Mapping) else None


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    status: str  # pass | warn | not_applicable | fail
    category: str
    message: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "not_applicable", "fail"}:
            raise ValueError(f"unsupported rule status: {self.status!r}")

    def to_finding(self, *, plugin_id: str) -> CheckFinding:
        severity, passed = {
            "pass": ("info", True),
            "not_applicable": ("info", True),
            "warn": ("warning", True),
            "fail": ("error", False),
        }[self.status]
        return CheckFinding(
            rule_id=self.rule_id,
            category=self.category,
            message=self.message,
            severity=severity,
            passed=passed,
            evidence={**json_safe(self.evidence), "status": self.status},
            plugin_id=plugin_id,
        )


def _rows(samples: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in samples if isinstance(row, Mapping)]


def _time_values(rows: Sequence[Mapping[str, Any]], time_key: str) -> list[float] | None:
    values: list[float] = []
    for row in rows:
        value = row.get(time_key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        if values and number < values[-1]:
            return None
        values.append(number)
    return values


def _tail_window(rows: Sequence[Mapping[str, Any]], *, fraction: float, minimum: int) -> list[Mapping[str, Any]]:
    count = max(minimum, int(math.ceil(len(rows) * fraction)))
    return list(rows[max(0, len(rows) - count) :])


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _signal_values(window: Sequence[Mapping[str, Any]], signal: str) -> list[float] | None:
    values: list[float] = []
    for row in window:
        value = row.get(signal)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values.append(number)
    return values


def _least_squares_slope(times: Sequence[float], values: Sequence[float]) -> float:
    mean_time = _mean(times)
    mean_value = _mean(values)
    denominator = sum((time - mean_time) ** 2 for time in times)
    if denominator <= 0.0:
        return 0.0
    return sum((time - mean_time) * (value - mean_value) for time, value in zip(times, values)) / denominator


@dataclass(frozen=True)
class SteadyStateThresholds:
    signals: tuple[str, ...] = ("vout",)
    window_fraction: float = 0.25
    minimum_window_samples: int = 8
    minimum_run_samples: int = 16
    max_relative_slope: float = 0.01
    max_relative_ripple: float = 0.20
    absolute_floor: Mapping[str, float] = field(default_factory=dict)
    on_violation: str = "warn"
    time_key: str = "time_s"

    def __post_init__(self) -> None:
        if self.on_violation not in {"warn", "fail"}:
            raise ValueError("on_violation must be 'warn' or 'fail'")
        if not 0.0 < float(self.window_fraction) <= 1.0:
            raise ValueError("window_fraction must be in (0, 1]")
        for name, value in (
            ("minimum_window_samples", self.minimum_window_samples),
            ("minimum_run_samples", self.minimum_run_samples),
        ):
            if isinstance(value, bool) or int(value) <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SteadyStateRule:
    plugin_id: str = "steady-state-rule"
    rule_version: str = "steady-state-v1"
    rule_id: str = "qualification.steady_state"
    thresholds: SteadyStateThresholds = SteadyStateThresholds()

    def evaluate(
        self,
        samples: Sequence[Mapping[str, Any]],
        context: CheckContext,
        oracle: PlantOracle | None,
    ) -> RuleOutcome:
        del context
        rows = _rows(samples)
        thresholds = self.thresholds
        if len(rows) < thresholds.minimum_run_samples:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "the run is too short for a settled-window assessment",
                {"reason": "insufficient_samples", "sample_count": len(rows)},
            )
        times = _time_values(rows, thresholds.time_key)
        if times is None:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "the sample time axis is not usable for a settled-window assessment",
                {"reason": "invalid_timebase"},
            )
        window = _tail_window(
            rows,
            fraction=thresholds.window_fraction,
            minimum=thresholds.minimum_window_samples,
        )
        if len(window) < thresholds.minimum_window_samples:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "the settled window contains too few samples",
                {"reason": "insufficient_settled_window", "window_samples": len(window)},
            )
        window_times = times[-len(window):]
        span = window_times[-1] - window_times[0]
        if span <= 0.0:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "the settled window has no time span",
                {"reason": "zero_time_span"},
            )

        switching_frequency = None
        if oracle is not None and isinstance(oracle.parameters, Mapping):
            value = oracle.parameters.get("switching_frequency_hz")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0:
                switching_frequency = float(value)
        if switching_frequency is not None and span < 2.0 / switching_frequency:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "the settled window is shorter than two switching periods",
                {
                    "reason": "window_shorter_than_switching_period",
                    "window_span_s": span,
                    "switching_period_s": 1.0 / switching_frequency,
                },
            )

        metrics: dict[str, Any] = {}
        violations: list[str] = []
        assessed = 0
        for signal in thresholds.signals:
            values = _signal_values(window, signal)
            if values is None:
                continue
            scale = max(abs(_mean(values)), float(thresholds.absolute_floor.get(signal, 0.0)))
            if scale <= 0.0:
                continue
            assessed += 1
            slope = _least_squares_slope(window_times, values)
            relative_slope = abs(slope) * span / scale
            relative_ripple = (max(values) - min(values)) / (2.0 * scale)
            passed = (
                relative_slope <= thresholds.max_relative_slope
                and relative_ripple <= thresholds.max_relative_ripple
            )
            metrics[signal] = {
                "mean": _mean(values),
                "minimum": min(values),
                "maximum": max(values),
                "slope_per_s": slope,
                "relative_slope": relative_slope,
                "relative_ripple": relative_ripple,
                "passed": passed,
            }
            if not passed:
                violations.append(signal)

        if assessed == 0:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "steady_state_unassessed",
                "none of the declared steady-state signals is present",
                {"reason": "signal_missing", "signals": list(thresholds.signals)},
            )

        evidence = {
            "window": {
                "start_index": len(rows) - len(window),
                "end_index": len(rows) - 1,
                "t_start_s": window_times[0],
                "t_end_s": window_times[-1],
                "span_s": span,
                "sample_count": len(window),
            },
            "thresholds": {
                "max_relative_slope": thresholds.max_relative_slope,
                "max_relative_ripple": thresholds.max_relative_ripple,
            },
            "signals": metrics,
            "violating_signals": violations,
        }
        if not violations:
            return RuleOutcome(
                self.rule_id,
                "pass",
                "steady_state",
                "the settled window meets the declared steady-state thresholds",
                evidence,
            )
        status = "fail" if thresholds.on_violation == "fail" else "warn"
        return RuleOutcome(
            self.rule_id,
            status,
            "steady_state_not_reached",
            "the settled window does not meet the declared steady-state thresholds",
            evidence,
        )


@dataclass(frozen=True)
class EquilibriumTolerance:
    relative: float = 0.05
    absolute: float = 0.02

    def __post_init__(self) -> None:
        for name, value in (("relative", self.relative), ("absolute", self.absolute)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} tolerance must be finite")
            if float(value) < 0.0:
                raise ValueError(f"{name} tolerance must be non-negative")


@dataclass(frozen=True)
class AnalyticalEquilibriumRule:
    plugin_id: str = "analytical-equilibrium-rule"
    rule_version: str = "analytical-equilibrium-v1"
    rule_id: str = "qualification.analytical_equilibrium"
    signal_map: Mapping[str, str] = field(
        default_factory=lambda: {
            "output_voltage_v": "vout",
            "inductor_current_a": "inductor_current",
        }
    )
    tolerances: Mapping[str, EquilibriumTolerance] = field(default_factory=dict)
    window_fraction: float = 0.25
    minimum_window_samples: int = 8
    action_key: str = "action"
    duty_stability_tolerance: float = 1e-6
    on_violation: str = "warn"
    time_key: str = "time_s"

    def __post_init__(self) -> None:
        if self.on_violation not in {"warn", "fail"}:
            raise ValueError("on_violation must be 'warn' or 'fail'")

    def evaluate(
        self,
        samples: Sequence[Mapping[str, Any]],
        context: CheckContext,
        oracle: PlantOracle | None,
    ) -> RuleOutcome:
        del context
        if oracle is None or not oracle.has_equilibrium:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the plant does not expose an analytical steady state",
                {"reason": "no_plant_oracle"},
            )
        rows = _rows(samples)
        times = _time_values(rows, self.time_key)
        if times is None:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the sample time axis is not usable",
                {"reason": "invalid_timebase"},
            )
        window = _tail_window(
            rows,
            fraction=self.window_fraction,
            minimum=self.minimum_window_samples,
        )
        if len(window) < self.minimum_window_samples:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the settled window contains too few samples",
                {"reason": "insufficient_settled_window", "window_samples": len(window)},
            )
        actions = _signal_values(window, self.action_key)
        if actions is None:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the action signal is not available",
                {"reason": "signal_missing", "signal": self.action_key},
            )
        duty = _mean(actions)
        duty_spread = max(actions) - min(actions)
        try:
            reference = oracle.analytical_steady_state(duty)
        except Exception as exc:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the analytical equilibrium is undefined at this duty ratio",
                {
                    "reason": "oracle_undefined",
                    "duty": duty,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
            )
        if reference is None:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "the analytical equilibrium returned no value",
                {"reason": "oracle_undefined", "duty": duty},
            )

        metrics: dict[str, Any] = {}
        violations: list[str] = []
        assessed = 0
        for oracle_key, signal in self.signal_map.items():
            if oracle_key not in reference or signal not in (window[0] if window else {}):
                continue
            values = _signal_values(window, signal)
            if values is None:
                continue
            assessed += 1
            expected_value = float(reference[oracle_key])
            measured = _mean(values)
            tolerance = self.tolerances.get(oracle_key, EquilibriumTolerance())
            limit = max(float(tolerance.absolute), float(tolerance.relative) * abs(expected_value))
            error = abs(measured - expected_value)
            passed = error <= limit
            metrics[signal] = {
                "oracle_key": oracle_key,
                "measured_mean": measured,
                "reference": expected_value,
                "error": error,
                "limit": limit,
                "tolerance": {
                    "relative": tolerance.relative,
                    "absolute": tolerance.absolute,
                },
                "passed": passed,
            }
            if not passed:
                violations.append(signal)

        if assessed == 0:
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "analytical_equilibrium_not_applicable",
                "none of the declared equilibrium signals is present",
                {"reason": "signal_missing", "signal_map": dict(self.signal_map)},
            )

        evidence = {
            "duty": duty,
            "duty_spread": duty_spread,
            "window_sample_count": len(window),
            "signals": metrics,
            "violating_signals": violations,
        }
        if duty_spread > self.duty_stability_tolerance:
            return RuleOutcome(
                self.rule_id,
                "warn",
                "duty_varying",
                "the settled window contains a changing duty ratio; the analytical equilibrium is only indicative",
                evidence,
            )
        if violations:
            status = "fail" if self.on_violation == "fail" else "warn"
            return RuleOutcome(
                self.rule_id,
                status,
                "analytical_equilibrium_mismatch",
                "the settled mean does not agree with the analytical equilibrium",
                evidence,
            )
        return RuleOutcome(
            self.rule_id,
            "pass",
            "analytical_equilibrium",
            "the settled mean agrees with the analytical equilibrium",
            evidence,
        )


@dataclass(frozen=True)
class NumericalHealthRule:
    plugin_id: str = "numerical-health-rule"
    rule_version: str = "numerical-health-v1"
    rule_id: str = "qualification.numerical_health"
    unknown_status: str = "not_applicable"

    def __post_init__(self) -> None:
        if self.unknown_status not in {"not_applicable", "warn"}:
            raise ValueError("unknown_status must be 'not_applicable' or 'warn'")

    def evaluate(
        self,
        samples: Sequence[Mapping[str, Any]],
        context: CheckContext,
        oracle: PlantOracle | None,
    ) -> RuleOutcome:
        del samples
        health = None
        if oracle is not None:
            health = oracle.numerical_health_evidence()
        if health is None:
            metadata = getattr(context, "metadata", None)
            if isinstance(metadata, Mapping):
                candidate = metadata.get("backend_numerical_health")
                if isinstance(candidate, Mapping):
                    health = candidate
        if health is None:
            if self.unknown_status == "warn":
                return RuleOutcome(
                    self.rule_id,
                    "warn",
                    "numerical_health_unknown",
                    "no numerical-health evidence was recorded for this backend",
                    {"reason": "numerical_health_unknown"},
                )
            return RuleOutcome(
                self.rule_id,
                "not_applicable",
                "numerical_health_not_applicable",
                "no numerical-health evidence was recorded for this backend",
                {"reason": "numerical_health_unknown"},
            )
        severity = str(health.get("severity", "unknown"))
        evidence = json_safe(health)
        if severity == "clean":
            return RuleOutcome(
                self.rule_id,
                "pass",
                "numerical_health",
                "the solver log contains no classified warnings or fatal errors",
                evidence,
            )
        if severity == "fatal":
            return RuleOutcome(
                self.rule_id,
                "fail",
                "numerical_health_fatal",
                "the solver log contains a fatal numerical category",
                evidence,
            )
        if severity == "warning":
            return RuleOutcome(
                self.rule_id,
                "warn",
                "numerical_health_warning",
                "the solver recovered from one or more numerical warnings",
                evidence,
            )
        return RuleOutcome(
            self.rule_id,
            "warn" if self.unknown_status == "warn" else "not_applicable",
            "numerical_health_unknown",
            "the numerical-health evidence has no recognised severity",
            evidence,
        )


class QualificationRule(Protocol):
    plugin_id: str
    rule_version: str
    rule_id: str

    def evaluate(
        self,
        samples: Sequence[Mapping[str, Any]],
        context: CheckContext,
        oracle: PlantOracle | None,
    ) -> RuleOutcome: ...


@dataclass(frozen=True)
class QualificationRulePack:
    """A composable plugin implementing the existing QualificationPlugin protocol."""

    plugin_id: str = "qualification-rule-pack"
    rule_version: str = "qualification-pack-v1"
    rules: tuple[QualificationRule, ...] = ()
    oracle: PlantOracle | None = None

    @classmethod
    def standard(
        cls,
        *,
        plant: Any = None,
        oracle: PlantOracle | None = None,
        steady_state: SteadyStateRule | None = None,
        equilibrium: AnalyticalEquilibriumRule | None = None,
        numerical_health: NumericalHealthRule | None = None,
        include: Sequence[str] = (
            "steady_state",
            "analytical_equilibrium",
            "numerical_health",
        ),
    ) -> "QualificationRulePack":
        selected: list[QualificationRule] = []
        if "steady_state" in include:
            selected.append(steady_state or SteadyStateRule())
        if "analytical_equilibrium" in include:
            selected.append(equilibrium or AnalyticalEquilibriumRule())
        if "numerical_health" in include:
            selected.append(numerical_health or NumericalHealthRule())
        resolved = oracle
        if resolved is None and plant is not None:
            resolved = PlantOracle.from_plant(plant)
        return cls(rules=tuple(selected), oracle=resolved)

    def check_samples(self, samples: list[Mapping[str, Any]], context: CheckContext) -> CheckReport:
        findings: list[CheckFinding] = []
        for rule in self.rules:
            try:
                outcome = rule.evaluate(samples, context, self.oracle)
            except Exception as exc:  # rule code is an extension boundary
                outcome = RuleOutcome(
                    getattr(rule, "rule_id", type(rule).__qualname__),
                    "fail",
                    "rule_error",
                    f"{type(exc).__name__}: {exc}",
                    {"exception_type": type(exc).__name__},
                )
            findings.append(outcome.to_finding(plugin_id=self.plugin_id))
        limitations = tuple(
            dict.fromkeys(
                finding.category
                for finding in findings
                if finding.severity == "warning" and not finding.blocking
            )
        )
        return CheckReport(
            passed=not any(finding.blocking for finding in findings),
            findings=tuple(findings),
            plugin_id=self.plugin_id,
            rule_version=self.rule_version,
            limitations=limitations,
        )


__all__ = [
    "AnalyticalEquilibriumRule",
    "EquilibriumTolerance",
    "NumericalHealthRule",
    "PlantOracle",
    "QualificationRule",
    "QualificationRulePack",
    "RuleOutcome",
    "SteadyStateRule",
    "SteadyStateThresholds",
]
