"""Detached attestation, Git anchoring, and L2-compatible signatures."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from pe_sim.artifacts import package_digest
from pe_sim.attestation import (
    ATTESTATION_VERIFICATION_SCHEMA,
    ATTESTATION_WRITE_SCHEMA,
    attest_run,
    build_payload,
    payload_digest,
    register_signature_verifier,
    unregister_signature_verifier,
    verify_attestation,
)
from pe_sim.cli import main as cli_main

from test_workbench_export import _published_buck_run


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def _commit_attestations(repo: Path) -> None:
    _git(repo, "add", "attestations")
    _git(repo, "commit", "-q", "-m", "attest run")


def test_payload_matches_the_published_package(tmp_path):
    run_dir, _, manifest = _published_buck_run(tmp_path)
    payload, loaded = build_payload(run_dir)

    assert loaded["run_id"] == manifest["run_id"]
    assert payload.manifest_sha256 == manifest["manifest_sha256"]
    assert payload.package_sha256 == manifest["package_sha256"]
    assert payload.run_state_status == "PUBLISHED"
    assert payload.package_status == manifest["status"]


def test_attestation_is_written_outside_the_package_and_does_not_change_it(tmp_path):
    run_dir, _, manifest = _published_buck_run(tmp_path)
    before = package_digest(run_dir, manifest)
    destination = tmp_path / "attestations"

    result = attest_run(run_dir, attestations_dir=destination, anchor=False)

    assert result.status == "ATTESTED"
    assert result.attestation_path is not None
    assert not Path(result.attestation_path).resolve().is_relative_to(run_dir.resolve())
    assert package_digest(run_dir, manifest) == before


def test_attestation_views_are_schema_tagged(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    result = attest_run(run_dir, attestations_dir=tmp_path / "attestations", anchor=False)

    written = result.to_dict()
    assert written["schema"] == ATTESTATION_WRITE_SCHEMA
    assert next(iter(written)) == "schema"

    verification = verify_attestation(run_dir, result.attestation_path, check_git=False)
    verified = verification.to_dict()
    assert verified["status"] == "ok"
    assert verified["schema"] == ATTESTATION_VERIFICATION_SCHEMA
    assert next(iter(verified)) == "schema"


def test_tampered_sample_bytes_are_detected(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    destination = tmp_path / "attestations"
    result = attest_run(run_dir, attestations_dir=destination, anchor=False)
    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 99.0
    samples_path.write_text(json.dumps(samples), encoding="utf-8")

    verification = verify_attestation(run_dir, result.attestation_path, check_git=False)

    assert verification.status == "invalid"
    assert verification.trust_level == "none"
    assert any("package_sha256" in problem or "artifact index" in problem for problem in verification.problems)


def test_tampered_hashes_summary_is_detected(tmp_path):
    run_dir, _, manifest = _published_buck_run(tmp_path)
    destination = tmp_path / "attestations"
    result = attest_run(run_dir, attestations_dir=destination, anchor=False)
    manifest["hashes"]["package_sha256"] = "0" * 64
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    verification = verify_attestation(run_dir, result.attestation_path, check_git=False)

    assert verification.status == "invalid"
    # The wording comes from the single package-integrity implementation, so
    # assert on the defect rather than on one reader's phrasing.
    assert any("hash summary" in problem for problem in verification.problems)


def test_reattestation_is_idempotent(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)

    first = attest_run(run_dir, repo_root=repository)
    second = attest_run(run_dir, repo_root=repository)
    index_lines = [
        line
        for line in (repository / "attestations" / "anchor-index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert first.status == second.status == "ATTESTED"
    assert second.idempotent is True
    assert second.attestation_written is False
    assert first.payload_sha256 == second.payload_sha256
    assert len(index_lines) == 1


def test_committed_anchor_reaches_l1_anchored(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)

    verification = verify_attestation(run_dir, result.attestation_path, repo_root=repository)

    assert verification.status == "ok"
    assert verification.trust_level == "L1_anchored"
    assert verification.anchor.status == "committed"
    assert verification.anchor.attestation_in_commit is True
    assert verification.anchor.anchor_commit is not None


def test_uncommitted_anchor_is_pending(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)

    verification = verify_attestation(run_dir, result.attestation_path, repo_root=repository)

    assert verification.status == "ok"
    assert verification.trust_level == "L1_pending"
    assert verification.anchor.status == "uncommitted"


def test_dirty_worktree_does_not_downgrade_a_committed_anchor(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)
    (repository / "untracked.txt").write_text("dirty", encoding="utf-8")

    verification = verify_attestation(run_dir, result.attestation_path, repo_root=repository)

    assert verification.trust_level == "L1_anchored"
    assert verification.anchor.worktree_status == "dirty"


def test_appending_a_signature_does_not_break_l1(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)
    path = Path(result.attestation_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["signatures"].append({"format": "ed25519", "signature_b64": "placeholder"})
    path.write_text(json.dumps(document), encoding="utf-8")

    verification = verify_attestation(run_dir, path, repo_root=repository)

    assert verification.trust_level == "L1_anchored"
    assert verification.signature_status == "unknown"


def test_registered_signature_verifier_reports_valid_and_invalid(tmp_path):
    class Verifier:
        def __init__(self, result):
            self.result = result

        def verify(self, *, payload_sha256, signature):
            return self.result

    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)
    path = Path(result.attestation_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["signatures"].append({"format": "test-signature", "value": "x"})
    path.write_text(json.dumps(document), encoding="utf-8")

    try:
        register_signature_verifier("test-signature", Verifier("valid"))
        valid = verify_attestation(run_dir, path, repo_root=repository)
        assert valid.signature_status == "valid"
        assert valid.trust_level == "L1_anchored"

        register_signature_verifier("test-signature", Verifier("invalid"))
        invalid = verify_attestation(run_dir, path, repo_root=repository)
        assert invalid.signature_status == "invalid"
        assert invalid.trust_level == "L1_anchored"
    finally:
        unregister_signature_verifier("test-signature")


def test_non_repository_anchor_is_explicitly_pending(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    destination = tmp_path / "attestations"
    result = attest_run(run_dir, attestations_dir=destination, anchor=False)

    verification = verify_attestation(
        run_dir,
        result.attestation_path,
        repo_root=tmp_path / "not-a-repository",
    )

    assert verification.status == "ok"
    assert verification.trust_level == "L1_pending"
    assert verification.anchor.status == "not_a_repository"


def test_moved_package_with_identical_bytes_still_verifies(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)
    moved = tmp_path / "moved" / "buck-demo"
    shutil.copytree(run_dir, moved)

    verification = verify_attestation(moved, result.attestation_path, repo_root=repository)

    assert verification.trust_level == "L1_anchored"


def test_inspect_run_with_attestation_exposes_authenticity(tmp_path):
    from pe_sim.inspection import inspect_run

    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)
    result = attest_run(run_dir, repo_root=repository)
    _commit_attestations(repository)

    inspection = inspect_run(
        run_dir,
        attestation_path=result.attestation_path,
        repo_root=repository,
    )

    assert inspection.status == "ok"
    assert inspection.summary["attestation"]["trust_level"] == "L1_anchored"
    authenticity = next(
        item
        for item in inspection.summary["credibility"]["factors"]
        if item["factor_id"] == "authenticity"
    )
    assert authenticity["status"] == "pass"


def test_cli_attest_and_verify_exit_codes(tmp_path):
    run_dir, _, _ = _published_buck_run(tmp_path)
    repository = _repository(tmp_path)

    assert cli_main(["attest-run", str(run_dir), "--repo", str(repository)]) == 0
    attestation = repository / "attestations" / "buck-demo.attestation.json"
    assert attestation.is_file()
    assert cli_main(
        [
            "verify-attestation",
            str(run_dir),
            "--attestation",
            str(attestation),
            "--repo",
            str(repository),
        ]
    ) == 1
    _commit_attestations(repository)
    assert cli_main(
        [
            "verify-attestation",
            str(run_dir),
            "--attestation",
            str(attestation),
            "--repo",
            str(repository),
        ]
    ) == 0
    assert cli_main(
        [
            "inspect-run",
            str(run_dir),
            "--attestation",
            str(attestation),
            "--repo",
            str(repository),
            "--require-anchored",
        ]
    ) == 0
