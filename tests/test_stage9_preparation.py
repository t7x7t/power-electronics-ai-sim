import json
import importlib.metadata
import sys

from pe_sim.baseline import build_baseline_report
from pe_sim.environment import check_recommended_environment
from pe_sim.reproducibility import compare_runs


def _manifest(*, status="clean", environment_status="partial", source_commit="a" * 40):
    return {
        "source_commit": source_commit,
        "branch": "main",
        "working_tree_status": status,
        "provenance": {"status": "known" if status == "clean" else "exploratory"},
        "environment": {
            "status": environment_status,
            "python": {"version": "3.12.7"},
        },
        "plant": {"id": "fixture", "hash": "p" * 64},
        "controller": {"id": "fixture", "hash": "c" * 64},
        "contracts": {},
        "timebase": {"control_period_s": 0.001, "duration_s": 0.002},
        "initial_state": {},
        "random_seed": 0,
    }


def _run_dir(path, manifest, samples):
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (path / "samples.json").write_text(json.dumps(samples), encoding="utf-8")


def test_recommended_environment_reports_verified_and_supported_statuses(tmp_path):
    current_python = ".".join(map(str, sys.version_info[:3]))
    requirements = tmp_path / "requirements-tested.txt"
    requirements.write_text(
        "\n".join(
            f"{name}=={importlib.metadata.version(name)}"
            for name in ("pytest", "jsonschema", "numpy")
        )
        + "\n",
        encoding="utf-8",
    )
    result = check_recommended_environment(
        requirements_path=requirements,
        verified_python_version=current_python,
    )
    assert result.status == "pass"
    value = result.to_dict()
    assert value["python"]["verified_status"] == "match"
    assert value["python"]["supported_status"] == "supported"
    assert value["backend"]["status"] == "not_assessed"


def test_environment_pin_mismatch_fails_without_claiming_supported_range(tmp_path):
    requirements = tmp_path / "requirements-tested.txt"
    requirements.write_text("pytest==0.0.1\n", encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nrequires-python = '>=3.10'\n[project.optional-dependencies]\ntest=['pytest>=7.0']\n", encoding="utf-8")
    result = check_recommended_environment(requirements, pyproject)
    assert result.status == "fail"
    assert result.packages["pytest"]["verified_status"] == "mismatch"
    assert result.packages["pytest"]["supported_status"] == "supported"


def test_explicit_unavailable_backend_fails_environment_preflight(tmp_path):
    class MissingBackend:
        def backend_provenance(self):
            return {"name": "ngspice", "status": "unavailable", "version": None}

    result = check_recommended_environment(backend_adapters=(MissingBackend(),))
    assert result.status == "fail"
    assert result.backend["status"] == "unknown"


def test_environment_ignores_non_matching_python_version_marker(tmp_path):
    requirements = tmp_path / "requirements-tested.txt"
    requirements.write_text('tomli==2.3.0; python_version < "3.11"\n', encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nrequires-python = '>=3.10'\ndependencies = [\"tomli>=2.0.1; python_version < '3.11'\"]\n",
        encoding="utf-8",
    )
    result = check_recommended_environment(requirements, pyproject)
    if sys.version_info >= (3, 11):
        assert "tomli" not in result.packages


def test_reproducibility_exact_and_tolerance_outcomes(tmp_path):
    samples = [{"step_index": 0, "time_s": 0.0, "vout": 1.0}]
    left_manifest = _manifest()
    right_manifest = _manifest()
    _run_dir(tmp_path / "left", left_manifest, samples)
    _run_dir(tmp_path / "right", right_manifest, samples)
    exact = compare_runs(tmp_path / "left", tmp_path / "right")
    assert exact.outcome == "exact_match"
    (tmp_path / "right" / "samples.json").write_text(json.dumps([{**samples[0], "vout": 1.0 + 1e-10}]), encoding="utf-8")
    tolerant = compare_runs(tmp_path / "left", tmp_path / "right", tolerance=1e-9)
    assert tolerant.outcome == "tolerance_match"


def test_reproducibility_rejects_dirty_and_unknown_environment(tmp_path):
    samples = [{"step_index": 0, "vout": 1.0}]
    dirty = _manifest(status="dirty")
    unknown = _manifest(environment_status="unknown")
    _run_dir(tmp_path / "dirty", dirty, samples)
    _run_dir(tmp_path / "unknown", unknown, samples)
    assert compare_runs(tmp_path / "dirty", tmp_path / "dirty").outcome == "unknown"
    assert compare_runs(tmp_path / "unknown", tmp_path / "unknown").outcome == "unknown"
    missing_environment_status = _manifest()
    missing_environment_status["environment"].pop("status")
    _run_dir(tmp_path / "missing-status", missing_environment_status, samples)
    assert compare_runs(tmp_path / "missing-status", tmp_path / "missing-status").outcome == "unknown"


def test_reproducibility_rejects_unavailable_source_and_reports_environment_difference(tmp_path):
    samples = [{"step_index": 0, "vout": 1.0}]
    unavailable = _manifest(source_commit="unknown")
    _run_dir(tmp_path / "unavailable", unavailable, samples)
    assert compare_runs(tmp_path / "unavailable", tmp_path / "unavailable").outcome == "unknown"
    left = _manifest()
    right = _manifest()
    right["environment"] = {"status": "partial", "python": {"version": "3.11.9"}}
    _run_dir(tmp_path / "env-left", left, samples)
    _run_dir(tmp_path / "env-right", right, samples)
    report = compare_runs(tmp_path / "env-left", tmp_path / "env-right")
    assert report.outcome == "unknown"
    assert report.differences[0]["path"] == "environment"


def test_baseline_report_keeps_ci_and_review_unrecorded_by_default():
    report = build_baseline_report(_manifest())
    assert report["ci"]["status"] == "not_recorded"
    assert report["human_review"]["status"] == "not_recorded"
    assert report["source_commit"] == "a" * 40
