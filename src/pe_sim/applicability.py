"""Applicability checks for declared model envelopes.

The plugin answers a different question from the safety and validity plugins:

- safety asks "is this action allowed?";
- validity asks "is this sample structurally usable?";
- applicability asks "is the model declared to apply at this operating point?".

Findings are policy-driven.  Exploratory runs record them, formal comparison
runs may enforce the ``hard`` limits.  An absent declaration is reported as
``unknown`` and never invented.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import math
from typing import Any, Mapping

from .checks import CheckContext, CheckFailureError, CheckFinding, CheckReport
from .jsonutil import json_safe, stable_sha256
from .model_claim import (
    MODEL_CLAIM_RULE_VERSION,
    ModelClaim,
    ParameterProvenance,
    collect_model_claim,
    collect_parameter_provenance,
    normalise_model_claim,
    normalise_parameter_provenance,
)


APPLICABILITY_SCHEMA_VERSION = "applicability-v1"
APPLICABILITY_MODES = ("default", "off", "record", "warn", "enforce")
APPLICABILITY_STATUSES = (
    "disabled",
    "unknown",
    "within",
    "limited",
    "violated",
    "declaration_invalid",
)


class ApplicabilityError(CheckFailureError):
    """Raised when the configured policy makes an envelope finding blocking."""

    category = "out_of_envelope"


@dataclass(frozen=True)
class ApplicabilityPolicy:
    """Policy controlling how envelope findings affect a run."""

    mode: str = "default"
    blocking_strengths: tuple[str, ...] = ("hard",)
    preflight_checks: bool = True
    step_checks: bool = True
    check_external_inputs: bool = True
    report_unknown_envelope: bool = True
    require_envelope: bool = False
    require_parameter_provenance: bool = False
    deduplicate: bool = True
    relative_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.mode not in APPLICABILITY_MODES:
            raise ValueError(f"unsupported applicability mode: {self.mode!r}")
        if isinstance(self.blocking_strengths, (str, bytes)):
            raise TypeError("blocking_strengths must be an array")
        unknown = set(self.blocking_strengths) - {"hard", "advisory"}
        if unknown:
            raise ValueError(f"unsupported blocking strengths: {sorted(unknown)}")
        if isinstance(self.relative_tolerance, bool) or not isinstance(self.relative_tolerance, (int, float)):
            raise TypeError("relative_tolerance must be a number")
        if not math.isfinite(float(self.relative_tolerance)) or float(self.relative_tolerance) < 0.0:
            raise ValueError("relative_tolerance must be finite and non-negative")

    def resolve(self, run_mode: str) -> "ApplicabilityPolicy":
        """Resolve ``default`` into the per-mode policy."""

        if self.mode != "default":
            return self
        if run_mode == "formal_comparison":
            return replace(self, mode="enforce")
        return replace(self, mode="record")

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "blocking_strengths": list(self.blocking_strengths),
            "preflight_checks": bool(self.preflight_checks),
            "step_checks": bool(self.step_checks),
            "check_external_inputs": bool(self.check_external_inputs),
            "report_unknown_envelope": bool(self.report_unknown_envelope),
            "require_envelope": bool(self.require_envelope),
            "require_parameter_provenance": bool(self.require_parameter_provenance),
            "deduplicate": bool(self.deduplicate),
            "relative_tolerance": float(self.relative_tolerance),
        }

    def sha256(self) -> str:
        return stable_sha256(self.to_dict())


@dataclass(frozen=True)
class ApplicabilitySummary:
    status: str
    mode: str
    claim_status: str
    claim_level: str
    topology: str | None
    envelope_sha256: str | None
    parameter_provenance_status: str
    parameter_provenance_sha256: str | None
    policy_sha256: str
    finding_count: int
    violation_count: int
    blocked: bool
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": APPLICABILITY_SCHEMA_VERSION,
            "status": self.status,
            "mode": self.mode,
            "claim_status": self.claim_status,
            "claim_level": self.claim_level,
            "topology": self.topology,
            "envelope_sha256": self.envelope_sha256,
            "parameter_provenance_status": self.parameter_provenance_status,
            "parameter_provenance_sha256": self.parameter_provenance_sha256,
            "policy_sha256": self.policy_sha256,
            "finding_count": int(self.finding_count),
            "violation_count": int(self.violation_count),
            "blocked": bool(self.blocked),
            "limitations": list(self.limitations),
        }


def _lookup(mapping: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in mapping:
            value = mapping[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            return number if math.isfinite(number) else None
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class ModelEnvelopePlugin:
    """Check actions and observations against a declared model envelope."""

    plugin_id = "model-envelope"
    rule_version = MODEL_CLAIM_RULE_VERSION

    def __init__(
        self,
        claim: ModelClaim | Mapping[str, Any],
        policy: ApplicabilityPolicy | None = None,
        *,
        provenance: ParameterProvenance | Mapping[str, Any] | None = None,
        actuator_bounds: tuple[float, float] | None = None,
    ) -> None:
        self.claim = normalise_model_claim(claim) if not isinstance(claim, ModelClaim) else claim
        self.policy = (policy or ApplicabilityPolicy()).resolve("exploratory")
        if provenance is None:
            self.provenance = ParameterProvenance.unknown("parameter provenance was not supplied")
        elif isinstance(provenance, ParameterProvenance):
            self.provenance = provenance
        else:
            self.provenance = normalise_parameter_provenance(provenance)
        self.actuator_bounds = None
        if actuator_bounds is not None:
            minimum, maximum = float(actuator_bounds[0]), float(actuator_bounds[1])
            if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
                raise ValueError("actuator_bounds must be finite and ordered")
            self.actuator_bounds = (minimum, maximum)
        self._all: list[CheckFinding] = []
        self._pending: list[CheckFinding] = []
        self._seen: set[tuple[str, str]] = set()
        self._counts: Counter[str] = Counter()
        self._last_external: tuple[float | None, float | None] | None = None

    # ------------------------------------------------------------------
    # Check phases
    # ------------------------------------------------------------------
    def check_preflight(self, spec: Any, context: CheckContext) -> CheckReport:
        del context
        if not self.policy.preflight_checks:
            return self._report()

        status = self.claim.status
        if status == "unknown":
            self._record(
                "envelope.declaration.unknown",
                "envelope_undeclared",
                "the model does not declare a validity envelope",
                strength="hard" if self.policy.require_envelope else "advisory",
                evidence={"claim_status": status, "reason": self.claim.declaration_error},
                variable="claim",
            )
        elif status == "declaration_invalid":
            self._record(
                "envelope.declaration.invalid",
                "envelope_declaration_invalid",
                "the model validity envelope could not be interpreted",
                strength="hard" if self.policy.require_envelope else "advisory",
                evidence={"claim_status": status, "reason": self.claim.declaration_error},
                variable="claim",
            )
        else:
            self._check_actuator_domain()
            self._check_schedule(spec)
            self._check_timebase(spec)
            self._check_provenance()

        return self._report()

    def check_observation(self, observation: Any, context: CheckContext) -> CheckReport:
        if not self.policy.step_checks or self.claim.status != "declared":
            return self._report()

        truth = _mapping(getattr(observation, "truth", None))
        vin = _lookup(truth, ("input_voltage", "input_voltage_v", "vin_v"))
        load = _lookup(truth, ("load_resistance", "load_resistance_ohm", "load_ohm"))
        duty = _lookup(truth, ("duty",))

        if self.policy.check_external_inputs:
            # Duty ratio changes with every control action and is not an
            # external operating-point change.  Only Vin and the load define
            # the external working point.
            signature = (vin, load)
            if self._last_external is not None and signature != self._last_external:
                self._record(
                    "envelope.external_input.changed",
                    "external_input_changed",
                    "the external operating point changed and was re-evaluated",
                    strength="advisory",
                    evidence={
                        "previous": {
                            "input_voltage_v": self._last_external[0],
                            "load_resistance_ohm": self._last_external[1],
                        },
                        "current": {
                            "input_voltage_v": vin,
                            "load_resistance_ohm": load,
                            "duty": duty,
                        },
                        "step_index": getattr(context, "step_index", None),
                        "time_s": getattr(context, "current_time_s", None),
                    },
                    variable="external",
                )
                self._seen.difference_update(
                    {
                        ("envelope.vin.domain", "input_voltage_v"),
                        ("envelope.load.domain", "load_resistance_ohm"),
                        ("envelope.ccm.boundary", "duty"),
                    }
                )
            self._last_external = signature

        self._check_range("input_voltage_v", vin, "envelope.vin.domain", "input_voltage_v")
        self._check_range("load_resistance_ohm", load, "envelope.load.domain", "load_resistance_ohm")
        self._check_ccm(vin, load, duty, context)
        return self._report()

    def check_action(self, subject: Any, context: CheckContext) -> CheckReport:
        if not self.policy.step_checks or self.claim.status != "declared":
            return self._report()
        if isinstance(subject, Mapping):
            action = subject.get("action")
        else:
            action = subject
        if isinstance(action, bool) or not isinstance(action, (int, float)):
            return self._report()
        duty = float(action)
        if not math.isfinite(duty):
            return self._report()
        self._check_range("duty", duty, "envelope.duty.domain", "duty")
        self._check_duty_resolution(duty, context)
        return self._report()

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------
    def findings(self) -> tuple[CheckFinding, ...]:
        return tuple(self._all)

    def counts(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))

    def declaration(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "policy_sha256": self.policy.sha256(),
            "mode": self.policy.mode,
            "claim": {
                "status": self.claim.status,
                "claim_level": self.claim.claim_level,
                "topology": self.claim.topology,
                "model_form": self.claim.model_form,
                "rule_version": self.claim.rule_version,
                "schema_version": self.claim.schema_version,
                "envelope_sha256": self.claim.sha256(),
                "assumptions": list(self.claim.assumptions),
                "operating_ranges": [item.name for item in self.claim.operating_ranges],
                "numerical_limits": [item.name for item in self.claim.numerical_limits],
                "limitations": list(self.claim.limitations),
                "uncheckable_rules": [dict(item) for item in self.claim.uncheckable_rules],
            },
            "parameter_provenance": {
                "status": self.provenance.status,
                "record_count": len(self.provenance.records),
                "sha256": self.provenance.sha256(),
                "unreviewed": [item.name for item in self.provenance.unreviewed],
            },
        }

    def summary(self) -> ApplicabilitySummary:
        if self.policy.mode == "off":
            status = "disabled"
        elif self.claim.status == "declaration_invalid":
            status = "declaration_invalid"
        elif self.claim.status == "unknown":
            status = "unknown"
        elif any(finding.blocking for finding in self._all):
            status = "violated"
        elif self._all:
            status = "limited"
        else:
            status = "within"
        limitations = tuple(
            dict.fromkeys(
                finding.category
                for finding in self._all
                if not finding.blocking
            )
        )
        return ApplicabilitySummary(
            status=status,
            mode=self.policy.mode,
            claim_status=self.claim.status,
            claim_level=self.claim.claim_level,
            topology=self.claim.topology,
            envelope_sha256=self.claim.sha256(),
            parameter_provenance_status=self.provenance.status,
            parameter_provenance_sha256=self.provenance.sha256(),
            policy_sha256=self.policy.sha256(),
            finding_count=len(self._all),
            violation_count=sum(self._counts.values()),
            blocked=any(finding.blocking for finding in self._all),
            limitations=limitations,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record(
        self,
        rule_id: str,
        category: str,
        message: str,
        *,
        strength: str,
        evidence: Mapping[str, Any],
        variable: str,
    ) -> None:
        if self.policy.mode == "off":
            return
        key = (rule_id, variable)
        if self.policy.deduplicate and key in self._seen:
            self._counts[rule_id] += 1
            return
        blocking = self.policy.mode == "enforce" and strength in self.policy.blocking_strengths
        severity = "error" if blocking else ("warning" if self.policy.mode == "warn" else "info")
        finding = CheckFinding(
            rule_id=rule_id,
            category=category,
            message=message,
            severity=severity,
            passed=False,
            stop_requested=blocking,
            evidence=json_safe(evidence),
            plugin_id=self.plugin_id,
        )
        self._seen.add(key)
        self._counts[rule_id] += 1
        self._all.append(finding)
        self._pending.append(finding)

    def _report(self) -> CheckReport:
        findings = tuple(self._pending)
        self._pending.clear()
        return CheckReport(
            passed=not any(finding.blocking for finding in findings),
            findings=findings,
            plugin_id=self.plugin_id,
            rule_version=self.rule_version,
        )

    def _check_range(self, range_name: str, value: float | None, rule_id: str, variable: str) -> None:
        if value is None:
            return
        declared = self.claim.operating_range(range_name)
        if declared is None:
            return
        if declared.contains(value, relative_tolerance=self.policy.relative_tolerance):
            return
        self._record(
            rule_id,
            "out_of_envelope",
            f"{variable} is outside the declared model envelope",
            strength="hard",
            evidence={
                "variable": variable,
                "value": value,
                "unit": declared.unit,
                "minimum": declared.minimum,
                "maximum": declared.maximum,
                "minimum_exclusive": declared.minimum_exclusive,
                "maximum_exclusive": declared.maximum_exclusive,
                "basis": declared.basis,
            },
            variable=variable,
        )

    def _check_actuator_domain(self) -> None:
        if self.actuator_bounds is None:
            return
        declared = self.claim.operating_range("duty")
        if declared is None:
            self._record(
                "envelope.actuator.undeclared",
                "actuator_undeclared",
                "the model declares an actuator but no duty ratio range",
                strength="advisory",
                evidence={"actuator_bounds": list(self.actuator_bounds)},
                variable="duty",
            )
            return
        minimum, maximum = self.actuator_bounds
        if not declared.contains(minimum, relative_tolerance=self.policy.relative_tolerance) or not declared.contains(
            maximum, relative_tolerance=self.policy.relative_tolerance
        ):
            self._record(
                "envelope.actuator.subset",
                "actuator_outside_envelope",
                "the controller actuator range is not contained in the declared duty range",
                strength="hard",
                evidence={
                    "actuator_bounds": [minimum, maximum],
                    "declared_minimum": declared.minimum,
                    "declared_maximum": declared.maximum,
                },
                variable="duty",
            )

    def _check_schedule(self, spec: Any) -> None:
        schedule = getattr(spec, "input_schedule", ()) or ()
        for index, item in enumerate(schedule):
            values = _mapping(item)
            for key in ("vin_v", "input_voltage_v"):
                if key not in values:
                    continue
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    self._record(
                        "envelope.schedule.invalid",
                        "invalid_schedule_value",
                        "input schedule contains a non-numeric voltage",
                        strength="hard",
                        evidence={"index": index, "key": key, "value": repr(value)},
                        variable=f"schedule[{index}].{key}",
                    )
                    continue
                self._check_range("input_voltage_v", float(value), "envelope.vin.domain", "input_voltage_v")
            for key in ("load_ohm", "load_resistance_ohm"):
                if key not in values:
                    continue
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    self._record(
                        "envelope.schedule.invalid",
                        "invalid_schedule_value",
                        "input schedule contains a non-numeric load",
                        strength="hard",
                        evidence={"index": index, "key": key, "value": repr(value)},
                        variable=f"schedule[{index}].{key}",
                    )
                    continue
                self._check_range("load_resistance_ohm", float(value), "envelope.load.domain", "load_resistance_ohm")

    def _check_timebase(self, spec: Any) -> None:
        parameters = self.claim.static_parameters
        timebase = getattr(spec, "timebase", None)
        if timebase is None:
            return

        switching_ratio = parameters.get("switching_periods_per_control_period")
        limit = self.claim.numerical_limit("control_period.min_switching_periods")
        if switching_ratio is not None and limit is not None and limit.value is not None:
            if float(switching_ratio) + 1e-9 < float(limit.value):
                self._record(
                    "envelope.control_period.switching_periods",
                    "control_period_too_short",
                    "the control period is not an integer number of switching periods",
                    strength=limit.strength,
                    evidence={
                        "value": float(switching_ratio),
                        "minimum": float(limit.value),
                        "unit": limit.unit,
                    },
                    variable="control_period",
                )

        samples = parameters.get("samples_per_switching_period")
        limit = self.claim.numerical_limit("samples_per_switching_period.min")
        if samples is not None and limit is not None and limit.value is not None:
            if float(samples) + 1e-9 < float(limit.value):
                self._record(
                    "envelope.samples_per_switching_period",
                    "insufficient_sample_resolution",
                    "the printed waveform has fewer samples per switching period than declared",
                    strength=limit.strength,
                    evidence={
                        "value": float(samples),
                        "minimum": float(limit.value),
                        "unit": limit.unit,
                    },
                    variable="sample_resolution",
                )

        integration_step = parameters.get("integration_step_s")
        limit = self.claim.numerical_limit("integration_step_s.max")
        if integration_step is not None and limit is not None and limit.value is not None:
            if float(integration_step) > float(limit.value) * (1.0 + self.policy.relative_tolerance):
                self._record(
                    "envelope.timestep.dynamics",
                    "integration_step_too_large",
                    "the integration step is larger than the declared stability limit",
                    strength=limit.strength,
                    evidence={
                        "value": float(integration_step),
                        "maximum": float(limit.value),
                        "unit": limit.unit,
                    },
                    variable="integration_step",
                )

        limit = self.claim.numerical_limit("control_period.min_integration_substeps")
        if integration_step is not None and limit is not None and limit.value is not None:
            substeps = float(timebase.control_period_s) / float(integration_step)
            if substeps + 1e-9 < float(limit.value):
                self._record(
                    "envelope.control_period.substeps",
                    "insufficient_integration_substeps",
                    "the control period contains fewer integration substeps than declared",
                    strength=limit.strength,
                    evidence={
                        "value": substeps,
                        "minimum": float(limit.value),
                        "unit": limit.unit,
                    },
                    variable="control_period",
                )

        load_ratio = parameters.get("load_off_resistance_ratio")
        limit = self.claim.numerical_limit("load.min_off_resistance_ratio")
        if load_ratio is not None and limit is not None and limit.value is not None:
            if float(load_ratio) + 1e-9 < float(limit.value):
                self._record(
                    "envelope.load.off_resistance_ratio",
                    "load_too_close_to_switch_off_resistance",
                    "the load resistance is too close to the switch off resistance",
                    strength=limit.strength,
                    evidence={
                        "value": float(load_ratio),
                        "minimum": float(limit.value),
                        "unit": limit.unit,
                    },
                    variable="load_ratio",
                )

    def _check_provenance(self) -> None:
        status = self.provenance.status
        if status == "declaration_invalid":
            self._record(
                "provenance.declaration.invalid",
                "parameter_provenance_invalid",
                "parameter provenance could not be interpreted",
                strength="hard" if self.policy.require_parameter_provenance else "advisory",
                evidence={"reason": self.provenance.declaration_error},
                variable="provenance",
            )
            return
        if status == "unknown":
            self._record(
                "provenance.records.unknown",
                "parameter_provenance_unknown",
                "the model does not declare parameter sources",
                strength="hard" if self.policy.require_parameter_provenance else "advisory",
                evidence={"reason": self.provenance.declaration_error},
                variable="provenance",
            )
            return
        for record in self.provenance.unreviewed:
            self._record(
                "provenance.review.unreviewed",
                "parameter_unreviewed",
                "a parameter is an assumption/default or has not been reviewed",
                strength="advisory",
                evidence={
                    "parameter": record.name,
                    "value": record.value,
                    "unit": record.unit,
                    "source": record.source,
                    "review_status": record.review_status,
                },
                variable=record.name,
            )

    def _check_ccm(self, vin: float | None, load: float | None, duty: float | None, context: CheckContext) -> None:
        if vin is None or load is None or duty is None:
            return
        parameters = self.claim.static_parameters
        switching_frequency = parameters.get("switching_frequency_hz")
        inductance = parameters.get("inductance_h")
        if switching_frequency is None or inductance is None:
            return
        if switching_frequency <= 0 or inductance <= 0 or load <= 0:
            return
        topology = self.claim.topology
        if topology == "buck":
            if not 0.0 <= duty <= 1.0:
                return
            delta_current = vin * duty * (1.0 - duty) / (inductance * switching_frequency)
            average_current = duty * vin / load
        elif topology == "boost":
            if not 0.0 <= duty < 1.0:
                return
            delta_current = vin * duty / (inductance * switching_frequency)
            average_current = vin / ((1.0 - duty) ** 2 * load)
        else:
            return
        estimated_ccm = average_current >= 0.5 * delta_current
        if estimated_ccm:
            return
        self._record(
            "envelope.ccm.boundary",
            "ccm_boundary_violated",
            "the declared continuous-conduction assumption is not satisfied at this operating point",
            strength="advisory",
            evidence={
                "estimated_ccm": False,
                "average_inductor_current_a": average_current,
                "peak_to_peak_current_a": delta_current,
                "duty": duty,
                "step_index": getattr(context, "step_index", None),
            },
            variable="duty",
        )

    def _check_duty_resolution(self, duty: float, context: CheckContext) -> None:
        parameters = self.claim.static_parameters
        switching_period = parameters.get("switching_period_s")
        pulse_edge = parameters.get("pulse_edge_s")
        if switching_period is None or pulse_edge is None:
            return
        if duty <= 0.0 or duty >= 1.0:
            return
        pulse_width = duty * switching_period
        minimum = 2.0 * pulse_edge
        if pulse_width + 1e-15 < minimum:
            self._record(
                "envelope.duty.resolution",
                "pulse_width_below_template_resolution",
                "the requested pulse width is smaller than the template edge time",
                strength="advisory",
                evidence={
                    "duty": duty,
                    "pulse_width_s": pulse_width,
                    "minimum_pulse_width_s": minimum,
                    "step_index": getattr(context, "step_index", None),
                },
                variable="duty_resolution",
            )


def track_claim(adapter: Any) -> tuple[ModelClaim, ParameterProvenance]:
    """Collect both declarations in one call for callers that need evidence."""

    return collect_model_claim(adapter), collect_parameter_provenance(adapter)


__all__ = [
    "APPLICABILITY_MODES",
    "APPLICABILITY_SCHEMA_VERSION",
    "ApplicabilityError",
    "ApplicabilityPolicy",
    "ApplicabilitySummary",
    "ModelEnvelopePlugin",
    "track_claim",
]
