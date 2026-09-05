import json
import shutil
from pathlib import Path

import pytest

from pe_sim import (
    ExperimentSpec,
    FakePIController,
    FakePlant,
    LifecycleTransitionError,
    Runner,
    Timebase,
    scan_abandoned_runs,
)
import pe_sim.artifacts as artifacts
from pe_sim.runner.lifecycle import LifecycleStateMachine


def _spec(tmp_path, run_id="stage8"):
    return ExperimentSpec(
        "stage8",
        run_id,
        "fake",
        "pi",
        Timebase(duration_s=0.004, control_period_s=0.001),
        output_dir=str(tmp_path),
    )


def test_lifecycle_exposes_complete_matrix_and_rejects_illegal_transition():
    matrix = LifecycleStateMachine.allowed_transitions()
    assert set(matrix) >= {"CREATED", "RUNNING", "RUN_OK", "INCOMPLETE", "RUN_FAILED"}
    machine = LifecycleStateMachine()
    with pytest.raises(LifecycleTransitionError):
        machine.transition("RUN_OK", "cannot_skip_execution")
    machine.transition("RUNNING", "started")
    machine.transition("INCOMPLETE", "cancelled")
    with pytest.raises(LifecycleTransitionError):
        machine.transition("RUN_OK", "resume_terminal_run")
    assert machine.is_terminal
    assert machine.transitions[0]["sequence"] == 0


def test_abandoned_temporary_directory_is_reported_without_publication(tmp_path):
    orphan = tmp_path / ".orphan-run.token"
    orphan.mkdir()
    (orphan / ".run-state.json").write_text(
        json.dumps({"run_id": "orphan-run", "status": "RUNNING"}), encoding="utf-8"
    )
    reports = scan_abandoned_runs(tmp_path)
    assert reports[0]["status"] == "INCOMPLETE"
    assert reports[0]["reason"] == "abandoned_temporary_directory"
    assert "manifest.json" in reports[0]["missing_artifacts"]
    assert orphan.exists()
    shutil.rmtree(orphan)


def test_abandoned_scan_supports_run_ids_with_dots_and_ignores_hidden_dirs(tmp_path):
    orphan = tmp_path / ".run.id.token"
    orphan.mkdir()
    (orphan / ".run-state.json").write_text(
        json.dumps({"run_id": "run.id", "status": "READY_TO_PUBLISH"}), encoding="utf-8"
    )
    (tmp_path / ".ordinary-hidden").mkdir()
    reports = scan_abandoned_runs(tmp_path)
    assert len(reports) == 1
    assert reports[0]["run_id"] == "run.id"
    shutil.rmtree(orphan)
    shutil.rmtree(tmp_path / ".ordinary-hidden")


def test_failed_directory_publish_remains_an_incomplete_candidate(tmp_path, monkeypatch):
    writer = artifacts.ArtifactWriter(tmp_path, "publish-failure")
    for name in artifacts.ArtifactWriter.REQUIRED:
        if name != "logs/.keep":
            writer.write_json(name, {})
    original_replace = artifacts.os.replace

    def fail_directory_publish(source, target):
        if Path(source) == writer.tmp:
            raise PermissionError("directory publish blocked")
        return original_replace(source, target)

    monkeypatch.setattr(artifacts.os, "replace", fail_directory_publish)
    with pytest.raises(PermissionError):
        writer.finalize()
    reports = scan_abandoned_runs(tmp_path)
    assert reports and reports[0]["status"] == "INCOMPLETE"
    assert reports[0]["marker"]["status"] == "READY_TO_PUBLISH"
    assert not (tmp_path / "publish-failure").exists()


def test_cancelled_run_is_incomplete_and_records_recovery_evidence(tmp_path):
    result = Runner().run(
        _spec(tmp_path, "cancelled"),
        FakePlant(),
        FakePIController(),
        checkpoint_interval_steps=1,
        cancel_after_steps=2,
    )
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result.status == "INCOMPLETE"
    assert manifest["failure"]["category"] == "cancelled"
    assert manifest["failure"]["recoverable"] is True
    assert manifest["recovery"]["requested"] is False
    assert (result.run_dir / "checkpoint.json").exists()


def test_resume_manifest_records_source_run_and_checkpoint(tmp_path):
    interrupted = Runner().run(
        _spec(tmp_path, "source"),
        FakePlant(),
        FakePIController(),
        checkpoint_interval_steps=1,
        interrupt_after_steps=2,
    )
    resumed = Runner().run(
        _spec(tmp_path, "resumed"),
        FakePlant(),
        FakePIController(),
        resume_from=interrupted.run_dir,
    )
    manifest = json.loads((resumed.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert resumed.status == "RUN_OK"
    assert manifest["recovery"]["requested"] is True
    assert manifest["recovery"]["resumed"] is True
    assert manifest["recovery"]["source"]["run_id"] == "source"
    assert manifest["recovery"]["source"]["status"] == "INCOMPLETE"
    assert "RUN_FAILED" in manifest["state_transition_matrix"]
    assert any(item.get("event") == "resume" for item in json.loads((resumed.run_dir / "events.json").read_text(encoding="utf-8")))


def test_resume_from_completed_run_is_rejected_fail_closed(tmp_path):
    completed = Runner().run(
        _spec(tmp_path, "completed"),
        FakePlant(),
        FakePIController(),
        checkpoint_interval_steps=1,
    )
    rejected = Runner().run(
        _spec(tmp_path, "rejected"),
        FakePlant(),
        FakePIController(),
        resume_from=completed.run_dir,
    )
    manifest = json.loads((rejected.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rejected.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "runtime_error"
    assert "INCOMPLETE or RUN_FAILED" in manifest["error"]


def test_direct_checkpoint_from_completed_run_is_rejected(tmp_path):
    completed = Runner().run(
        _spec(tmp_path, "completed-direct"),
        FakePlant(),
        FakePIController(),
        checkpoint_interval_steps=1,
    )
    checkpoint = completed.run_dir / "checkpoints" / "step-000001.json"
    rejected = Runner().run(
        _spec(tmp_path, "rejected-direct"),
        FakePlant(),
        FakePIController(),
        resume_from=checkpoint,
    )
    manifest = json.loads((rejected.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rejected.status == "RUN_FAILED"
    assert "INCOMPLETE or RUN_FAILED" in manifest["error"]
