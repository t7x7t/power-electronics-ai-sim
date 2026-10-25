"""CLI surface for the read-only parameter view and the confirmation record.

Every test builds a self-contained, integrity-verified package in ``tmp_path``
from the checked-in visualization fixture.  Nothing here reads ``dist/``, the
network, or the developer worktree: the package is assembled, hashed, and then
read back through ``pe_sim.cli.main``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.cli import main

FIXTURE = Path(__file__).with_name("fixtures") / "visualization_service" / "buck-contract-fixture"
PARAMETER_VIEW_SCHEMA = "pe-sim.parameter-view.v1"
PARAMETER_CONFIRMATION_SCHEMA = "pe-sim.parameter-confirmation.v1"


@pytest.fixture(autouse=True)
def _not_ci(monkeypatch):
    """A confirmation is a human action, so success paths must run outside CI."""

    monkeypatch.delenv("CI", raising=False)


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
    """Copy the fixture and add the groups the parameter view projects."""

    run_dir = root / "buck-package"
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


def _confirmation_command(run_dir: Path, output: Path) -> list[str]:
    return [
        "confirm-parameters",
        str(run_dir),
        "--reviewer",
        "Reviewer One",
        "--statement",
        "I reviewed every recorded parameter and its bounds.",
        "--output",
        str(output),
        "--timestamp",
        "2026-10-25T00:00:00Z",
        "--compact",
    ]


def test_parameter_view_returns_the_versioned_parameter_document(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(["parameter-view", str(run_dir), "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["schema_version"] == PARAMETER_VIEW_SCHEMA
    group_ids = [group["id"] for group in payload["parameter_set"]["groups"]]
    assert group_ids[0] == "plant"
    assert "controller" in group_ids
    assert "timebase" in group_ids
    assert "action_policy" in group_ids
    assert "solver:pe_sim.ideal_averaged_python" in group_ids


def test_parameter_view_compact_prints_exactly_one_line(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(["parameter-view", str(run_dir), "--compact"])

    text = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert len(text.splitlines()) == 1
    assert json.loads(text)["schema_version"] == PARAMETER_VIEW_SCHEMA


def test_parameter_view_rejects_a_missing_run_directory(tmp_path, capsys):
    exit_code = main(["parameter-view", str(tmp_path / "missing"), "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "RUN_INSPECTION_REJECTED"
    assert payload["scope"] == "parameter_view"


def test_confirm_parameters_writes_the_record_outside_the_package(tmp_path, capsys):
    run_dir = _package(tmp_path)
    output = tmp_path / "confirmation.json"

    exit_code = main(_confirmation_command(run_dir, output))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "written"
    assert Path(payload["path"]).resolve() == output.resolve()
    assert output.is_file()
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == PARAMETER_CONFIRMATION_SCHEMA
    assert document["review"]["reviewer"] == "Reviewer One"
    assert document["run"]["run_id"] == payload["run_id"]
    assert document["parameter_set"]["digest"] == payload["parameter_set_digest"]
    assert document["confirmation_digest"] == payload["confirmation_digest"]


def test_confirm_parameters_refuses_an_output_inside_the_package(tmp_path, capsys):
    run_dir = _package(tmp_path)
    output = run_dir / "confirmation.json"

    exit_code = main(_confirmation_command(run_dir, output))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "RUN_INSPECTION_REJECTED"
    assert payload["scope"] == "parameter_confirmation"
    assert not output.exists()


def test_confirm_parameters_refuses_under_ci(tmp_path, capsys, monkeypatch):
    run_dir = _package(tmp_path)
    output = tmp_path / "confirmation.json"
    monkeypatch.setenv("CI", "true")

    exit_code = main(_confirmation_command(run_dir, output))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["scope"] == "parameter_confirmation"
    assert not output.exists()


def test_verify_parameter_confirmation_reports_current(tmp_path, capsys):
    run_dir = _package(tmp_path)
    output = tmp_path / "confirmation.json"
    assert main(_confirmation_command(run_dir, output)) == 0
    capsys.readouterr()

    exit_code = main(
        ["verify-parameter-confirmation", str(run_dir), "--confirmation", str(output), "--compact"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "current"


def test_verify_parameter_confirmation_reports_a_stale_parameter_set(tmp_path, capsys):
    run_dir = _package(tmp_path)
    output = tmp_path / "confirmation.json"
    assert main(_confirmation_command(run_dir, output)) == 0
    capsys.readouterr()

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["plant"]["identity"]["parameters"]["input_voltage_v"] = 15.0
    _rewrite_manifest(run_dir, manifest)

    exit_code = main(
        ["verify-parameter-confirmation", str(run_dir), "--confirmation", str(output), "--compact"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "stale"
    assert payload["differences"]


def test_verify_parameter_confirmation_rejects_a_missing_confirmation_file(tmp_path, capsys):
    run_dir = _package(tmp_path)

    exit_code = main(
        [
            "verify-parameter-confirmation",
            str(run_dir),
            "--confirmation",
            str(tmp_path / "missing.json"),
            "--compact",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["schema"] == "pe-sim.rejection.v1"


def test_cli_version_reports_the_parameter_view_release(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "0.8.0"
