"""Read-only verification of published run packages."""

from __future__ import annotations

import json

from pe_sim.inspection import inspect_run

from test_workbench_export import _published_buck_run


def _rewrite_manifest(run_dir, manifest):
    from pe_sim.artifacts import artifact_index, manifest_digest, package_digest

    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def test_inspect_run_verifies_a_published_package(tmp_path):
    run_dir, samples, manifest = _published_buck_run(tmp_path)

    result = inspect_run(run_dir)

    assert result.status == "ok", result.problems
    assert result.summary["run_id"] == "buck-demo"
    assert result.summary["samples"]["count"] == len(samples)
    assert result.summary["samples"]["time_monotonic"] is True
    assert "vout" in result.summary["samples"]["signals"]
    assert result.summary["artifact_index_verified"] is True
    assert result.summary["hashes"] == manifest["hashes"]


def test_inspect_run_detects_a_tampered_package(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 99.0
    samples_path.write_text(json.dumps(samples), encoding="utf-8")

    result = inspect_run(run_dir)

    assert result.status == "invalid"
    assert any("package_sha256" in problem or "artifact index" in problem for problem in result.problems)


def test_inspect_run_rechecks_the_recorded_implementation_against_the_current_pin(tmp_path):
    from pe_sim.backends.identity import implementation_digest

    run_dir, _, manifest = _published_buck_run(tmp_path)
    digest = implementation_digest("ngspice-buck")
    manifest["plant"]["id"] = "ngspice-buck"
    manifest["plant"]["identity"]["implementation"] = {
        "sha256": digest,
        "review": {"status": "unreviewed"},
    }
    _rewrite_manifest(run_dir, manifest)

    result = inspect_run(run_dir)

    assert result.status == "ok", result.problems
    implementation = result.summary["plant"]["implementation"]
    assert implementation["review"]["status"] == "unreviewed"
    assert implementation["current_checkout_review"]["status"] == "reviewed"


def test_inspect_run_reports_failure_without_calling_the_package_tampered(tmp_path):
    run_dir, _, manifest = _published_buck_run(tmp_path)
    manifest["status"] = "RUN_FAILED"
    manifest["error"] = "BackendProcessError: simulated failure"
    _rewrite_manifest(run_dir, manifest)

    result = inspect_run(run_dir)

    assert result.status == "ok"
    assert result.summary["run_ok"] is False
    assert result.summary["run_status"] == "RUN_FAILED"
    assert result.summary["error"] == "BackendProcessError: simulated failure"


def test_inspect_run_rejects_a_missing_package(tmp_path):
    import pytest
    from pe_sim.inspection import RunInspectionError

    with pytest.raises(RunInspectionError, match="missing required files"):
        inspect_run(tmp_path)
