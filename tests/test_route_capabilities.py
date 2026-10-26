"""S2: route declarations are data and the evidence cap has one owner.

ADR 0004 step 1 is behaviour-neutral: the two route facts that used to be
implicit become declarations, the evidence cap becomes a single derivation, and
the manifest reports the declarations.  These tests hold the implementation to
that claim:

1. the cap table covers every admissible combination and refuses the
   contradictory one at construction;
2. the historical one-field helpers keep their call shape and their values;
3. the registry refuses ``whole_run + kernel`` at registration, not at run time;
4. a real run reports its declarations, and
5. the runtime reads the shared table instead of a local copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pe_sim.runtime as runtime_module
import pe_sim.time_model as time_model_module
from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.default_contracts import default_contract_refs
from pe_sim.dirty import DirtyAnalysis
from pe_sim.plant_registry import (
    PlantRegistration,
    clear_registrations,
    get_plant,
    plant_ids,
    register_plant,
)
from pe_sim.provenance import GitProvenance
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import FakePIController, Runner
from pe_sim.time_model import (
    CONTROL_LOCATIONS,
    EVENT_GRANULARITIES,
    RouteCapabilities,
    cap_evidence_level,
    cap_evidence_level_for,
    evidence_cap,
    evidence_cap_for,
    formal_comparison_allowed,
    route_limitations,
    route_limitations_for,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The registry is process-wide, so every test starts and ends empty."""

    clear_registrations()
    yield
    clear_registrations()


def _registration(plant_id: str = "route-demo", **overrides) -> PlantRegistration:
    values = {
        "plant_id": plant_id,
        "backend_id": plant_id,
        "factory": lambda config, context: object(),
        "class_name": "FakePlant",
    }
    values.update(overrides)
    return PlantRegistration(**values)


def _spec(
    tmp_path: Path,
    run_id: str,
    *,
    plant_id: str = "buck",
    duration_s: float = 4e-5,
    control_period_s: float = 1e-5,
    contracts=None,
) -> ExperimentSpec:
    extra = {} if contracts is None else {"contracts": contracts}
    return ExperimentSpec(
        "s2-route",
        run_id,
        plant_id,
        "pi",
        Timebase(duration_s=duration_s, control_period_s=control_period_s),
        output_dir=str(tmp_path),
        seed=17,
        **extra,
    )


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


class _DeclaredController(FakePIController):
    """Fixture controller that also declares a known backend identity.

    A formal comparison requires known environment provenance, and the fixture
    controller is the only component that does not otherwise declare one.
    """

    def backend_provenance(self):
        return {
            "status": "known",
            "name": "pe_sim.fixture-controller",
            "version": "fixture-v1",
            "solver_settings": {"loop": "positional"},
            "limitations": [],
        }


def _use_clean_provenance(monkeypatch) -> None:
    """Publish a real run under test-controlled clean provenance."""

    observed_git = runtime_module.collect_git_provenance()
    observed_worktree = runtime_module.analyze_git_worktree()
    clean_git = GitProvenance(
        source_commit="a" * 40,
        branch=observed_git.branch if observed_git.branch != "unavailable" else "test",
        working_tree_status="clean",
        status="known",
        limitations=(),
    )
    clean_worktree = DirtyAnalysis(
        status="clean",
        source_commit=clean_git.source_commit,
        branch=clean_git.branch,
        repo_root=observed_worktree.repo_root,
        paths=(),
        paths_by_category={
            category: ()
            for category in ("source", "config", "tests", "docs", "generated", "other")
        },
        reasons=(),
        suggestions=(),
    )
    monkeypatch.setattr(runtime_module, "collect_git_provenance", lambda: clean_git)
    monkeypatch.setattr(runtime_module, "analyze_git_worktree", lambda: clean_worktree)


# ---------------------------------------------------------------------------
# The derivation table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("time_model", "control_location", "expected"),
    [
        ("stepped", "kernel", "comparable"),
        ("stepped", "model", "limited"),
        ("whole_run", "model", "limited"),
    ],
)
def test_the_cap_table_covers_every_admissible_combination(
    time_model, control_location, expected
):
    route = RouteCapabilities(
        time_model=time_model, control_location=control_location
    )

    assert evidence_cap_for(route) == expected
    assert cap_evidence_level_for("comparable", route) == expected


def test_whole_run_with_kernel_control_is_rejected_at_construction():
    with pytest.raises(ValueError, match="contradictory route"):
        RouteCapabilities(time_model="whole_run", control_location="kernel")


