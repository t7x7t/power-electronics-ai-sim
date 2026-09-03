import hashlib
import json

import pytest

from pe_sim.contracts import (
    ActionRequest,
    CapabilitySet,
    ExperimentSpec,
    InitialState,
    PlantObservation,
    Timebase,
    ensure_schema_compatible,
    negotiate_capabilities,
    snapshot_digest,
    validate_observation_visibility,
)
from pe_sim.runtime import FakePIController, FakePlant, Runner
from pe_sim.cli import main as cli_main


def test_legacy_capability_set_and_multirate_negotiation():
    plant, controller = negotiate_capabilities(
        {"continuous_time"},
        None,
        Timebase(control_period_s=0.001, plant_step_s=0.0001),
        required=("continuous_time",),
    )
    assert plant.supports("continuous_time")
    assert controller.names == frozenset()


def test_event_requirement_fails_closed_without_plant_support():
    with pytest.raises(ValueError, match="plant:events"):
        negotiate_capabilities({"continuous_time"}, set(), Timebase(), event_schedule=({"time_s": 0.0, "event": "load_step"},))


def test_unsupported_declared_event_type_fails_closed():
    with pytest.raises(ValueError, match="event_type:load_step"):
        negotiate_capabilities(
            {"names": ["events", "continuous_time"], "event_types": ["other"]},
            set(),
            Timebase(),
            event_schedule=({"time_s": 0.0, "event_type": "load_step"},),
        )


def test_schema_version_is_backward_compatible_but_not_forward_compatible():
    ensure_schema_compatible("0.0")
    with pytest.raises(ValueError):
        ensure_schema_compatible("0.2")
    with pytest.raises(ValueError):
        ensure_schema_compatible("1.0")


def test_observation_visibility_rejects_future_and_regression():
    with pytest.raises(ValueError, match="future"):
        validate_observation_visibility(PlantObservation(1.1, {"v": 1.0}), 1.0)
    with pytest.raises(ValueError, match="regressed"):
        validate_observation_visibility(PlantObservation(0.9, {"v": 1.0}), 0.9, 1.0)


def test_required_capability_is_enforced_by_runner(tmp_path):
    spec = ExperimentSpec(
        "cap", "cap-run", "fake", "pi", Timebase(duration_s=0.001),
        output_dir=str(tmp_path), required_capabilities=("plant:thermal_state",),
    )
    result = Runner().run(spec, FakePlant(), FakePIController())
    assert result.status == "RUN_FAILED"
    assert (result.run_dir / "safety.json").exists()


def test_qualified_snapshot_hash_is_checked_and_restore_is_deterministic(tmp_path):
    source = FakePlant()
    source.reset({"value": 0.25})
    source.advance(0.5, 0.001)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(source.snapshot(), sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    spec = ExperimentSpec(
        "snap", "snap-run", "fake", "pi", Timebase(duration_s=0.001),
        initial_state=InitialState("qualified_snapshot", str(snapshot_path), digest),
        output_dir=str(tmp_path),
    )
    first = Runner().run(spec, FakePlant(), FakePIController())
    assert first.status == "RUN_OK"
    snapshot_path.write_text("{}", encoding="utf-8")
    second = Runner().run(
        ExperimentSpec(
            "snap", "snap-run-2", "fake", "pi", Timebase(duration_s=0.001),
            initial_state=InitialState("qualified_snapshot", str(snapshot_path), digest),
            output_dir=str(tmp_path),
        ),
        FakePlant(), FakePIController(),
    )
    assert second.status == "RUN_FAILED"


def test_capability_mapping_is_normalized():
    caps = CapabilitySet.from_value({"features": ["events"], "event_types": ["load_step"], "control_period_s": 1e-3})
    assert caps.supports("events")
    assert caps.event_types == frozenset({"load_step"})
    assert caps.to_dict()["control_period_s"] == 1e-3


def test_snapshot_reset_is_deterministic():
    first = FakePlant(gain=1.2, tau_s=0.01)
    second = FakePlant(gain=1.2, tau_s=0.01)
    first.reset({"value": 0.3})
    second.reset({"value": 0.3})
    assert snapshot_digest(first.snapshot()) == snapshot_digest(second.snapshot())
    first.advance(0.7, 0.001)
    second.advance(0.7, 0.001)
    assert snapshot_digest(first.snapshot()) == snapshot_digest(second.snapshot())


def test_action_request_remains_legacy_compatible():
    assert ActionRequest(0.1).target_time_s == 0.0


def test_psfb_step_cli_accepts_command_schedule(tmp_path):
    code = cli_main(["psfb-step", "--output-dir", str(tmp_path), "--run-id", "cli-regression"])
    assert code == 0
    assert (tmp_path / "cli-regression" / "manifest.json").exists()
