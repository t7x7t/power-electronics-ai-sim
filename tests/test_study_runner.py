"""Acceptance tests for the S7 Study execution layer (pure orchestration).

The runner writes one config document per planned point, delegates every point
to an injected executor, reads the published manifest and samples, and then
publishes one ``study-report.json``.  These tests use fake executors only: no
Plant, solver, or real run package participates.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from pe_sim.study import (
    STUDY_SCHEMA,
    StudyPlan,
    StudyPoint,
    monte_carlo_draws,
    monte_carlo_draws_sha256,
    parse_study_document,
)
from pe_sim.study_runner import run_study


_ROUTE = {
    "time_model": "stepped",
    "control_location": "kernel",
    "event_granularity": "window",
}


def _base_experiment(duration_s: float = 1e-4) -> dict:
    text = (
        resources.files("pe_sim.templates")
        .joinpath("buck_pi_step.json")
        .read_text(encoding="utf-8")
    )
    document = json.loads(text)
    document["timebase"]["duration_s"] = duration_s
    return document


def _plan(
    root: Path,
    *,
    kind: str = "parameter_sweep",
    plan: dict | None = None,
    base: dict | None = None,
    study_id: str = "runner-study",
):
    if plan is None:
        plan = {"parameters": [{"path": "controller_config.kp", "values": [0.2, 0.4]}]}
    document = {
        "schema": STUDY_SCHEMA,
        "study_id": study_id,
        "kind": kind,
        "base_experiment": _base_experiment() if base is None else base,
        "plan": plan,
        "output": {"directory": str(root / "study")},
    }
    return parse_study_document(document)


def _manifest(
    *,
    evidence_level: str | None = "limited",
    status: str = "QUALIFIED",
    run_mode: str = "stepped-kernel",
    plant_id: str = "buck",
    route: dict | None = None,
) -> dict:
    manifest = {
        "status": status,
        "run_mode": run_mode,
        "plant": {"id": plant_id},
        "route": dict(_ROUTE if route is None else route),
        "manifest_sha256": "a" * 64,
    }
    if evidence_level is not None:
        manifest["evidence_level"] = evidence_level
    return manifest


def _samples(vout_values: list[float] | None = None) -> list[dict]:
    values = vout_values if vout_values is not None else [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    return [
        {
            "step_index": index,
            "time_s": index * 1e-5,
            "vout": value,
            "inductor_current": 0.5,
            "action": 0.4,
        }
        for index, value in enumerate(values)
    ]


class _FakeExecutor:
    """Write a minimal run package per point and record every call."""

    def __init__(
        self,
        runs_root: Path,
        *,
        manifests: dict[int, dict] | None = None,
        samples: dict[int, list[dict]] | None = None,
        exit_codes: dict[int, int] | None = None,
        run_dir_missing: set[int] | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.manifests = manifests or {}
        self.samples = samples or {}
        self.exit_codes = exit_codes or {}
        self.run_dir_missing = run_dir_missing or set()
        self.calls: list[int] = []
        self.config_paths: list[Path] = []

    def __call__(self, point, config_path: Path) -> tuple[int, dict]:
        config_path = Path(config_path)
        # The contract is that the point document is on disk before the
        # executor is asked to run it.
        assert config_path.is_file()
        self.calls.append(point.index)
        self.config_paths.append(config_path)
        exit_code = self.exit_codes.get(point.index, 0)
        payload: dict = {"run_id": point.run_id}
        if point.index in self.run_dir_missing:
            return exit_code, payload
        run_dir = self.runs_root / point.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(
            json.dumps(self.manifests.get(point.index, _manifest())),
            encoding="utf-8",
        )
        (run_dir / "samples.json").write_text(
            json.dumps(self.samples.get(point.index, _samples())),
            encoding="utf-8",
        )
        payload["run_dir"] = str(run_dir)
        return exit_code, payload


def _run(plan, executor: _FakeExecutor):
    return run_study(plan, execute_point=executor)


# ---------------------------------------------------------------------------
# Point inputs
# ---------------------------------------------------------------------------


def test_point_configs_are_written_before_execution(tmp_path):
    plan = _plan(tmp_path)
    executor = _FakeExecutor(Path(plan.output_dir) / "runs")

    result = _run(plan, executor)

    config_path = Path(plan.output_dir) / "points" / "p00" / "config.json"
    assert config_path.is_file()
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written == plan.points[0].document
    assert written["run_id"] == "runner-study-p00"
    assert plan.points[0].run_id == "runner-study-p00"
    assert executor.calls == [0, 1]
    assert result.executed_points == 2
    assert result.skipped_points == 0
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Aggregation honesty
# ---------------------------------------------------------------------------


def test_limited_manifests_aggregate_as_per_point(tmp_path):
    plan = _plan(tmp_path)
    executor = _FakeExecutor(Path(plan.output_dir) / "runs")

    result = _run(plan, executor)

    aggregate = result.report["aggregate"]
    assert aggregate["evidence_level"] == "per-point"
    assert aggregate["inherited_ceiling"] == "limited"
    assert aggregate["cross_point_comparability"] == "not_claimed"
    assert "points_not_all_comparable" in aggregate["comparability_reasons"]
    assert "grid_resolution" in aggregate["added_by_aggregation"]
    assert "aggregate_does_not_inherit_comparable" in result.report["limitations"]
    assert "aggregate_does_not_inherit_comparable" in aggregate["limitations"]


def test_comparable_points_claim_cross_point_comparability_only(tmp_path):
    plan = _plan(tmp_path)
    manifests = {0: _manifest(evidence_level="comparable"), 1: _manifest(evidence_level="comparable")}
    executor = _FakeExecutor(Path(plan.output_dir) / "runs", manifests=manifests)

    result = _run(plan, executor)

    aggregate = result.report["aggregate"]
    assert aggregate["cross_point_comparability"] == "claimed"
    assert aggregate["comparability_reasons"] == []
    assert aggregate["inherited_ceiling"] == "comparable"
    # An aggregate is never promoted to comparable evidence.
    assert aggregate["evidence_level"] == "per-point"
    assert "aggregate_does_not_inherit_comparable" in aggregate["limitations"]


# ---------------------------------------------------------------------------
# Failures still produce a report
# ---------------------------------------------------------------------------


def test_nonzero_point_exit_code_fails_study_but_runs_every_point(tmp_path):
    plan = _plan(
        tmp_path,
        plan={"parameters": [{"path": "controller_config.kp", "values": [0.2, 0.04, 0.06]}]},
    )
    executor = _FakeExecutor(
        Path(plan.output_dir) / "runs",
        exit_codes={1: 1},
    )

    result = _run(plan, executor)

    assert executor.calls == [0, 1, 2]
    assert result.exit_code == 1
    assert result.report["points"][1]["status"] == "QUALIFIED"
    assert result.report_path.is_file()


def test_missing_run_dir_is_recorded_and_other_points_continue(tmp_path):
    plan = _plan(
        tmp_path,
        plan={"parameters": [{"path": "controller_config.kp", "values": [0.2, 0.04, 0.06]}]},
    )
    executor = _FakeExecutor(
        Path(plan.output_dir) / "runs",
        run_dir_missing={0},
    )

    result = _run(plan, executor)

    assert executor.calls == [0, 1, 2]
    assert result.exit_code == 1
    first = result.report["points"][0]
    assert first["details"]["status"] == "run_dir_missing"
    assert first["manifest_sha256"] is None
    assert first["statistics"] is None
    assert result.report["points"][1]["status"] == "QUALIFIED"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_statistics_are_computed_from_point_samples(tmp_path):
    statistics = [
        {"name": "vout_mean", "sample_key": "vout", "reducer": "mean"},
        {"name": "vout_last", "sample_key": "vout", "reducer": "last"},
    ]
    plan = _plan(
        tmp_path,
        plan={
            "parameters": [{"path": "controller_config.kp", "values": [0.2, 0.4]}],
            "statistics": statistics,
        },
    )
    samples = {
        0: _samples([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
        1: _samples([2.0, 2.0, 2.0, 2.0, 2.0, 2.0]),
    }
    executor = _FakeExecutor(Path(plan.output_dir) / "runs", samples=samples)

    result = _run(plan, executor)

    assert result.report["points"][0]["statistics"]["vout_mean"] == pytest.approx(3.5)
    assert result.report["points"][0]["statistics"]["vout_last"] == pytest.approx(6.0)
    assert result.report["points"][1]["statistics"]["vout_mean"] == pytest.approx(2.0)
    assert "descriptive_statistics" in result.report["aggregate"]["added_by_aggregation"]
    assert result.report["kind_result"]["statistic_names"] == ["vout_mean", "vout_last"]


# ---------------------------------------------------------------------------
# Periodicity
# ---------------------------------------------------------------------------


def _periodicity_plan(root: Path, *, max_runs: int = 4):
    return _plan(
        root,
        kind="periodicity",
        plan={
            "compare_period": 1,
            "compare_windows": 4,
            "tolerance_relative": 1e-4,
            "signals": ["vout"],
            "max_runs": max_runs,
            "extend_factor": 2.0,
        },
    )


def test_periodicity_stops_at_the_first_converged_point(tmp_path):
    plan = _periodicity_plan(tmp_path, max_runs=4)
    samples = {
        0: _samples([0.0, 1.0, 2.0, 3.0, 4.0, 5.0]),
        1: _samples([5.0, 5.0, 5.0, 5.0, 5.0, 5.0]),
        2: _samples([5.0, 5.0, 5.0, 5.0, 5.0, 5.0]),
        3: _samples([5.0, 5.0, 5.0, 5.0, 5.0, 5.0]),
    }
    executor = _FakeExecutor(Path(plan.output_dir) / "runs", samples=samples)

    result = _run(plan, executor)

    assert executor.calls == [0, 1]
    assert result.executed_points == 2
    assert result.skipped_points == 2
    assert result.exit_code == 0
    kind_result = result.report["kind_result"]
    assert kind_result["converged"] is True
    assert kind_result["runs_executed"] == 2
    assert kind_result["runs_planned"] == 4
    assert kind_result["skipped_indices"] == [2, 3]
    assert kind_result["comparison"]["converged"] is True
    assert kind_result["tolerance_relative"] == pytest.approx(1e-4)
    assert result.report["points"][0]["details"]["periodicity"]["converged"] is False
    assert result.report["points"][1]["details"]["periodicity"]["converged"] is True
    assert result.report["points"][0]["details"]["duration_s"] == pytest.approx(1e-4)
    assert result.report["points"][1]["details"]["duration_s"] == pytest.approx(2e-4)


def test_periodicity_never_converging_still_writes_a_report(tmp_path):
    plan = _periodicity_plan(tmp_path, max_runs=3)
    samples = {
        index: _samples([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        for index in range(3)
    }
    executor = _FakeExecutor(Path(plan.output_dir) / "runs", samples=samples)

    result = _run(plan, executor)

    assert executor.calls == [0, 1, 2]
    assert result.exit_code == 1
    assert result.skipped_points == 0
    kind_result = result.report["kind_result"]
    assert kind_result["converged"] is False
    assert kind_result["runs_executed"] == 3
    assert kind_result["skipped_indices"] == []
    assert result.report_path.is_file()


def test_periodicity_insufficient_samples_records_an_error(tmp_path):
    plan = _periodicity_plan(tmp_path, max_runs=2)
    samples = {
        0: _samples([5.0, 5.0])[:2],
        1: _samples([5.0, 5.0])[:2],
    }
    executor = _FakeExecutor(Path(plan.output_dir) / "runs", samples=samples)

    result = _run(plan, executor)

    assert result.exit_code == 1
    assert result.report["kind_result"]["converged"] is False
    assert result.report["kind_result"]["comparison"] is None
    details = result.report["points"][0]["details"]["periodicity"]
    assert "error" in details
    assert "insufficient_samples" in details["error"]


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def _monte_carlo_plan(root: Path):
    return _plan(
        root,
        kind="monte_carlo",
        plan={
            "parameters": [
                {
                    "path": "plant_config.inductance_h",
                    "distribution": "uniform",
                    "low": 8e-5,
                    "high": 1.2e-4,
                }
            ],
            "samples": 3,
            "seed": 20261027,
        },
    )


def test_monte_carlo_draws_match_an_independent_computation(tmp_path):
    plan = _monte_carlo_plan(tmp_path / "a")
    executor = _FakeExecutor(Path(plan.output_dir) / "runs")

    first = _run(plan, executor)

    expected_draws = monte_carlo_draws(
        plan.plan["parameters"], plan.plan["samples"], plan.plan["seed"]
    )
    kind_result = first.report["kind_result"]
    assert kind_result["draws"] == [dict(draw) for draw in expected_draws]
    assert kind_result["draws_sha256"] == monte_carlo_draws_sha256(expected_draws)
    assert kind_result["seed"] == 20261027
    assert kind_result["samples_requested"] == 3
    assert "distribution_sampling" in first.report["aggregate"]["added_by_aggregation"]

    second_plan = _monte_carlo_plan(tmp_path / "b")
    second_executor = _FakeExecutor(Path(second_plan.output_dir) / "runs")
    second = _run(second_plan, second_executor)

    assert second.report["kind_result"]["draws_sha256"] == kind_result["draws_sha256"]
    assert [
        point["parameters"] for point in second.report["points"]
    ] == [point["parameters"] for point in first.report["points"]]


# ---------------------------------------------------------------------------
# Report serialization
# ---------------------------------------------------------------------------


def test_report_round_trips_through_json_without_nan(tmp_path):
    plan = _plan(tmp_path)
    executor = _FakeExecutor(Path(plan.output_dir) / "runs")

    result = _run(plan, executor)

    text = result.report_path.read_text(encoding="utf-8")
    decoded = json.loads(text)
    assert decoded == result.report
    json.dumps(result.report, sort_keys=True, allow_nan=False)
    assert result.report_path == Path(plan.output_dir) / "study-report.json"
    assert result.report["schema"] == "pe-sim.study-report.v1"
    assert result.report["study_id"] == "runner-study"


# ---------------------------------------------------------------------------
# Adversarial regressions: unreadable packages, executor failures, config IO
# ---------------------------------------------------------------------------


def _write_run_package(
    run_dir: Path,
    *,
    manifest: dict | None = None,
    manifest_text: str | None = None,
    samples: list[dict] | None = None,
    samples_text: str | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if manifest_text is not None:
        (run_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    elif manifest is not None:
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    if samples_text is not None:
        (run_dir / "samples.json").write_text(samples_text, encoding="utf-8")
    elif samples is not None:
        (run_dir / "samples.json").write_text(
            json.dumps(samples), encoding="utf-8"
        )


@pytest.mark.parametrize("manifest_text", ["{not json", "[]"])
def test_unreadable_manifest_fails_the_study_but_keeps_the_point(
    tmp_path, manifest_text
):
    plan = _plan(tmp_path)
    runs_root = Path(plan.output_dir) / "runs"
    calls: list[int] = []

    def executor(point, config_path):
        assert Path(config_path).is_file()
        calls.append(point.index)
        run_dir = runs_root / point.run_id
        if point.index == 0:
            _write_run_package(
                run_dir, manifest_text=manifest_text, samples=_samples()
            )
        else:
            _write_run_package(
                run_dir, manifest=_manifest(), samples=_samples()
            )
        return 0, {"run_dir": str(run_dir)}

    result = run_study(plan, execute_point=executor)

    assert calls == [0, 1]
    assert result.exit_code == 1
    assert result.report_path.is_file()
    assert result.report["aggregate"]["point_count"] == 2
    first = result.report["points"][0]
    assert (
        first["details"]["manifest_error"]
        == "manifest.json is missing or not a JSON object"
    )
    assert first["status"] is None
    assert result.report["points"][1]["status"] == "QUALIFIED"


def test_manifest_status_outside_the_success_set_fails_the_study(tmp_path):
    plan = _plan(tmp_path)
    executor = _FakeExecutor(
        Path(plan.output_dir) / "runs",
        manifests={0: _manifest(status="FAILED"), 1: _manifest()},
    )

    result = _run(plan, executor)

    assert executor.calls == [0, 1]
    assert result.exit_code == 1
    assert result.report_path.is_file()
    first = result.report["points"][0]
    assert first["details"]["manifest_error"] == "manifest status is 'FAILED'"
    assert result.report["points"][1]["status"] == "QUALIFIED"


@pytest.mark.parametrize("samples_text", [None, "[1, 2]"])
def test_declared_statistics_without_readable_samples_fail_the_study(
    tmp_path, samples_text
):
    plan = _plan(
        tmp_path,
        plan={
            "parameters": [
                {"path": "controller_config.kp", "values": [0.2, 0.4]}
            ],
            "statistics": [
                {"name": "vout_mean", "sample_key": "vout", "reducer": "mean"}
            ],
        },
    )
    runs_root = Path(plan.output_dir) / "runs"
    calls: list[int] = []

    def executor(point, config_path):
        assert Path(config_path).is_file()
        calls.append(point.index)
        run_dir = runs_root / point.run_id
        _write_run_package(
            run_dir, manifest=_manifest(), samples_text=samples_text
        )
        return 0, {"run_dir": str(run_dir)}

    result = run_study(plan, execute_point=executor)

    assert calls == [0, 1]
    assert result.exit_code == 1
    assert result.report_path.is_file()
    first = result.report["points"][0]
    assert (
        first["details"]["statistics_error"]
        == "samples.json is missing or not an array of objects"
    )
    assert first["statistics"] is None


def test_executor_exception_fails_the_study_but_later_points_still_run(tmp_path):
    plan = _plan(
        tmp_path,
        plan={
            "parameters": [
                {"path": "controller_config.kp", "values": [0.2, 0.4, 0.6]}
            ]
        },
    )
    runs_root = Path(plan.output_dir) / "runs"
    calls: list[int] = []

    def executor(point, config_path):
        assert Path(config_path).is_file()
        calls.append(point.index)
        if point.index == 0:
            raise RuntimeError("executor exploded")
        run_dir = runs_root / point.run_id
        _write_run_package(run_dir, manifest=_manifest(), samples=_samples())
        return 0, {"run_dir": str(run_dir)}

    result = run_study(plan, execute_point=executor)

    assert calls == [0, 1, 2]
    assert result.exit_code == 1
    assert result.report_path.is_file()
    first = result.report["points"][0]
    assert "RuntimeError" in first["details"]["executor_error"]
    assert "executor exploded" in first["details"]["executor_error"]
    assert result.report["points"][1]["status"] == "QUALIFIED"
    assert result.report["points"][2]["status"] == "QUALIFIED"


def test_periodicity_writes_configs_only_for_executed_points(tmp_path):
    plan = _periodicity_plan(tmp_path, max_runs=4)
    constant = _samples([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    executor = _FakeExecutor(
        Path(plan.output_dir) / "runs", samples={0: constant}
    )

    result = _run(plan, executor)

    assert executor.calls == [0]
    points_dir = Path(plan.output_dir) / "points"
    assert (points_dir / "p00" / "config.json").is_file()
    assert not (points_dir / "p01" / "config.json").exists()
    assert result.report["kind_result"]["skipped_indices"] == [1, 2, 3]


def test_config_write_failure_is_recorded_and_later_points_still_run(tmp_path):
    plan = _plan(
        tmp_path,
        plan={
            "parameters": [
                {"path": "controller_config.kp", "values": [0.2, 0.4]}
            ]
        },
    )
    broken_document = dict(plan.points[0].document)
    broken_document["controller_config"] = {"kp": float("nan")}
    broken_point = StudyPoint(
        index=plan.points[0].index,
        label=plan.points[0].label,
        run_id=plan.points[0].run_id,
        parameters=plan.points[0].parameters,
        document=broken_document,
        details={},
    )
    broken_plan = StudyPlan(
        study_id=plan.study_id,
        kind=plan.kind,
        base_experiment=plan.base_experiment,
        plan=plan.plan,
        output_dir=plan.output_dir,
        points=(broken_point, plan.points[1]),
        limitations=plan.limitations,
    )
    executor = _FakeExecutor(Path(plan.output_dir) / "runs")

    result = run_study(broken_plan, execute_point=executor)

    assert executor.calls == [1]
    assert result.exit_code == 1
    assert result.report_path.is_file()
    first = result.report["points"][0]
    assert "ValueError" in first["details"]["config_error"]
    assert result.report["points"][1]["status"] == "QUALIFIED"
    assert not (
        Path(plan.output_dir) / "points" / "p00" / "config.json"
    ).exists()
