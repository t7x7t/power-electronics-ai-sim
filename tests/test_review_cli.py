"""CLI surface for the text-first run review.

Every test assembles a self-contained, integrity-verified package in
``tmp_path`` from the checked-in visualization fixture and then reads it back
through ``pe_sim.cli.main``.  Nothing here reads ``dist/``, the network, or the
developer worktree, and no model is executed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pe_sim import __version__
from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.cli import main

FIXTURE = Path(__file__).with_name("fixtures") / "visualization_service" / "buck-contract-fixture"
REVIEW_SCHEMA = "pe-sim.run-review.v1"


def _rewrite_manifest(run_dir: Path, manifest: dict) -> None:
    """Recompute every derived hash in the order the publisher would."""

    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _package(root: Path) -> Path:
    """Copy the fixture and add the sections the review reads."""

    run_dir = root / "buck-review"
    shutil.copytree(FIXTURE, run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["controller"] = {
        "id": "pi",
        "hash": "1" * 64,
        "identity": {
            "kind": "runtime_component",
            "module": "pe_sim.runtime",
            "class": "FakePIController",
            "parameters": {"kp": 0.5, "ki": 100.0, "output_min": 0.0, "output_max": 1.0},
            "run_configuration": {"controller_id": "pi"},
        },
    }
    manifest["timebase"] = {
        "unit": "s",
        "control_period_s": 1e-05,
        "duration_s": 2e-05,
        "sample_offset_s": 0.0,
        "event_tolerance_s": 1e-12,
        "plant_step_s": 1e-06,
    }
    manifest["action_policy"] = {
        "hash": "2" * 64,
        "identity": {
            "module": "pe_sim.reference_plants",
            "class": "BoundedActionPolicy",
            "name": "buck-duty",
            "minimum": 0.0,
            "maximum": 0.95,
        },
    }
    manifest["environment"] = {
        "status": "known",
        "backends": [
            {
                "name": "pe_sim.ideal_averaged_python",
                "version": "reference-v1",
                "status": "known",
                "solver_settings": {"integrator": "rk4", "step_s": 1e-06},
            }
        ],
    }
    manifest["applicability"] = {
        "status": "limited",
        "mode": "record",
        "claim_status": "declared",
        "claim_level": "limited",
        "topology": "buck",
        "envelope_sha256": "3" * 64,
        "parameter_provenance_status": "declared",
        "parameter_provenance_sha256": "4" * 64,
        "policy_sha256": "5" * 64,
        "finding_count": 1,
        "violation_count": 0,
        "blocked": False,
        "limitations": ["steady_state_unassessed"],
        "checks": {
            "passed": True,
            "level": "partial",
            "limitations": ["steady_state_unassessed"],
            "rule_version": "checks-v1",
            "plugin_id": "model-envelope",
            "findings": [
                {
                    "rule_id": "provenance.input_voltage_v",
                    "category": "steady_state_unassessed",
                    "message": "input voltage was recorded as an unreviewed assumption",
                    "severity": "warning",
                    "passed": False,
                    "evidence": {
                        "parameter": "input_voltage_v",
                        "unit": "V",
                        "source": "assumption",
                        "review_status": "unreviewed",
                    },
                    "stop_requested": False,
                    "plugin_id": "model-envelope",
                }
            ],
        },
    }
    _rewrite_manifest(run_dir, manifest)
    return run_dir


def test_review_run_defaults_to_the_rendered_text_report(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(["review-run", str(run_dir)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("pe-sim run review:")
    assert out.endswith("\n")
    assert out.count("\n") > 1


def test_review_run_renders_markdown_on_request(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(["review-run", str(run_dir), "--format", "markdown"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## 1." in out
    assert out.endswith("\n")


def test_review_run_json_emits_the_envelope(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(["review-run", str(run_dir), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["schema_version"] == REVIEW_SCHEMA
    assert [section["id"] for section in payload["sections"]] == [
        "integrity",
        "claim",
        "parameters",
        "checks",
        "signals",
        "provenance",
        "artifacts",
        "limitations",
    ]


def test_review_run_rejects_a_missing_run_directory(tmp_path, capsys):
    exit_code = main(["review-run", str(tmp_path / "missing")])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "RUN_INSPECTION_REJECTED"
    assert payload["scope"] == "run_review"


def test_review_run_help_documents_the_output_flags(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["review-run", "--help"])

    out = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "--format" in out
    assert "--json" in out


def test_cli_version_reports_the_review_release(capsys):
    # The version has one source (``pe_sim._version``); asserting a literal here
    # would go stale on the next patch release.
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__

