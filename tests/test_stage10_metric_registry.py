import json
from pathlib import Path

import pytest
import jsonschema

from pe_sim.postprocess import (
    MetricDefinition,
    MetricRegistry,
    PostprocessEligibilityError,
    UnknownMetricError,
    build_evidence_report,
    compare_metric_runs,
    default_metric_registry,
    summarize_run,
    write_evidence_report,
)

from test_stage10_restricted_postprocess import _published_run


def test_default_registry_is_versioned_and_unknown_metrics_fail(tmp_path):
    run = _published_run(tmp_path / "run")
    summary = summarize_run(run)

    assert summary["metrics"]["samples.count"]["version"] == "1"
    assert summary["metrics"]["series.vout.mean"]["unit"] == "V"
    assert summary["data"]["sample_schema_version"] == "samples-v1"
    assert summary["data"]["metrics_schema_version"] == "run-metrics-v1"
    assert summary["metric_registry"]["schema"] == "stage10-metric-registry-v1"
    assert len(summary["metric_registry"]["fingerprint"]) == 64
    with pytest.raises(UnknownMetricError, match="unknown metric_id"):
        summarize_run(run, metric_ids=("not-reviewed",))
    with pytest.raises(ValueError, match="at least one metric_id"):
        summarize_run(run, metric_ids=())


def test_custom_metric_registry_is_explicit_and_numeric(tmp_path):
    run = _published_run(tmp_path / "run")
    registry = MetricRegistry(
        (
            MetricDefinition(
                "custom.vout.range",
                "2026-09-09",
                "V",
                ("vout",),
                lambda evidence: max(float(row["vout"]) for row in evidence.samples)
                - min(float(row["vout"]) for row in evidence.samples),
            ),
        )
    )
    summary = summarize_run(run, registry=registry)
    assert summary["metrics"]["custom.vout.range"]["value"] == 2.0

    with pytest.raises(ValueError, match="input_fields"):
        MetricDefinition("invalid", "1", "V", ("",), lambda evidence: 1.0)


def test_metric_comparison_and_evidence_report_link_all_sources(tmp_path):
    left = _published_run(tmp_path / "left")
    right = _published_run(tmp_path / "right")
    comparison = compare_metric_runs(
        (left, right), metric_ids=("samples.count", "series.vout.mean"), tolerance=0.0
    )
    assert comparison["schema"] == "stage10-metric-comparison-v1"
    assert comparison["outcome"] == "exact_match"
    assert len(comparison["runs"]) == 2
    assert all(item["manifest_sha256"] for item in comparison["runs"])
    assert all(item["experiment_id"] == "stage10-fixture" for item in comparison["runs"])
    assert comparison["data"]["sample_schema_version"] == "samples-v1"
    assert len(comparison["metric_registry"]["fingerprint"]) == 64
    assert comparison["metrics"]["series.vout.mean"]["pairs"][0]["within_tolerance"]

    report_path = write_evidence_report(
        (left, right), tmp_path / "reports" / "comparison.json", metric_ids=("series.vout.final",)
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "stage10-evidence-report-v1"
    assert report["provenance"]["runs"][0]["package_sha256"]
    assert report["comparison"]["metrics"]["series.vout.final"]["unit"] == "V"
    metrics_schema = json.loads(Path("schemas/stage10-metrics.schema.json").read_text(encoding="utf-8"))
    report_schema = json.loads(Path("schemas/stage10-evidence-report.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(metrics_schema).validate(summarize_run(left))
    jsonschema.Draft202012Validator(report_schema).validate(report)


def test_metric_report_cannot_be_written_inside_a_source_package(tmp_path):
    run = _published_run(tmp_path / "run")
    with pytest.raises(ValueError, match="outside the immutable run package"):
        write_evidence_report((run, run), run / "report.json")


def test_metric_comparison_rejects_mixed_data_schema_versions(tmp_path):
    left = _published_run(tmp_path / "left")
    right = _published_run(tmp_path / "right", sample_schema_version="samples-v2")
    with pytest.raises(PostprocessEligibilityError, match="incompatible data schema versions"):
        compare_metric_runs((left, right), metric_ids=("samples.count",))
