"""Acceptance tests for the S7 Study plan layer (pure logic).

The Study layer is a *planning* layer: it expands a declared study into
independent, ordinary experiment documents and then aggregates per-point
manifests.  It must never promote an aggregate to ``comparable`` evidence.
These tests pin that honesty rule together with the document grammar, the
deterministic Monte Carlo sampler, periodicity evaluation, descriptive
statistics, and report round-tripping.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from importlib import resources
from pathlib import Path

import pytest

from pe_sim.contracts import validate_experiment_document
from pe_sim.time_model import RUNTIME_EVIDENCE_LEVELS
from pe_sim.study import (
    MAX_STUDY_POINTS,
    STATISTIC_REDUCERS,
    STUDY_AGGREGATE_SCHEMA,
    STUDY_COMPARABILITY_REASONS,
    STUDY_KINDS,
    STUDY_LIMITATIONS,
    STUDY_POINT_EVIDENCE_LEVEL,
    STUDY_REPORT_SCHEMA,
    STUDY_SCHEMA,
    StudyPlan,
    StudyPlanError,
    StudyPointRecord,
    aggregate_study,
    build_study_report,
    evaluate_periodicity,
    monte_carlo_draws,
    monte_carlo_draws_sha256,
    parse_study_document,
    statistics_for_point,
    write_study_report,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _base_document() -> dict:
    """Load the packaged reference experiment (never written by these tests)."""

    text = (
        resources.files("pe_sim.templates")
        .joinpath("buck_pi_step.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _study(
    kind: str,
    plan: dict,
    *,
    base: dict | None = None,
    study_id: str = "study-x",
    output_directory: str = "study-out",
) -> dict:
    return {
        "schema": STUDY_SCHEMA,
        "study_id": study_id,
        "kind": kind,
        "base_experiment": copy.deepcopy(base if base is not None else _base_document()),
        "plan": plan,
        "output": {"directory": output_directory},
    }


def _sweep_plan(**overrides) -> dict:
    plan = {
        "parameters": [
            {"path": "controller_config.kp", "values": [0.1, 0.2]},
        ]
    }
    plan.update(overrides)
    return plan


def _uniform_parameter(**overrides) -> dict:
    parameter = {
        "path": "plant_config.inductance_h",
        "distribution": "uniform",
        "low": 8e-5,
        "high": 1.2e-4,
    }
    parameter.update(overrides)
    return parameter


def _periodicity_plan(**overrides) -> dict:
    plan = {
        "compare_period": 1,
        "compare_windows": 3,
        "tolerance_relative": 1e-3,
        "signals": ["vout", "inductor_current"],
        "max_runs": 4,
        "extend_factor": 4.0,
    }
    plan.update(overrides)
    return plan


_DEFAULT_ROUTE = {
    "time_model": "stepped",
    "control_location": "kernel",
    "event_granularity": "window",
}


def _manifest(
    *,
    status: str = "QUALIFIED",
    evidence_level: str | None = "comparable",
    route: dict | None = None,
    run_mode: str = "stepped-kernel",
    plant_id: str = "buck",
) -> dict:
    manifest = {
        "status": status,
        "run_mode": run_mode,
        "plant": {"id": plant_id},
        "route": dict(route if route is not None else _DEFAULT_ROUTE),
    }
    if evidence_level is not None:
        manifest["evidence_level"] = evidence_level
    return manifest


def _record(
    index: int,
    manifest: dict,
    *,
    statistics: dict | None = None,
    details: dict | None = None,
    parameters: dict | None = None,
) -> StudyPointRecord:
    return StudyPointRecord(
        index=index,
        run_id=f"study-p{index:02d}",
        run_dir=f"study-out/runs/{index:02d}",
        parameters=(
            parameters
            if parameters is not None
            else {"controller_config.kp": 0.1 * (index + 1)}
        ),
        manifest=manifest,
        statistics=statistics,
        details=details,
    )


def _periodic_rows(
    values_by_signal: dict[str, list[float]],
    *,
    period: int,
    windows: int,
) -> list[dict]:
    count = windows * period + 1
    rows = []
    for index in range(count):
        row: dict = {"time_s": index * 1e-3}
        for signal, period_values in values_by_signal.items():
            row[signal] = period_values[index % period]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Constants and document grammar
# ---------------------------------------------------------------------------


def test_study_constants_are_closed_and_stable():
    assert STUDY_SCHEMA == "pe-sim.study.v1"
    assert STUDY_REPORT_SCHEMA == "pe-sim.study-report.v1"
    assert STUDY_AGGREGATE_SCHEMA == "pe-sim.study-aggregate.v1"
    assert STUDY_KINDS == ("parameter_sweep", "monte_carlo", "periodicity")
    assert MAX_STUDY_POINTS == 64
    assert STUDY_POINT_EVIDENCE_LEVEL == "per-point"
    assert STATISTIC_REDUCERS == (
        "mean",
        "min",
        "max",
        "rms",
        "peak_to_peak",
        "last",
        "ratio_of_means",
    )
    assert STUDY_LIMITATIONS == frozenset(
        {
            "aggregate_does_not_inherit_comparable",
            "monte_carlo_draws_depend_on_declared_distributions",
            "periodicity_convergence_is_tolerance_based",
            "descriptive_statistics_only_no_loss_claim",
        }
    )
    assert STUDY_COMPARABILITY_REASONS == frozenset(
        {
            "points_not_all_comparable",
            "route_identity_differs",
            "point_not_qualified",
            "point_route_not_declared",
            "point_evidence_level_not_declared",
            "single_point_study",
            "empty_study",
            "duplicate_points",
            "point_identity_incomplete",
        }
    )


def test_study_evidence_ladder_is_the_single_time_model_object():
    from pe_sim.study import RUNTIME_EVIDENCE_LEVELS as STUDY_LEVELS

    # Identity, not equality: a copy would drift silently.  The aggregate must
    # rank points against the one ladder the kernel publishes.
    assert STUDY_LEVELS is RUNTIME_EVIDENCE_LEVELS


def test_unknown_top_level_key_is_rejected():
    document = _study("parameter_sweep", _sweep_plan())
    document["extra"] = True

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "unknown_key"
    assert "extra" in str(info.value)


def test_unknown_plan_key_is_rejected():
    document = _study("parameter_sweep", _sweep_plan(unexpected=1))

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "unknown_key"
    assert "unexpected" in str(info.value)


@pytest.mark.parametrize("study_id", ["", "-bad", "has space", "a" * 129, "a..b"])
def test_invalid_study_id_is_rejected(study_id):
    document = _study("parameter_sweep", _sweep_plan(), study_id=study_id)

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "invalid_study_id"


def test_unknown_kind_lists_allowed_values():
    document = _study("not_a_kind", {})

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "unknown_kind"
    message = str(info.value)
    for kind in STUDY_KINDS:
        assert kind in message


def test_schema_must_match_exactly():
    document = _study("parameter_sweep", _sweep_plan())
    document["schema"] = "pe-sim.study.v2"

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "invalid_schema"


# ---------------------------------------------------------------------------
# parameter_sweep
# ---------------------------------------------------------------------------


def test_sweep_expands_row_major_cartesian_product():
    plan = {
        "parameters": [
            {"path": "controller_config.kp", "values": [0.1, 0.2]},
            {"path": "plant_config.load_resistance_ohm", "values": [10.0, 5.0]},
        ]
    }
    parsed = parse_study_document(
        _study("parameter_sweep", plan, study_id="sweep")
    )

    assert len(parsed.points) == 4
    assert [point.index for point in parsed.points] == [0, 1, 2, 3]
    assert [point.label for point in parsed.points] == [
        "p00",
        "p01",
        "p02",
        "p03",
    ]
    assert [point.run_id for point in parsed.points] == [
        "sweep-p00",
        "sweep-p01",
        "sweep-p02",
        "sweep-p03",
    ]
    expected = [
        {"controller_config.kp": 0.1, "plant_config.load_resistance_ohm": 10.0},
        {"controller_config.kp": 0.1, "plant_config.load_resistance_ohm": 5.0},
        {"controller_config.kp": 0.2, "plant_config.load_resistance_ohm": 10.0},
        {"controller_config.kp": 0.2, "plant_config.load_resistance_ohm": 5.0},
    ]
    for point, values in zip(parsed.points, expected):
        assert dict(point.parameters) == values
        assert point.document["controller_config"]["kp"] == values["controller_config.kp"]
        assert (
            point.document["plant_config"]["load_resistance_ohm"]
            == values["plant_config.load_resistance_ohm"]
        )
        assert point.details == {}


def test_sweep_point_budget_is_enforced():
    plan = {
        "parameters": [
            {"path": "controller_config.kp", "values": [float(i) for i in range(9)]},
            {
                "path": "plant_config.load_resistance_ohm",
                "values": [float(i) for i in range(8)],
            },
        ]
    }

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "point_budget_exceeded"
    assert str(MAX_STUDY_POINTS) in str(info.value)


@pytest.mark.parametrize(
    "path",
    [
        "controller_config.missing",
        "schema_version",
        "run_id",
        "output.directory",
        "experiment_id",
        "timebase.unit",
        "controller_config",
    ],
)
def test_sweep_rejects_invalid_paths(path):
    plan = {"parameters": [{"path": path, "values": [0.1, 0.2]}]}

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "invalid_path"


@pytest.mark.parametrize(
    "values",
    [
        [0.1, 0.1],
        [True, 0.2],
        [0.1],
        [float(index) for index in range(17)],
        [0.1, float("nan")],
    ],
)
def test_invalid_sweep_values_are_rejected(values):
    plan = {"parameters": [{"path": "controller_config.kp", "values": values}]}

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "invalid_plan"


def test_output_rejects_unknown_keys_and_requires_a_directory():
    document = _study("parameter_sweep", _sweep_plan())
    document["output"]["retention"] = "keep"

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "unknown_key"

    document = _study("parameter_sweep", _sweep_plan())
    document["output"] = {}

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(document)

    assert info.value.reason == "invalid_plan"


def test_explicit_null_statistics_are_rejected():
    plan = _sweep_plan(statistics=None)

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "invalid_plan"


def test_sweep_point_documents_validate_and_do_not_mutate_the_base():
    base = _base_document()
    original = copy.deepcopy(base)
    document = _study(
        "parameter_sweep",
        _sweep_plan(),
        base=base,
        study_id="sweep",
        output_directory="out",
    )

    parsed = parse_study_document(document)

    assert base == original
    assert document["base_experiment"] == original
    assert parsed.base_experiment == original
    run_ids = []
    for point in parsed.points:
        validate_experiment_document(point.document)
        assert point.document["run_id"] == point.run_id
        assert point.document["output"]["directory"] == str(
            Path("out") / "runs"
        )
        assert point.document["output"]["retention"] == original["output"]["retention"]
        run_ids.append(point.document["run_id"])
    assert len(set(run_ids)) == len(parsed.points)

    # Documents are independent deep copies, not shared aliases.
    parsed.points[0].document["controller_config"]["kp"] = 99.0
    assert parsed.points[1].document["controller_config"]["kp"] == 0.2
    assert parsed.base_experiment["controller_config"]["kp"] == original["controller_config"]["kp"]


def test_base_run_id_collision_is_rewritten_not_rejected():
    base = _base_document()
    base["run_id"] = "sweep-p01"
    parsed = parse_study_document(
        _study("parameter_sweep", _sweep_plan(), base=base, study_id="sweep")
    )

    assert parsed.points[0].document["run_id"] == "sweep-p00"
    assert parsed.points[1].document["run_id"] == "sweep-p01"


@pytest.mark.parametrize(
    ("statistic", "reason"),
    [
        (
            {"name": "bad", "sample_key": "vout", "reducer": "median"},
            "invalid_statistic",
        ),
        (
            {
                "name": "bad",
                "sample_key": "vout",
                "reducer": "mean",
                "denominator": "inductor_current",
            },
            "invalid_statistic",
        ),
        (
            {
                "name": "bad",
                "reducer": "ratio_of_means",
                "sample_key": "vout",
                "denominator": "inductor_current",
            },
            "invalid_statistic",
        ),
        (
            {"name": "bad", "sample_key": "vout"},
            "invalid_statistic",
        ),
        (
            {
                "name": "bad",
                "reducer": "ratio_of_means",
                "numerator": "vout",
            },
            "missing_sample_key",
        ),
        (
            {"name": "1bad", "sample_key": "vout", "reducer": "mean"},
            "invalid_statistic",
        ),
    ],
)
def test_invalid_statistic_plans_are_rejected(statistic, reason):
    plan = _sweep_plan(statistics=[statistic])

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == reason


def test_duplicate_statistic_names_are_rejected():
    plan = _sweep_plan(
        statistics=[
            {"name": "vout_mean", "sample_key": "vout", "reducer": "mean"},
            {"name": "vout_mean", "sample_key": "vout", "reducer": "max"},
        ]
    )

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "invalid_statistic"


def test_sweep_accepts_ratio_of_means_with_numerator_and_denominator():
    plan = _sweep_plan(
        statistics=[
            {
                "name": "ratio",
                "reducer": "ratio_of_means",
                "numerator": "inductor_current",
                "denominator": "vout",
            }
        ]
    )

    parsed = parse_study_document(_study("parameter_sweep", plan))

    assert parsed.plan["statistics"] == plan["statistics"]


# ---------------------------------------------------------------------------
# monte_carlo
# ---------------------------------------------------------------------------


def _mc_plan(**overrides) -> dict:
    plan = {
        "parameters": [
            {
                "path": "plant_config.inductance_h",
                "distribution": "uniform",
                "low": 8e-5,
                "high": 1.2e-4,
            },
            {
                "path": "plant_config.capacitance_f",
                "distribution": "normal",
                "mean": 4.7e-4,
                "sigma": 2e-5,
            },
        ],
        "samples": 8,
        "seed": 12345,
    }
    plan.update(overrides)
    return plan


def test_monte_carlo_same_seed_is_reproducible():
    parameters = _mc_plan()["parameters"]

    first = monte_carlo_draws(parameters, 8, 12345)
    second = monte_carlo_draws(parameters, 8, 12345)
    other = monte_carlo_draws(parameters, 8, 12346)

    assert first == second
    assert first != other
    assert len(first) == 8
    expected_keys = {"plant_config.inductance_h", "plant_config.capacitance_f"}
    assert all(set(draw) == expected_keys for draw in first)
    assert monte_carlo_draws_sha256(first) == monte_carlo_draws_sha256(second)
    assert monte_carlo_draws_sha256(first) != monte_carlo_draws_sha256(other)
    assert len(monte_carlo_draws_sha256(first)) == 64


def test_monte_carlo_normal_draws_are_standardized():
    draws = monte_carlo_draws(
        [{"path": "x", "distribution": "normal", "mean": 0.0, "sigma": 1.0}],
        64,
        12345,
    )
    values = [draw["x"] for draw in draws]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)

    assert abs(mean) < 0.6
    assert abs(math.sqrt(variance) - 1.0) < 0.4


def test_monte_carlo_points_record_draws_and_keep_valid_documents():
    base = _base_document()
    plan = _mc_plan(samples=3)

    parsed = parse_study_document(
        _study("monte_carlo", plan, base=base, study_id="mc")
    )

    assert len(parsed.points) == 3
    for point in parsed.points:
        validate_experiment_document(point.document)
        assert point.details == {"draws": dict(point.parameters)}
        assert "details" not in point.document
        assert point.document["run_id"] == point.run_id
        assert point.document["output"]["directory"] == str(
            Path("study-out") / "runs"
        )

    replay = parse_study_document(
        _study("monte_carlo", plan, base=base, study_id="mc")
    )
    assert [dict(point.parameters) for point in parsed.points] == [
        dict(point.parameters) for point in replay.points
    ]


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (
            _mc_plan(
                parameters=[
                    _uniform_parameter(low=1.0, high=1.0),
                ]
            ),
            "invalid_distribution",
        ),
        (
            _mc_plan(
                parameters=[
                    {
                        "path": "plant_config.inductance_h",
                        "distribution": "normal",
                        "mean": 0.0,
                        "sigma": 0.0,
                    }
                ]
            ),
            "invalid_distribution",
        ),
        (
            _mc_plan(
                parameters=[
                    {
                        "path": "plant_config.inductance_h",
                        "distribution": "triangular",
                        "low": 1.0,
                        "high": 2.0,
                    }
                ]
            ),
            "invalid_distribution",
        ),
        (_mc_plan(seed=-1), "invalid_seed"),
        (_mc_plan(seed=2**32), "invalid_seed"),
        (_mc_plan(seed=True), "invalid_seed"),
        (_mc_plan(samples=MAX_STUDY_POINTS + 1), "point_budget_exceeded"),
        (_mc_plan(samples=0), "invalid_plan"),
        (_mc_plan(samples=True), "invalid_plan"),
        (_mc_plan(parameters=[]), "invalid_plan"),
    ],
)
def test_invalid_monte_carlo_plans_are_rejected(plan, reason):
    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("monte_carlo", plan))

    assert info.value.reason == reason


@pytest.mark.parametrize(
    "path",
    [
        "run_id",
        "output.directory",
        "controller_config.missing",
        "timebase.unit",
    ],
)
def test_monte_carlo_rejects_invalid_paths(path):
    plan = _mc_plan(parameters=[_uniform_parameter(path=path)], samples=2)

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("monte_carlo", plan))

    assert info.value.reason == "invalid_path"


def test_monte_carlo_rejects_unknown_distribution_keys():
    plan = _mc_plan(
        parameters=[_uniform_parameter(mean=1.0)],
        samples=2,
    )

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("monte_carlo", plan))

    assert info.value.reason == "unknown_key"


# ---------------------------------------------------------------------------
# periodicity
# ---------------------------------------------------------------------------


def test_periodicity_expands_duration_geometrically():
    base = _base_document()
    base_duration = base["timebase"]["duration_s"]
    parsed = parse_study_document(
        _study(
            "periodicity",
            _periodicity_plan(),
            base=base,
            study_id="periodic",
        )
    )

    assert len(parsed.points) == 4
    for index, point in enumerate(parsed.points):
        expected = base_duration * 4.0**index
        assert point.parameters == {"timebase.duration_s": expected}
        assert point.document["timebase"]["duration_s"] == expected
        validate_experiment_document(point.document)
        assert point.document["run_id"] == f"periodic-p{index:02d}"
        assert point.document["output"]["directory"] == str(
            Path("study-out") / "runs"
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"compare_period": 0},
        {"compare_period": 65},
        {"compare_period": 1.5},
        {"compare_windows": 1},
        {"compare_windows": 33},
        {"tolerance_relative": 0.0},
        {"tolerance_relative": -1.0},
        {"signals": []},
        {"signals": ["vout", "vout"]},
        {"signals": ["vout", ""]},
        {"max_runs": 1},
        {"max_runs": 9},
        {"extend_factor": 1.0},
        {"extend_factor": 0.5},
    ],
)
def test_invalid_periodicity_plans_are_rejected(overrides):
    with pytest.raises(StudyPlanError) as info:
        parse_study_document(
            _study("periodicity", _periodicity_plan(**overrides))
        )

    assert info.value.reason == "invalid_plan"


def test_periodicity_unknown_plan_key_is_rejected():
    with pytest.raises(StudyPlanError) as info:
        parse_study_document(
            _study("periodicity", _periodicity_plan(unknown=1))
        )

    assert info.value.reason == "unknown_key"


def test_evaluate_periodicity_converges_on_strictly_periodic_samples():
    samples = _periodic_rows(
        {"vout": [0.0, 1.0], "inductor_current": [2.0, 4.0]},
        period=2,
        windows=3,
    )

    result = evaluate_periodicity(
        samples,
        signals=["vout", "inductor_current"],
        compare_period=2,
        compare_windows=3,
        tolerance_relative=1e-3,
    )

    assert result["converged"] is True
    assert result["worst_relative_difference"] == 0.0
    assert result["tolerance_relative"] == 1e-3
    assert result["signal_differences"] == {
        "vout": 0.0,
        "inductor_current": 0.0,
    }
    assert result["compared_pairs"] == 5
    assert result["windows_examined"] == 3
    assert result["method"] == "trailing_window_pairwise_relative_difference"


def test_evaluate_periodicity_detects_a_perturbation():
    samples = _periodic_rows(
        {"vout": [0.0, 1.0], "inductor_current": [2.0, 4.0]},
        period=2,
        windows=3,
    )
    samples[-1]["vout"] = 0.5

    result = evaluate_periodicity(
        samples,
        signals=["vout", "inductor_current"],
        compare_period=2,
        compare_windows=3,
        tolerance_relative=1e-3,
    )

    assert result["converged"] is False
    assert result["signal_differences"]["vout"] == pytest.approx(1.0)
    assert result["signal_differences"]["inductor_current"] == 0.0
    assert result["worst_relative_difference"] == pytest.approx(1.0)


def test_evaluate_periodicity_requires_enough_samples():
    samples = _periodic_rows(
        {"vout": [0.0, 1.0], "inductor_current": [2.0, 4.0]},
        period=2,
        windows=3,
    )[:-1]

    with pytest.raises(StudyPlanError) as info:
        evaluate_periodicity(
            samples,
            signals=["vout", "inductor_current"],
            compare_period=2,
            compare_windows=3,
            tolerance_relative=1e-3,
        )

    assert info.value.reason == "insufficient_samples"


@pytest.mark.parametrize("mutation", ["missing", "nan"])
def test_evaluate_periodicity_rejects_missing_or_non_finite_signals(mutation):
    samples = _periodic_rows(
        {"vout": [0.0, 1.0], "inductor_current": [2.0, 4.0]},
        period=2,
        windows=3,
    )
    if mutation == "missing":
        del samples[2]["vout"]
    else:
        samples[2]["vout"] = float("nan")

    with pytest.raises(StudyPlanError) as info:
        evaluate_periodicity(
            samples,
            signals=["vout", "inductor_current"],
            compare_period=2,
            compare_windows=3,
            tolerance_relative=1e-3,
        )

    assert info.value.reason == "missing_sample_key"


# ---------------------------------------------------------------------------
# Descriptive statistics
# ---------------------------------------------------------------------------


def test_statistics_for_point_computes_each_reducer():
    samples = [
        {"vout": 1.0, "inductor_current": 2.0},
        {"vout": 3.0, "inductor_current": 4.0},
        {"vout": 5.0, "inductor_current": 6.0},
    ]
    statistics = [
        {"name": "vmean", "sample_key": "vout", "reducer": "mean"},
        {"name": "vmin", "sample_key": "vout", "reducer": "min"},
        {"name": "vmax", "sample_key": "vout", "reducer": "max"},
        {"name": "vrms", "sample_key": "vout", "reducer": "rms"},
        {"name": "vpp", "sample_key": "vout", "reducer": "peak_to_peak"},
        {"name": "vlast", "sample_key": "vout", "reducer": "last"},
        {
            "name": "ratio",
            "reducer": "ratio_of_means",
            "numerator": "inductor_current",
            "denominator": "vout",
        },
    ]

    result = statistics_for_point(samples, statistics)

    assert result["vmean"] == pytest.approx(3.0)
    assert result["vmin"] == 1.0
    assert result["vmax"] == 5.0
    assert result["vrms"] == pytest.approx(math.sqrt(35.0 / 3.0))
    assert result["vpp"] == 4.0
    assert result["vlast"] == 5.0
    assert result["ratio"] == pytest.approx(4.0 / 3.0)


def test_statistics_for_point_rejects_zero_denominator_mean():
    samples = [
        {"vout": 1.0, "inductor_current": 2.0},
        {"vout": -1.0, "inductor_current": 4.0},
    ]

    with pytest.raises(StudyPlanError) as info:
        statistics_for_point(
            samples,
            [
                {
                    "name": "ratio",
                    "reducer": "ratio_of_means",
                    "numerator": "inductor_current",
                    "denominator": "vout",
                }
            ],
        )

    assert info.value.reason == "invalid_statistic"


@pytest.mark.parametrize(
    ("samples", "statistic", "reason"),
    [
        (
            [{"vout": 1.0}, {"inductor_current": 2.0}],
            {"name": "m", "sample_key": "vout", "reducer": "mean"},
            "missing_sample_key",
        ),
        (
            [{"vout": float("inf")}],
            {"name": "m", "sample_key": "vout", "reducer": "mean"},
            "missing_sample_key",
        ),
        (
            [{"vout": True}],
            {"name": "m", "sample_key": "vout", "reducer": "mean"},
            "missing_sample_key",
        ),
        (
            [{"vout": 1.0}],
            {"name": "m", "sample_key": "vout", "reducer": "median"},
            "invalid_statistic",
        ),
    ],
)
def test_statistics_for_point_error_branches(samples, statistic, reason):
    with pytest.raises(StudyPlanError) as info:
        statistics_for_point(samples, [statistic])

    assert info.value.reason == reason


def test_statistics_for_point_rejects_empty_samples():
    with pytest.raises(StudyPlanError) as info:
        statistics_for_point(
            [],
            [{"name": "m", "sample_key": "vout", "reducer": "mean"}],
        )

    assert info.value.reason == "invalid_statistic"


# ---------------------------------------------------------------------------
# Aggregation honesty rules
# ---------------------------------------------------------------------------


def test_aggregate_all_limited_never_becomes_comparable():
    records = [
        _record(0, _manifest(evidence_level="limited")),
        _record(1, _manifest(evidence_level="limited")),
    ]

    result = aggregate_study(records)

    assert result["schema"] == STUDY_AGGREGATE_SCHEMA
    assert result["point_count"] == 2
    assert result["evidence_level"] == "per-point"
    assert result["evidence_level"] != "comparable"
    assert result["inherited_ceiling"] == "limited"
    assert result["point_evidence_levels"] == {"limited": 2}
    assert result["cross_point_comparability"] == "not_claimed"
    assert result["comparability_reasons"] == ["points_not_all_comparable"]
    assert set(result["comparability_reasons"]) <= STUDY_COMPARABILITY_REASONS
    assert result["added_by_aggregation"] == [
        "aggregation_over_points",
        "sampling_over_points",
    ]
    assert result["limitations"] == ["aggregate_does_not_inherit_comparable"]


def test_aggregate_mixed_levels_use_the_weakest_ceiling():
    records = [
        _record(0, _manifest(evidence_level="comparable")),
        _record(1, _manifest(evidence_level="limited")),
    ]

    result = aggregate_study(records)

    assert result["inherited_ceiling"] == "limited"
    assert result["point_evidence_levels"] == {"limited": 1, "comparable": 1}
    assert result["cross_point_comparability"] == "not_claimed"
    assert result["comparability_reasons"] == ["points_not_all_comparable"]


def test_aggregate_all_comparable_with_different_routes_is_not_claimed():
    route_a = dict(_DEFAULT_ROUTE)
    route_b = dict(_DEFAULT_ROUTE, event_granularity="continuous")
    records = [
        _record(0, _manifest(route=route_a)),
        _record(1, _manifest(route=route_b)),
    ]

    result = aggregate_study(records)

    assert result["inherited_ceiling"] == "comparable"
    assert result["cross_point_comparability"] == "not_claimed"
    assert result["comparability_reasons"] == ["route_identity_differs"]


def test_aggregate_all_comparable_with_identical_routes_is_claimed():
    records = [
        _record(0, _manifest(route=dict(_DEFAULT_ROUTE))),
        _record(1, _manifest(route=dict(_DEFAULT_ROUTE))),
    ]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "claimed"
    assert result["comparability_reasons"] == []
    assert result["evidence_level"] == "per-point"
    assert result["evidence_level"] != "comparable"


def test_aggregate_single_point_is_not_comparable():
    result = aggregate_study([_record(0, _manifest())])

    assert result["cross_point_comparability"] == "not_claimed"
    assert result["comparability_reasons"] == ["single_point_study"]


def test_aggregate_undeclared_evidence_level_fails_closed():
    records = [
        _record(0, _manifest(evidence_level=None)),
        _record(1, _manifest(evidence_level="comparable")),
    ]

    result = aggregate_study(records)

    assert result["point_evidence_levels"] == {"comparable": 1, "undeclared": 1}
    assert result["inherited_ceiling"] == "diagnostic_only"
    assert "point_evidence_level_not_declared" in result["comparability_reasons"]
    assert "points_not_all_comparable" in result["comparability_reasons"]
    assert result["cross_point_comparability"] == "not_claimed"


def test_aggregate_invalid_evidence_level_is_undeclared():
    records = [
        _record(0, _manifest(evidence_level="bogus")),
        _record(1, _manifest(evidence_level="comparable")),
    ]

    result = aggregate_study(records)

    assert result["point_evidence_levels"] == {"comparable": 1, "undeclared": 1}
    assert result["inherited_ceiling"] == "diagnostic_only"
    assert "point_evidence_level_not_declared" in result["comparability_reasons"]


@pytest.mark.parametrize(
    "changed",
    [{"run_mode": "whole-run-model"}, {"plant_id": "boost"}],
)
def test_aggregate_identity_differences_are_not_comparable(changed):
    records = [
        _record(0, _manifest()),
        _record(1, _manifest(**changed)),
    ]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "not_claimed"
    assert result["comparability_reasons"] == ["route_identity_differs"]


def test_aggregate_non_qualified_point_is_not_comparable():
    records = [
        _record(0, _manifest()),
        _record(1, _manifest(status="INCOMPLETE")),
    ]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "not_claimed"
    assert "point_not_qualified" in result["comparability_reasons"]


def test_aggregate_missing_route_is_not_declared():
    manifest = _manifest()
    manifest.pop("route")
    records = [_record(0, manifest), _record(1, _manifest())]

    result = aggregate_study(records)

    assert "point_route_not_declared" in result["comparability_reasons"]
    assert "route_identity_differs" in result["comparability_reasons"]
    assert result["cross_point_comparability"] == "not_claimed"


def test_aggregate_incomplete_route_is_not_declared():
    incomplete = dict(_DEFAULT_ROUTE)
    incomplete.pop("event_granularity")
    records = [
        _record(0, _manifest(route=incomplete)),
        _record(1, _manifest(route=dict(_DEFAULT_ROUTE))),
    ]

    result = aggregate_study(records)

    assert "point_route_not_declared" in result["comparability_reasons"]
    assert "route_identity_differs" in result["comparability_reasons"]
    assert result["cross_point_comparability"] == "not_claimed"


@pytest.mark.parametrize("missing", ["run_mode", "plant"])
def test_aggregate_missing_identity_fields_are_not_comparable(missing):
    manifest = _manifest()
    manifest.pop(missing)
    records = [_record(0, manifest), _record(1, _manifest())]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "not_claimed"
    assert "route_identity_differs" in result["comparability_reasons"]


def test_aggregate_empty_study_has_a_diagnostic_ceiling():
    result = aggregate_study([])

    assert result["point_count"] == 0
    assert result["inherited_ceiling"] == "diagnostic_only"
    assert result["point_evidence_levels"] == {}
    assert result["cross_point_comparability"] == "not_claimed"
    # F1: zero points is ``empty_study``; one point is ``single_point_study``.
    assert result["comparability_reasons"] == ["empty_study"]


def test_aggregate_extra_added_is_whitelisted_and_sorted():
    records = [
        _record(0, _manifest(), statistics={"vout_mean": 1.0}),
        _record(1, _manifest()),
    ]

    result = aggregate_study(
        records,
        extra_added=("grid_resolution", "duration_extension"),
    )

    assert result["added_by_aggregation"] == [
        "aggregation_over_points",
        "descriptive_statistics",
        "duration_extension",
        "grid_resolution",
        "sampling_over_points",
    ]

    with pytest.raises(ValueError):
        aggregate_study(
            records,
            extra_added=("aggregate_does_not_inherit_comparable",),
        )
    with pytest.raises(ValueError):
        aggregate_study(records, extra_added=("unknown_addition",))


# ---------------------------------------------------------------------------
# Study report
# ---------------------------------------------------------------------------


def test_build_and_write_study_report_round_trips(tmp_path):
    base = _base_document()
    parsed = parse_study_document(
        _study(
            "parameter_sweep",
            _sweep_plan(
                statistics=[
                    {
                        "name": "vout_mean",
                        "sample_key": "vout",
                        "reducer": "mean",
                    }
                ]
            ),
            base=base,
            study_id="report-study",
        )
    )
    manifests = [
        _manifest(route=dict(_DEFAULT_ROUTE)),
        _manifest(route=dict(_DEFAULT_ROUTE)),
    ]
    manifests[0]["manifest_sha256"] = "1" * 64
    manifests[1]["manifest_sha256"] = "2" * 64
    records = [
        _record(
            0,
            manifests[0],
            statistics={"vout_mean": 1.0},
            details={"note": "a"},
        ),
        _record(1, manifests[1], statistics={"vout_mean": 2.0}),
    ]

    report = build_study_report(
        parsed,
        records,
        kind_result={"grid": [0.1, 0.2]},
        created_utc="2026-01-02T03:04:05Z",
    )

    assert report["schema"] == STUDY_REPORT_SCHEMA
    assert report["study_id"] == "report-study"
    assert report["kind"] == "parameter_sweep"
    assert report["plan"] == parsed.plan
    assert report["created_utc"] == "2026-01-02T03:04:05Z"
    assert report["kind_result"] == {"grid": [0.1, 0.2]}
    assert report["aggregate"]["evidence_level"] == "per-point"
    assert report["limitations"] == sorted(parsed.limitations)

    expected_base_sha = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert report["base"]["experiment_sha256"] == expected_base_sha

    assert len(report["points"]) == 2
    assert report["points"][0]["index"] == 0
    assert report["points"][0]["label"] == "p00"
    assert report["points"][0]["run_id"] == "study-p00"
    assert report["points"][0]["manifest_sha256"] == "1" * 64
    assert report["points"][0]["status"] == "QUALIFIED"
    assert report["points"][0]["evidence_level"] == "comparable"
    assert report["points"][0]["statistics"] == {"vout_mean": 1.0}
    assert report["points"][0]["details"] == {"note": "a"}
    assert report["points"][1]["details"] is None

    text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False)
    assert json.loads(text) == report

    path = write_study_report(report, tmp_path / "reports")

    assert path == tmp_path / "reports" / "study-report.json"
    assert path.is_file()
    assert not (tmp_path / "reports" / "study-report.json.tmp").exists()
    assert path.read_bytes() == (text + "\n").encode("utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_report_records_missing_manifest_values_as_none():
    parsed = parse_study_document(
        _study("parameter_sweep", _sweep_plan(), study_id="missing-manifest")
    )
    record = StudyPointRecord(
        index=0,
        run_id="missing-manifest-p00",
        run_dir="study-out/runs/00",
        parameters={},
        manifest={},
        statistics=None,
        details=None,
    )

    report = build_study_report(
        parsed,
        [record],
        created_utc="2026-01-02T03:04:05Z",
    )

    point = report["points"][0]
    assert point["manifest_sha256"] is None
    assert point["status"] is None
    assert point["evidence_level"] is None
    assert point["statistics"] is None
    assert point["details"] is None
    assert report["aggregate"]["inherited_ceiling"] == "diagnostic_only"


def test_build_report_rejects_non_finite_values():
    parsed = parse_study_document(
        _study("parameter_sweep", _sweep_plan(), study_id="bad-report")
    )
    record = _record(0, _manifest(), parameters={"x": float("nan")})

    with pytest.raises(StudyPlanError) as info:
        build_study_report(
            parsed,
            [record],
            created_utc="2026-01-02T03:04:05Z",
        )

    assert info.value.reason == "invalid_report"


def test_write_report_rejects_non_finite_values(tmp_path):
    with pytest.raises(StudyPlanError) as info:
        write_study_report({"value": float("inf")}, tmp_path)

    assert info.value.reason == "invalid_report"
    assert not (tmp_path / "study-report.json").exists()


def test_study_plan_to_dict_round_trips_and_copies():
    parsed = parse_study_document(
        _study("parameter_sweep", _sweep_plan(), study_id="roundtrip")
    )

    value = parsed.to_dict()

    assert value["schema"] == STUDY_SCHEMA
    assert StudyPlan.from_dict(value) == parsed

    value["base_experiment"]["controller_config"]["kp"] = 999.0
    assert parsed.base_experiment["controller_config"]["kp"] == 0.2


# ---------------------------------------------------------------------------
# Adversarial regressions: duplicate paths, overflowing draws, point identity
# ---------------------------------------------------------------------------


def test_duplicate_sweep_parameter_path_is_rejected():
    plan = {
        "parameters": [
            {"path": "controller_config.kp", "values": [0.1, 0.2]},
            {"path": "controller_config.kp", "values": [0.2, 0.4]},
        ]
    }

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("parameter_sweep", plan))

    assert info.value.reason == "duplicate_parameter_path"
    assert "controller_config.kp" in str(info.value)
    assert "once" in str(info.value)


def test_duplicate_monte_carlo_parameter_path_is_rejected():
    plan = _mc_plan(
        parameters=[
            _uniform_parameter(),
            _uniform_parameter(low=1e-4, high=2e-4),
        ],
        samples=2,
    )

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("monte_carlo", plan))

    assert info.value.reason == "duplicate_parameter_path"
    assert "plant_config.inductance_h" in str(info.value)
    assert "once" in str(info.value)


_OVERFLOW_MC_PARAMETERS = [
    {
        "path": "plant_config.inductance_h",
        "distribution": "normal",
        "mean": 0.0,
        "sigma": 1e308,
    }
]


def test_monte_carlo_non_finite_draw_is_rejected_before_planning():
    # Seed 12345 deterministically produces one overflowing draw for
    # sigma=1e308, so this regression is not probabilistic.
    with pytest.raises(StudyPlanError) as info:
        monte_carlo_draws(_OVERFLOW_MC_PARAMETERS, 8, 12345)

    assert info.value.reason == "invalid_draw"
    assert "plant_config.inductance_h" in str(info.value)
    assert "sigma" in str(info.value)

    plan = _mc_plan(
        parameters=_OVERFLOW_MC_PARAMETERS,
        samples=8,
        seed=12345,
    )

    with pytest.raises(StudyPlanError) as info:
        parse_study_document(_study("monte_carlo", plan))

    assert info.value.reason == "invalid_draw"


def _record_with_run_id(index: int, run_id: str) -> StudyPointRecord:
    return StudyPointRecord(
        index=index,
        run_id=run_id,
        run_dir=f"study-out/runs/{index:02d}",
        parameters={"controller_config.kp": 0.1},
        manifest=_manifest(),
        statistics=None,
        details=None,
    )


def test_aggregate_duplicate_run_ids_are_not_comparable():
    records = [
        _record_with_run_id(0, "study-p00"),
        _record_with_run_id(1, "study-p00"),
    ]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "not_claimed"
    assert "duplicate_points" in result["comparability_reasons"]


def test_aggregate_duplicate_indices_are_not_comparable():
    records = [
        _record_with_run_id(0, "study-p00"),
        _record_with_run_id(0, "study-p01"),
    ]

    result = aggregate_study(records)

    assert result["cross_point_comparability"] == "not_claimed"
    assert "duplicate_points" in result["comparability_reasons"]


def test_aggregate_empty_study_uses_the_empty_study_reason():
    result = aggregate_study([])

    assert result["point_count"] == 0
    assert result["comparability_reasons"] == ["empty_study"]
    assert "empty_study" in STUDY_COMPARABILITY_REASONS


def test_aggregate_single_point_uses_the_single_point_reason():
    result = aggregate_study([_record(0, _manifest())])

    assert result["comparability_reasons"] == ["single_point_study"]


@pytest.mark.parametrize("identity_value", ["", 7])
@pytest.mark.parametrize(
    "field",
    [
        "run_mode",
        "plant_id",
        "route_time_model",
        "route_control_location",
        "route_event_granularity",
    ],
)
def test_aggregate_incomplete_point_identity_is_not_comparable(
    field, identity_value
):
    manifest = _manifest()
    if field == "run_mode":
        manifest["run_mode"] = identity_value
    elif field == "plant_id":
        manifest["plant"]["id"] = identity_value
    else:
        manifest["route"][field.removeprefix("route_")] = identity_value

    result = aggregate_study([_record(0, _manifest()), _record(1, manifest)])

    assert result["cross_point_comparability"] == "not_claimed"
    assert "point_identity_incomplete" in result["comparability_reasons"]


def test_evaluate_periodicity_uses_index_lag_and_reports_sampling_interval():
    period = 3
    windows = 4
    samples = _periodic_rows(
        {"vout": [1.0, 2.0, 3.0]},
        period=period,
        windows=windows,
    )

    result = evaluate_periodicity(
        samples,
        signals=["vout"],
        compare_period=period,
        compare_windows=windows,
        tolerance_relative=1e-6,
    )

    assert result["converged"] is True
    assert result["compared_pairs"] == 10
    assert result["compared_pairs"] == period * (windows - 1) + 1
    assert result["sampling_interval_s"] == pytest.approx(1e-3)

    # Every row is validated, not only the trailing window: a non-finite row
    # before the tail still fails closed.
    prefix = [{"time_s": -1e-3, "vout": float("nan")}]
    with pytest.raises(StudyPlanError) as info:
        evaluate_periodicity(
            prefix + samples,
            signals=["vout"],
            compare_period=period,
            compare_windows=windows,
            tolerance_relative=1e-6,
        )
    assert info.value.reason == "missing_sample_key"

    # A non-increasing time base keeps the comparison but cannot report a
    # single sampling interval.
    irregular = [dict(row) for row in samples]
    irregular[4]["time_s"] = irregular[3]["time_s"]
    irregular_result = evaluate_periodicity(
        irregular,
        signals=["vout"],
        compare_period=period,
        compare_windows=windows,
        tolerance_relative=1e-6,
    )
    assert irregular_result["sampling_interval_s"] is None
