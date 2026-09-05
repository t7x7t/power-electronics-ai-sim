"""Qualification is deliberately independent from performance metrics."""

from __future__ import annotations
from typing import Iterable, Mapping, Any
import math

from .checks import CheckContext, CheckFinding, CheckReport, QualificationPlugin


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


__all__ = ["qualify_samples", "BasicQualificationPlugin", "QualificationPlugin"]
