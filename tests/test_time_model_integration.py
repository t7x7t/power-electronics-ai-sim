"""Phase A2: a plant declares who owns time, and the Runner refuses dead routes.

D1 is "dual with priced evidence": a stepped adapter keeps kernel-owned time and
can still reach ``comparable``; a whole-run engine owns its own time axis, is
capped at ``limited``, and its execution route is not implemented in this
release, so the Runner refuses it before any run directory exists.
"""

from __future__ import annotations

import json

import pytest

from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.cli import _build_plant, _validate_plant_adapter
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.plant_registry import (
    PlantRegistration,
    clear_registrations,
    plant_catalog,
    plant_ids,
    register_plant,
)
from pe_sim.run_events import EVENT_RUN_STARTED, ProgressPolicy
from pe_sim.runner import RunOptions
from pe_sim.runtime import FakePIController, FakePlant, Runner
from pe_sim.time_model import (
    TIME_MODEL_STEPPED,
    TIME_MODEL_WHOLE_RUN,
    TIME_MODELS,
    TraceContract,
    WholeRunResult,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The registry is process-wide, so every test starts and ends clean."""

    clear_registrations()
    yield
    clear_registrations()


def _registration(plant_id: str = "demo", **overrides) -> PlantRegistration:
    values = {
        "plant_id": plant_id,
        "backend_id": plant_id,
        "factory": lambda config, context: FakePlant(),
        "class_name": "FakePlant",
        "trust_level": "third-party",
        "export_eligible": False,
    }
    values.update(overrides)
    return PlantRegistration(**values)


def _spec(tmp_path, run_id: str = "time-model"):
    return ExperimentSpec(
        "phase-a2",
        run_id,
        "fake",
        "pi",
        Timebase(duration_s=0.003, control_period_s=0.001),
        output_dir=str(tmp_path),
    )


class _WholeRunPlant:
    """An engine that owns its own time axis; only the declared shape matters."""

    time_model = TIME_MODEL_WHOLE_RUN

    def trace_contract(self) -> TraceContract:
        return TraceContract(cadence_s=1e-3)

    def run_whole(self, request) -> WholeRunResult:
        duration_s = float(request.duration_s)
        return WholeRunResult(time_s=(0.0, duration_s), signals={"vout": (0.0, 0.0)})


class _SlottedWholeRunPlant:
    """A whole-run adapter that cannot accept attributes at runtime."""

    __slots__ = ()
    time_model = TIME_MODEL_WHOLE_RUN

    def trace_contract(self) -> TraceContract:
        return TraceContract(cadence_s=1e-3)

    def run_whole(self, request) -> WholeRunResult:
        return WholeRunResult(time_s=(0.0,), signals={"vout": (0.0,)})


def test_registry_rejects_an_unknown_time_model():
    with pytest.raises(ValueError) as excinfo:
        register_plant(_registration("odd", time_model="event_driven"))

    message = str(excinfo.value)
    assert "odd" in message
    assert "time_model" in message
    for allowed in TIME_MODELS:
        assert allowed in message
    assert plant_ids() == ()


def test_the_catalog_exposes_the_declared_time_model():
    load_builtin_plants()

    entries = plant_catalog()["plants"]

    assert entries
    assert {item["plant_id"] for item in entries} >= {"buck", "boost", "fake"}
    time_models = {item["time_model"] for item in entries}
    assert {"stepped", TIME_MODEL_WHOLE_RUN} <= time_models
    assert {
        item["time_model"] for item in entries if item["plant_id"] != "whole-run-buck"
    } == {TIME_MODEL_STEPPED}


def test_validate_plant_adapter_accepts_a_whole_run_shape():
    registration = _registration(
        "whole",
        class_name="_WholeRunPlant",
        time_model=TIME_MODEL_WHOLE_RUN,
    )

    _validate_plant_adapter(registration, _WholeRunPlant())


def test_validate_plant_adapter_rejects_a_stepped_plant_registered_as_whole_run():
    registration = _registration(
        "whole",
        class_name="FakePlant",
        time_model=TIME_MODEL_WHOLE_RUN,
    )

    with pytest.raises(ValueError, match=r"trace_contract.*run_whole"):
        _validate_plant_adapter(registration, FakePlant())


def test_validate_plant_adapter_rejects_a_plant_declaring_a_different_model():
    registration = _registration("demo", class_name="_WholeRunPlant")

    with pytest.raises(ValueError, match="time_model"):
        _validate_plant_adapter(registration, _WholeRunPlant())


def test_build_plant_does_not_overwrite_a_factory_declaration():
    registration = _registration(
        "demo",
        class_name="_WholeRunPlant",
        factory=lambda config, context: _WholeRunPlant(),
    )

    plant = _build_plant(registration, {}, PlantBuildContext(timebase=Timebase(duration_s=0.001)))

    assert plant.time_model == TIME_MODEL_WHOLE_RUN
    with pytest.raises(ValueError, match="time_model"):
        _validate_plant_adapter(registration, plant)


def test_the_registration_route_is_authoritative_for_a_slotted_adapter(tmp_path):
    registration = _registration(
        "whole",
        factory=lambda config, context: _SlottedWholeRunPlant(),
        class_name="_SlottedWholeRunPlant",
        time_model=TIME_MODEL_WHOLE_RUN,
    )
    plant = _build_plant(registration, {}, PlantBuildContext(timebase=Timebase(duration_s=0.003)))
    _validate_plant_adapter(registration, plant)

    # The declaration is authoritative: the Runner takes the whole-run route
    # even though the adapter cannot be annotated at runtime, and prices the
    # trace it actually returned.
    result = Runner().run(
        _spec(tmp_path, "slotted"),
        plant,
        FakePIController(),
        time_model=registration.time_model,
    )

    assert result.status == "RUN_FAILED"
    assert result.failure_category == "trace_contract_violation"
    assert (tmp_path / "slotted" / "manifest.json").exists()


def test_runner_prices_a_whole_run_trace_instead_of_refusing_the_route(tmp_path):
    spec = _spec(tmp_path, "whole-run")

    result = Runner().run(spec, _WholeRunPlant(), FakePIController())

    # The stub returns a two-sample trace for a three-window horizon, so the
    # route publishes a failed package instead of crashing.
    assert result.status == "RUN_FAILED"
    assert result.failure_category == "trace_contract_violation"
    assert result.evidence_level == "diagnostic_only"
    manifest = json.loads(
        (tmp_path / "whole-run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["time_model"] == TIME_MODEL_WHOLE_RUN
    assert manifest["route_limitations"] == [
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    ]


def test_runner_rejects_an_unknown_time_model(tmp_path):
    plant = FakePlant()
    plant.time_model = "event_driven"

    with pytest.raises(ValueError, match="unknown time_model"):
        Runner().run(_spec(tmp_path, "unknown-model"), plant, FakePIController())

    assert list(tmp_path.iterdir()) == []


def test_a_stepped_run_records_the_time_model_in_the_manifest(tmp_path):
    result = Runner().run(_spec(tmp_path, "stepped"), FakePlant(), FakePIController())

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["time_model"] == TIME_MODEL_STEPPED


def test_the_started_event_announces_the_time_model(tmp_path):
    events: list[dict] = []
    policy = ProgressPolicy(sink=events.append, interval_steps=None, interval_s=None)

    Runner().run(
        _spec(tmp_path, "announced"),
        FakePlant(),
        FakePIController(),
        options=RunOptions(progress=policy),
    )

    started = [event for event in events if event["event"] == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert started[0]["data"]["time_model"] == TIME_MODEL_STEPPED
