import json
import sys

from pe_sim.environment import check_recommended_environment
from pe_sim.provenance import ExecutableBackendAdapter, collect_backend_provenance, collect_environment_provenance
from pe_sim.reproducibility import audit_repeated_runs, compare_runs, cross_machine_evidence


def _manifest(*, machine="host-a", value=1.0):
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
    }


def _run(path, manifest, value=1.0):
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (path / "samples.json").write_text(json.dumps([{"step_index": 0, "vout": value}]), encoding="utf-8")


def test_backend_adapter_and_not_assessed_environment_semantics():
    class Adapter:
        def backend_provenance(self):
            return {"name": "ngspice", "version": "42", "solver_settings": {"reltol": 1e-3}}

    evidence = collect_backend_provenance(Adapter())
    assert evidence["status"] == "known"
    assert evidence["version"] == "42"
    assert collect_backend_provenance(None)["status"] == "not_assessed"
    environment = collect_environment_provenance(backend_adapters=(Adapter(),))
    assert environment["backends"][0]["version"] == "42"
    assert environment["backends"][0]["solver_settings"]["reltol"] == 1e-3
    checked = check_recommended_environment(backend_adapters=(Adapter(),))
    assert checked.backend["status"] == "known"


def test_executable_backend_adapter_records_version_output_digest():
    adapter = ExecutableBackendAdapter(
        sys.executable,
        name="test-backend",
        version_args=("-c", "print('test-backend version 4.2.1')"),
        solver_settings={"threads": 1},
    )
    evidence = adapter.backend_provenance()
    assert evidence["status"] == "known"
    assert evidence["version"] == "4.2.1"
    assert len(evidence["version_output_sha256"]) == 64


def test_executable_backend_without_solver_settings_is_partial():
    adapter = ExecutableBackendAdapter(
        sys.executable,
        name="test-backend",
        version_args=("-c", "print('test-backend version 4.2.1')"),
    )
    evidence = adapter.backend_provenance()
    assert evidence["status"] == "partial"
    assert any("solver settings" in item for item in evidence["limitations"])


def test_repetition_audit_aggregates_pairwise_outcomes(tmp_path):
    for name, value in (("a", 1.0), ("b", 1.0 + 1e-10), ("c", 1.0)):
        _run(tmp_path / name, _manifest(), value)
    report = audit_repeated_runs([tmp_path / "a", tmp_path / "b", tmp_path / "c"], tolerance=1e-9)
    assert report.outcome == "tolerance_match"
    assert report.tolerance_pairs == 2
    assert report.exact_pairs == 1
    assert report.reproducible


def test_cross_machine_evidence_is_ready_only_for_distinct_platforms(tmp_path):
    _run(tmp_path / "a", _manifest(machine="host-a"))
    _run(tmp_path / "b", _manifest(machine="host-b"))
    evidence = cross_machine_evidence([tmp_path / "a", tmp_path / "b"])
    assert evidence["status"] == "ready_for_trial"
    assert evidence["scope"] == "cross_machine"
    assert any("numeric equivalence" in item for item in evidence["limitations"])


def test_compare_report_attributes_numeric_tolerance_difference(tmp_path):
    _run(tmp_path / "a", _manifest(), 1.0)
    _run(tmp_path / "b", _manifest(), 1.0 + 1e-10)
    report = compare_runs(tmp_path / "a", tmp_path / "b", tolerance=1e-9)
    assert report.outcome == "tolerance_match"
    assert report.identity_equal is True
    assert report.environment_equal is True
    assert report.difference_summary["numeric_within_tolerance"] == 1
    assert report.to_dict()["identity"]["left_hash"]


def test_compare_distinguishes_exact_sample_hash_from_equal_numeric_value(tmp_path):
    _run(tmp_path / "a", _manifest(), 1)
    _run(tmp_path / "b", _manifest(), 1.0)
    report = compare_runs(tmp_path / "a", tmp_path / "b")
    assert report.outcome == "tolerance_match"
    assert report.exact is False
    assert report.sample_hash_equal is False
    assert report.differences[0]["attribution"] == "numeric_representation"


def test_compare_rejects_explicitly_incomplete_backend_identity(tmp_path):
    left_manifest = _manifest()
    right_manifest = _manifest()
    left_manifest["environment"]["backends"] = [{"status": "unavailable", "name": "ngspice", "version": None}]
    right_manifest["environment"]["backends"] = [{"status": "unavailable", "name": "ngspice", "version": None}]
    _run(tmp_path / "left", left_manifest)
    _run(tmp_path / "right", right_manifest)
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert not report.exact
    assert "backend provenance is unavailable" in report.differences[0]["reason"]


def test_cross_machine_evidence_requires_aligned_source_commit(tmp_path):
    left_manifest = _manifest(machine="host-a")
    right_manifest = _manifest(machine="host-b")
    right_manifest["source_commit"] = "b" * 40
    _run(tmp_path / "left", left_manifest)
    _run(tmp_path / "right", right_manifest)
    evidence = cross_machine_evidence([tmp_path / "left", tmp_path / "right"])
    assert evidence["status"] == "not_assessed"
    assert any("source commits differ" in item for item in evidence["limitations"])


def test_compare_rejects_attempted_but_unavailable_backend(tmp_path):
    left = _manifest()
    right = _manifest()
    for manifest in (left, right):
        manifest["environment"]["backends"] = [{"status": "unavailable", "name": "ngspice", "version": None}]
    _run(tmp_path / "left", left)
    _run(tmp_path / "right", right)
    report = compare_runs(tmp_path / "left", tmp_path / "right")
    assert report.outcome == "unknown"
    assert report.differences[0]["attribution"] == "environment"
