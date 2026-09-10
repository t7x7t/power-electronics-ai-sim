import csv
import json

import pytest

from pe_sim.artifacts import ArtifactWriter, artifact_index, manifest_digest, package_digest
from pe_sim.postprocess import PostprocessEligibilityError, export_metrics, load_run_evidence, summarize_run


def _published_run(
    path,
    *,
    status="QUALIFIED",
    clean=True,
    plant_class="FakePlant",
    environment_status="partial",
    sample_schema_version=None,
    metrics_schema_version=None,
):
    path.mkdir()
    samples = [
        {"step_index": 0, "time_s": 0.0, "action": 0.2, "action_requested": 0.25, "vout": 1.0},
        {"step_index": 1, "time_s": 0.001, "action": 0.4, "action_requested": 0.45, "vout": 3.0},
    ]
    values = {
        "config.snapshot.json": {"fixture": True},
        "environment.json": {"fixture": True},
        "qualification.json": {"passed": True},
        "safety.json": {"passed": True},
        "metrics.json": {
            **({"sample_schema_version": sample_schema_version} if sample_schema_version else {}),
            **({"schema_version": metrics_schema_version} if metrics_schema_version else {}),
            "sample_count": len(samples),
            "audit_call_count": 2,
        },
        "events.json": [],
        "samples.json": samples,
        "audit.json": {"calls": [{"call_id": 1}, {"call_id": 2}]},
        "state_transitions.json": [],
        "logs/run.json": {"status": status},
    }
    for name, value in values.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    (path / "logs" / ".keep").write_bytes(b"")
    (path / ".run-state.json").write_text(json.dumps({"status": "PUBLISHED"}), encoding="utf-8")
    manifest = {
        "schema_version": "0.1",
        "experiment_id": "stage10-fixture",
        "run_id": path.name,
        "source_commit": "a" * 40,
        "working_tree_status": "clean" if clean else "dirty",
        "provenance": {"status": "known" if clean else "exploratory"},
        "environment": {
            "status": environment_status,
            "python": {"version": "3.12.7"},
            "backends": [{"name": "fixture", "status": "not_declared", "version": None, "solver_settings": None}],
        },
        "plant": {"id": "fixture-plant", "hash": "p" * 64, "identity": {"class": plant_class}},
        "controller": {"id": "fixture-controller", "hash": "c" * 64, "identity": {"class": "FakePIController"}},
        "contracts": {},
        "timebase": {"unit": "s", "control_period_s": 0.001, "duration_s": 0.002},
        "initial_state": {"mode": "cold_start"},
        "random_seed": 0,
        "status": status,
        "qualification": {"passed": status == "QUALIFIED"},
        "safety": {"passed": status == "QUALIFIED"},
        "artifacts": artifact_index(path),
    }
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(path, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (path / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def test_qualified_reference_run_has_traceable_summary_and_exports(tmp_path):
    run = _published_run(tmp_path / "qualified")

    evidence = load_run_evidence(run)
    summary = summarize_run(run)
    json_output = export_metrics(run, tmp_path / "exports" / "summary.json")
    csv_output = export_metrics(run, tmp_path / "exports" / "samples.csv")

    assert len(evidence.samples) == 2
    assert summary["schema"] == "stage10-metrics-v1"
    assert summary["sample_count"] == 2
    assert summary["audit_count"] == 2
    assert summary["series"]["vout"]["mean"] == 2.0
    assert summary["time"] == {"first_s": 0.0, "last_s": 0.001, "duration_s": 0.001}
    exported = json.loads(json_output.read_text(encoding="utf-8"))
    assert exported["provenance"] == summary["provenance"]
    with csv_output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["run_id"] for row in rows] == ["qualified", "qualified"]
    assert all(row["manifest_sha256"] == summary["provenance"]["manifest_sha256"] for row in rows)
    with pytest.raises(ValueError, match="outside the immutable run package"):
        export_metrics(run, run / "summary.json")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": "RUN_FAILED"}, "expected QUALIFIED"),
        ({"clean": False}, "not clean"),
        ({"plant_class": "UnreviewedPlant"}, "outside the restricted"),
        ({"environment_status": "unknown"}, "environment provenance is unknown"),
    ],
)
def test_restricted_postprocessing_rejects_ineligible_evidence(tmp_path, kwargs, reason):
    run = _published_run(tmp_path / f"ineligible-{len(kwargs)}", **kwargs)

    with pytest.raises(PostprocessEligibilityError, match=reason):
        summarize_run(run)


def test_restricted_postprocessing_rejects_tampered_package_and_metric_mismatch(tmp_path):
    tampered = _published_run(tmp_path / "tampered")
    (tampered / "samples.json").write_text("[]", encoding="utf-8")
    with pytest.raises(PostprocessEligibilityError, match="artifact index is incomplete or inconsistent"):
        summarize_run(tampered)

    inconsistent = _published_run(tmp_path / "inconsistent")
    metrics_path = inconsistent / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["sample_count"] = 10
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    manifest_path = inconsistent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = artifact_index(inconsistent)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(inconsistent, manifest)
    manifest["hashes"] = {"manifest_sha256": manifest["manifest_sha256"], "package_sha256": manifest["package_sha256"]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PostprocessEligibilityError, match="sample_count 10 does not match"):
        summarize_run(inconsistent)
