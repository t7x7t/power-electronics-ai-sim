import pytest

from pe_sim import (
    AuditPolicy,
    RecoveryPolicy,
    RunOptions,
    TimingPolicy,
    Timebase,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    run_experiment,
)


def _spec(tmp_path, run_id="facade"):
    return ExperimentSpec(
        "runner-facade",
        run_id,
        "fake",
        "pi",
        Timebase(duration_s=0.002, control_period_s=0.001),
        output_dir=str(tmp_path),
    )


def test_simple_default_entry_point_requires_no_policy_configuration(tmp_path):
    result = run_experiment(_spec(tmp_path), FakePlant(), FakePIController())
    assert result.status == "RUN_OK"


def test_advanced_recovery_policy_composes_with_facade(tmp_path):
    options = RunOptions(recovery=RecoveryPolicy(checkpoint_interval_steps=1))
    result = run_experiment(_spec(tmp_path), FakePlant(), FakePIController(), options)
    assert result.status == "RUN_OK"
    assert (result.run_dir / "checkpoint.json").exists()


def test_timing_policy_can_fail_closed_before_running(tmp_path):
    spec = _spec(tmp_path)
    options = RunOptions(timing=TimingPolicy(engine="event_queue"))
    with pytest.raises(ValueError, match="unsupported timing engine"):
        run_experiment(spec, FakePlant(), FakePIController(), options)


def test_formal_runs_cannot_disable_audit(tmp_path):
    options = RunOptions(mode="formal_comparison", audit=AuditPolicy(enabled=False))
    with pytest.raises(ValueError, match="requires audit evidence"):
        run_experiment(_spec(tmp_path), FakePlant(), FakePIController(), options)


def test_legacy_runner_options_remain_compatible(tmp_path):
    from pe_sim.runtime import Runner

    options = RunOptions(recovery=RecoveryPolicy(checkpoint_interval_steps=1))
    result = Runner().run(_spec(tmp_path), FakePlant(), FakePIController(), options=options)
    assert result.status == "RUN_OK"
