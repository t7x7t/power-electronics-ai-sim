"""Study execution layer: run every planned point and publish one report.

The pure planning rules live in :mod:`pe_sim.study`; this module owns only the
orchestration around them.  For every point it actually executes, it writes
the point's experiment document under ``<output>/points/<label>/config.json``
*immediately before* that point runs (never for points that are skipped),
delegates the run to an injected executor, then reads the published
``manifest.json`` and ``samples.json`` back from the run package.  An
unreadable manifest, a manifest status outside the success set, or missing
samples for declared statistics fails the point but never suppresses the
study report or the remaining points.

The module deliberately knows nothing about Plants, controllers, backends, the
runtime, or the CLI.  Execution is an injected callable, so the same runner
works with fake executors in tests and with the real CLI composition root.

An aggregate is never promoted to ``comparable`` evidence: the aggregate is
built by :func:`pe_sim.study.aggregate_study`, which only ever reports
``per-point`` evidence and declares cross-point comparability separately.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import manifest_digest, verify_package
from .study import (
    StudyPlan,
    StudyPlanError,
    StudyPoint,
    StudyPointRecord,
    build_study_report,
    evaluate_periodicity,
    monte_carlo_draws,
    monte_carlo_draws_sha256,
    statistics_for_point,
    write_study_report,
)


#: A point executor receives the planned point and the exact config path that
#: this runner wrote for it, and returns ``(exit_code, payload)``.  The payload
#: must carry the published run directory in ``payload["run_dir"]``.
PointExecutor = Callable[[StudyPoint, Path], tuple[int, Mapping[str, Any]]]

#: Per-kind declarations the aggregate records as added by the study layer.
_KIND_EXTRA_ADDED: dict[str, tuple[str, ...]] = {
    "parameter_sweep": ("grid_resolution",),
    "monte_carlo": ("distribution_sampling",),
    "periodicity": ("duration_extension",),
}

_RUN_DIR_MISSING = "run_dir_missing"
_MANIFEST_READ_ERROR = "manifest.json is missing or not a JSON object"
_SAMPLES_READ_ERROR = "samples.json is missing or not an array of objects"
_MANIFEST_OK_STATUSES = frozenset({"RUN_OK", "QUALIFIED"})

#: Machine-readable point failure reasons recorded in the study report.
_FAILURE_CONFIG_WRITE = "config_write_error"
_FAILURE_EXECUTOR = "executor_error"
_FAILURE_EXIT_CODE = "executor_exit_code_nonzero"
_FAILURE_RUN_DIR = "run_dir_missing"
_FAILURE_MANIFEST = "manifest_error"
_FAILURE_MANIFEST_STATUS = "manifest_status_not_success"
_FAILURE_MANIFEST_RUN_ID = "manifest_run_id_mismatch"
_FAILURE_MANIFEST_PLANT_ID = "manifest_plant_id_mismatch"
_FAILURE_MANIFEST_DIGEST = "manifest_sha256_mismatch"
_FAILURE_PACKAGE_DIGEST = "package_digest_mismatch"
_FAILURE_SAMPLES = "samples_error"
_FAILURE_STATISTICS = "statistics_error"
_FAILURE_PERIODICITY = "periodicity_error"


@dataclass(frozen=True)
class StudyRunResult:
    """Terminal outcome of one study run.

    ``report`` is the exact object written to ``study-report.json``;
    ``executed_points`` counts points the runner attempted (including points
    whose config could not be written) and ``skipped_points`` counts planned
    points that were never attempted because periodicity converged.
    """

    report: Mapping[str, Any]
    report_path: Path
    exit_code: int
    executed_points: int
    skipped_points: int


def run_study(
    plan: StudyPlan,
    *,
    execute_point: PointExecutor,
    created_utc: str | None = None,
) -> StudyRunResult:
    """Execute every planned point and publish ``study-report.json``.

    Points run in ``plan.points`` order, and each point's config is written
    only when that point is about to run.  A config-write failure, an executor
    exception, a payload without a usable ``run_dir``, an unreadable manifest,
    a manifest status outside ``RUN_OK``/``QUALIFIED``, or missing samples for
    declared statistics fails the point (``exit_code=1``) but the report is
    still published and later points still run.  A periodicity study stops
    after the first point whose samples converge within the declared relative
    tolerance.
    """

    if not isinstance(plan, StudyPlan):
        raise TypeError(
            f"plan must be a StudyPlan; got {type(plan).__name__}"
        )
    if not callable(execute_point):
        raise TypeError("execute_point must be callable")

    base_dir = Path(plan.output_dir)
    if plan.kind == "monte_carlo":
        # The plan and the report must describe the same draws.  Recomputing
        # them here makes a planning bug fail before any run starts instead of
        # publishing points that silently disagree with the published seed.
        declared = monte_carlo_draws(
            plan.plan["parameters"],
            plan.plan["samples"],
            plan.plan["seed"],
        )
        planned = tuple(dict(point.parameters) for point in plan.points)
        if planned != declared:
            raise StudyPlanError(
                "invalid_plan",
                "monte carlo points do not match the draws replayed from the "
                "declared seed",
            )

    periodicity = plan.kind == "periodicity"
    records: list[StudyPointRecord] = []
    executed = 0
    all_points_ok = True
    converged = False
    periodicity_failed = False
    last_comparison: Mapping[str, Any] | None = None

    for point in plan.points:
        if periodicity and converged:
            # The first converged point is the convergence point; later
            # planned runs are explicitly skipped rather than silently dropped.
            break
        # The point is committed to execution as soon as the loop reaches it;
        # a config-write or executor failure still counts as executed, not as
        # skipped.
        executed += 1
        details: dict[str, Any] = dict(point.details)
        try:
            config_path = _write_point_config(point, base_dir)
        except Exception as exc:  # noqa: BLE001 - recorded in the report
            all_points_ok = False
            details["config_error"] = f"{type(exc).__name__}: {exc}"
            records.append(
                _failed_point_record(
                    point, details, failure_reason=_FAILURE_CONFIG_WRITE
                )
            )
            continue

        exit_code: int | None = None
        try:
            exit_code, payload = execute_point(point, config_path)
            exit_code = int(exit_code)
        except Exception as exc:  # noqa: BLE001 - recorded, not re-raised
            # KeyboardInterrupt/SystemExit are BaseException, so they still
            # propagate; ordinary executor failures become failed points.
            all_points_ok = False
            details["executor_error"] = f"{type(exc).__name__}: {exc}"
            records.append(
                _failed_point_record(
                    point, details, failure_reason=_FAILURE_EXECUTOR
                )
            )
            continue

        execution_ok = exit_code == 0
        analysis_ok = True
        failure_reason: str | None = None
        if not execution_ok:
            all_points_ok = False
            failure_reason = _FAILURE_EXIT_CODE
            details["exit_code"] = exit_code

        run_dir = _payload_run_dir(payload)
        if run_dir is None or not run_dir.is_dir():
            # A payload without a usable package is a failed point even when
            # the executor reported success: there is no evidence to read.
            all_points_ok = False
            details["status"] = _RUN_DIR_MISSING
            records.append(
                StudyPointRecord(
                    index=point.index,
                    run_id=point.run_id,
                    run_dir=str(run_dir) if run_dir is not None else "",
                    parameters=dict(point.parameters),
                    manifest={},
                    statistics=None,
                    details=details,
                    execution_ok=False,
                    analysis_ok=False,
                    failure_reason=failure_reason or _FAILURE_RUN_DIR,
                    exit_code=exit_code,
                )
            )
            continue

        manifest, manifest_error = _read_manifest(run_dir)
        if manifest_error is not None:
            all_points_ok = False
            analysis_ok = False
            failure_reason = failure_reason or _FAILURE_MANIFEST
            details["manifest_error"] = manifest_error
        else:
            if manifest.get("status") not in _MANIFEST_OK_STATUSES:
                all_points_ok = False
                analysis_ok = False
                failure_reason = failure_reason or _FAILURE_MANIFEST_STATUS
                details["manifest_error"] = (
                    f"manifest status is {manifest.get('status')!r}"
                )
            identity_problem, identity_details = _manifest_identity_problem(
                manifest, run_dir, point
            )
            if identity_problem is not None:
                all_points_ok = False
                analysis_ok = False
                failure_reason = failure_reason or identity_problem
                details.update(identity_details)

        samples = _read_samples(run_dir)
        declared_statistics = plan.plan.get("statistics", [])
        if samples is None:
            # A published point package always owns its samples.  Missing,
            # malformed, or non-finite samples are a failed point even when the
            # plan did not ask for a statistic: otherwise a NaN could be
            # published without any consumer noticing.
            all_points_ok = False
            analysis_ok = False
            failure_reason = failure_reason or _FAILURE_SAMPLES
            details["samples_error"] = _SAMPLES_READ_ERROR
            if declared_statistics:
                details["statistics_error"] = _SAMPLES_READ_ERROR
            statistics = None
        else:
            statistics, statistics_error = _point_statistics(
                samples, declared_statistics
            )
            if statistics_error is not None:
                # A declared statistic that cannot be computed is recorded, not
                # swallowed: the point is still evidence, but the reason its
                # statistic is missing must survive in the report.  It is also a
                # failed point, because the study promised an analysis it could
                # not deliver.
                all_points_ok = False
                analysis_ok = False
                failure_reason = failure_reason or _FAILURE_STATISTICS
                details["statistics_error"] = statistics_error

        if periodicity:
            details["duration_s"] = _point_duration_s(point)
            comparison, periodicity_details = _point_periodicity(
                plan, samples
            )
            details["periodicity"] = periodicity_details
            if comparison is None:
                all_points_ok = False
                analysis_ok = False
                periodicity_failed = True
                failure_reason = failure_reason or _FAILURE_PERIODICITY
            else:
                last_comparison = comparison
                if comparison.get("converged") is True:
                    converged = True

        records.append(
            StudyPointRecord(
                index=point.index,
                run_id=point.run_id,
                run_dir=str(run_dir),
                parameters=dict(point.parameters),
                manifest=manifest,
                statistics=statistics,
                details=details,
                execution_ok=execution_ok,
                analysis_ok=analysis_ok,
                failure_reason=failure_reason,
                exit_code=exit_code,
            )
        )

    if periodicity_failed:
        # A comparison error on an earlier point must not be erased by a later
        # point that happens to converge.
        converged = False

    skipped_indices = [point.index for point in plan.points[executed:]]
    skipped = len(plan.points) - executed

    kind_result = _kind_result(
        plan,
        records,
        executed=executed,
        skipped_indices=skipped_indices,
        converged=converged,
        last_comparison=last_comparison,
    )
    report = build_study_report(
        plan,
        records,
        kind_result=kind_result,
        created_utc=created_utc,
        extra_added=_KIND_EXTRA_ADDED[plan.kind],
    )
    report_path = write_study_report(report, base_dir)

    success = all_points_ok and (not periodicity or converged)
    return StudyRunResult(
        report=report,
        report_path=report_path,
        exit_code=0 if success else 1,
        executed_points=executed,
        skipped_points=skipped,
    )


# ---------------------------------------------------------------------------
# Point inputs
# ---------------------------------------------------------------------------


def _write_point_config(point: StudyPoint, base_dir: Path) -> Path:
    """Write one point document immediately before that point executes.

    The document is written byte-for-byte as planned (canonical JSON) so the
    file is the reproducible input of that point, including its stated
    ``output.directory`` under the study's ``runs`` root.  Serialization
    happens before the directory is created, so a non-finite document fails
    without leaving a half-written point directory.
    """

    text = (
        json.dumps(
            dict(point.document),
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    directory = base_dir / "points" / point.label
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    # Write through a process-unique temporary file: a crash must not leave a
    # half-written point document that a later reader could mistake for the
    # planned input.
    temporary = directory / f"config.json.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return config_path


def _failed_point_record(
    point: StudyPoint,
    details: dict[str, Any],
    *,
    failure_reason: str,
) -> StudyPointRecord:
    """Build the record for a point that never produced a run package."""

    return StudyPointRecord(
        index=point.index,
        run_id=point.run_id,
        run_dir="",
        parameters=dict(point.parameters),
        manifest={},
        statistics=None,
        details=details,
        execution_ok=False,
        analysis_ok=False,
        failure_reason=failure_reason,
        exit_code=None,
    )


def _manifest_identity_problem(
    manifest: Mapping[str, Any], run_dir: Path, point: StudyPoint
) -> tuple[str | None, dict[str, Any]]:
    """Return the first identity problem with a published point package.

    A package is evidence for *this* point only when its manifest names the
    same run and plant, and its recorded digest matches its contents.  The
    package digest is checked with :func:`pe_sim.artifacts.verify_package`, the
    single implementation of that rule, but only when the manifest declares
    one.  Minimal fake packages in tests may omit ``package_sha256``.
    """

    manifest_run_id = manifest.get("run_id")
    if manifest_run_id != point.run_id:
        return _FAILURE_MANIFEST_RUN_ID, {
            "manifest_run_id": manifest_run_id,
            "expected_run_id": point.run_id,
        }

    expected_plant_id = point.document.get("plant_id")
    manifest_plant = manifest.get("plant")
    manifest_plant_id = (
        manifest_plant.get("id")
        if isinstance(manifest_plant, Mapping)
        else None
    )
    if (
        not isinstance(manifest_plant_id, str)
        or not manifest_plant_id
        or manifest_plant_id != expected_plant_id
    ):
        return _FAILURE_MANIFEST_PLANT_ID, {
            "manifest_plant_id": manifest_plant_id,
            "expected_plant_id": expected_plant_id,
        }

    declared_digest = manifest.get("manifest_sha256")
    if not isinstance(declared_digest, str) or not declared_digest:
        return _FAILURE_MANIFEST_DIGEST, {
            "manifest_sha256": declared_digest,
        }
    try:
        computed_digest = manifest_digest(manifest)
    except (TypeError, ValueError) as exc:
        return _FAILURE_MANIFEST_DIGEST, {
            "manifest_digest_error": f"{type(exc).__name__}: {exc}",
        }
    if computed_digest != declared_digest:
        return _FAILURE_MANIFEST_DIGEST, {
            "manifest_sha256": declared_digest,
            "computed_manifest_sha256": computed_digest,
        }

    if "package_sha256" in manifest:
        try:
            problems = verify_package(manifest, run_dir)
        except (OSError, TypeError, ValueError) as exc:
            problems = (f"package cannot be verified: {exc}",)
        if problems:
            return _FAILURE_PACKAGE_DIGEST, {
                "package_problems": list(problems),
            }

    return None, {}


# ---------------------------------------------------------------------------
# Published package reading
# ---------------------------------------------------------------------------


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard ``NaN``/``Infinity`` JSON extensions."""

    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _read_json(path: Path) -> Any:
    """Return decoded JSON, or ``None`` when the file is missing or invalid."""

    try:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError):
        return None


