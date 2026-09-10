"""Extensible, structured safety and data-quality checks.

The runner treats a check failure as evidence, not as a free-form message.
Plugins may be small project-local objects; only the method matching their
phase is required.  The normalisation helpers keep legacy boolean/tuple
callbacks usable while giving new plugins a stable result shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Mapping, Protocol


@dataclass(frozen=True)
class CheckContext:
    """Read-only context supplied to a check plugin."""

    spec: Any = None
    step_index: int | None = None
    current_time_s: float | None = None
    previous_observation_time_s: float | None = None
    primary_measurement: str | None = None
    phase: str = "runtime"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckFinding:
    """One machine-readable check result."""

    rule_id: str
    category: str
    message: str
    severity: str = "error"
    passed: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)
    # Warnings and informational findings are diagnostic by default.  Errors
    # and fatals remain fail-closed even when a poorly behaved plugin tries to
    # set this to false.
    stop_requested: bool = False
    plugin_id: str | None = None

    def __post_init__(self) -> None:
        if not self.rule_id or not self.category or not self.message:
            raise ValueError("check findings require rule_id, category, and message")
        if self.severity not in {"info", "warning", "error", "fatal"}:
            raise ValueError("unsupported check finding severity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "message": self.message,
            "severity": self.severity,
            "passed": self.passed,
            "evidence": dict(self.evidence),
            "stop_requested": self.stop_requested,
            "plugin_id": self.plugin_id,
        }

    @property
    def blocking(self) -> bool:
        return not self.passed and (self.stop_requested or self.severity in {"error", "fatal"})


@dataclass(frozen=True)
class CheckReport:
    """Aggregate result returned by one plugin or a plugin group."""

    passed: bool
    findings: tuple[CheckFinding, ...] = ()
    plugin_id: str | None = None
    rule_version: str = "checks-v1"

    @classmethod
    def ok(cls, *, plugin_id: str | None = None) -> "CheckReport":
        return cls(True, (), plugin_id)

    @classmethod
    def failure(
        cls,
        category: str,
        message: str,
        *,
        rule_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        plugin_id: str | None = None,
        severity: str = "error",
        stop_requested: bool = False,
    ) -> "CheckReport":
        finding = CheckFinding(
            rule_id=rule_id or category,
            category=category,
            message=message,
            severity=severity,
            evidence=dict(evidence or {}),
            stop_requested=stop_requested,
            plugin_id=plugin_id,
        )
        return cls(not finding.blocking, (finding,), plugin_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            # ``passed`` means no finding requires runtime/qualification
            # rejection. Diagnostics remain in ``findings`` for warnings.
            "passed": not self.blocking,
            "rule_version": self.rule_version,
            "plugin_id": self.plugin_id,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @property
    def reasons(self) -> list[str]:
        reasons = [finding.category for finding in self.findings if not finding.passed]
        return reasons or (["check_failed"] if not self.passed else [])

    @property
    def blocking(self) -> bool:
        return any(finding.blocking for finding in self.findings)

    def merge(self, other: "CheckReport") -> "CheckReport":
        return CheckReport(
            not (self.blocking or other.blocking),
            self.findings + other.findings,
            self.plugin_id,
            self.rule_version,
        )


# Short alias for plugin authors who prefer the result-oriented name.
CheckResult = CheckReport


class SafetyPlugin(Protocol):
    plugin_id: str
    rule_version: str

    def check_observation(self, observation: Any, context: CheckContext) -> Any: ...


class ValidityPlugin(Protocol):
    plugin_id: str
    rule_version: str

    def check_observation(self, observation: Any, context: CheckContext) -> Any: ...


class QualificationPlugin(Protocol):
    plugin_id: str
    rule_version: str

    def check_samples(self, samples: list[Mapping[str, Any]], context: CheckContext) -> Any: ...


class CheckFailureError(RuntimeError):
    """Raised when a machine check requires fail-closed termination."""

    category = "check_failed"

    def __init__(self, message: str, report: CheckReport, *, phase: str):
        self.report = report
        self.phase = phase
        finding = next((item for item in report.findings if not item.passed), None)
        self.finding = finding
        if finding is not None:
            self.category = finding.category
        super().__init__(message)


class SafetyCheckError(CheckFailureError):
    category = "safety_violation"


class DataValidityError(CheckFailureError):
    category = "data_invalid"


class QualificationCheckError(CheckFailureError):
    category = "qualification_failure"


def plugin_identity(plugin: Any) -> dict[str, str]:
    """Return stable human/machine labels without requiring JSON support."""

    plugin_id = str(getattr(plugin, "plugin_id", getattr(plugin, "name", type(plugin).__qualname__)))
    version = str(getattr(plugin, "rule_version", getattr(plugin, "version", "checks-v1")))
    return {"id": plugin_id, "version": version}


def normalise_check_result(result: Any, *, plugin_id: str | None = None) -> CheckReport:
    """Accept a CheckReport plus the legacy callback shapes."""

    if result is None:
        return CheckReport.ok(plugin_id=plugin_id)
    if isinstance(result, CheckReport):
        findings = tuple(
            CheckFinding(
                rule_id=finding.rule_id,
                category=finding.category,
                message=finding.message,
                severity=finding.severity,
                passed=finding.passed,
                evidence=finding.evidence,
                stop_requested=finding.stop_requested,
                plugin_id=finding.plugin_id or plugin_id,
            )
            for finding in result.findings
        )
        if not result.passed and not findings:
            raise ValueError("check report with passed=false must include at least one finding")
        normalized = CheckReport(
            not any(finding.blocking for finding in findings),
            findings,
            result.plugin_id or plugin_id,
            result.rule_version,
        )
        if bool(result.passed) != normalized.passed:
            raise ValueError("check report passed flag is inconsistent with its findings")
        return normalized
    if isinstance(result, CheckFinding):
        finding = result
        if finding.plugin_id is None and plugin_id is not None:
            finding = CheckFinding(
                rule_id=finding.rule_id,
                category=finding.category,
                message=finding.message,
                severity=finding.severity,
                passed=finding.passed,
                evidence=finding.evidence,
                stop_requested=finding.stop_requested,
                plugin_id=plugin_id,
            )
        return CheckReport(not finding.blocking, (finding,), plugin_id or finding.plugin_id)
    if isinstance(result, bool):
        return CheckReport.ok(plugin_id=plugin_id) if result else CheckReport.failure(
            "check_failed", "plugin returned false", plugin_id=plugin_id
        )
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        passed, reasons = result
        if passed:
            return CheckReport.ok(plugin_id=plugin_id)
        reason_values = [reasons] if isinstance(reasons, (str, bytes)) else (reasons or [])
        reason_list = [str(item) for item in reason_values]
        findings = tuple(
            CheckFinding(
                rule_id="legacy_checker",
                category=reason.split(":", 1)[0] or "check_failed",
                message=reason,
                plugin_id=plugin_id,
            )
            for reason in reason_list
        ) or (CheckFinding("legacy_checker", "check_failed", "plugin returned false", plugin_id=plugin_id),)
        return CheckReport(False, findings, plugin_id)
    if isinstance(result, Mapping):
        passed = bool(result.get("passed", False))
        if passed:
            return CheckReport.ok(plugin_id=plugin_id)
        return CheckReport.failure(
            str(result.get("category", "check_failed")),
            str(result.get("message", "plugin returned a failed result")),
            rule_id=str(result.get("rule_id", result.get("category", "check_failed"))),
            evidence=result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {},
            plugin_id=plugin_id,
        )
    raise TypeError("check plugin must return CheckReport, CheckFinding, bool, or (bool, reasons)")


def run_plugin(plugin: Any, method: str, subject: Any, context: CheckContext) -> CheckReport:
    """Invoke a plugin method, failing closed when its implementation errors."""

    identity = plugin_identity(plugin)
    fn = getattr(plugin, method, None) or getattr(plugin, "check", None)
    if fn is None:
        # A plugin can be shared between phases; an absent phase is a no-op.
        return CheckReport.ok(plugin_id=identity["id"])
    try:
        result = fn(subject, context)
    except Exception as exc:  # plugin code is untrusted extension code
        return CheckReport.failure(
            "checker_error",
            f"{identity['id']} raised {type(exc).__name__}: {exc}",
            rule_id=f"{identity['id']}.execution",
            plugin_id=identity["id"],
            evidence={"exception_type": type(exc).__name__},
        )
    return normalise_check_result(result, plugin_id=identity["id"])


def run_plugins(plugins: Iterable[Any], method: str, subject: Any, context: CheckContext) -> CheckReport:
    report = CheckReport.ok()
    for plugin in plugins:
        report = report.merge(run_plugin(plugin, method, subject, context))
    return report


@dataclass(frozen=True)
class ObservationValidityPlugin:
    """General-purpose observation integrity checks.

    Empty ``expected_units`` and ``ranges`` keep the default compatible with
    simple models.  ``max_age_steps=0`` means a controller may only consume a
    current observation unless a caller explicitly opts into stale data.
    """

    required_measurements: tuple[str, ...] = ()
    max_age_steps: int | None = 0
    expected_units: Mapping[str, str] = field(default_factory=dict)
    ranges: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)
    plugin_id: str = "observation-validity"
    rule_version: str = "observation-validity-v1"

    def __post_init__(self) -> None:
        if self.max_age_steps is not None and (isinstance(self.max_age_steps, bool) or int(self.max_age_steps) < 0):
            raise ValueError("max_age_steps must be non-negative or null")
        for key, bounds in self.ranges.items():
            if len(bounds) != 2:
                raise ValueError(f"range for {key} must be (minimum, maximum)")
            minimum, maximum = bounds
            if minimum is not None and not math.isfinite(float(minimum)):
                raise ValueError(f"minimum range for {key} must be finite")
            if maximum is not None and not math.isfinite(float(maximum)):
                raise ValueError(f"maximum range for {key} must be finite")
            if minimum is not None and maximum is not None and float(minimum) > float(maximum):
                raise ValueError(f"range for {key} is not ordered")

    def check_observation(self, observation: Any, context: CheckContext) -> CheckReport:
        findings: list[CheckFinding] = []
        measurement = getattr(observation, "measurement", None)
        if not isinstance(measurement, Mapping):
            return CheckReport.failure(
                "missing_measurement", "observation.measurement must be a mapping", plugin_id=self.plugin_id
            )
        for key in self.required_measurements:
            if key not in measurement:
                findings.append(CheckFinding("measurement.required", "missing_measurement", f"required measurement is missing: {key}", evidence={"key": key}, plugin_id=self.plugin_id))
        for key, value in measurement.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                findings.append(CheckFinding("measurement.numeric", "invalid_measurement", f"measurement is not numeric: {key}", evidence={"key": str(key), "value": repr(value)}, plugin_id=self.plugin_id))
                continue
            if not math.isfinite(numeric):
                findings.append(CheckFinding("measurement.finite", "nonfinite_observation", f"measurement is not finite: {key}", evidence={"key": str(key), "value": repr(value)}, plugin_id=self.plugin_id))
                continue
            if key in self.ranges:
                minimum, maximum = self.ranges[key]
                if minimum is not None and numeric < float(minimum) or maximum is not None and numeric > float(maximum):
                    findings.append(CheckFinding("measurement.range", "measurement_out_of_range", f"measurement is outside the declared range: {key}", evidence={"key": str(key), "value": numeric, "minimum": minimum, "maximum": maximum}, plugin_id=self.plugin_id))
        try:
            timestamp = float(getattr(observation, "time_s"))
        except (TypeError, ValueError, AttributeError):
            timestamp = math.nan
        if not math.isfinite(timestamp):
            findings.append(CheckFinding("observation.timestamp.finite", "timestamp_violation", "observation timestamp is not finite", plugin_id=self.plugin_id))
        else:
            previous = context.previous_observation_time_s
            if previous is not None and timestamp < float(previous) - 1e-12:
                findings.append(CheckFinding("observation.timestamp.monotonic", "timestamp_violation", "observation timestamp regressed", evidence={"time_s": timestamp, "previous_time_s": previous}, plugin_id=self.plugin_id))
            current = context.current_time_s
            if current is not None:
                if timestamp < float(current) - 1e-12:
                    findings.append(CheckFinding("observation.timestamp.rollback", "timestamp_violation", "observation timestamp is before the current logical time", evidence={"time_s": timestamp, "current_time_s": current}, plugin_id=self.plugin_id))
                elif timestamp > float(current) + 1e-12:
                    findings.append(CheckFinding("observation.timestamp.visible", "future_observation", "observation timestamp is in the future", evidence={"time_s": timestamp, "current_time_s": current}, plugin_id=self.plugin_id))
        try:
            age_steps = int(getattr(observation, "age_steps", 0))
            if age_steps < 0:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            findings.append(CheckFinding("observation.age.valid", "invalid_observation_age", "observation age_steps is invalid", plugin_id=self.plugin_id))
        else:
            if self.max_age_steps is not None and age_steps > int(self.max_age_steps):
                findings.append(CheckFinding("observation.age.max", "stale_observation", "observation is older than the allowed age", evidence={"age_steps": age_steps, "max_age_steps": self.max_age_steps}, plugin_id=self.plugin_id))
        units = getattr(observation, "measurement_units", {})
        if not isinstance(units, Mapping):
            findings.append(CheckFinding("measurement.units.mapping", "unit_mismatch", "measurement_units must be a mapping", plugin_id=self.plugin_id))
        else:
            for key, expected in self.expected_units.items():
                actual = units.get(key)
                if actual != expected:
                    findings.append(CheckFinding("measurement.units.expected", "unit_mismatch", f"measurement unit mismatch: {key}", evidence={"key": key, "expected": expected, "actual": actual}, plugin_id=self.plugin_id))
        return CheckReport(not findings, tuple(findings), self.plugin_id, self.rule_version)


def first_failure(report: CheckReport) -> CheckFinding | None:
    return next((finding for finding in report.findings if not finding.passed), None)


__all__ = [
    "CheckContext",
    "CheckFinding",
    "CheckReport",
    "CheckResult",
    "SafetyPlugin",
    "ValidityPlugin",
    "QualificationPlugin",
    "CheckFailureError",
    "SafetyCheckError",
    "DataValidityError",
    "QualificationCheckError",
    "ObservationValidityPlugin",
    "plugin_identity",
    "normalise_check_result",
    "run_plugin",
    "run_plugins",
    "first_failure",
]
