"""Standard qualification rules, levels, and numerical-health classification."""

from __future__ import annotations

import json
import math

import pytest

from pe_sim import ExperimentSpec, FakePIController, Timebase
from pe_sim.applicability import ApplicabilityPolicy
from pe_sim.checks import CheckContext, CheckFinding, CheckReport
from pe_sim.numerical_health import NumericalHealth, parse_log
from pe_sim.qualification import QualificationOutcome, qualification_document
from pe_sim.qualification_rules import (
    AnalyticalEquilibriumRule,
    NumericalHealthRule,
    PlantOracle,
    QualificationRulePack,
    SteadyStateRule,
    SteadyStateThresholds,
)
from pe_sim.reference_plants import BuckPlant, BoostPlant
from pe_sim.runner import RunOptions
from pe_sim.runtime import Runner


def _plant(**overrides):
    values = {
        "input_voltage_v": 12.0,
        "inductance_h": 100e-6,
        "capacitance_f": 470e-6,
        "load_resistance_ohm": 10.0,
        "integration_step_s": 1e-6,
    }
    values.update(overrides)
    return BuckPlant(**values)


def _samples(count, *, time_step=1e-5, vout=5.0, current=0.5, action=0.4):
    return [
        {
            "step_index": index,
            "time_s": index * time_step,
            "vout": vout,
            "inductor_current": current,
            "action": action,
        }
        for index in range(count)
    ]


def test_warning_only_qualification_is_partial_not_failed():
    report = CheckReport(
        True,
        (CheckFinding("rule", "warning_check", "warning only", severity="warning", passed=True),),
    )
    document = qualification_document(report)

    assert report.effective_level == "partial"
    assert document["passed"] is True
    assert document["level"] == "partial"
    assert document["limitations"] == []


def test_blocking_qualification_finding_is_failed():
    report = CheckReport(
        False,
        (CheckFinding("rule", "blocking_check", "blocking", severity="error", passed=False),),
    )
    document = qualification_document(report)

    assert report.effective_level == "failed"
    assert document["passed"] is False
    assert document["level"] == "failed"


def test_forced_failed_level_is_consistent_in_the_document():
    document = qualification_document(CheckReport.ok(), level="failed", reasons=["incomplete"])

    assert document["level"] == "failed"
    assert document["passed"] is False
    assert document["checks"]["level"] == "failed"
    assert document["reasons"] == ["incomplete"]


def test_qualification_merge_takes_the_most_severe_level():
    full = CheckReport.ok()
    limited = CheckReport(True, (CheckFinding("r", "c", "w", severity="warning", passed=True),))
    failed = CheckReport.failure("c", "f")

    assert full.effective_level == "assessed"
    assert limited.merge(failed).effective_level == "failed"
    assert limited.merge(full).effective_level == "partial"


def test_short_run_is_not_applicable():
    outcome = SteadyStateRule().evaluate(_samples(10), CheckContext(), None)

    assert outcome.status == "not_applicable"
    assert outcome.category == "steady_state_unassessed"


def test_unsettled_trend_is_a_warning():
    samples = _samples(40, vout=None)
    for index, row in enumerate(samples):
        row["vout"] = 0.1 * index
    outcome = SteadyStateRule().evaluate(samples, CheckContext(), None)

    assert outcome.status == "warn"
    assert outcome.category == "steady_state_not_reached"


def test_settled_ripple_within_threshold_passes():
    samples = _samples(60)
    for index, row in enumerate(samples):
        row["vout"] = 5.0 + 0.02 * math.sin(index / 3.0)
    outcome = SteadyStateRule().evaluate(samples, CheckContext(), None)

    assert outcome.status == "pass"
    assert outcome.category == "steady_state"


def test_window_shorter_than_two_switching_periods_is_not_applicable():
    oracle = PlantOracle(parameters={"switching_frequency_hz": 1e6})
    outcome = SteadyStateRule().evaluate(_samples(20, time_step=1e-7), CheckContext(), oracle)

    assert outcome.status == "not_applicable"
    assert outcome.evidence["reason"] == "window_shorter_than_switching_period"


def test_analytical_equilibrium_passes_against_the_reference_oracle():
    plant = _plant()
    oracle = PlantOracle.from_plant(plant)
    outcome = AnalyticalEquilibriumRule().evaluate(_samples(40), CheckContext(), oracle)

    assert outcome.status == "pass"
    assert outcome.category == "analytical_equilibrium"


