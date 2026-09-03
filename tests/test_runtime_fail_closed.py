import pytest

from pe_sim.contracts import ActionRequest, ExperimentSpec, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


class FutureController(FakePIController):
    def observe(self, measurement, command, timing, state):
        now = timing["sample_time_s"]
        return ActionRequest(0.5, produced_time_s=now + 1.0, target_time_s=now + 1.0)


class BrokenPlant(FakePlant):
    def advance(self, action, duration, external_input=None):
        self.time_s -= duration
        return self.observe()


def test_future_action_fails_closed_and_preserves_artifacts(tmp_path):
    spec = ExperimentSpec("bad", "run-bad", "fake", "future", Timebase(duration_s=0.001, control_period_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, FakePlant(), FutureController())
    assert result.status == "RUN_FAILED"
    assert (result.run_dir / "safety.json").exists()
    assert "runtime_error" in result.qualification["reasons"]


def test_time_regression_fails_closed(tmp_path):
    spec = ExperimentSpec("bad", "run-time", "broken", "pi", Timebase(duration_s=0.001, control_period_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, BrokenPlant(), FakePIController())
    assert result.status == "RUN_FAILED"


def test_nonfinite_action_rejected():
    with pytest.raises(ValueError):
        ActionRequest(float("nan"))
