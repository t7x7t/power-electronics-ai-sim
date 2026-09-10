import json

from pe_sim.artifacts import ArtifactWriter, artifact_index, manifest_digest, package_digest
from pe_sim.reproducibility import compare_runs, cross_machine_evidence


def _manifest(machine="host-a", status="QUALIFIED"):
    return {
        "source_commit": "a" * 40,
        "working_tree_status": "clean",
        "provenance": {"status": "known"},
        "environment": {
            "status": "partial",
            "python": {"version": "3.12.7"},
            "platform": {"system": "TestOS", "release": machine, "machine": machine, "architecture": "64bit"},
            "dependencies": {"numpy": "1.26.4"},
            "backends": [{"status": "not_assessed", "name": "ngspice", "version": None}],
        },
        "plant": {"id": "fixture", "hash": "p" * 64},
        "controller": {"id": "fixture", "hash": "c" * 64},
        "contracts": {},
        "timebase": {"control_period_s": 0.001, "duration_s": 0.002},
        "initial_state": {},
        "random_seed": 0,
        "action_policy": {},
        "status": status,
    }


def _published(path, *, status="QUALIFIED", machine="host-a"):
    path.mkdir()
    for name in ArtifactWriter.REQUIRED:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "manifest.json":
            continue
        if name == "samples.json":
            target.write_text(json.dumps([{"step_index": 0, "vout": 1.0}]), encoding="utf-8")
        else:
            target.write_bytes(b"fixture")
    (path / ".run-state.json").write_text(json.dumps({"status": "PUBLISHED"}), encoding="utf-8")
    manifest = _manifest(machine=machine, status=status)
    manifest["artifacts"] = artifact_index(path)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(path, manifest)
    manifest["hashes"] = {"manifest_sha256": manifest["manifest_sha256"], "package_sha256": manifest["package_sha256"]}
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_compare_rejects_failed_and_incomplete_statuses(tmp_path):
    _published(tmp_path / "left", status="RUN_FAILED")
    _published(tmp_path / "right")
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert report.differences[0]["attribution"] == "integrity"
    assert "not publishable" in report.differences[0]["reason"]


def test_compare_rejects_tampered_artifact_and_digest(tmp_path):
    _published(tmp_path / "left")
    _published(tmp_path / "right")
    (tmp_path / "right" / "samples.json").write_text("tampered", encoding="utf-8")
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert "artifact hash mismatch" in report.differences[0]["reason"]


def test_compare_rejects_missing_published_state_marker(tmp_path):
    _published(tmp_path / "left")
    _published(tmp_path / "right")
    (tmp_path / "right" / ".run-state.json").unlink()
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert report.differences[0]["attribution"] == "integrity"


def test_compare_rejects_missing_summary_and_invalid_commit(tmp_path):
    _published(tmp_path / "left")
    manifest_path = tmp_path / "left" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("package_sha256")
    manifest["hashes"].pop("package_sha256")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _published(tmp_path / "right")
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert "package_sha256" in report.differences[0]["reason"]

    manifest = json.loads((tmp_path / "right" / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_commit"] = "deadbeef"
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(tmp_path / "right", manifest)
    manifest["hashes"] = {"manifest_sha256": manifest["manifest_sha256"], "package_sha256": manifest["package_sha256"]}
    (tmp_path / "right" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = compare_runs(tmp_path / "right", tmp_path / "right")
    assert report.outcome == "unknown"
    assert "full hexadecimal Git object id" in report.differences[0]["reason"]


def test_cross_machine_evidence_records_integrity_failure(tmp_path):
    _published(tmp_path / "left", machine="host-a")
    _published(tmp_path / "right", machine="host-b")
    (tmp_path / "right" / "metrics.json").write_text("tampered", encoding="utf-8")
    evidence = cross_machine_evidence([tmp_path / "left", tmp_path / "right"])
    assert evidence["status"] == "not_assessed"
    assert any("artifact hash mismatch" in item for item in evidence["limitations"])