def test_analytical_equilibrium_mismatch_is_a_warning():
    plant = _plant()
    oracle = PlantOracle.from_plant(plant)
    outcome = AnalyticalEquilibriumRule().evaluate(
        _samples(40, vout=9.0), CheckContext(), oracle
    )

    assert outcome.status == "warn"
    assert outcome.category == "analytical_equilibrium_mismatch"


def test_analytical_equilibrium_marks_varying_duty_as_indicative():
    plant = _plant()
    oracle = PlantOracle.from_plant(plant)
    samples = _samples(40)
    for index, row in enumerate(samples):
        row["action"] = 0.2 + 0.001 * index
    outcome = AnalyticalEquilibriumRule().evaluate(samples, CheckContext(), oracle)

    assert outcome.status == "warn"
    assert outcome.category == "duty_varying"


def test_analytical_equilibrium_is_undefined_at_boost_duty_one():
    plant = BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )
    oracle = PlantOracle.from_plant(plant)
    outcome = AnalyticalEquilibriumRule().evaluate(
        _samples(40, vout=100.0, action=1.0), CheckContext(), oracle
    )

    assert outcome.status == "not_applicable"
    assert outcome.evidence["reason"] == "oracle_undefined"


def test_rule_pack_evidence_is_json_safe_and_does_not_embed_the_plant():
    plant = _plant()
    pack = QualificationRulePack.standard(plant=plant)
    report = pack.check_samples(_samples(40), CheckContext())
    text = json.dumps(report.to_dict(), sort_keys=True, allow_nan=False)

    assert "BuckPlant object" not in text
    assert "D:" not in text
    assert report.passed is True


def test_numerical_health_parses_recovered_warnings():
    health = parse_log(
        "Circuit: test\n"
        "Doing analysis\n"
        "Warning: singular matrix, resetting to diagonal\n"
        "Warning: singular matrix again\n"
        "No. of Data Rows : 10\n"
    )

    assert health.severity == "warning"
    assert health.warning_count == 2
    assert health.categories["singular_matrix"] == 2
    assert health.error_count == 0


def test_numerical_health_fatal_takes_priority_on_the_same_line():
    health = parse_log("Warning: timestep too small; doAnalyses: no convergence\n")

    assert health.severity == "fatal"
    assert health.warning_count == 0
    assert health.error_count >= 1
    assert any(key.startswith("timestep") or key == "no_convergence" for key in health.categories)


def test_numerical_health_ignores_netlist_echo():
    health = parse_log(
        "Circuit: singular matrix comment in the netlist\n"
        "Doing analysis\n"
        "No. of Data Rows : 10\n"
    )

    assert health.severity == "clean"
    assert health.categories == {}


def test_numerical_health_rule_maps_warning_and_fatal():
    warning_oracle = PlantOracle(
        _health=lambda: {"numerical_health": {"severity": "warning", "categories": {"singular_matrix": 1}}}
    )
    fatal_oracle = PlantOracle(
        _health=lambda: {"numerical_health": {"severity": "fatal", "categories": {"timestep_too_small": 1}}}
    )

    warning = NumericalHealthRule().evaluate([], CheckContext(), warning_oracle)
    fatal = NumericalHealthRule().evaluate([], CheckContext(), fatal_oracle)

    assert warning.status == "warn"
    assert warning.category == "numerical_health_warning"
    assert fatal.status == "fail"
    assert fatal.category == "numerical_health_fatal"


def test_numerical_health_is_recorded_but_not_in_the_plant_identity():
    from pe_sim.backends.ngspice_plants import NgspiceBuckPlant

    plain = NgspiceBuckPlant(control_period_s=10e-6, solver_callable=lambda netlist: None)
    with_health = NgspiceBuckPlant(control_period_s=10e-6, solver_callable=lambda netlist: None)
    with_health._numerical_health = NumericalHealth(
        severity="warning",
        categories={"singular_matrix": 1},
        warning_count=1,
        windows=1,
    )

    assert plain.manifest_identity()["sha256"] == with_health.manifest_identity()["sha256"]
    assert "numerical_health" not in plain.manifest_identity()


def test_runtime_standard_pack_warning_only_run_stays_qualified_but_limited(tmp_path):
    plant = _plant()
    spec = ExperimentSpec(
        "qualification-pack",
        "qualification-pack",
        plant.topology,
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        output_dir=str(tmp_path),
        seed=3,
    )
    result = Runner().run(
        spec,
        plant,
        FakePIController(kp=0.2),
        options=RunOptions(
            applicability=ApplicabilityPolicy(mode="off"),
            qualification_plugins=(QualificationRulePack.standard(plant=plant),),
        ),
    )

    assert result.status == "QUALIFIED"
    assert result.qualification["level"] == "partial"
    assert result.qualification["passed"] is True
