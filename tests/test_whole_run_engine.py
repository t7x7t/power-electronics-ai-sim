"""Acceptance tests for the whole-run linear state-space engine.

The engine is the second time-ownership model: it integrates an entire horizon
internally and returns one trace.  These tests cover the post-hoc trace
contract, determinism, closed-loop tracking, schedule rejection, registration,
and the kernel-purity constraint.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

import pe_sim
from pe_sim.backends.whole_run_engine import (
    BACKEND_ID,
    LinearStateSpace,
    WholeRunBuckPlant,
    integrate_rk4,
)
from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.cli import _validate_plant_adapter
from pe_sim.contracts import Timebase
from pe_sim.plant_registry import clear_registrations, get_plant, plant_catalog
from pe_sim.time_model import WholeRunRequest, validate_whole_run_result


CADENCE_S = 1e-4
DURATION_S = 0.02
KERNEL_MODULES = (
    "runtime.py",
    "time_model.py",
    "plant_registry.py",
    "checks.py",
    "inspection.py",
)
FORBIDDEN_KERNEL_NAMES = ("whole_run_engine", "WholeRunBuckPlant")


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registrations()
    yield
    clear_registrations()


def _plant(**overrides) -> WholeRunBuckPlant:
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 1.0,
        "integration_step_s": 1e-6,
        "cadence_s": CADENCE_S,
        "default_vref_v": 3.0,
        "kp": 0.1,
        "ki": 60.0,
        "output_min": 0.0,
        "output_max": 1.0,
        "anti_windup": True,
    }
    values.update(overrides)
    return WholeRunBuckPlant(**values)


def _request(
    *,
    duration_s: float = DURATION_S,
    schedule=None,
    initial_state=None,
    seed=None,
) -> WholeRunRequest:
    metadata = {} if schedule is None else {"input_schedule": schedule}
    return WholeRunRequest(
        duration_s=duration_s,
        initial_state={} if initial_state is None else initial_state,
        seed=seed,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Trace contract and determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "duration_s",
    [
        0.02,  # exact cadence multiple
        0.02035,  # not a cadence multiple; endpoint must still be exact
        0.00007,  # shorter than one cadence
        0.0200000000001,  # a sliver beyond the last cadence sample
    ],
)
def test_trace_satisfies_the_whole_run_contract(duration_s):
    plant = _plant()
    result = plant.run_whole(_request(duration_s=duration_s))

    assert validate_whole_run_result(
        result, plant.trace_contract(), duration_s=duration_s
    ) == ()
    assert result.time_s[0] == 0.0
    assert result.time_s[-1] == duration_s
    assert {"vout", "inductor_current_a", "duty", "vref"} <= set(result.signals)
    for samples in result.signals.values():
        assert len(samples) == len(result.time_s)
    assert all(
        right > left for left, right in zip(result.time_s, result.time_s[1:])
    )


def test_two_runs_produce_identical_arrays():
    request = _request(
        schedule=[
            {"time_s": 0.0, "vref": 3.0},
            {"time_s": 0.01, "vref": 5.0},
        ]
    )

    first = _plant().run_whole(request)
    second = _plant().run_whole(request)

    assert first.time_s == second.time_s
    assert set(first.signals) == set(second.signals)
    for name in first.signals:
        assert first.signals[name] == second.signals[name]
    assert first.final_state == second.final_state


# ---------------------------------------------------------------------------
# Closed-loop tracking and steady-state sanity
# ---------------------------------------------------------------------------


def test_closed_loop_tracks_a_twelve_volt_to_five_volt_step():
    plant = _plant(default_vref_v=3.0)
    result = plant.run_whole(
        _request(
            schedule=[
                {"time_s": 0.0, "vref": 3.0},
                {"time_s": 0.01, "vref": 5.0},
            ]
        )
    )

    final_vout = result.signals["vout"][-1]
    final_duty = result.signals["duty"][-1]
    assert abs(final_vout - 5.0) <= 0.05, final_vout
    # The averaged Buck equilibrium is vout = duty * Vin; the PI loop should
    # have found roughly the ideal duty for 5 V from a 12 V input.
    assert final_duty == pytest.approx(5.0 / 12.0, abs=0.03)
    assert result.metadata["control_law"] == "engine_owned_pi"
    assert result.metadata["enforcement"] == "post_hoc_only"


def test_final_operating_point_matches_the_analytical_steady_state():
    plant = _plant()
    result = plant.run_whole(
        _request(
            duration_s=0.03,
            schedule=[{"time_s": 0.0, "vref": 5.0}],
        )
    )

    final_duty = result.signals["duty"][-1]
    analytical = plant.analytical_steady_state(final_duty)
    final_vout = result.signals["vout"][-1]
    final_current = result.signals["inductor_current_a"][-1]

    assert abs(final_vout - analytical["output_voltage_v"]) <= 0.01
    assert abs(final_current - analytical["inductor_current_a"]) <= 0.02


def test_backend_evidence_is_post_hoc_and_reports_the_last_run():
    plant = _plant()
    assert plant.backend_evidence()["status"] == "not_run"

    result = plant.run_whole(_request(schedule=[{"time_s": 0.0, "vref": 5.0}]))
    evidence = plant.backend_evidence()
    health = evidence["numerical_health"]

    assert evidence["status"] == "post_hoc_only"
    assert evidence["enforcement"] == "none"
    assert health["severity"] == "clean"
    assert health["substeps"] > 0
    assert health["sample_count"] == len(result.time_s)
    assert "maximum_duty" in health
    assert "duty_saturated" in health


# ---------------------------------------------------------------------------
# Rejected inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schedule", "match"),
    [
        ([{"time_s": 0.0, "vref": 5.0, "bogus": 1}], "unsupported keys"),
        ([{"time_s": 0.0, "vref": math.nan}], "finite"),
        ([{"time_s": 0.0, "vref": math.inf}], "finite"),
        ([{"time_s": math.nan, "vref": 5.0}], "finite"),
        ([{"time_s": -1e-3, "vref": 5.0}], "non-negative"),
        (
            [{"time_s": 0.01, "vref": 5.0}, {"time_s": 0.005, "vref": 3.0}],
            "non-decreasing",
        ),
        ([{"time_s": 0.0}], "both time_s and vref"),
        ([{"vref": 5.0}], "both time_s and vref"),
        ("not a list", "list"),
    ],
)
def test_bad_input_schedule_is_rejected(schedule, match):
    with pytest.raises(ValueError, match=match):
        _plant().run_whole(_request(schedule=schedule))


def test_default_vref_is_used_when_no_schedule_is_given():
    plant = _plant(default_vref_v=4.0)

    result = plant.run_whole(_request())

    assert result.signals["vref"][0] == 4.0
    assert result.signals["vref"][-1] == 4.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_voltage_v": math.nan},
        {"input_voltage_v": 0.0},
        {"inductance_h": math.inf},
        {"capacitance_f": -1.0},
        {"load_resistance_ohm": math.nan},
        {"integration_step_s": 0.0},
        {"cadence_s": math.nan},
        {"cadence_s": -1e-4},
        {"default_vref_v": math.nan},
        {"default_vref_v": -1.0},
        {"kp": math.nan},
        {"ki": math.inf},
        {"output_min": 0.5, "output_max": 0.5},
        {"output_min": -0.1},
        {"output_max": 1.1},
        {"anti_windup": 1},
    ],
)
def test_non_finite_or_invalid_constructor_parameters_are_rejected(overrides):
    with pytest.raises(ValueError):
        _plant(**overrides)


# ---------------------------------------------------------------------------
# Registration and the composition root
# ---------------------------------------------------------------------------


def test_registration_and_catalog_report_the_whole_run_route():
    load_builtin_plants()
    catalog = plant_catalog()
    entries = {item["plant_id"]: item for item in catalog["plants"]}

    assert BACKEND_ID in entries
    entry = entries[BACKEND_ID]
    assert entry["backend_id"] == BACKEND_ID
    assert entry["time_model"] == "whole_run"
    assert entry["class_name"] == "WholeRunBuckPlant"
    assert entry["trust_level"] == "reference"
    assert entry["export_eligible"] is False
    assert entry["schedule_keys"] == ["time_s", "vref"]

    registration = get_plant(BACKEND_ID)
    plant = registration.factory(
        {},
        PlantBuildContext(
            timebase=Timebase(control_period_s=CADENCE_S, duration_s=DURATION_S)
        ),
    )
    _validate_plant_adapter(registration, plant)
    assert isinstance(plant, WholeRunBuckPlant)
    assert plant.trace_contract().cadence_s == CADENCE_S


def test_the_default_loader_registers_the_whole_run_route():
    """The route is executable, so the default loader offers it."""

    load_builtin_plants()
    entries = {item["plant_id"]: item for item in plant_catalog()["plants"]}

    assert BACKEND_ID in entries
    assert entries[BACKEND_ID]["time_model"] == "whole_run"


def test_factory_rejects_unknown_configuration_keys():
    load_builtin_plants()

    with pytest.raises(ValueError, match="plant_config"):
        get_plant(BACKEND_ID).factory(
            {"not_a_key": 1},
            PlantBuildContext(
                timebase=Timebase(control_period_s=CADENCE_S, duration_s=DURATION_S)
            ),
        )


def test_factory_requires_a_cadence_from_config_or_timebase():
    load_builtin_plants()

    with pytest.raises(ValueError, match="cadence_s"):
        get_plant(BACKEND_ID).factory({}, PlantBuildContext())


# ---------------------------------------------------------------------------
# Generic integrator behaviour
# ---------------------------------------------------------------------------


def test_integrator_splits_exactly_at_a_non_aligned_breakpoint():
    system = LinearStateSpace(matrix_a=((-1.0,),), input_matrix=((1.0,),))
    # u = 1 before the breakpoint and u = 2 after it.  The breakpoint at 0.5 s
    # is not a multiple of 0.03 s, so a stepper that ignored breakpoints would
    # cross it inside a step.
    combined = integrate_rk4(
        system,
        (0.0,),
        start_s=0.0,
        end_s=1.0,
        input_breakpoints=((0.0, (1.0,)), (0.5, (2.0,))),
        max_step_s=0.03,
    )
    first = integrate_rk4(
        system,
        (0.0,),
        start_s=0.0,
        end_s=0.5,
        input_breakpoints=((0.0, (1.0,)),),
        max_step_s=0.03,
    )
    second = integrate_rk4(
        system,
        first.state,
        start_s=0.5,
        end_s=1.0,
        input_breakpoints=((0.5, (2.0,)),),
        max_step_s=0.03,
    )

    assert combined.state == second.state
    assert combined.substeps == first.substeps + second.substeps
    assert combined.segments == 2


def test_integrator_matches_the_closed_form_solution():
    system = LinearStateSpace(matrix_a=((-1.0,),), input_matrix=((1.0,),))
    outcome = integrate_rk4(
        system,
        (0.0,),
        start_s=0.0,
        end_s=1.0,
        input_breakpoints=((0.0, (1.0,)), (0.5, (2.0,))),
        max_step_s=0.001,
    )
    # x(0.5) = 1 - exp(-0.5); x(1) = 2 + (x(0.5) - 2) * exp(-0.5).
    expected = 2.0 + ((1.0 - math.exp(-0.5)) - 2.0) * math.exp(-0.5)

    assert outcome.state[0] == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Kernel purity
# ---------------------------------------------------------------------------


def test_kernel_modules_do_not_name_the_whole_run_engine():
    package_root = Path(pe_sim.__file__).resolve().parent

    for name in KERNEL_MODULES:
        source = (package_root / name).read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_KERNEL_NAMES:
            assert forbidden not in source, f"{name} names {forbidden}"

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                assert "whole_run_engine" not in module
                for alias in node.names:
                    assert "WholeRunBuckPlant" not in alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "whole_run_engine" not in alias.name
                    assert "WholeRunBuckPlant" not in alias.name
