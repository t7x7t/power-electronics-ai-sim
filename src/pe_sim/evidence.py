"""Restricted Stage 12 evidence classification and conclusion boundaries.

This module classifies already validated Stage 10 summaries/reports. It never
upgrades evidence to physical, safety, or hardware claims and treats malformed
or incomplete provenance as diagnostic-only.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
import hashlib
import re

from .artifacts import canonical_json


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LEVELS = frozenset({"mechanism", "functional", "comparable", "diagnostic_only", "limited"})
_FORBIDDEN = ("physical-performance", "research-safety", "hardware-readiness")


class EvidenceClassificationError(ValueError):
    """Raised when a classification input is malformed or not eligible."""


def _run_ok(run: Mapping[str, Any], *, require_qualified: bool = True) -> tuple[bool, str | None]:
    if require_qualified and run.get("status") != "QUALIFIED":
        return False, f"run {run.get('run_id', '<unknown>')} is not QUALIFIED"
    for key in ("run_id", "manifest_sha256", "package_sha256"):
        if not isinstance(run.get(key), str) or not run[key]:
            return False, f"run {run.get('run_id', '<unknown>')} is missing {key}"
    for key in ("manifest_sha256", "package_sha256"):
        if not _HASH_RE.fullmatch(str(run[key])):
            return False, f"run {run.get('run_id', '<unknown>')} has invalid {key}"
    for key in ("working_tree_status", "source_provenance_status", "environment_status"):
        if key not in run or run.get(key) in (None, "", "unknown", "unavailable"):
            return False, f"run {run.get('run_id', '<unknown>')} has incomplete {key}"
    if run.get("working_tree_status") != "clean":
        return False, f"run {run.get('run_id', '<unknown>')} is not from a clean working tree"
    if run.get("source_provenance_status") != "known":
        return False, f"run {run.get('run_id', '<unknown>')} has unknown source provenance"
    if run.get("environment_status") not in {"known", "partial"}:
        return False, f"run {run.get('run_id', '<unknown>')} has unknown environment provenance"
    return True, None


def _validate_runs(runs: Any, *, require_qualified: bool = True) -> tuple[dict[str, Any], ...]:
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)) or not runs:
        raise EvidenceClassificationError(("report provenance.runs must be a non-empty array",))
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in runs:
        if not isinstance(item, Mapping):
            errors.append("report contains a malformed run record")
            continue
        record = dict(item)
        ok, reason = _run_ok(record, require_qualified=require_qualified)
        if not ok and reason:
            errors.append(reason)
        normalized.append(record)
    if errors:
        raise EvidenceClassificationError(tuple(errors))
    return tuple(normalized)


def _base_output(
    level: str,
    *,
    limitations: Sequence[str],
    blocked: Sequence[str],
    review_required: bool,
    review_record: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if level not in _LEVELS:
        raise EvidenceClassificationError((f"unsupported evidence level: {level}",))
    record = dict(review_record) if review_record is not None else {
        "status": "not_recorded",
        "reviewer": None,
        "reviewed_at": None,
        "decision": None,
        "notes": None,
    }
    return {
        "schema": "stage12-evidence-classification-v1",
        "evidence_level": level,
        "runs": [dict(item) for item in runs],
        "limitations": list(dict.fromkeys(str(item) for item in limitations)),
        "blocked_conclusions": list(dict.fromkeys(str(item) for item in blocked)),
        "review_required": bool(review_required),
        "review_record": record,
        "automatic_upgrade_blocked": list(_FORBIDDEN),
    }


def classify_summary(
    summary: Mapping[str, Any],
    *,
    requested_level: str = "functional",
    review_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one Stage 10 summary as functional or diagnostic-only."""

    if requested_level not in {"mechanism", "functional", "diagnostic_only"}:
        raise EvidenceClassificationError((f"unsupported summary evidence level: {requested_level}",))
    if not isinstance(summary, Mapping):
        raise EvidenceClassificationError(("summary must be an object",))
    provenance = summary.get("provenance")
    if not isinstance(provenance, Mapping):
        raise EvidenceClassificationError(("summary provenance is missing",))
    run = dict(provenance)
    run["status"] = summary.get("status") or "UNKNOWN"
    # Preserve a valid identity record for failed/incomplete summaries so they
    # can be classified as diagnostic-only; malformed or incomplete hashes are
    # still rejected at the boundary.
    runs = _validate_runs((run,), require_qualified=False)
    limitations: list[str] = []
    blocked = [
        "physical-performance",
        "research-safety",
        "hardware-readiness",
        "engineering or product conclusions",
    ]
    if summary.get("schema") != "stage10-metrics-v1":
        limitations.append("input is not a recognized Stage 10 metrics summary")
    data = summary.get("data")
    if not isinstance(data, Mapping) or data.get("schema") != "stage10-data-descriptor-v1":
        limitations.append("summary data descriptor is missing or unsupported")
    elif not data.get("schema_fingerprint") or not _HASH_RE.fullmatch(str(data.get("schema_fingerprint"))):
        limitations.append("summary data schema fingerprint is missing or invalid")
    if isinstance(data, Mapping) and _HASH_RE.fullmatch(str(data.get("schema_fingerprint", ""))) and _HASH_RE.fullmatch(str(provenance.get("manifest_sha256", ""))) and _HASH_RE.fullmatch(str(provenance.get("package_sha256", ""))):
        expected_dataset = hashlib.sha256(
            canonical_json(
                {
                    "schema_fingerprint": data["schema_fingerprint"],
                    "manifest_sha256": provenance["manifest_sha256"],
                    "package_sha256": provenance["package_sha256"],
                }
            )
        ).hexdigest()
        if data.get("dataset_fingerprint") != expected_dataset:
            raise EvidenceClassificationError(("summary dataset fingerprint does not match package provenance",))
    registry = summary.get("metric_registry")
    if not isinstance(registry, Mapping) or registry.get("schema") != "stage10-metric-registry-v1":
        limitations.append("summary metric registry is missing or unsupported")
    elif not _HASH_RE.fullmatch(str(registry.get("fingerprint", ""))):
        limitations.append("summary metric registry fingerprint is missing or invalid")
    if not isinstance(summary.get("metrics"), Mapping) or not summary.get("metrics"):
        limitations.append("metric registry output is missing or empty")
    if summary.get("status") != "QUALIFIED":
        limitations.append("run is not qualified")
    if requested_level == "diagnostic_only":
        limitations.append("diagnostic-only classification was explicitly requested")
    if limitations:
        return _base_output(
            "diagnostic_only",
            limitations=limitations,
            blocked=blocked,
            review_required=True,
            review_record=review_record,
            runs=runs,
        )
    level_limit = (
        "Mechanism evidence records that the runner produced a qualified, traceable reference artifact; it is not a physical-model claim."
        if requested_level == "mechanism"
        else "Functional evidence summarizes a qualified reference run only."
    )
    return _base_output(
        requested_level,
        limitations=[level_limit],
        blocked=blocked,
        review_required=True,
        review_record=review_record,
        runs=runs,
    )


