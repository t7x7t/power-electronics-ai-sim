"""Focused checks for the declarative per-signal tolerance policy (CRED-04).

The comparison pair is built from the existing workbench-export fixture and then
rewritten into a fully published package: missing required artifacts are added,
the publication manifest is re-derived, and no digest is bypassed.  Only the
left/right sample value differs, so every expectation below is about the
tolerance policy rather than about package validity.
"""

from __future__ import annotations

import json
import shutil
import struct

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.cli import main
from pe_sim.reproducibility import ToleranceSpec, audit_repeated_runs, compare_runs
from test_workbench_export import _published_buck_run


_ENVIRONMENT = {
    "status": "partial",
    "python": {"version": "3.12.7"},
    "platform": {"system": "TestOS", "release": "host-a", "machine": "host-a", "architecture": "64bit"},
    "dependencies": {"numpy": "1.26.4"},
    "backends": [{"status": "not_assessed", "name": "ngspice", "version": None}],
}

_EXTRA_ARTIFACTS = (
    "config.snapshot.json",
    "environment.json",
    "qualification.json",
    "safety.json",
    "metrics.json",
    "events.json",
    "logs/.keep",
)


def _republish(run_dir, samples, *, run_id):
    """Fill in the required artifacts and re-derive every published digest."""

    (run_dir / "samples.json").write_text(json.dumps(samples), encoding="utf-8")
    for name in _EXTRA_ARTIFACTS:
        target = run_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}" if name.endswith(".json") else "", encoding="utf-8")
    (run_dir / ".run-state.json").write_text(
        json.dumps({"run_id": run_id, "status": "PUBLISHED"}), encoding="utf-8"
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    manifest["working_tree_status"] = "clean"
    manifest["provenance"] = {"status": "known"}
    manifest["environment"] = dict(_ENVIRONMENT)
    manifest["controller"] = {"id": "buck-controller", "hash": "c" * 64}
    for key in ("artifacts", "manifest_sha256", "package_sha256", "hashes"):
        manifest.pop(key, None)
    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _comparable_runs(tmp_path, *, right_vout=0.0025):
    """Return two valid published runs that differ only in one sample value."""

    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed, samples, _ = _published_buck_run(seed_root)
    left, right = tmp_path / "run-a", tmp_path / "run-b"
    for name, target, vout in (("run-a", left, 0.0025), ("run-b", right, right_vout)):
        shutil.copytree(seed, target)
        rewritten = [dict(row) for row in samples]
        rewritten[1]["vout"] = vout
        _republish(target, rewritten, run_id=name)
    return left, right


def test_default_policy_reproduces_legacy_absolute_behaviour(tmp_path):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)

    legacy = compare_runs(left, right, tolerance=1e-5)
    explicit = compare_runs(left, right, tolerance=1e-5, tolerance_policy=ToleranceSpec(absolute=1e-5))

    assert legacy.outcome == "tolerance_match"
    assert explicit.outcome == legacy.outcome
    assert legacy.maximum_absolute_error == pytest.approx(4e-6)
    assert explicit.maximum_absolute_error == legacy.maximum_absolute_error
    assert "tolerance_policy" not in legacy.to_dict()
    explicit_payload = explicit.to_dict()
    explicit_payload.pop("tolerance_policy")
    assert explicit_payload == legacy.to_dict()
    assert explicit.differences == legacy.differences
    assert all(item["limit"] == 1e-5 for item in explicit.differences if "limit" in item)


def test_relative_limit_scales_with_reference_value():
    assert ToleranceSpec(absolute=0.0, relative=1e-3).limit_for("vout", 12.0) == 0.012


def test_per_signal_override_wins_over_global_policy():
    absolute_override = ToleranceSpec(absolute=1e-9, per_signal=(("vout", 0.5, 0.0),))
    assert absolute_override.limit_for("vout", 12.0) == 0.5
    assert absolute_override.limit_for("time_s", 12.0) == 1e-9

    relative_override = ToleranceSpec(relative=1e-6, per_signal=(("time_s", 0.0, 1e-3),))
    assert relative_override.limit_for("time_s", 0.0004) == pytest.approx(4e-7)
    assert relative_override.limit_for("vout", 12.0) == pytest.approx(1.2e-5)


def test_zero_relative_limit_is_bitwise_equal_to_absolute():
    policy = ToleranceSpec(absolute=0.1)
    for reference in (0.0, -3.7, 1e300, 1e-300):
        limit = policy.limit_for("vout", reference)
        assert limit == policy.absolute
        assert struct.pack("<d", limit) == struct.pack("<d", 0.1)
    assert struct.pack("<d", ToleranceSpec().limit_for("time_s", 5.0)) == struct.pack("<d", 0.0)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"absolute": -1.0},
        {"relative": -1e-3},
        {"absolute": float("nan")},
        {"relative": float("inf")},
        {"absolute": True},
        {"relative": False},
        {"per_signal": (("vout", True, 0.0),)},
        {"per_signal": (("vout", 1e-3, 0.0), ("vout", 1e-4, 0.0))},
        {"per_signal": (("", 1e-3, 0.0),)},
        {"per_signal": (("   ", 1e-3, 0.0),)},
        {"per_signal": (("vout", 1e-3),)},
        {"per_signal": {"vout": {"absolute": 1e-3}}},
    ),
)
def test_invalid_tolerance_spec_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ToleranceSpec(**kwargs)


