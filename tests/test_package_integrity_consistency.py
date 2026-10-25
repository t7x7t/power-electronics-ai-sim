"""Every package reader must apply the same self-consistency rule.

The rule "the recorded derived hashes must agree with the manifest and with the
files on disk" used to be implemented once per reader: publication
(`artifacts`), inspection, attestation, restricted post-processing, workbench
export, and reproducibility comparison.  The copies had already drifted — until
0.9.3 ``inspect-run`` accepted a package whose ``hashes`` summary had been
altered, while every other reader rejected it.

These tests pin the *shared* behaviour, so a future reader cannot quietly
implement a weaker variant.
"""

from __future__ import annotations

import json

import pytest

from pe_sim.artifacts import _validate_runtime_manifest
from pe_sim.attestation import AttestationError, build_payload
from pe_sim.inspection import inspect_run
from pe_sim.postprocess import PostprocessEligibilityError, load_run_evidence

from test_workbench_export import _published_buck_run


def _corrupt_hash_summary(run_dir):
    """Alter only the derived-hash summary.

    ``manifest_digest`` and ``package_digest`` both strip the derived fields
    before hashing, so this edit leaves both digests valid and isolates the
    summary rule from the digest rules.
    """

    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["hashes"]["package_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def _corrupt_package_contents(run_dir):
    """Change an immutable artifact without touching the manifest."""

    samples_path = run_dir / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples[0]["vout"] = 99.0
    samples_path.write_text(json.dumps(samples), encoding="utf-8")


def _from_inspection(run_dir):
    return list(inspect_run(run_dir).problems)


def _from_attestation(run_dir):
    try:
        build_payload(run_dir)
    except AttestationError as exc:
        return [str(exc)]
    return []


def _from_postprocess(run_dir):
    try:
        load_run_evidence(run_dir)
    except PostprocessEligibilityError as exc:
        return [str(exc)]
    return []


def _from_publication(run_dir):
    try:
        _validate_runtime_manifest(run_dir)
    except ValueError as exc:
        return [str(exc)]
    return []


READERS = {
    "attestation": _from_attestation,
    "inspection": _from_inspection,
    "postprocess": _from_postprocess,
    "publication": _from_publication,
}

# ``load_run_evidence`` additionally requires clean-worktree and known-environment
# provenance.  That is a separate eligibility rule with its own tests, so the
# intact-package case is asserted only for the readers that only ask "is this
# package self-consistent?".
INTEGRITY_ONLY_READERS = ("attestation", "inspection", "publication")


@pytest.mark.parametrize("reader", sorted(READERS))
def test_tampered_hash_summary_is_rejected_by_every_reader(tmp_path, reader):
    """Regression: ``inspection`` used to be the one reader that stayed silent."""

    run_dir, _, _ = _published_buck_run(tmp_path)
    _corrupt_hash_summary(run_dir)

    problems = READERS[reader](run_dir)

    assert problems, f"{reader} accepted a package with an altered hash summary"
    assert any("summary" in problem for problem in problems), problems


@pytest.mark.parametrize("reader", sorted(READERS))
def test_tampered_package_contents_is_rejected_by_every_reader(tmp_path, reader):
    run_dir, _, _ = _published_buck_run(tmp_path)
    _corrupt_package_contents(run_dir)

    problems = READERS[reader](run_dir)

    assert problems, f"{reader} accepted a package with altered contents"
    assert any(
        "package_sha256" in problem or "artifact" in problem for problem in problems
    ), problems


@pytest.mark.parametrize("reader", INTEGRITY_ONLY_READERS)
def test_an_intact_package_is_accepted_by_every_reader(tmp_path, reader):
    run_dir, _, _ = _published_buck_run(tmp_path)

    assert READERS[reader](run_dir) == []