def _read_manifest(run_dir: Path) -> tuple[dict[str, Any], str | None]:
    """Return the published manifest and why it could not be read, if so."""

    value = _read_json(run_dir / "manifest.json")
    if not isinstance(value, Mapping):
        return {}, _MANIFEST_READ_ERROR
    return dict(value), None


def _read_samples(run_dir: Path) -> list[dict[str, Any]] | None:
    value = _read_json(run_dir / "samples.json")
    if not isinstance(value, list):
        return None
    if any(not isinstance(row, Mapping) for row in value):
        return None
    return [dict(row) for row in value]


def _point_statistics(
    samples: list[dict[str, Any]] | None,
    statistics: Any,
) -> tuple[dict[str, float] | None, str | None]:
    if samples is None:
        return None, None
    try:
        return statistics_for_point(samples, statistics), None
    except StudyPlanError as exc:
        return None, str(exc)


def _payload_run_dir(payload: Any) -> Path | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("run_dir")
    if value is None or value == "":
        return None
    return Path(str(value))


def _point_duration_s(point: StudyPoint) -> float:
    timebase = point.document.get("timebase")
    if not isinstance(timebase, Mapping):
        raise ValueError(
            f"periodicity point {point.index} has no timebase.duration_s"
        )
    return float(timebase["duration_s"])