def classify_report(
    report: Mapping[str, Any],
    *,
    requested_level: str | None = None,
    review_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a Stage 10 comparison report as comparable or limited."""

    if not isinstance(report, Mapping):
        raise EvidenceClassificationError(("report must be an object",))
    if requested_level is not None and requested_level not in {"comparable", "limited", "diagnostic_only"}:
        raise EvidenceClassificationError((f"unsupported report evidence level: {requested_level}",))
    provenance = report.get("provenance")
    comparison = report.get("comparison")
    if not isinstance(provenance, Mapping) or not isinstance(comparison, Mapping):
        raise EvidenceClassificationError(("report provenance or comparison is missing",))
    runs = _validate_runs(provenance.get("runs"), require_qualified=False)
    declared_limitations = (
        list(report.get("limitations", ()))
        if isinstance(report.get("limitations"), Sequence) and not isinstance(report.get("limitations"), (str, bytes))
        else []
    )
    limitations: list[str] = []
    blocked = [
        "physical-performance",
        "research-safety",
        "hardware-readiness",
        "controller quality or superiority",
        "engineering or product conclusions",
    ]
    required_provenance = ("metric_schema", "metric_registry", "data")
    missing = [key for key in required_provenance if key not in provenance or provenance.get(key) in (None, {})]
    outcome = comparison.get("outcome")
    nonqualified = any(item.get("status") != "QUALIFIED" for item in runs)
    if nonqualified:
        limitations.append("one or more source runs are not QUALIFIED")
    if report.get("schema") != "stage10-evidence-report-v1":
        limitations.append("input is not a recognized Stage 10 evidence report")
    if comparison.get("runs") != [dict(item) for item in runs]:
        limitations.append("report provenance runs do not match comparison runs")
    if comparison.get("metric_schema") != provenance.get("metric_schema"):
        limitations.append("report metric schema does not match comparison provenance")
    if comparison.get("metric_registry") != provenance.get("metric_registry"):
        limitations.append("report metric registry does not match comparison provenance")
    if comparison.get("data") != provenance.get("data"):
        limitations.append("report data descriptor does not match comparison provenance")
    registry = provenance.get("metric_registry")
    if not isinstance(registry, Mapping) or registry.get("schema") != "stage10-metric-registry-v1" or not _HASH_RE.fullmatch(str(registry.get("fingerprint", ""))):
        limitations.append("report metric registry fingerprint is missing or invalid")
    data = provenance.get("data")
    if not isinstance(data, Mapping) or data.get("schema") != "stage10-data-descriptor-v1" or not _HASH_RE.fullmatch(str(data.get("schema_fingerprint", ""))):
        limitations.append("report data schema fingerprint is missing or invalid")
    if not isinstance(comparison.get("metrics"), Mapping) or not comparison.get("metrics"):
        limitations.append("comparison metrics are missing or empty")
    if missing:
        limitations.append(f"report provenance is incomplete: {', '.join(missing)}")
    if outcome not in {"exact_match", "tolerance_match"}:
        limitations.append(f"comparison outcome is not comparable: {outcome or 'missing'}")
    tolerance = comparison.get("tolerance")
    if not isinstance(tolerance, (int, float)) or tolerance < 0:
        limitations.append("comparison tolerance is missing or invalid")
    if limitations:
        level = "diagnostic_only" if requested_level == "diagnostic_only" or nonqualified else "limited"
        return _base_output(
            level,
            limitations=declared_limitations + limitations,
            blocked=blocked,
            review_required=True,
            review_record=review_record,
            runs=runs,
        )
    level = "diagnostic_only" if requested_level == "diagnostic_only" else "limited" if requested_level == "limited" else "comparable"
    return _base_output(
        level,
        limitations=declared_limitations + [
            "Comparable means registered numeric metrics matched within the declared tolerance.",
            "It does not establish physical equivalence or controller quality.",
        ],
        blocked=blocked,
        review_required=True,
        review_record=review_record,
        runs=runs,
    )


def classify_stage10(
    value: Mapping[str, Any],
    *,
    requested_level: str | None = None,
    review_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch classification based on the Stage 10 input schema."""

    if not isinstance(value, Mapping):
        raise EvidenceClassificationError(("Stage 10 input must be an object",))
    schema = value.get("schema")
    if schema == "stage10-metrics-v1":
        return classify_summary(value, requested_level=requested_level or "functional", review_record=review_record)
    if schema == "stage10-evidence-report-v1":
        return classify_report(value, requested_level=requested_level, review_record=review_record)
    raise EvidenceClassificationError((f"unsupported Stage 10 schema: {schema or 'missing'}",))


__all__ = ["EvidenceClassificationError", "classify_summary", "classify_report", "classify_stage10"]
