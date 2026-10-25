"""Qualification is deliberately independent from performance metrics.

The qualification ladder is ``assessed`` / ``partial`` / ``failed``.  It says
how completely the configured checks could be evaluated.  It is not the
evidence ladder (``diagnostic_only`` / ``functional`` / ``limited`` /
``comparable``) and the two vocabularies share no token.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Mapping, Any
import math

from .checks import (
    CHECK_LEVELS,
    CheckContext,
    CheckFinding,
    CheckReport,
    QualificationPlugin,
    normalize_check_level,
)


LEVELS = CHECK_LEVELS


@dataclass(frozen=True)
class QualificationOutcome:
    """Qualification result with an explicit usability level.

    ``passed`` keeps its historical meaning ("the sample set may be used").
    ``level`` distinguishes a fully assessed run from one that is usable only
    with declared limitations.
    """

    level: str
    passed: bool
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    findings: tuple[CheckFinding, ...] = ()
    rule_version: str = "qualification-v3"

    def __post_init__(self) -> None:
        level = normalize_check_level(self.level)
        object.__setattr__(self, "level", level)
        if self.passed != (level != "failed"):
            raise ValueError("qualification passed must match level != 'failed'")

    @classmethod
    def from_report(cls, report: CheckReport) -> "QualificationOutcome":
        level = report.effective_level
        return cls(
            level=level,
            passed=level != "failed",
            reasons=tuple(report.reasons),
            limitations=tuple(report.limitations),
            findings=tuple(report.findings),
            rule_version=report.rule_version,
        )

    def to_report(self) -> CheckReport:
        return CheckReport(
            self.passed,
            self.findings,
            None,
            self.rule_version,
            self.level,
            self.limitations,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "qualification-v3",
            "passed": self.passed,
            "level": self.level,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "checks": self.to_report().to_dict(),
        }


def qualification_document(
    report: CheckReport,
    *,
    level: str | None = None,
    reasons: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the additive qualification document used by the Runner."""

    outcome = QualificationOutcome.from_report(report)
    document = outcome.to_document()
    if level is not None:
        selected = normalize_check_level(level)
        document["level"] = selected
        document["passed"] = selected != "failed"
        checks = document.get("checks")
        if isinstance(checks, dict):
            checks["level"] = selected
            checks["passed"] = selected != "failed"
    if reasons is not None:
        document["reasons"] = [str(item) for item in reasons]
    return document


def qualify_samples(samples: Iterable[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    rows = list(samples)
    reasons: list[str] = []
    if not rows:
        reasons.append("no_samples")
    for index, row in enumerate(rows):
        for key, value in row.items():
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                reasons.append(f"non_finite:{index}:{key}")
    return not reasons, reasons


class BasicQualificationPlugin:
    """Default post-run sample qualification rule set.

    This intentionally answers only whether the captured sample set is
    structurally usable. It does not claim physical-model validity or approve
    an engineering conclusion.
    """

    plugin_id = "basic-sample-qualification"
    rule_version = "basic-qualification-v1"

    def check_samples(self, samples: list[Mapping[str, Any]], context: CheckContext) -> CheckReport:
        del context
        rows = list(samples)
        findings: list[CheckFinding] = []
        if not rows:
            findings.append(CheckFinding("samples.nonempty", "no_samples", "run produced no samples", plugin_id=self.plugin_id))
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                findings.append(CheckFinding("samples.row.mapping", "invalid_sample", f"sample row {index} is not a mapping", evidence={"index": index}, plugin_id=self.plugin_id))
                continue
            for key, value in row.items():
                if isinstance(value, (int, float)):
                    try:
                        finite = math.isfinite(float(value))
                    except (TypeError, ValueError):
                        finite = False
                    if not finite:
                        findings.append(CheckFinding("samples.numeric.finite", "nonfinite_sample", f"sample value is not finite: {key}", evidence={"index": index, "key": str(key)}, plugin_id=self.plugin_id))
        return CheckReport(not findings, tuple(findings), self.plugin_id, "basic-qualification-v1")


__all__ = [
    "LEVELS",
    "BasicQualificationPlugin",
    "QualificationOutcome",
    "QualificationPlugin",
    "qualification_document",
    "qualify_samples",
]