@pytest.mark.parametrize(
    "value",
    (
        ["absolute", 1e-3],
        {"abs": 1e-3},
        {"per_signal": 3},
        {"per_signal": {"vout": 0.1}},
        {"per_signal": {"vout": {"absolute": 1e-3, "bogus": 0.0}}},
        {"per_signal": {"vout": {"absolute": -1e-3}}},
        {"absolute": "1e-3"},
    ),
)
def test_invalid_from_mapping_is_rejected(value):
    with pytest.raises(ValueError):
        ToleranceSpec.from_mapping(value)


def test_from_mapping_accepts_documented_structure():
    policy = ToleranceSpec.from_mapping(
        {"absolute": 1e-6, "relative": 1e-4, "per_signal": {"vout": {"absolute": 1e-3, "relative": 0.0}}}
    )
    assert policy.absolute == 1e-6
    assert policy.relative == 1e-4
    assert policy.limit_for("vout", 100.0) == 1e-3
    assert policy.limit_for("inductor_current", 100.0) == pytest.approx(1e-2)


def test_relative_policy_turns_mismatch_into_tolerance_match(tmp_path):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)

    strict = compare_runs(left, right)
    assert strict.outcome == "mismatch"
    assert strict.differences[0]["absolute_error"] > strict.differences[0]["limit"]

    relaxed = compare_runs(left, right, tolerance_policy=ToleranceSpec(relative=1e-2))
    assert relaxed.outcome == "tolerance_match"
    assert relaxed.differences[0]["limit"] == pytest.approx(2.5e-5)

    mapped = compare_runs(left, right, tolerance_policy={"relative": 1e-2})
    assert mapped.outcome == "tolerance_match"


def test_report_policy_payload_is_complete_and_stable(tmp_path):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)
    policy = ToleranceSpec(relative=1e-3, per_signal=(("vout", 0.0, 1e-2),))

    first = compare_runs(left, right, tolerance_policy=policy).to_dict()["tolerance_policy"]
    rebuilt = ToleranceSpec.from_mapping(policy.to_dict())
    second = compare_runs(left, right, tolerance_policy=rebuilt).to_dict()["tolerance_policy"]

    assert len(first["sha256"]) == 64
    assert first["sha256"] == second["sha256"]
    assert first["absolute"] == 0.0
    assert first["relative"] == 1e-3
    assert first["signals"] == ["vout"]
    assert first["per_signal"]["vout"] == {"absolute": 0.0, "relative": 1e-2}


def test_audit_repeated_runs_forwards_the_policy(tmp_path):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)

    assert audit_repeated_runs([left, right]).outcome == "mismatch"
    relaxed = audit_repeated_runs([left, right], tolerance_policy=ToleranceSpec(relative=1e-2))
    assert relaxed.outcome == "tolerance_match"
    assert relaxed.pairwise[0]["tolerance_policy"]["relative"] == 1e-2


def test_cli_relative_tolerance_reports_policy(tmp_path, capsys):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)

    code = main(["compare-runs", str(left), str(right), "--relative-tolerance", "1e-2"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["outcome"] == "tolerance_match"
    assert payload["tolerance_policy"]["relative"] == 1e-2
    assert payload["tolerance_policy"]["sha256"]
    assert payload["tolerance_policy"]["signals"] == []


def test_cli_signal_tolerance_override(tmp_path, capsys):
    left, right = _comparable_runs(tmp_path, right_vout=0.002504)

    code = main(["compare-runs", str(left), str(right), "--signal-tolerance", "vout:0.01:0"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["outcome"] == "tolerance_match"
    assert payload["tolerance_policy"]["signals"] == ["vout"]
    assert payload["tolerance_policy"]["per_signal"]["vout"] == {"absolute": 0.01, "relative": 0.0}


def test_cli_rejects_malformed_signal_tolerance(tmp_path, capsys):
    left, right = _comparable_runs(tmp_path)

    code = main(["compare-runs", str(left), str(right), "--signal-tolerance", "bad-format"])

    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["status"] == "CONFIG_REJECTED"
    assert "NAME:ABSOLUTE:RELATIVE" in payload["error"]


def test_cli_rejects_negative_relative_tolerance(tmp_path, capsys):
    left, right = _comparable_runs(tmp_path)

    code = main(["compare-runs", str(left), str(right), "--relative-tolerance", "-1"])

    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["status"] == "CONFIG_REJECTED"


def test_cli_default_output_has_no_policy_key(tmp_path, capsys):
    left, right = _comparable_runs(tmp_path)

    code = main(["compare-runs", str(left), str(right)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["outcome"] == "exact_match"
    assert "tolerance_policy" not in payload
