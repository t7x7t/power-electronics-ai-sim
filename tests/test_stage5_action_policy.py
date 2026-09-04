import math

import pytest

from pe_sim import (
    ActionRequest,
    BoundedActionPolicy,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    FiniteActionPolicy,
    RunOptions,
    Timebase,
    run_experiment,
)


class FixedActionController(FakePIController):
    def __init__(self, action: float):
        super().__init__()
        self.action = action

    def observe(self, measurement, command, timing, state):
        del measurement, command, state
        now = float(timing["sample_time_s"])
        return ActionRequest(self.action, produced_time_s=now, target_time_s=now)


class DeclaredPolicyPlant(FakePlant):
    action_policy = BoundedActionPolicy(0.0, 1.0, name="declared")


def _spec(tmp_path, run_id="action-policy"):
    return ExperimentSpec(
        "stage5-action-policy",
        run_id,
        "fake",
        "fixed",
        Timebase(duration_s=0.001, control_period_s=0.001),
        output_dir=str(tmp_path),
    )


def test_default_policy_preserves_finite_actions_for_generic_plants(tmp_path):
    result = run_experiment(_spec(tmp_path), FakePlant(), FixedActionController(5.0))
    assert result.status == "RUN_OK"
    samples = (result.run_dir / "samples.json").read_text(encoding="utf-8")
    assert '"action":5.0' in samples


def test_explicit_policy_can_bound_generic_plant_actions(tmp_path):
    options = RunOptions(action_policy=BoundedActionPolicy(-1.0, 1.0))
    result = run_experiment(_spec(tmp_path), FakePlant(), FixedActionController(5.0), options)
    assert result.status == "RUN_OK"
    samples = (result.run_dir / "samples.json").read_text(encoding="utf-8")
    assert '"action":1.0' in samples
    assert '"clamp_reason":"upper"' in samples
    manifest = (result.run_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"minimum":-1.0' in manifest
    assert '"maximum":1.0' in manifest


def test_nonfinite_action_policy_input_is_rejected():
    policy = FiniteActionPolicy()
    with pytest.raises(ValueError, match="non-finite action"):
        policy.project(math.inf)


def test_plant_declared_policy_is_used_without_runner_range_defaults(tmp_path):
    result = run_experiment(_spec(tmp_path, "declared-policy"), DeclaredPolicyPlant(), FixedActionController(2.0))
    assert result.status == "RUN_OK"
    samples = (result.run_dir / "samples.json").read_text(encoding="utf-8")
    assert '"action":1.0' in samples


def test_bounded_policy_requires_finite_ordered_bounds():
    with pytest.raises(ValueError, match="finite and ordered"):
        BoundedActionPolicy(1.0, 0.0)
    with pytest.raises(ValueError, match="finite and ordered"):
        BoundedActionPolicy(0.0, math.inf)