@pytest.mark.parametrize(
    ("field", "value"),
    [("control_location", "netlist"), ("event_granularity", "sample")],
)
def test_the_record_rejects_unknown_route_vocabulary(field, value):
    values = {
        "time_model": "stepped",
        "control_location": "kernel",
        "event_granularity": "window",
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        RouteCapabilities(**values)


def test_the_declaration_vocabularies_are_closed():
    assert CONTROL_LOCATIONS == ("kernel", "model")
    assert EVENT_GRANULARITIES == ("window", "continuous")


def test_the_full_limitation_list_has_a_stable_order():
    route = RouteCapabilities(
        time_model="whole_run",
        control_location="model",
        event_granularity="window",
    )

    assert route_limitations_for(route) == (
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
        "actuator_control_in_model",
        "events_quantized_to_control_windows",
    )


# ---------------------------------------------------------------------------
# Backward compatibility of the one-field helpers
# ---------------------------------------------------------------------------


def test_the_legacy_helpers_keep_their_shape_and_values():
    assert evidence_cap("stepped") == "comparable"
    assert evidence_cap("whole_run") == "limited"
    assert cap_evidence_level("comparable", "whole_run") == "limited"
    assert cap_evidence_level("functional", "whole_run") == "functional"
    assert cap_evidence_level("limited", "stepped") == "limited"
    assert route_limitations("stepped") == ()
    assert route_limitations("whole_run") == (
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    )
    assert formal_comparison_allowed("stepped") is True
    assert formal_comparison_allowed("whole_run") is False
    assert formal_comparison_allowed("event_driven") is False

    for call in (
        lambda: evidence_cap("event_driven"),
        lambda: cap_evidence_level("comparable", "event_driven"),
        lambda: route_limitations("event_driven"),
    ):
        with pytest.raises(ValueError):
            call()


# ---------------------------------------------------------------------------
# Registration-time refusal
# ---------------------------------------------------------------------------


def test_registration_rejects_whole_run_with_kernel_control():
    with pytest.raises(ValueError, match="contradictory route"):
        register_plant(
            _registration(
                "contradiction",
                time_model="whole_run",
                control_location="kernel",
            )
        )

    assert plant_ids() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [("control_location", "netlist"), ("event_granularity", "sample")],
)
def test_registration_rejects_unknown_route_vocabulary(field, value):
    with pytest.raises(ValueError, match=field):
        register_plant(_registration("bad-vocabulary", **{field: value}))

    assert plant_ids() == ()


def test_the_runner_refuses_a_contradictory_route_before_any_directory(tmp_path):
    class _StubWholeRun:
        time_model = "whole_run"

    with pytest.raises(ValueError, match="contradictory route"):
        Runner().run(
            _spec(tmp_path, "contradictory-run"),
            _StubWholeRun(),
            FakePIController(),
            time_model="whole_run",
            control_location="kernel",
        )

    assert not (tmp_path / "contradictory-run").exists()


# ---------------------------------------------------------------------------
# Real runs report their declarations
# ---------------------------------------------------------------------------


def test_a_real_buck_run_reports_its_declared_route(tmp_path):
    load_builtin_plants()

    result = Runner().run(
        _spec(tmp_path, "route-buck"), _buck(), FakePIController(kp=0.2)
    )

    assert result.status == "QUALIFIED"
    manifest = json.loads(
        (result.run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    route = manifest["route"]
    assert route["time_model"] == "stepped"
    assert route["control_location"] == "kernel"
    assert route["event_granularity"] == "window"
    assert route["evidence_cap"] == "comparable"
    # The kernel holds the loop here, but inputs still resolve per window.
    assert route["limitations"] == ["events_quantized_to_control_windows"]
    # The historical keys keep their values.
    assert manifest["time_model"] == "stepped"
    assert manifest["evidence_level"] == "functional"


def test_a_real_whole_run_buck_reports_model_control(tmp_path):
    load_builtin_plants()
    registration = get_plant("whole-run-buck")
    spec = _spec(
        tmp_path,
        "route-whole",
        plant_id="whole-run-buck",
        duration_s=0.02,
        control_period_s=1e-3,
    )
    plant = registration.factory(
        {"cadence_s": 1e-3, "load_resistance_ohm": 1.0},
        PlantBuildContext(timebase=spec.timebase),
    )

    result = Runner().run(
        spec, plant, FakePIController(), time_model=registration.time_model
    )

    manifest = json.loads(
        (result.run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    route = manifest["route"]
    assert route["time_model"] == "whole_run"
    assert route["control_location"] == "model"
    assert route["event_granularity"] == "window"
    assert route["evidence_cap"] == "limited"
    assert "in_run_enforcement_unavailable" in route["limitations"]
    assert "post_hoc_validation_only" in route["limitations"]
    assert "actuator_control_in_model" in route["limitations"]
    # The compatibility key stays exactly what it was before S2.
    assert manifest["route_limitations"] == [
        "in_run_enforcement_unavailable",
        "post_hoc_validation_only",
    ]


def test_the_runtime_reads_the_single_cap_table(tmp_path, monkeypatch):
    """Lowering the shared table must move the manifest level.

    A local cap in the runtime would keep this run at ``comparable``; the only
    way the assertion below holds is that the runtime asks the shared table.
    """

    _use_clean_provenance(monkeypatch)
    monkeypatch.setitem(
        time_model_module._ROUTE_EVIDENCE_CAP, ("stepped", "kernel"), "limited"
    )
    load_builtin_plants()

    result = Runner().run(
        _spec(tmp_path, "route-cap", contracts=default_contract_refs()),
        _buck(),
        _DeclaredController(kp=0.2),
        mode="formal_comparison",
    )

    assert result.status == "QUALIFIED"
    manifest = json.loads(
        (result.run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["route"]["evidence_cap"] == "limited"
    assert manifest["evidence_level"] == "limited"
