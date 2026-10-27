"""End-to-end tests for ``pe-sim study`` (real averaged Buck runs, no ngspice).

Every test calls :func:`pe_sim.cli.main` in-process and writes into
``tmp_path``.  The base experiment is a copy of the packaged Buck reference
template with a shortened duration; no external simulator is discovered or
required.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path

import pytest

from pe_sim.cli import main
from pe_sim.study import STUDY_SCHEMA, evaluate_periodicity


def _template_document(duration_s: float = 2e-4) -> dict:
    text = (
        resources.files("pe_sim.templates")
        .joinpath("buck_pi_step.json")
        .read_text(encoding="utf-8")
    )
    document = json.loads(text)
    document["timebase"]["duration_s"] = duration_s
    return document


def _write_study(
    path: Path,
    *,
    kind: str,
    plan: dict,
    base: dict | None = None,
    study_id: str = "cli-study",
    output: Path | None = None,
) -> Path:
    document = {
        "schema": STUDY_SCHEMA,
        "study_id": study_id,
        "kind": kind,
        "base_experiment": _template_document() if base is None else base,
        "plan": plan,
        "output": {"directory": str(output if output is not None else path.parent / "study")},
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _read_report(output: Path) -> dict:
    return json.loads((output / "study-report.json").read_text(encoding="utf-8"))


def _sweep_plan(values: list[float]) -> dict:
    return {
        "parameters": [
            {"path": "controller_config.kp", "values": values},
        ]
    }


# ---------------------------------------------------------------------------
# Document rejection
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected_with_the_closed_envelope(tmp_path, capsys):
    path = _write_study(tmp_path / "study.json", kind="parameter_sweep", plan=_sweep_plan([0.2, 0.4]))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["unexpected_key"] = True
    path.write_text(json.dumps(document), encoding="utf-8")

    code = main(["study", str(path)])

    captured = capsys.readouterr()
    assert code == 2
    rejection = json.loads(captured.out)
    assert rejection["schema"] == "pe-sim.rejection.v1"
    assert rejection["status"] == "CONFIG_REJECTED"
    assert rejection["error_code"] == "invalid_request"
    assert "unexpected_key" in rejection["error"]
    assert not (tmp_path / "study").exists()


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------


def test_kp_sweep_runs_every_point_and_writes_the_report(tmp_path, capsys):
    output = tmp_path / "study"
    path = _write_study(
        tmp_path / "study.json",
        kind="parameter_sweep",
        plan=_sweep_plan([0.2, 0.4]),
        output=output,
    )

    code = main(["study", str(path)])

    captured = capsys.readouterr()
    assert code == 0
    stdout_report = json.loads(captured.out)
    report = _read_report(output)
    assert stdout_report == report
    assert report["kind"] == "parameter_sweep"
    assert len(report["points"]) == 2
    for point in report["points"]:
        assert point["status"]
        assert point["manifest_sha256"]
    assert (output / "points" / "p00" / "config.json").is_file()
    assert (output / "points" / "p01" / "config.json").is_file()
    assert (output / "runs" / "cli-study-p00").is_dir()
    assert (output / "runs" / "cli-study-p01").is_dir()
    assert report["aggregate"]["evidence_level"] == "per-point"
    assert report["kind_result"]["statistic_names"] == []
    assert len(report["kind_result"]["series"]) == 2


def test_aggregate_evidence_is_never_promoted_to_comparable(tmp_path, capsys):
    output = tmp_path / "study"
    path = _write_study(
        tmp_path / "study.json",
        kind="parameter_sweep",
        plan=_sweep_plan([0.2, 0.4]),
        output=output,
    )

    assert main(["study", str(path)]) == 0
    capsys.readouterr()
    report = _read_report(output)

    aggregate = report["aggregate"]
    assert aggregate["evidence_level"] == "per-point"
    assert aggregate["evidence_level"] != "comparable"
    assert aggregate["inherited_ceiling"] != "comparable"
    assert aggregate["cross_point_comparability"] == "not_claimed"
    assert "aggregate_does_not_inherit_comparable" in report["limitations"]


# ---------------------------------------------------------------------------
# Monte Carlo determinism
# ---------------------------------------------------------------------------


def _monte_carlo_plan() -> dict:
    return {
        "parameters": [
            {
                "path": "plant_config.inductance_h",
                "distribution": "uniform",
                "low": 9e-5,
                "high": 1.1e-4,
            }
        ],
        "samples": 2,
        "seed": 424242,
    }


def test_monte_carlo_is_reproducible_at_the_cli(tmp_path, capsys):
    reports = []
    for name in ("mc-a", "mc-b"):
        output = tmp_path / name
        path = _write_study(
            tmp_path / f"{name}.json",
            kind="monte_carlo",
            plan=_monte_carlo_plan(),
            output=output,
            study_id="cli-study",
        )
        assert main(["study", str(path)]) == 0
        capsys.readouterr()
        reports.append(_read_report(output))

    first, second = reports
    assert first["kind_result"]["seed"] == 424242
    assert first["kind_result"]["samples_requested"] == 2
    assert first["kind_result"]["draws_sha256"] == second["kind_result"]["draws_sha256"]
    assert first["kind_result"]["draws"] == second["kind_result"]["draws"]
    assert [point["parameters"] for point in first["points"]] == [
        point["parameters"] for point in second["points"]
    ]
    assert "distribution_sampling" in first["aggregate"]["added_by_aggregation"]


# ---------------------------------------------------------------------------
# Periodicity on the averaged Buck reference
# ---------------------------------------------------------------------------


def _periodicity_base() -> dict:
    # ``kp = 0.2`` with ``vref = 5 V`` saturates the duty at zero on the
    # overshoot (a proportional loop with no integral term cannot pull the
    # output down actively), which sustains a relay-type limit cycle: measured
    # worst relative difference stayed near 2e-2 even at 1.02 s.  A small gain
    # keeps the loop inside the actuator range so the LC ring decays with the
    # RC time constant and the trailing window becomes periodic.
    document = _template_document(duration_s=2e-3)
    document["controller_config"] = {"kp": 0.02}
    document["input_schedule"] = [{"time_s": 0.0, "vref": 5.0}]
    return document


def _periodicity_plan() -> dict:
    return {
        "compare_period": 1,
        "compare_windows": 4,
        "tolerance_relative": 1e-4,
        "signals": ["vout"],
        "max_runs": 4,
        "extend_factor": 8.0,
    }


def test_periodicity_reaches_tolerance_convergence_on_averaged_buck(tmp_path, capsys):
    output = tmp_path / "study"
    path = _write_study(
        tmp_path / "study.json",
        kind="periodicity",
        plan=_periodicity_plan(),
        base=_periodicity_base(),
        output=output,
    )

    code = main(["study", str(path)])

    capsys.readouterr()
    assert code == 0
    report = _read_report(output)
    kind_result = report["kind_result"]
    assert kind_result["converged"] is True
    assert kind_result["runs_executed"] >= 2
    assert kind_result["runs_executed"] == len(report["points"])
    assert kind_result["skipped_indices"] == list(
        range(kind_result["runs_executed"], kind_result["runs_planned"])
    )
    comparison = kind_result["comparison"]
    assert comparison["converged"] is True
    assert comparison["worst_relative_difference"] <= kind_result["tolerance_relative"]
    assert all(point["status"] == "QUALIFIED" for point in report["points"])
    assert "duration_extension" in report["aggregate"]["added_by_aggregation"]


# ---------------------------------------------------------------------------
# Whole-run routes cannot be formal comparisons
# ---------------------------------------------------------------------------


def test_formal_comparison_of_a_whole_run_study_is_rejected_before_any_run(tmp_path, capsys):
    base = _template_document()
    base["plant_id"] = "whole-run-buck"
    output = tmp_path / "study"
    path = _write_study(
        tmp_path / "study.json",
        kind="parameter_sweep",
        plan=_sweep_plan([0.1, 0.2]),
        base=base,
        output=output,
    )

    code = main(["study", str(path), "--mode", "formal_comparison"])

    captured = capsys.readouterr()
    assert code == 2
    rejection = json.loads(captured.out)
    assert rejection["error_code"] == "formal_comparison_rejected"
    assert not output.exists()
    assert not any(tmp_path.glob("**/runs"))


# ---------------------------------------------------------------------------
# Adversarial end-to-end regressions
# ---------------------------------------------------------------------------


def test_sweep_over_a_plant_numeric_leaf_runs(tmp_path, capsys):
    output = tmp_path / "study"
    plan = {
        "parameters": [
            {
                "path": "plant_config.load_resistance_ohm",
                "values": [10.0, 5.0],
            }
        ]
    }
    path = _write_study(
        tmp_path / "study.json",
        kind="parameter_sweep",
        plan=plan,
        output=output,
    )

    assert main(["study", str(path)]) == 0
    capsys.readouterr()
    report = _read_report(output)

    assert len(report["points"]) == 2
    assert [
        point["parameters"]["plant_config.load_resistance_ohm"]
        for point in report["points"]
    ] == [10.0, 5.0]
    assert all(
        point["status"] in {"RUN_OK", "QUALIFIED"}
        for point in report["points"]
    )


def test_periodicity_comparison_matches_independent_evaluation(tmp_path, capsys):
    output = tmp_path / "study"
    plan = _periodicity_plan()
    path = _write_study(
        tmp_path / "study.json",
        kind="periodicity",
        plan=plan,
        base=_periodicity_base(),
        output=output,
    )

    assert main(["study", str(path)]) == 0
    capsys.readouterr()
    report = _read_report(output)

    comparison = report["kind_result"]["comparison"]
    last_point = report["points"][-1]
    samples = json.loads(
        (Path(last_point["run_dir"]) / "samples.json").read_text(
            encoding="utf-8"
        )
    )
    independent = evaluate_periodicity(
        samples,
        signals=plan["signals"],
        compare_period=plan["compare_period"],
        compare_windows=plan["compare_windows"],
        tolerance_relative=plan["tolerance_relative"],
    )

    assert comparison["converged"] is True
    assert comparison["converged"] == independent["converged"]
    assert comparison["compared_pairs"] == independent["compared_pairs"]
    assert comparison["method"] == independent["method"]
    assert comparison["worst_relative_difference"] == pytest.approx(
        independent["worst_relative_difference"]
    )
    assert comparison["signal_differences"] == pytest.approx(
        independent["signal_differences"]
    )
    if independent["sampling_interval_s"] is None:
        assert comparison["sampling_interval_s"] is None
    else:
        assert comparison["sampling_interval_s"] == pytest.approx(
            independent["sampling_interval_s"]
        )


def test_monte_carlo_draws_match_points_and_independent_hash(tmp_path, capsys):
    output = tmp_path / "study"
    path = _write_study(
        tmp_path / "study.json",
        kind="monte_carlo",
        plan=_monte_carlo_plan(),
        output=output,
    )

    assert main(["study", str(path)]) == 0
    capsys.readouterr()
    report = _read_report(output)

    draws = report["kind_result"]["draws"]
    assert [point["parameters"] for point in report["points"]] == draws
    canonical = json.dumps(
        draws, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert report["kind_result"]["draws_sha256"] == expected
