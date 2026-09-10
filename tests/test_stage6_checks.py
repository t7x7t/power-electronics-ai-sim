import json

from pe_sim import (
    BackendProcessError,
    BackendTimeoutError,
    CheckContext,
    CheckReport,
    ExperimentSpec,
    FakePIController,
    FakePlant,
    ObservationValidityPlugin,
    PlantAdvanceError,
    RunOptions,
    Timebase,
    run_experiment,
)
from pe_sim.contracts import PlantObservation


def _spec(tmp_path, run_id="stage6"):
    return ExperimentSpec("stage6", run_id, "plant", "controller", Timebase(duration_s=.001, control_period_s=.001), output_dir=str(tmp_path))


class StalePlant(FakePlant):
    def observe(self):
        observation = super().observe()
        return PlantObservation(observation.time_s, observation.measurement, observation.truth, age_steps=2, measurement_units=observation.measurement_units)


def test_default_validity_rejects_stale_observation_and_records_finding(tmp_path):
    result = run_experiment(_spec(tmp_path), StalePlant(), FakePIController())
    assert result.status == "RUN_FAILED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["failure"]["category"] == "stale_observation"
    assert manifest["failure"]["findings"][0]["rule_id"] == "observation.age.max"
    assert manifest["safety"]["validity"]["passed"] is False


def test_validity_plugin_can_explicitly_allow_declared_latency(tmp_path):
    plugin = ObservationValidityPlugin(required_measurements=("vout",), max_age_steps=2)
    result = run_experiment(_spec(tmp_path, "latency"), StalePlant(), FakePIController(), RunOptions(validity_plugins=(plugin,)))
    assert result.status == "QUALIFIED"


def test_adapter_rejection_of_nonfinite_observation_is_recorded_as_validity_failure(tmp_path):
    class NonfinitePlant(FakePlant):
        def observe(self):
            return PlantObservation(self.time_s, {"vout": float("nan")})

    result = run_experiment(_spec(tmp_path, "nonfinite"), NonfinitePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "nonfinite_observation"
    assert manifest["safety"]["validity"]["passed"] is False
    assert manifest["failure"]["findings"][0]["plugin_id"] == "runner-observation-boundary"


class MissingPlant(FakePlant):
    def observe(self):
        observation = super().observe()
        return PlantObservation(observation.time_s, {"other": observation.measurement["vout"]}, observation.truth, measurement_units={})


def test_missing_measurement_is_rejected_before_controller(tmp_path):
    result = run_experiment(_spec(tmp_path, "missing"), MissingPlant(), FakePIController())
    assert result.status == "RUN_FAILED"
    assert result.qualification["reasons"] == ["missing_measurement"]


def test_units_and_ranges_are_configurable(tmp_path):
    class UnitRangePlant(FakePlant):
        def observe(self):
            observation = super().observe()
            return PlantObservation(observation.time_s, observation.measurement, observation.truth, measurement_units={"vout": "A"})

    plugin = ObservationValidityPlugin(expected_units={"vout": "V"}, ranges={"vout": (0.1, 0.2)})
    result = run_experiment(_spec(tmp_path, "unit-range"), UnitRangePlant(), FakePIController(), RunOptions(validity_plugins=(plugin,)))
    assert result.status == "RUN_FAILED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    categories = {item["category"] for item in manifest["failure"]["findings"]}
    assert categories == {"unit_mismatch", "measurement_out_of_range"}


def test_validity_rejects_observation_before_current_logical_time():
    plugin = ObservationValidityPlugin(required_measurements=("vout",))
    observation = PlantObservation(0.5, {"vout": 1.0})
    report = plugin.check_observation(observation, CheckContext(current_time_s=1.0))
    assert report.passed is False
    assert "timestamp_violation" in report.reasons


class StopPlugin:
    plugin_id = "stop-plugin"
    rule_version = "test-v1"

    def check_observation(self, observation, context):
        return CheckReport.failure("domain_stop", "test stop", evidence={"step": context.step_index})


def test_custom_safety_plugin_is_composable_and_audited(tmp_path):
    result = run_experiment(_spec(tmp_path, "plugin"), FakePlant(), FakePIController(), RunOptions(safety_plugins=(StopPlugin(),)))
    assert result.status == "RUN_FAILED"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["failure"]["category"] == "domain_stop"
    assert manifest["checks"]["plugins"]["safety"][0]["id"] == "stop-plugin"
    audit = json.loads((result.run_dir / "audit.json").read_text())
    assert audit["call_counts"]["initial_observation.stop-plugin"] == 1
    assert manifest["failure"]["findings"][0]["plugin_id"] == "stop-plugin"


def test_plugin_generator_survives_options_validation(tmp_path):
    class NoopValidity:
        plugin_id = "generator-validity"

        def check_observation(self, observation, context):
            return CheckReport.ok(plugin_id=self.plugin_id)

    result = run_experiment(
        _spec(tmp_path, "generator"),
        FakePlant(),
        FakePIController(),
        RunOptions(validity_plugins=(item for item in (NoopValidity(),))),
    )
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "QUALIFIED"
    assert manifest["checks"]["plugins"]["validity"][1]["id"] == "generator-validity"


class RejectQualification:
    plugin_id = "reject"

    def check_samples(self, samples, context):
        return False, ["outside_scope"]


def test_failed_qualification_is_disqualified_not_execution_failure(tmp_path):
    result = run_experiment(_spec(tmp_path, "disqualified"), FakePlant(), FakePIController(), RunOptions(qualification_plugins=(RejectQualification(),)))
    assert result.status == "DISQUALIFIED"
    assert result.qualification["passed"] is False
    assert "outside_scope" in result.qualification["reasons"]


class TimeoutPlant(FakePlant):
    def advance(self, action, duration, external_input=None):
        raise BackendTimeoutError("backend timed out")


def test_backend_failures_have_stable_categories(tmp_path):
    result = run_experiment(_spec(tmp_path, "timeout"), TimeoutPlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "backend_timeout"


class ConvergencePlant(FakePlant):
    def advance(self, action, duration, external_input=None):
        raise RuntimeError("ngspice failed to converge at timestep")


def test_convergence_failure_is_classified(tmp_path):
    result = run_experiment(_spec(tmp_path, "convergence"), ConvergencePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["failure"]["category"] == "numerical_nonconvergence"