def _point_periodicity(
    plan: StudyPlan,
    samples: list[dict[str, Any]] | None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
    """Evaluate one point; a study-plan error becomes a recorded detail."""

    plan_values = plan.plan
    try:
        comparison = evaluate_periodicity(
            samples if samples is not None else [],
            signals=plan_values["signals"],
            compare_period=plan_values["compare_period"],
            compare_windows=plan_values["compare_windows"],
            tolerance_relative=plan_values["tolerance_relative"],
        )
    except StudyPlanError as exc:
        return None, {"error": str(exc)}
    return comparison, comparison


# ---------------------------------------------------------------------------
# Kind-specific result
# ---------------------------------------------------------------------------


def _kind_result(
    plan: StudyPlan,
    records: list[StudyPointRecord],
    *,
    executed: int,
    skipped_indices: list[int],
    converged: bool,
    last_comparison: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if plan.kind == "parameter_sweep":
        return {
            "statistic_names": [
                item["name"] for item in plan.plan.get("statistics", [])
            ],
            "series": [
                {
                    "parameters": dict(record.parameters),
                    "statistics": dict(record.statistics or {}),
                }
                for record in records
            ],
        }
    if plan.kind == "monte_carlo":
        draws = monte_carlo_draws(
            plan.plan["parameters"],
            plan.plan["samples"],
            plan.plan["seed"],
        )
        draw_list = [dict(draw) for draw in draws]
        return {
            "seed": int(plan.plan["seed"]),
            "samples_requested": int(plan.plan["samples"]),
            "draws_sha256": monte_carlo_draws_sha256(draw_list),
            "draws": draw_list,
        }
    return {
        "converged": bool(converged),
        "runs_executed": executed,
        "runs_planned": len(plan.points),
        "skipped_indices": list(skipped_indices),
        "comparison": dict(last_comparison) if last_comparison is not None else None,
        "tolerance_relative": float(plan.plan["tolerance_relative"]),
    }


__all__ = [
    "PointExecutor",
    "StudyRunResult",
    "run_study",
]
