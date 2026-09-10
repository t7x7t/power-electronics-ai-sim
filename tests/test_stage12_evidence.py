import json
from pathlib import Path

import jsonschema
import pytest

from pe_sim import (
    EvidenceClassificationError,
    classify_report,
    classify_stage10,
    classify_summary,
    compare_metric_runs,
    summarize_run,
    write_evidence_report,
)

from test_stage10_restricted_postprocess import _published_run


def _summary(tmp_path, name="run"):
    return summarize_run(_published_run(tmp_path / name))


def test_summary_classifies_as_mechanism_or_functional_and_blocks_claims(tmp_path):
    summary = _summary(tmp_path)
    mechanism = classify_summary(summary, requested_level="mechanism")
    functional = classify_stage10(summary)

    assert mechanism["evidence_level"] == "mechanism"
    assert functional["evidence_level"] == "functional"
    assert functional["review_required"] is True
    assert "physical-performance" in functional["blocked_conclusions"]
    assert functional["review_record"]["status"] == "not_recorded"

    schema = json.loads(Path("schemas/stage12-evidence-classification.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(functional)


def test_matching_report_is_comparable_and_keeps_source_provenance(tmp_path):
    left = _published_run(tmp_path / "left")
    right = _published_run(tmp_path / "right")
    report_path = write_evidence_report((left, right), tmp_path / "report.json", metric_ids=("series.vout.mean",))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    classification = classify_report(report, review_record={"status": "recorded", "reviewer": "local-user"})
    assert classification["evidence_level"] == "comparable"
    assert classification["review_record"]["reviewer"] == "local-user"
    assert {item["package_sha256"] for item in classification["runs"]} == {
        report["provenance"]["runs"][0]["package_sha256"],
        report["provenance"]["runs"][1]["package_sha256"],
    }

    report_schema = json.loads(Path("schemas/stage10-evidence-report.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(report_schema).validate(report)


def test_mismatching_report_is_limited_not_comparable(tmp_path):
    left = _published_run(tmp_path / "left")
    right = _published_run(tmp_path / "right")
    right_manifest = json.loads((right / "manifest.json").read_text(encoding="utf-8"))
    right_manifest["run_id"] = "different"
    # The report itself is deliberately malformed for a boundary test: the
    # classifier must retain a limited status instead of upgrading it.
    report = {
        "schema": "stage10-evidence-report-v1",
        "report_type": "restricted_metric_comparison",
        "provenance": {
            "runs": [
                {"run_id": "left", "manifest_sha256": "a" * 64, "package_sha256": "b" * 64, "status": "QUALIFIED", "working_tree_status": "clean", "source_provenance_status": "known", "environment_status": "partial"},
                {"run_id": "right", "manifest_sha256": "c" * 64, "package_sha256": "d" * 64, "status": "QUALIFIED", "working_tree_status": "clean", "source_provenance_status": "known", "environment_status": "partial"},
            ],
            "metric_schema": "stage10-metrics-v1",
            "metric_registry": {"registry_id": "stage10-default"},
            "data": {"schema": "stage10-data-descriptor-v1"},
        },
        "comparison": {
            "schema": "stage10-metric-comparison-v1",
            "metric_schema": "stage10-metrics-v1",
            "metric_registry": {"registry_id": "stage10-default"},
            "data": {"schema": "stage10-data-descriptor-v1"},
            "tolerance": 0.0,
            "runs": [
                {"run_id": "left", "manifest_sha256": "a" * 64, "package_sha256": "b" * 64, "status": "QUALIFIED", "working_tree_status": "clean", "source_provenance_status": "known", "environment_status": "partial"},
                {"run_id": "right", "manifest_sha256": "c" * 64, "package_sha256": "d" * 64, "status": "QUALIFIED", "working_tree_status": "clean", "source_provenance_status": "known", "environment_status": "partial"},
            ],
            "metrics": {"series.vout.mean": {"outcome": "mismatch"}},
            "outcome": "mismatch",
            "limitations": ["values differ"],
        },
        "limitations": ["values differ"],
    }
    result = classify_report(report)
    assert result["evidence_level"] == "limited"
    assert result["review_required"] is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda summary: summary["provenance"].update({"package_sha256": "bad"}),
        lambda summary: summary["provenance"].update({"manifest_sha256": "f" * 63}),
    ],
)
def test_invalid_summary_provenance_is_rejected(tmp_path, mutator):
    summary = _summary(tmp_path)
    mutator(summary)
    with pytest.raises(EvidenceClassificationError):
        classify_summary(summary)


def test_failed_summary_is_diagnostic_only(tmp_path):
    summary = _summary(tmp_path)
    summary["status"] = "RUN_FAILED"
    result = classify_summary(summary)
    assert result["evidence_level"] == "diagnostic_only"


@pytest.mark.parametrize(
    "field,value",
    [("working_tree_status", "dirty"), ("source_provenance_status", "unknown"), ("environment_status", "unknown")],
)
def test_dirty_or_unknown_source_cannot_enter_evidence_level(tmp_path, field, value):
    summary = _summary(tmp_path)
    summary["provenance"][field] = value
    with pytest.raises(EvidenceClassificationError):
        classify_summary(summary)


def test_report_provenance_mismatch_is_not_upgraded(tmp_path):
    left = _published_run(tmp_path / "left")
    right = _published_run(tmp_path / "right")
    report = json.loads(write_evidence_report((left, right), tmp_path / "report.json").read_text(encoding="utf-8"))
    report["comparison"]["runs"][0]["package_sha256"] = "e" * 64
    result = classify_report(report)
    assert result["evidence_level"] == "limited"
    assert any("do not match" in item for item in result["limitations"])


def test_forbidden_requested_levels_are_rejected(tmp_path):
    summary = _summary(tmp_path)
    with pytest.raises(EvidenceClassificationError, match="unsupported summary evidence level"):
        classify_summary(summary, requested_level="physical-performance")

    report = {
        "schema": "stage10-evidence-report-v1",
        "provenance": {},
        "comparison": {},
    }
    with pytest.raises(EvidenceClassificationError):
        classify_stage10(report, requested_level="hardware-readiness")
