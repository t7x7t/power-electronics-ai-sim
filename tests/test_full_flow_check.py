"""End-to-end proof of ``scripts/full_flow_check.py`` (``docs/56``).

The check itself walks the documented flow; these tests run it in temporary
directories and assert the contract: the exit-code mapping, the step order, the
required/optional outcome rules, and the machine-readable record.

The audit step must work on either side of the Git boundary: an output
directory inside the repository is anchored, an output directory outside it is
written with ``--no-anchor`` and recorded as a limitation instead of a failure
(``docs/56``).  ``tmp_path`` can land on either side depending on where
``--basetemp`` points, so both cases are exercised explicitly rather than
inferred from the pytest temporary root.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SOURCE_ROOT / "scripts" / "full_flow_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("full_flow_check", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``from __future__ import annotations`` makes dataclasses resolve string
    # annotations through ``sys.modules``, so the module must be registered
    # before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FLOW = _load_module()


def _record(output: Path) -> dict:
    path = output / "full-flow-check.json"
    assert path.is_file(), f"the check wrote no record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _step(record: dict, step_id: str) -> dict:
    for step in record["steps"]:
        if step["id"] == step_id:
            return step
    raise AssertionError(f"the record carries no {step_id!r} step")


def _evidence(step: dict) -> str:
    return " ".join(str(item) for item in step["evidence"])


def _run_check(output: Path) -> tuple[int, dict]:
    exit_code = FLOW.main(["--output", str(output)])
    assert exit_code in (0, 2), f"the flow check returned {exit_code}"
    return exit_code, _record(output)


def test_full_flow_end_to_end(tmp_path):
    output = tmp_path / "flow"
    exit_code = FLOW.main(["--output", str(output)])

    assert exit_code in (0, 2), f"the flow check returned {exit_code}"
    record = _record(output)
    assert record["schema_version"] == FLOW.FULL_FLOW_SCHEMA
    assert record["rule_version"] == FLOW.FULL_FLOW_RULE_VERSION
    assert record["outcome"] == ("pass" if exit_code == 0 else "partial")

    steps = record["steps"]
    assert tuple(step["id"] for step in steps) == FLOW.STEP_ORDER
    for step in steps:
        assert set(step) == {"id", "title", "required", "status", "detail", "evidence"}
        assert step["title"]
        if step["required"]:
            assert step["status"] == "pass", step
        else:
            assert step["status"] in {"pass", "skip"}, step
            if step["status"] == "skip":
                assert step["detail"].strip(), step

    run_dir = Path(record["workspace"]["run_dir"])
    assert run_dir.is_dir()
    assert (run_dir / "manifest.json").is_file()

    assert record["limitations"]
    assert (output / "workbench" / "topology.json").is_file()


def test_json_output_is_the_contract_envelope(tmp_path, capsys):
    output = tmp_path / "flow-json"
    exit_code = FLOW.main(["--output", str(output), "--json"])

    assert exit_code in (0, 2), f"the flow check returned {exit_code}"
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == "pe-sim.full-flow-check.v1"
    assert document["rule_version"] == "full-flow-check-v1"
    assert document["outcome"] in {"pass", "partial"}
    assert tuple(step["id"] for step in document["steps"]) == FLOW.STEP_ORDER


def test_containment_matches_the_producer_rule():
    """The containment helper asks what ``pe_sim.attestation`` asks."""

    assert FLOW._inside_repository(SOURCE_ROOT, SOURCE_ROOT)
    assert FLOW._inside_repository(SOURCE_ROOT / ".tmp" / "flow" / "attestations", SOURCE_ROOT)
    assert not FLOW._inside_repository(SOURCE_ROOT.parent / "elsewhere" / "attestations", SOURCE_ROOT)
    if os.name == "nt":
        # Windows reaches the same directory through either case of the drive
        # letter; both sides are resolved before they are compared.
        spelled = Path(str(SOURCE_ROOT).swapcase()) / "attestations"
        assert FLOW._inside_repository(spelled, SOURCE_ROOT)


def test_audit_step_is_unanchored_outside_the_repository():
    """An output directory outside the repository never fails the audit step."""

    outside = Path(tempfile.mkdtemp(prefix="pe-sim-flow-outside-"))
    try:
        assert not FLOW._inside_repository(outside / "attestations", SOURCE_ROOT), (
            f"{outside} is inside the repository, so the outside case was not exercised"
        )
        exit_code, record = _run_check(outside)

        audit = _step(record, "audit")
        assert audit["status"] in {"pass", "skip"}, audit
        if audit["status"] == "skip":
            pytest.skip(f"the audit step was skipped: {audit['detail']}")

        # The unanchored decision is recorded, not hidden: the evidence names
        # the reason and the limitation keeps the envelope at ``partial``.
        assert "anchor=disabled" in _evidence(audit), audit
        assert "attestations_dir_outside_repository" in _evidence(audit), audit
        assert record["outcome"] == "partial"
        assert exit_code == 2
        assert any("--no-anchor" in item for item in record["limitations"]), record[
            "limitations"
        ]
        written = outside / "attestations" / f"{FLOW.PRIMARY_RUN_ID}.attestation.json"
        assert written.is_file(), f"attest-run wrote no attestation under {outside}"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_audit_step_stays_anchored_inside_the_repository():
    """An output directory inside the repository keeps the anchored call."""

    temp_root = SOURCE_ROOT / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    inside = Path(tempfile.mkdtemp(dir=temp_root, prefix="flow-in-repo-"))
    try:
        assert FLOW._inside_repository(inside / "attestations", SOURCE_ROOT)
        exit_code, record = _run_check(inside)

        audit = _step(record, "audit")
        assert audit["status"] in {"pass", "skip"}, audit
        if audit["status"] == "skip":
            pytest.skip(f"the audit step was skipped: {audit['detail']}")

        evidence = _evidence(audit)
        assert "anchor=disabled" not in evidence, audit
        assert "--no-anchor" not in evidence, audit
        assert "--repo" in evidence, audit
        written = inside / "attestations" / f"{FLOW.PRIMARY_RUN_ID}.attestation.json"
        assert written.is_file(), f"attest-run wrote no attestation under {inside}"
    finally:
        shutil.rmtree(inside, ignore_errors=True)
