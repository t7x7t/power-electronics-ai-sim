import json

from pe_sim.contracts import ExperimentSpec, PlantObservation, Timebase
from pe_sim.runtime import FakePIController, FakePlant, Runner


def _spec(tmp_path, run_id="runner"):
    return ExperimentSpec("stage5", run_id, "fake", "pi", Timebase(duration_s=0.004, control_period_s=0.001), output_dir=str(tmp_path))


def test_runner_records_state_transitions_and_call_audit(tmp_path):
    result = Runner().run(_spec(tmp_path), FakePlant(), FakePIController(), checkpoint_interval_steps=2)
    assert result.status == "RUN_OK"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    transitions = json.loads((result.run_dir / "state_transitions.json").read_text())
    audit = json.loads((result.run_dir / "audit.json").read_text())
    assert [(item["from"], item["to"]) for item in transitions] == [("CREATED", "RUNNING"), ("RUNNING", "RUN_OK")]
    assert audit["call_counts"]["controller.observe"] == 4
    assert audit["call_counts"]["plant.advance"] == 4
    assert manifest["audit"]["call_count"] == len(audit["calls"])
    assert (result.run_dir / "checkpoint.json").exists()


def test_interrupted_run_is_incomplete_and_can_resume(tmp_path):
    interrupted = Runner().run(_spec(tmp_path, "interrupted"), FakePlant(), FakePIController(), checkpoint_interval_steps=1, interrupt_after_steps=2)
    assert interrupted.status == "INCOMPLETE"
    assert json.loads((interrupted.run_dir / "manifest.json").read_text())["failure"]["category"] == "interrupted"
    resumed = Runner().run(_spec(tmp_path, "resumed"), FakePlant(), FakePIController(), resume_from=interrupted.run_dir, checkpoint_interval_steps=1)
    full = Runner().run(_spec(tmp_path, "full"), FakePlant(), FakePIController())
    assert resumed.status == "RUN_OK"
    resumed_samples = json.loads((resumed.run_dir / "samples.json").read_text())
    full_samples = json.loads((full.run_dir / "samples.json").read_text())
    assert resumed_samples == full_samples


def test_tampered_checkpoint_is_rejected(tmp_path):
    interrupted = Runner().run(_spec(tmp_path, "tamper-source"), FakePlant(), FakePIController(), checkpoint_interval_steps=1, interrupt_after_steps=1)
    checkpoint_path = interrupted.run_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["last_action"] = 0.123
    checkpoint_path.write_text(json.dumps(checkpoint))
    resumed = Runner().run(_spec(tmp_path, "tamper-resume"), FakePlant(), FakePIController(), resume_from=interrupted.run_dir)
    manifest = json.loads((resumed.run_dir / "manifest.json").read_text())
    assert resumed.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "runtime_error"
    assert "checkpoint hash mismatch" in manifest["error"]


def test_missing_capability_has_structured_failure_category(tmp_path):
    spec = ExperimentSpec("stage5-cap", "cap-failure", "fake", "pi", Timebase(duration_s=0.001), output_dir=str(tmp_path), required_capabilities=("plant:thermal_state",))
    result = Runner().run(spec, FakePlant(), FakePIController())
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert result.status == "RUN_FAILED"
    assert manifest["failure"]["category"] == "missing_capability"


class AlternateMeasurementPlant(FakePlant):
    def observe(self):
        return PlantObservation(self.time_s, {"output_voltage": self.state}, {"output_voltage": self.state, "secret_gain": self.gain}, 0)


class AlternateMeasurementController(FakePIController):
    def observe(self, measurement, command, timing, state):
        del state
        from pe_sim.contracts import ActionRequest
        now = float(timing["sample_time_s"])
        return ActionRequest(self.kp * (float(command.get("vref", 1.0)) - measurement["output_voltage"]), produced_time_s=now, target_time_s=now)


def test_runner_supports_configured_primary_measurement(tmp_path):
    spec = ExperimentSpec("stage5-alt", "alt", "alt-plant", "alt-controller", Timebase(duration_s=0.002, control_period_s=0.001), output_dir=str(tmp_path), plant_config={"primary_measurement": "output_voltage"})
    result = Runner().run(spec, AlternateMeasurementPlant(), AlternateMeasurementController(), measurement_key="output_voltage")
    assert result.status == "RUN_OK"
    rows = json.loads((result.run_dir / "samples.json").read_text())
    assert "output_voltage" in rows[0]
    assert "vout" not in rows[0]
