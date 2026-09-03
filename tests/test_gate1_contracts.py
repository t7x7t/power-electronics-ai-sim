from pe_sim.contracts import ActionRequest, ExperimentSpec, InitialState, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def test_fake_closed_loop_separates_truth_and_measurement(tmp_path):
    plant = FakePlant()
    plant.reset({"value": 0.0})
    observation = plant.observe()
    assert "secret_gain" not in observation.measurement
    assert "secret_gain" in observation.truth
    controller = FakePIController()
    controller.reset(None, 0)
    request = controller.observe(observation.controller_measurement(), {"vref": 1.0}, {"sample_time_s": 0.0}, {})
    assert isinstance(request, ActionRequest)


def test_runner_produces_successful_closed_loop(tmp_path):
    spec = ExperimentSpec("fake", "run-1", "fake-plant", "fake-controller", Timebase(duration_s=0.003, control_period_s=0.001), output_dir=str(tmp_path))
    result = Runner().run(spec, FakePlant(), FakePIController())
    assert result.status == "RUN_OK"
    assert (result.run_dir / "manifest.json").exists()
