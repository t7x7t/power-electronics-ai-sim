import json
import subprocess

import pytest

from pe_sim import (
    ActionRequest,
    CheckReport,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    RunOptions,
    Runner,
    Timebase,
    run_experiment,
)


def _spec(tmp_path, run_id="run"):
    return ExperimentSpec(
        "preflight",
        run_id,
        "fake",
        "pi",
        Timebase(duration_s=0.002, control_period_s=0.001),
        output_dir=str(tmp_path),
    )


def _clean_repo(path):
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
    (path / "baseline.txt").write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "baseline.txt"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "baseline"], check=True, capture_output=True, text=True)


def test_invalid_run_id_is_rejected_before_adapter_reset(tmp_path):
    class ResetPlant(FakePlant):
        def reset(self, initial_state):
            raise AssertionError("reset must not run")

    with pytest.raises(ValueError, match="run_id"):
        Runner().run(_spec(tmp_path, "../escape"), ResetPlant(), FakePIController())
    assert not (tmp_path / "escape").exists()


def test_duplicate_run_id_is_rejected_before_adapter_reset(tmp_path):
    first = Runner().run(_spec(tmp_path, "duplicate"), FakePlant(), FakePIController())

    class ResetPlant(FakePlant):
        def reset(self, initial_state):
            raise AssertionError("reset must not run")

    assert first.status == "QUALIFIED"
    with pytest.raises(FileExistsError):
        Runner().run(_spec(tmp_path, "duplicate"), ResetPlant(), FakePIController())


def test_partial_plant_advance_is_rejected_at_window_boundary(tmp_path):
    class PartialPlant(FakePlant):
        def advance(self, action, duration, external_input=None):
            return super().advance(action, duration * 0.5, external_input)

    result = Runner().run(_spec(tmp_path, "partial"), PartialPlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "plant_advance_failure"


def test_formal_comparison_rejects_unseeded_or_placeholder_contracts(tmp_path, monkeypatch):
    _clean_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="random seed"):
        Runner().run(
            ExperimentSpec(
                "formal",
                "unseeded",
                "fake",
                "pi",
                Timebase(duration_s=0.001),
                seed=None,
                output_dir="runs",
            ),
            FakePlant(),
            FakePIController(),
            mode="formal_comparison",
        )

    with pytest.raises(ValueError, match="non-placeholder contract"):
        Runner().run(
            ExperimentSpec("formal", "placeholder", "fake", "pi", Timebase(duration_s=0.001), output_dir="runs"),
            FakePlant(),
            FakePIController(),
            mode="formal_comparison",
        )


def test_checkpoint_is_bound_to_execution_spec(tmp_path):
    source = Runner().run(_spec(tmp_path, "source"), FakePlant(), FakePIController(), checkpoint_interval_steps=1, interrupt_after_steps=1)
    changed = ExperimentSpec(
        "preflight",
        "resume",
        "fake",
        "pi",
        Timebase(duration_s=0.003, control_period_s=0.001),
        output_dir=str(tmp_path),
    )
    resumed = Runner().run(changed, FakePlant(), FakePIController(), resume_from=source.run_dir)
    manifest = json.loads((resumed.run_dir / "manifest.json").read_text())
    assert resumed.status == "RUN_FAILED"
    assert "experiment specification mismatch" in manifest["error"]


def test_warning_finding_is_recorded_without_stopping(tmp_path):
    class WarningPlugin:
        plugin_id = "warning-only"

        def check_observation(self, observation, context):
            return CheckReport.failure(
                "diagnostic_warning",
                "diagnostic only",
                severity="warning",
                stop_requested=False,
            )

    result = run_experiment(_spec(tmp_path, "warning"), FakePlant(), FakePIController(), RunOptions(validity_plugins=(WarningPlugin(),)))
    assert result.status == "QUALIFIED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    findings = manifest["checks"]["validity"]["findings"]
    assert any(item["category"] == "diagnostic_warning" for item in findings)


def test_malformed_check_report_fails_closed(tmp_path):
    class MalformedPlugin:
        plugin_id = "malformed"

        def check_observation(self, observation, context):
            return CheckReport(False, ())

    result = run_experiment(
        _spec(tmp_path, "malformed"),
        FakePlant(),
        FakePIController(),
        RunOptions(validity_plugins=(MalformedPlugin(),)),
    )
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "runtime_error"
    assert "passed=false" in manifest["error"]


def test_controller_state_update_is_serialized_for_checkpoint(tmp_path):
    class Stateful(FakePIController):
        def observe(self, measurement, command, timing, state):
            return ActionRequest(0.1, produced_time_s=timing["sample_time_s"], target_time_s=timing["sample_time_s"], state_update={"count": int(state.get("count", 0)) + 1})

    result = Runner().run(_spec(tmp_path, "stateful"), FakePlant(), Stateful(), checkpoint_interval_steps=1)
    checkpoint = json.loads((result.run_dir / "checkpoint.json").read_text())
    assert checkpoint["controller_state_protocol"] == "reset_serialized_state"
    assert checkpoint["experiment_spec_hash"]
