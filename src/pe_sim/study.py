"""Study planning layer: expansion, sampling, statistics, and aggregation.

A *study* is a plan over ordinary runs, not a new execution mode.  This module
owns only the pure planning logic:

* :func:`parse_study_document` validates a ``pe-sim.study.v1`` document and
  expands it into independent per-point experiment documents.  Every point is
  a complete, schema-valid experiment document with its own ``run_id`` and a
  shared ``<output>/runs`` directory; nothing here executes a Plant.
* :func:`monte_carlo_draws` reproduces the declared sampling contract from a
  seed using only ``random.Random(seed).random()`` plus a local Box-Muller
  transform.  A report records the draws and their canonical SHA-256.
* :func:`evaluate_periodicity` is a *tolerance-based* trailing-window
  comparison of recorded samples.  It never claims a physical steady state.
* :func:`statistics_for_point` computes descriptive statistics only; no loss
  or efficiency claim is made.
* :func:`aggregate_study` reports a ``per-point`` aggregate.  An aggregate
  never inherits ``comparable`` evidence, even when every point happens to be
  comparable and cross-point comparability is explicitly claimed.

The module is deliberately stdlib-only (plus the experiment-schema validator
from :mod:`pe_sim.contracts`).  It does not import the runtime, the CLI, or a
solver, and it never derives a route evidence cap locally.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import random
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import validate_experiment_document
# The evidence ladder has exactly one definition in this project (docs/43,
# ADR 0004).  Import it instead of mirroring it: a local copy would silently
# drift and let an aggregate rank points against a stale vocabulary.
from .time_model import RUNTIME_EVIDENCE_LEVELS


STUDY_SCHEMA = "pe-sim.study.v1"
STUDY_REPORT_SCHEMA = "pe-sim.study-report.v1"
STUDY_AGGREGATE_SCHEMA = "pe-sim.study-aggregate.v1"
STUDY_KINDS: tuple[str, ...] = ("parameter_sweep", "monte_carlo", "periodicity")
MAX_STUDY_POINTS = 64
STUDY_POINT_EVIDENCE_LEVEL = "per-point"

STATISTIC_REDUCERS: tuple[str, ...] = (
    "mean",
    "min",
    "max",
    "rms",
    "peak_to_peak",
    "last",
    "ratio_of_means",
)

STUDY_LIMITATIONS = frozenset(
    {
        "aggregate_does_not_inherit_comparable",
        "monte_carlo_draws_depend_on_declared_distributions",
        "periodicity_convergence_is_tolerance_based",
        "descriptive_statistics_only_no_loss_claim",
    }
)

STUDY_COMPARABILITY_REASONS = frozenset(
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

_EVIDENCE_RANK = {level: rank for rank, level in enumerate(RUNTIME_EVIDENCE_LEVELS)}
_UNDECLARED = "undeclared"

_STUDY_KEYS = frozenset(
    {"schema", "study_id", "kind", "base_experiment", "plan", "output"}
)
_FORBIDDEN_PATH_ROOTS = frozenset(
    {"schema_version", "run_id", "output", "experiment_id"}
)
_STAT_KEYS = frozenset(
    {"name", "sample_key", "reducer", "numerator", "denominator"}
)
_EXTRA_ADDED_ALLOWED = frozenset(
    {"grid_resolution", "distribution_sampling", "duration_extension"}
)

_STUDY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_STATISTIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_REPORT_FILENAME = "study-report.json"
_RELATIVE_DIFFERENCE_FLOOR = 1e-9
_PERIODICITY_METHOD = "trailing_window_pairwise_relative_difference"


class StudyPlanError(ValueError):
    """A fail-closed Study planning error with a machine-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = str(reason)
        super().__init__(f"{self.reason}: {message}")


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: Any, label: str, reason: str) -> float:
    """Return ``value`` as a finite float or raise ``StudyPlanError``."""

    if not _is_number(value):
        raise StudyPlanError(
            reason, f"{label} must be a finite number (bool is not a number); got {value!r}"
        )
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise StudyPlanError(
            reason, f"{label} must be a finite number; got {value!r}"
        ) from None
    if not math.isfinite(number):
        raise StudyPlanError(
            reason, f"{label} must be finite; got {value!r}"
        )
    return number


def _integer_in_range(
    value: Any, label: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StudyPlanError(
            "invalid_plan",
            f"{label} must be an integer in [{minimum}, {maximum}]; got {value!r}",
        )
    if not minimum <= value <= maximum:
        raise StudyPlanError(
            "invalid_plan",
            f"{label} must be an integer in [{minimum}, {maximum}]; got {value!r}",
        )
    return value


def _samples_in_range(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StudyPlanError(
            "invalid_plan",
            "samples must be an integer in [1, "
            f"{MAX_STUDY_POINTS}]; got {value!r}",
        )
    if value < 1:
        raise StudyPlanError(
            "invalid_plan",
            f"samples must be at least 1; got {value}",
        )
    if value > MAX_STUDY_POINTS:
        raise StudyPlanError(
            "point_budget_exceeded",
            f"samples={value} exceeds MAX_STUDY_POINTS={MAX_STUDY_POINTS}",
        )
    return value


def _seed_value(value: Any) -> int:
    maximum = 2**32 - 1
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise StudyPlanError(
            "invalid_seed",
            f"seed must be an integer in [0, {maximum}]; got {value!r}",
        )
    return value


def _check_keys(
    mapping: Mapping[str, Any],
    *,
    allowed: frozenset[str] | set[str],
    required: frozenset[str] | set[str],
    label: str,
) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise StudyPlanError(
            "unknown_key",
            f"{label} contains unknown key(s) {unknown}; allowed keys: {sorted(allowed)}",
        )
    missing = sorted(set(required) - set(mapping))
    if missing:
        raise StudyPlanError(
            "invalid_plan",
            f"{label} is missing required key(s) {missing}; required keys: {sorted(required)}",
        )


def _validate_path_grammar(path: Any) -> str:
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        raise StudyPlanError(
            "invalid_path",
            "parameter path must be a dotted path matching "
            f"{_PATH_RE.pattern!r}; got {path!r}",
        )
    root = path.split(".", 1)[0]
    if root in _FORBIDDEN_PATH_ROOTS:
        raise StudyPlanError(
            "invalid_path",
            f"parameter path {path!r} starts with reserved root {root!r}; "
            f"forbidden roots: {sorted(_FORBIDDEN_PATH_ROOTS)}",
        )
    return path


def _resolve_path_parent(
    document: Mapping[str, Any], path: str
) -> tuple[Mapping[str, Any], str]:
    """Return the parent mapping and final key of a numeric leaf path."""

    segments = path.split(".")
    current: Any = document
    for segment in segments[:-1]:
        if not isinstance(current, Mapping) or segment not in current:
            raise StudyPlanError(
                "invalid_path",
                f"parameter path {path!r} does not exist in base_experiment",
            )
        current = current[segment]
    final = segments[-1]
    if not isinstance(current, Mapping) or final not in current:
        raise StudyPlanError(
            "invalid_path",
            f"parameter path {path!r} does not exist in base_experiment",
        )
    leaf = current[final]
    if isinstance(leaf, bool) or not isinstance(leaf, (int, float)):
        raise StudyPlanError(
            "invalid_path",
            f"parameter path {path!r} must point to an int/float leaf "
            f"(bool is not a number); got {type(leaf).__name__}",
        )
    if not math.isfinite(float(leaf)):
        raise StudyPlanError(
            "invalid_path",
            f"parameter path {path!r} points to a non-finite leaf {leaf!r}",
        )
    return current, final


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    segments = path.split(".")
    current: Any = document
    for segment in segments[:-1]:
        current = current[segment]
    current[segments[-1]] = value


def _parse_statistics(
    value: Any,
    *,
    max_items: int | None,
    shape_reason: str,
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StudyPlanError(
            shape_reason,
            "statistics must be a sequence of objects; "
            f"got {type(value).__name__}",
        )
    items = list(value)
    if max_items is not None and len(items) > max_items:
        raise StudyPlanError(
            shape_reason,
            f"statistics accepts at most {max_items} entries; got {len(items)}",
        )
    parsed: list[dict[str, Any]] = []
    names: set[str] = set()
    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise StudyPlanError(
                "invalid_statistic",
                f"statistics[{position}] must be an object",
            )
        unknown = sorted(set(item) - _STAT_KEYS)
        if unknown:
            raise StudyPlanError(
                "unknown_key",
                f"statistics[{position}] contains unknown key(s) {unknown}; "
                f"allowed keys: {sorted(_STAT_KEYS)}",
            )
        name = item.get("name")
        if not isinstance(name, str) or not _STATISTIC_NAME_RE.fullmatch(name):
            raise StudyPlanError(
                "invalid_statistic",
                f"statistics[{position}].name must match "
                f"{_STATISTIC_NAME_RE.pattern!r}; got {name!r}",
            )
        if name in names:
            raise StudyPlanError(
                "invalid_statistic",
                f"statistics name {name!r} is duplicated",
            )
        names.add(name)
        reducer = item.get("reducer")
        if reducer not in STATISTIC_REDUCERS:
            raise StudyPlanError(
                "invalid_statistic",
                f"statistics[{position}].reducer must be one of "
                f"{list(STATISTIC_REDUCERS)}; got {reducer!r}",
            )
        if reducer == "ratio_of_means":
            if "sample_key" in item:
                raise StudyPlanError(
                    "invalid_statistic",
                    f"statistics[{position}] uses reducer 'ratio_of_means', which "
                    "takes numerator and denominator, not sample_key",
                )
            numerator = item.get("numerator")
            denominator = item.get("denominator")
            if not isinstance(numerator, str) or not numerator:
                raise StudyPlanError(
                    "missing_sample_key",
                    f"statistics[{position}].numerator must be a non-empty sample key",
                )
            if not isinstance(denominator, str) or not denominator:
                raise StudyPlanError(
                    "missing_sample_key",
                    f"statistics[{position}].denominator must be a non-empty sample key",
                )
            parsed.append(
                {
                    "name": name,
                    "reducer": reducer,
                    "numerator": numerator,
                    "denominator": denominator,
                }
            )
        else:
            if "numerator" in item or "denominator" in item:
                raise StudyPlanError(
                    "invalid_statistic",
                    f"statistics[{position}] uses reducer {reducer!r}, which does "
                    "not accept numerator or denominator; use sample_key",
                )
            sample_key = item.get("sample_key")
            if not isinstance(sample_key, str) or not sample_key:
                raise StudyPlanError(
                    "missing_sample_key",
                    f"statistics[{position}].sample_key must be a non-empty string",
                )
            parsed.append(
                {"name": name, "reducer": reducer, "sample_key": sample_key}
            )
    return parsed


def _parse_signals(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StudyPlanError(
            "invalid_plan",
            "signals must be a sequence of 1-8 non-empty strings; "
            f"got {type(value).__name__}",
        )
    items = list(value)
    if not 1 <= len(items) <= 8:
        raise StudyPlanError(
            "invalid_plan",
            f"signals must contain 1-8 entries; got {len(items)}",
        )
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item:
            raise StudyPlanError(
                "invalid_plan",
                f"signals entries must be non-empty strings; got {item!r}",
            )
        if item in seen:
            raise StudyPlanError(
                "invalid_plan", f"signals contains duplicate {item!r}"
            )
        seen.add(item)
    return items


def _normalize_mc_parameters(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StudyPlanError(
            "invalid_plan",
            "monte_carlo parameters must be a sequence of 1-3 objects; "
            f"got {type(value).__name__}",
        )
    items = list(value)
    if not 1 <= len(items) <= 3:
        raise StudyPlanError(
            "invalid_plan",
            f"monte_carlo parameters must contain 1-3 entries; got {len(items)}",
        )
    normalized: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise StudyPlanError(
                "invalid_plan",
                f"monte_carlo parameters[{position}] must be an object",
            )
        distribution = item.get("distribution")
        if distribution not in ("uniform", "normal"):
            raise StudyPlanError(
                "invalid_distribution",
                f"monte_carlo parameters[{position}].distribution must be "
                f"'uniform' or 'normal'; got {distribution!r}",
            )
        if distribution == "uniform":
            allowed = {"path", "distribution", "low", "high"}
        else:
            allowed = {"path", "distribution", "mean", "sigma"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise StudyPlanError(
                "unknown_key",
                f"monte_carlo parameters[{position}] contains unknown key(s) "
                f"{unknown} for distribution {distribution!r}; "
                f"allowed keys: {sorted(allowed)}",
            )
        path = _validate_path_grammar(item.get("path"))
        if distribution == "uniform":
            low = _finite_number(
                item.get("low"),
                f"monte_carlo parameters[{position}].low",
                "invalid_distribution",
            )
            high = _finite_number(
                item.get("high"),
                f"monte_carlo parameters[{position}].high",
                "invalid_distribution",
            )
            if not low < high:
                raise StudyPlanError(
                    "invalid_distribution",
                    f"monte_carlo parameters[{position}] requires low < high; "
                    f"got low={low!r}, high={high!r}",
                )
            normalized.append(
                {
                    "path": path,
                    "distribution": "uniform",
                    "low": low,
                    "high": high,
                }
            )
        else:
            mean = _finite_number(
                item.get("mean"),
                f"monte_carlo parameters[{position}].mean",
                "invalid_distribution",
            )
            sigma = _finite_number(
                item.get("sigma"),
                f"monte_carlo parameters[{position}].sigma",
                "invalid_distribution",
            )
            if not sigma > 0.0:
                raise StudyPlanError(
                    "invalid_distribution",
                    f"monte_carlo parameters[{position}] requires sigma > 0; "
                    f"got sigma={sigma!r}",
                )
            normalized.append(
                {
                    "path": path,
                    "distribution": "normal",
                    "mean": mean,
                    "sigma": sigma,
                }
            )
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_point_document(
    base: Mapping[str, Any],
    parameters: Mapping[str, Any],
    *,
    index: int,
    study_id: str,
    output_dir: str,
) -> tuple[str, dict[str, Any]]:
    run_id = f"{study_id}-p{index:02d}"
    document = copy.deepcopy(dict(base))
    for path, value in parameters.items():
        _set_path(document, path, value)
    document["run_id"] = run_id
    output = document.get("output")
    if not isinstance(output, dict):
        raise StudyPlanError(
            "invalid_plan",
            "base_experiment.output must be an object after schema validation",
        )
    output["directory"] = str(Path(output_dir) / "runs")
    try:
        validate_experiment_document(document)
    except ValueError as exc:
        raise StudyPlanError(
            "invalid_plan",
            f"point {index} document failed experiment schema validation: {exc}",
        ) from exc
    return run_id, document


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyPoint:
    index: int
    label: str
    run_id: str
    parameters: Mapping[str, float | int]
    document: Mapping[str, Any]
    details: Mapping[str, Any]


@dataclass(frozen=True)
class StudyPlan:
    study_id: str
    kind: str
    base_experiment: Mapping[str, Any]
    plan: Mapping[str, Any]
    output_dir: str
    points: tuple[StudyPoint, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STUDY_SCHEMA,
            "study_id": self.study_id,
            "kind": self.kind,
            "base_experiment": copy.deepcopy(dict(self.base_experiment)),
            "plan": copy.deepcopy(dict(self.plan)),
            "output": {"directory": self.output_dir},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StudyPlan":
        return parse_study_document(value)


def _parse_parameter_sweep(
    plan: Any,
    *,
    base: Mapping[str, Any],
    output_dir: str,
    study_id: str,
) -> tuple[dict[str, Any], tuple[StudyPoint, ...]]:
    if not isinstance(plan, Mapping):
        raise StudyPlanError(
            "invalid_plan",
            "parameter_sweep.plan must be an object",
        )
    _check_keys(
        plan,
        allowed=frozenset({"parameters", "statistics"}),
        required=frozenset({"parameters"}),
        label="parameter_sweep.plan",
    )
    raw_parameters = plan["parameters"]
    if isinstance(raw_parameters, (str, bytes)) or not isinstance(
        raw_parameters, Sequence
    ):
        raise StudyPlanError(
            "invalid_plan",
            "parameter_sweep.plan.parameters must be a sequence of 1-3 objects",
        )
    items = list(raw_parameters)
    if not 1 <= len(items) <= 3:
        raise StudyPlanError(
            "invalid_plan",
            "parameter_sweep.plan.parameters must contain 1-3 entries; "
            f"got {len(items)}",
        )

    paths: list[str] = []
    value_lists: list[tuple[float | int, ...]] = []
    normalized_parameters: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise StudyPlanError(
                "invalid_plan",
                f"parameter_sweep.plan.parameters[{position}] must be an object",
            )
        unknown = sorted(set(item) - {"path", "values"})
        if unknown:
            raise StudyPlanError(
                "unknown_key",
                f"parameter_sweep.plan.parameters[{position}] contains unknown "
                f"key(s) {unknown}; allowed keys: ['path', 'values']",
            )
        if "path" not in item or "values" not in item:
            raise StudyPlanError(
                "invalid_plan",
                f"parameter_sweep.plan.parameters[{position}] requires both "
                "'path' and 'values'",
            )
        path = _validate_path_grammar(item["path"])
        if path in paths:
            raise StudyPlanError(
                "duplicate_parameter_path",
                f"parameter path {path!r} appears more than once; each path "
                "may appear only once in parameter_sweep.plan.parameters",
            )
        _resolve_path_parent(base, path)

        raw_values = item["values"]
        if isinstance(raw_values, (str, bytes)) or not isinstance(
            raw_values, Sequence
        ):
            raise StudyPlanError(
                "invalid_plan",
                f"parameter_sweep.plan.parameters[{position}].values must be a "
                "sequence of 2-16 distinct finite numbers",
            )
        values = list(raw_values)
        if not 2 <= len(values) <= 16:
            raise StudyPlanError(
                "invalid_plan",
                f"parameter_sweep.plan.parameters[{position}].values must "
                f"contain 2-16 entries; got {len(values)}",
            )
        seen: set[float] = set()
        normalized_values: list[float | int] = []
        for value in values:
            number = _finite_number(
                value,
                f"parameter_sweep.plan.parameters[{position}].values",
                "invalid_plan",
            )
            if number in seen:
                raise StudyPlanError(
                    "invalid_plan",
                    f"parameter_sweep.plan.parameters[{position}].values must "
                    f"be distinct; duplicate {value!r}",
                )
            seen.add(number)
            normalized_values.append(value)
        paths.append(path)
        value_lists.append(tuple(normalized_values))
        normalized_parameters.append(
            {"path": path, "values": normalized_values}
        )

    total = 1
    for values in value_lists:
        total *= len(values)
    if total > MAX_STUDY_POINTS:
        raise StudyPlanError(
            "point_budget_exceeded",
            f"parameter_sweep expands to {total} points, which exceeds "
            f"MAX_STUDY_POINTS={MAX_STUDY_POINTS}",
        )

    statistics = _parse_statistics(
        plan.get("statistics", []),
        max_items=4,
        shape_reason="invalid_plan",
    )
    normalized_plan: dict[str, Any] = {
        "parameters": normalized_parameters,
        "statistics": statistics,
    }

    points: list[StudyPoint] = []
    for index, combination in enumerate(itertools.product(*value_lists)):
        parameters = dict(zip(paths, combination))
        run_id, document = _build_point_document(
            base,
            parameters,
            index=index,
            study_id=study_id,
            output_dir=output_dir,
        )
        points.append(
            StudyPoint(
                index=index,
                label=f"p{index:02d}",
                run_id=run_id,
                parameters=parameters,
                document=document,
                details={},
            )
        )
    return normalized_plan, tuple(points)


def _parse_monte_carlo(
    plan: Any,
    *,
    base: Mapping[str, Any],
    output_dir: str,
    study_id: str,
) -> tuple[dict[str, Any], tuple[StudyPoint, ...]]:
    if not isinstance(plan, Mapping):
        raise StudyPlanError("invalid_plan", "monte_carlo.plan must be an object")
    _check_keys(
        plan,
        allowed=frozenset({"parameters", "samples", "seed"}),
        required=frozenset({"parameters", "samples", "seed"}),
        label="monte_carlo.plan",
    )
    parameters = _normalize_mc_parameters(plan["parameters"])
    seen_paths: set[str] = set()
    for entry in parameters:
        if entry["path"] in seen_paths:
            raise StudyPlanError(
                "duplicate_parameter_path",
                f"parameter path {entry['path']!r} appears more than once; "
                "each path may appear only once in monte_carlo.plan.parameters",
            )
        seen_paths.add(entry["path"])
        _resolve_path_parent(base, entry["path"])
    samples = _samples_in_range(plan["samples"])
    seed = _seed_value(plan["seed"])

    draws = monte_carlo_draws(parameters, samples, seed)
    normalized_plan: dict[str, Any] = {
        "parameters": parameters,
        "samples": samples,
        "seed": seed,
    }

    points: list[StudyPoint] = []
    for index, draw in enumerate(draws):
        point_parameters = dict(draw)
        run_id, document = _build_point_document(
            base,
            point_parameters,
            index=index,
            study_id=study_id,
            output_dir=output_dir,
        )
        points.append(
            StudyPoint(
                index=index,
                label=f"p{index:02d}",
                run_id=run_id,
                parameters=point_parameters,
                document=document,
                details={"draws": dict(point_parameters)},
            )
        )
    return normalized_plan, tuple(points)


def _parse_periodicity(
    plan: Any,
    *,
    base: Mapping[str, Any],
    output_dir: str,
    study_id: str,
) -> tuple[dict[str, Any], tuple[StudyPoint, ...]]:
    if not isinstance(plan, Mapping):
        raise StudyPlanError("invalid_plan", "periodicity.plan must be an object")
    allowed = frozenset(
        {
            "compare_period",
            "compare_windows",
            "tolerance_relative",
            "signals",
            "max_runs",
            "extend_factor",
        }
    )
    _check_keys(
        plan,
        allowed=allowed,
        required=allowed,
        label="periodicity.plan",
    )
    compare_period = _integer_in_range(
        plan["compare_period"], "compare_period", 1, 64
    )
    compare_windows = _integer_in_range(
        plan["compare_windows"], "compare_windows", 2, 32
    )
    max_runs = _integer_in_range(plan["max_runs"], "max_runs", 2, 8)
    tolerance_relative = _finite_number(
        plan["tolerance_relative"], "tolerance_relative", "invalid_plan"
    )
    if tolerance_relative <= 0.0:
        raise StudyPlanError(
            "invalid_plan",
            f"tolerance_relative must be finite and > 0; got {tolerance_relative!r}",
        )
    extend_factor = _finite_number(
        plan["extend_factor"], "extend_factor", "invalid_plan"
    )
    if extend_factor <= 1.0:
        raise StudyPlanError(
            "invalid_plan",
            f"extend_factor must be finite and > 1; got {extend_factor!r}",
        )
    signals = _parse_signals(plan["signals"])

    timebase = base.get("timebase")
    if not isinstance(timebase, Mapping):
        raise StudyPlanError(
            "invalid_plan", "base_experiment.timebase must be an object"
        )
    base_duration = _finite_number(
        timebase.get("duration_s"),
        "base_experiment.timebase.duration_s",
        "invalid_plan",
    )

    normalized_plan: dict[str, Any] = {
        "compare_period": compare_period,
        "compare_windows": compare_windows,
        "tolerance_relative": tolerance_relative,
        "signals": signals,
        "max_runs": max_runs,
        "extend_factor": extend_factor,
    }

    points: list[StudyPoint] = []
    for index in range(max_runs):
        try:
            duration = base_duration * (extend_factor**index)
        except OverflowError:
            raise StudyPlanError(
                "invalid_plan",
                f"periodicity point {index} duration overflows",
            ) from None
        if not math.isfinite(duration) or duration <= 0.0:
            raise StudyPlanError(
                "invalid_plan",
                f"periodicity point {index} duration must be finite and "
                f"positive; got {duration!r}",
            )
        parameters = {"timebase.duration_s": duration}
        run_id, document = _build_point_document(
            base,
            parameters,
            index=index,
            study_id=study_id,
            output_dir=output_dir,
        )
        points.append(
            StudyPoint(
                index=index,
                label=f"p{index:02d}",
                run_id=run_id,
                parameters=parameters,
                document=document,
                details={},
            )
        )
    return normalized_plan, tuple(points)


def parse_study_document(value: Mapping[str, Any]) -> StudyPlan:
    """Validate and expand one ``pe-sim.study.v1`` document."""

    if not isinstance(value, Mapping):
        raise StudyPlanError(
            "invalid_plan",
            f"study document must be an object; got {type(value).__name__}",
        )
    unknown = sorted(set(value) - _STUDY_KEYS)
    if unknown:
        raise StudyPlanError(
            "unknown_key",
            f"study document contains unknown key(s) {unknown}; "
            f"allowed keys: {sorted(_STUDY_KEYS)}",
        )
    if value.get("schema") != STUDY_SCHEMA:
        raise StudyPlanError(
            "invalid_schema",
            f"schema must be exactly {STUDY_SCHEMA!r}; "
            f"got {value.get('schema')!r}",
        )
    missing = sorted(_STUDY_KEYS - set(value))
    if missing:
        raise StudyPlanError(
            "invalid_plan",
            f"study document is missing required key(s) {missing}; "
            f"required keys: {sorted(_STUDY_KEYS)}",
        )

    study_id = value["study_id"]
    if (
        not isinstance(study_id, str)
        or not 1 <= len(study_id) <= 128
        or not _STUDY_ID_RE.fullmatch(study_id)
    ):
        raise StudyPlanError(
            "invalid_study_id",
            "study_id must be 1-128 characters matching "
            f"{_STUDY_ID_RE.pattern!r}; got {study_id!r}",
        )
    # The derived run id is ``<study_id>-pNN`` and must satisfy the experiment
    # schema; an embedded ``..`` cannot survive that grammar.
    if ".." in study_id:
        raise StudyPlanError(
            "invalid_study_id",
            f"study_id {study_id!r} contains '..' and cannot produce a valid "
            "per-point run_id",
        )

    kind = value["kind"]
    if kind not in STUDY_KINDS:
        raise StudyPlanError(
            "unknown_kind",
            f"kind must be one of {list(STUDY_KINDS)}; got {kind!r}",
        )

    output = value["output"]
    if not isinstance(output, Mapping):
        raise StudyPlanError(
            "invalid_plan",
            "output must be an object with a single 'directory' key",
        )
    unknown_output = sorted(set(output) - {"directory"})
    if unknown_output:
        raise StudyPlanError(
            "unknown_key",
            f"output contains unknown key(s) {unknown_output}; "
            "allowed keys: ['directory']",
        )
    if "directory" not in output:
        raise StudyPlanError("invalid_plan", "output.directory is required")
    output_dir = output["directory"]
    if not isinstance(output_dir, str) or not output_dir:
        raise StudyPlanError(
            "invalid_plan",
            f"output.directory must be a non-empty string; got {output_dir!r}",
        )

    base = value["base_experiment"]
    if not isinstance(base, Mapping):
        raise StudyPlanError(
            "invalid_plan",
            f"base_experiment must be an object; got {type(base).__name__}",
        )
    try:
        validate_experiment_document(base)
    except ValueError as exc:
        raise StudyPlanError(
            "invalid_plan",
            f"base_experiment failed experiment schema validation: {exc}",
        ) from exc
    base_copy = copy.deepcopy(dict(base))

    plan = value["plan"]
    if kind == "parameter_sweep":
        normalized_plan, points = _parse_parameter_sweep(
            plan, base=base_copy, output_dir=output_dir, study_id=study_id
        )
    elif kind == "monte_carlo":
        normalized_plan, points = _parse_monte_carlo(
            plan, base=base_copy, output_dir=output_dir, study_id=study_id
        )
    else:
        normalized_plan, points = _parse_periodicity(
            plan, base=base_copy, output_dir=output_dir, study_id=study_id
        )

    limitations = {
        "aggregate_does_not_inherit_comparable",
        "descriptive_statistics_only_no_loss_claim",
    }
    if kind == "monte_carlo":
        limitations.add("monte_carlo_draws_depend_on_declared_distributions")
    if kind == "periodicity":
        limitations.add("periodicity_convergence_is_tolerance_based")

    return StudyPlan(
        study_id=study_id,
        kind=kind,
        base_experiment=base_copy,
        plan=normalized_plan,
        output_dir=output_dir,
        points=points,
        limitations=tuple(sorted(limitations)),
    )


# ---------------------------------------------------------------------------
# Monte Carlo sampling
# ---------------------------------------------------------------------------


def monte_carlo_draws(
    parameters: Sequence[Mapping[str, Any]],
    samples: int,
    seed: int,
) -> tuple[dict[str, float], ...]:
    """Return deterministic draws in sample-major, parameter-major order.

    Only ``random.Random(seed).random()`` is consumed.  A normal draw consumes
    two uniforms and applies the local Box-Muller transform::

        u1 = 1 - random()
        u2 = random()
        z  = sqrt(-2 * ln(u1)) * cos(2 * pi * u2)
        x  = mean + sigma * z

    ``u1 = 1 - random()`` keeps ``ln`` away from zero.  Changing the order of
    the parameter table changes the draws, so the plan records that order.
    """

    normalized = _normalize_mc_parameters(parameters)
    sample_count = _samples_in_range(samples)
    seed_value = _seed_value(seed)
    rng = random.Random(seed_value)

    draws: list[dict[str, float]] = []
    for _ in range(sample_count):
        draw: dict[str, float] = {}
        for entry in normalized:
            if entry["distribution"] == "uniform":
                value = entry["low"] + (
                    entry["high"] - entry["low"]
                ) * rng.random()
            else:
                u1 = 1.0 - rng.random()
                u2 = rng.random()
                z = math.sqrt(-2.0 * math.log(u1)) * math.cos(
                    2.0 * math.pi * u2
                )
                value = entry["mean"] + entry["sigma"] * z
            if not math.isfinite(value):
                # A draw that overflows (for example ``sigma=1e308``) would
                # otherwise become ``inf`` in the point document, pass the
                # planning layer, and crash only when the config is written.
                raise StudyPlanError(
                    "invalid_draw",
                    f"monte carlo draw for path {entry['path']!r} is not "
                    f"finite: {value!r}; a distribution parameter such as "
                    "sigma is too large",
                )
            draw[entry["path"]] = float(value)
        draws.append(draw)
    return tuple(draws)


def monte_carlo_draws_sha256(
    draws: Sequence[Mapping[str, float]],
) -> str:
    """Return the SHA-256 of the canonical JSON encoding of ``draws``."""

    if isinstance(draws, (str, bytes)) or not isinstance(draws, Sequence):
        raise StudyPlanError(
            "invalid_plan",
            "draws must be a sequence of mappings",
        )
    payload: list[dict[str, Any]] = []
    for position, draw in enumerate(draws):
        if not isinstance(draw, Mapping):
            raise StudyPlanError(
                "invalid_plan",
                f"draws[{position}] must be an object",
            )
        payload.append(dict(draw))
    try:
        text = _canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise StudyPlanError(
            "invalid_plan",
            f"draws are not canonical JSON with finite numbers: {exc}",
        ) from exc
    return _sha256_text(text)


# ---------------------------------------------------------------------------
# Periodicity evaluation
# ---------------------------------------------------------------------------


def _sample_number(row: Any, key: str, row_index: int) -> float:
    if not isinstance(row, Mapping) or key not in row:
        raise StudyPlanError(
            "missing_sample_key",
            f"sample row {row_index} is missing key {key!r}",
        )
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudyPlanError(
            "missing_sample_key",
            f"sample row {row_index} key {key!r} must be a finite number; "
            f"got {value!r}",
        )
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise StudyPlanError(
            "missing_sample_key",
            f"sample row {row_index} key {key!r} is not a finite number",
        ) from None
    if not math.isfinite(number):
        raise StudyPlanError(
            "missing_sample_key",
            f"sample row {row_index} key {key!r} must be finite; got {value!r}",
        )
    return number


def evaluate_periodicity(
    samples: Sequence[Mapping[str, Any]],
    *,
    signals: Sequence[str],
    compare_period: int,
    compare_windows: int,
    tolerance_relative: float,
) -> dict[str, Any]:
    """Compare trailing window pairs separated by an **index** lag.

    The comparison is descriptive and tolerance-based: it says whether the
    recorded samples repeat within the declared relative tolerance, not
    whether a physical steady state was reached.  ``compare_period`` is an
    index lag (a number of sample rows), never a time lag.

    Every input row is validated, not only the trailing window: a missing or
    non-finite ``time_s`` or signal fails closed with ``missing_sample_key``,
    and too few rows fail with ``insufficient_samples``.  The trailing window
    keeps ``compare_period * compare_windows + 1`` samples and reports
    ``compared_pairs = compare_period * (compare_windows - 1) + 1`` pairs.

    ``sampling_interval_s`` is the median ``time_s`` step when every row has
    a finite ``time_s`` and the sequence is strictly increasing; otherwise it
    is ``None``.
    """

    signal_names = _parse_signals(signals)
    period = _integer_in_range(compare_period, "compare_period", 1, 64)
    windows = _integer_in_range(compare_windows, "compare_windows", 2, 32)
    tolerance = _finite_number(
        tolerance_relative, "tolerance_relative", "invalid_plan"
    )
    if tolerance <= 0.0:
        raise StudyPlanError(
            "invalid_plan",
            f"tolerance_relative must be finite and > 0; got {tolerance_relative!r}",
        )
    if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise StudyPlanError(
            "invalid_plan",
            "samples must be a sequence of mappings",
        )

    rows = list(samples)
    required = windows * period + 1
    if len(rows) < required:
        raise StudyPlanError(
            "insufficient_samples",
            f"periodicity comparison needs at least {required} samples "
            f"(compare_windows={windows} * compare_period={period} + 1); "
            f"got {len(rows)}",
        )

    times: list[float] = []
    values: dict[str, list[float]] = {signal: [] for signal in signal_names}
    for row_index, row in enumerate(rows):
        times.append(_sample_number(row, "time_s", row_index))
        for signal in signal_names:
            values[signal].append(_sample_number(row, signal, row_index))

    tail = {
        signal: signal_values[-required:]
        for signal, signal_values in values.items()
    }
    pair_count = required - period
    signal_differences: dict[str, float] = {}
    for signal in signal_names:
        maximum = 0.0
        signal_values = tail[signal]
        for index in range(pair_count):
            first = signal_values[index]
            second = signal_values[index + period]
            difference = abs(first - second) / max(
                abs(first), abs(second), _RELATIVE_DIFFERENCE_FLOOR
            )
            if difference > maximum:
                maximum = difference
        signal_differences[signal] = float(maximum)

    worst = max(signal_differences.values(), default=0.0)
    return {
        "converged": all(
            difference <= tolerance
            for difference in signal_differences.values()
        ),
        "worst_relative_difference": float(worst),
        "tolerance_relative": float(tolerance),
        "signal_differences": signal_differences,
        "compared_pairs": pair_count,
        "windows_examined": windows,
        "sampling_interval_s": _sampling_interval(times),
        "method": _PERIODICITY_METHOD,
    }


def _sampling_interval(times: Sequence[float]) -> float | None:
    """Return the median time step, or ``None`` if the grid is not uniform."""

    steps = [second - first for first, second in zip(times, times[1:])]
    if not steps:
        return None
    if any(not math.isfinite(step) or step <= 0.0 for step in steps):
        return None
    ordered = sorted(steps)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


# ---------------------------------------------------------------------------
# Descriptive statistics
# ---------------------------------------------------------------------------


def _sample_column(
    rows: Sequence[Any], key: str
) -> list[float]:
    return [
        _sample_number(row, key, row_index)
        for row_index, row in enumerate(rows)
    ]


def statistics_for_point(
    samples: Sequence[Mapping[str, Any]],
    statistics: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """Compute descriptive statistics for one point's samples."""

    parsed = _parse_statistics(
        statistics,
        max_items=None,
        shape_reason="invalid_statistic",
    )
    if not parsed:
        return {}
    if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise StudyPlanError(
            "invalid_plan",
            "samples must be a sequence of mappings",
        )
    rows = list(samples)
    if not rows:
        raise StudyPlanError(
            "invalid_statistic",
            "samples must contain at least one row to compute statistics",
        )

    result: dict[str, float] = {}
    for statistic in parsed:
        reducer = statistic["reducer"]
        if reducer == "ratio_of_means":
            numerator = _sample_column(rows, statistic["numerator"])
            denominator = _sample_column(rows, statistic["denominator"])
            denominator_mean = sum(denominator) / len(denominator)
            if denominator_mean == 0.0:
                raise StudyPlanError(
                    "invalid_statistic",
                    f"statistic {statistic['name']!r} has a zero denominator mean",
                )
            value = (sum(numerator) / len(numerator)) / denominator_mean
        else:
            values = _sample_column(rows, statistic["sample_key"])
            if reducer == "mean":
                value = sum(values) / len(values)
            elif reducer == "min":
                value = min(values)
            elif reducer == "max":
                value = max(values)
            elif reducer == "rms":
                value = math.sqrt(
                    sum(item * item for item in values) / len(values)
                )
            elif reducer == "peak_to_peak":
                value = max(values) - min(values)
            else:  # reducer == "last"
                value = values[-1]
        if not math.isfinite(float(value)):
            raise StudyPlanError(
                "invalid_statistic",
                f"statistic {statistic['name']!r} produced a non-finite value",
            )
        result[statistic["name"]] = float(value)
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyPointRecord:
    index: int
    run_id: str
    run_dir: str
    parameters: Mapping[str, Any]
    manifest: Mapping[str, Any]
    statistics: Mapping[str, float] | None = None
    details: Mapping[str, Any] | None = None


def _identity(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _validate_extra_added(extra_added: Any) -> tuple[str, ...]:
    if extra_added is None:
        return ()
    if isinstance(extra_added, (str, bytes)):
        raise ValueError(
            "extra_added must be a sequence of strings, not a single string"
        )
    if not isinstance(extra_added, Sequence):
        raise ValueError("extra_added must be a sequence of strings")
    validated: list[str] = []
    for item in extra_added:
        if (
            not isinstance(item, str)
            or item in STUDY_LIMITATIONS
            or item not in _EXTRA_ADDED_ALLOWED
        ):
            raise ValueError(
                f"extra_added contains unsupported value {item!r}; "
                f"allowed values: {sorted(_EXTRA_ADDED_ALLOWED)}; "
                "limitation codes cannot be added here"
            )
        validated.append(item)
    return tuple(validated)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def aggregate_study(
    records: Sequence[StudyPointRecord],
    extra_added: Sequence[str] = (),
) -> dict[str, Any]:
    """Aggregate per-point records without ever claiming ``comparable``.

    ``inherited_ceiling`` is derived only from each point's *published*
    ``evidence_level``: an undeclared or unknown level counts as
    ``diagnostic_only`` for that ranking.  ``manifest.status`` is deliberately
    ignored when computing the ceiling; it only feeds the cross-point
    comparability decision.

    Cross-point comparability is exposed only through
    ``cross_point_comparability`` and ``comparability_reasons``.  It is
    claimed only when every point has a unique, non-empty ``run_id`` and
    ``index`` and a complete identity (non-empty string ``run_mode``,
    ``plant.id``, and all three route values).  The aggregate
    ``evidence_level`` stays ``per-point`` regardless.

    ``extra_added`` is an explicit declaration by the caller.  The function
    never infers a grid resolution, sampling method, or duration extension
    from point spacing.
    """

    record_list = list(records)
    extras = _validate_extra_added(extra_added)

    route_keys = ("time_model", "control_location", "event_granularity")
    levels: list[str | None] = []
    statuses: list[Any] = []
    routes: list[tuple[Any, Any, Any] | None] = []
    route_declared: list[bool] = []
    run_modes: list[Any] = []
    run_mode_declared: list[bool] = []
    plant_ids: list[Any] = []
    plant_id_declared: list[bool] = []
    run_ids: list[Any] = []
    indices: list[Any] = []
    identity_incomplete = False
    counts = {level: 0 for level in RUNTIME_EVIDENCE_LEVELS}
    undeclared_count = 0

    for record in record_list:
        run_ids.append(record.run_id)
        indices.append(record.index)
        manifest = record.manifest if isinstance(record.manifest, Mapping) else {}
        level = manifest.get("evidence_level")
        if level not in RUNTIME_EVIDENCE_LEVELS:
            level = None
            undeclared_count += 1
        else:
            counts[level] += 1
        levels.append(level)
        statuses.append(manifest.get("status"))

        route = manifest.get("route")
        if isinstance(route, Mapping) and all(
            key in route and route[key] is not None for key in route_keys
        ):
            routes.append(
                tuple(_identity(route[key]) for key in route_keys)
            )
            route_declared.append(True)
        else:
            routes.append(None)
            route_declared.append(False)
        if not (
            isinstance(route, Mapping)
            and all(_nonempty_string(route.get(key)) for key in route_keys)
        ):
            identity_incomplete = True

        run_mode = manifest.get("run_mode")
        run_modes.append(_identity(run_mode))
        run_mode_declared.append("run_mode" in manifest and run_mode is not None)
        if not _nonempty_string(run_mode):
            identity_incomplete = True

        plant = manifest.get("plant")
        plant_id = plant.get("id") if isinstance(plant, Mapping) else None
        plant_ids.append(_identity(plant_id))
        plant_id_declared.append(
            isinstance(plant, Mapping) and "id" in plant and plant_id is not None
        )
        if not _nonempty_string(plant_id):
            identity_incomplete = True

    point_evidence_levels = {
        level: counts[level]
        for level in RUNTIME_EVIDENCE_LEVELS
        if counts[level]
    }
    if undeclared_count:
        point_evidence_levels[_UNDECLARED] = undeclared_count

    if levels:
        inherited_rank = min(
            _EVIDENCE_RANK[level] if level is not None else 0
            for level in levels
        )
        inherited_ceiling = RUNTIME_EVIDENCE_LEVELS[inherited_rank]
    else:
        inherited_ceiling = RUNTIME_EVIDENCE_LEVELS[0]

    duplicate_points = (
        not all(_nonempty_string(run_id) for run_id in run_ids)
        or len({_identity(run_id) for run_id in run_ids}) != len(run_ids)
        or len({_identity(index) for index in indices}) != len(indices)
    )

    claimed = (
        len(record_list) >= 2
        and not duplicate_points
        and not identity_incomplete
        and all(status == "QUALIFIED" for status in statuses)
        and all(level == "comparable" for level in levels)
        and all(run_mode_declared)
        and all(plant_id_declared)
        and len(set(run_modes)) == 1
        and len(set(plant_ids)) == 1
        and all(route_declared)
        and len(set(routes)) == 1
    )

    reasons: set[str] = set()
    if not record_list:
        reasons.add("empty_study")
    elif len(record_list) == 1:
        reasons.add("single_point_study")
    if duplicate_points:
        reasons.add("duplicate_points")
    if identity_incomplete:
        reasons.add("point_identity_incomplete")
    if any(status != "QUALIFIED" for status in statuses):
        reasons.add("point_not_qualified")
    if any(level != "comparable" for level in levels):
        reasons.add("points_not_all_comparable")
    if not all(route_declared):
        reasons.add("point_route_not_declared")
    if (
        not all(run_mode_declared)
        or not all(plant_id_declared)
        or len(set(run_modes)) > 1
        or len(set(plant_ids)) > 1
        or len(set(routes)) > 1
    ):
        reasons.add("route_identity_differs")
    if undeclared_count:
        reasons.add("point_evidence_level_not_declared")
    if claimed:
        reasons = set()

    added = {
        "aggregation_over_points",
        "sampling_over_points",
    }
    added.update(extras)
    if any(record.statistics for record in record_list):
        added.add("descriptive_statistics")

    return {
        "schema": STUDY_AGGREGATE_SCHEMA,
        "point_count": len(record_list),
        "evidence_level": STUDY_POINT_EVIDENCE_LEVEL,
        "inherited_ceiling": inherited_ceiling,
        "point_evidence_levels": point_evidence_levels,
        "added_by_aggregation": sorted(added),
        "cross_point_comparability": "claimed" if claimed else "not_claimed",
        "comparability_reasons": sorted(reasons),
        "limitations": ["aggregate_does_not_inherit_comparable"],
    }


# ---------------------------------------------------------------------------
# Study report
# ---------------------------------------------------------------------------


def _assert_json_round_trip(report: Mapping[str, Any]) -> None:
    try:
        decoded = json.loads(
            json.dumps(report, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise StudyPlanError(
            "invalid_report",
            "study report is not JSON-serializable with finite numbers: "
            f"{exc}",
        ) from exc
    if decoded != report:
        raise StudyPlanError(
            "invalid_report",
            "study report does not round-trip through JSON; for example a "
            "tuple became a list",
        )


def build_study_report(
    plan: StudyPlan,
    records: Sequence[StudyPointRecord],
    *,
    kind_result: Mapping[str, Any] | None = None,
    created_utc: str | None = None,
    extra_added: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the JSON report for one study from per-point records."""

    record_list = sorted(list(records), key=lambda record: record.index)
    if created_utc is None:
        created_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not isinstance(created_utc, str) or not created_utc:
        raise StudyPlanError(
            "invalid_report",
            f"created_utc must be a non-empty string; got {created_utc!r}",
        )
    if kind_result is None:
        kind_result_value: dict[str, Any] = {}
    elif isinstance(kind_result, Mapping):
        kind_result_value = copy.deepcopy(dict(kind_result))
    else:
        raise StudyPlanError(
            "invalid_report",
            "kind_result must be a mapping or None; "
            f"got {type(kind_result).__name__}",
        )

    try:
        base_sha = _sha256_text(_canonical_json(plan.base_experiment))
    except (TypeError, ValueError) as exc:
        raise StudyPlanError(
            "invalid_report",
            f"base_experiment is not canonical JSON: {exc}",
        ) from exc

    points: list[dict[str, Any]] = []
    for record in record_list:
        manifest = record.manifest if isinstance(record.manifest, Mapping) else {}
        try:
            parameters = copy.deepcopy(dict(record.parameters))
            statistics = (
                copy.deepcopy(dict(record.statistics))
                if record.statistics is not None
                else None
            )
            details = (
                copy.deepcopy(dict(record.details))
                if record.details is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise StudyPlanError(
                "invalid_report",
                f"point {record.index} record is not canonical JSON: {exc}",
            ) from exc
        points.append(
            {
                "index": record.index,
                "label": f"p{record.index:02d}",
                "run_id": record.run_id,
                "run_dir": record.run_dir,
                "parameters": parameters,
                # The published manifest owns its identity digest; recomputing
                # one here would not be the digest that consumers verify.
                "manifest_sha256": manifest.get("manifest_sha256"),
                "status": manifest.get("status"),
                "evidence_level": manifest.get("evidence_level"),
                "statistics": statistics,
                "details": details,
            }
        )

    limitations = sorted(
        set(plan.limitations) | {"aggregate_does_not_inherit_comparable"}
    )
    report: dict[str, Any] = {
        "schema": STUDY_REPORT_SCHEMA,
        "study_id": plan.study_id,
        "kind": plan.kind,
        "plan": copy.deepcopy(dict(plan.plan)),
        "base": {"experiment_sha256": base_sha},
        "points": points,
        "aggregate": aggregate_study(record_list, extra_added=extra_added),
        "kind_result": kind_result_value,
        "limitations": limitations,
        "created_utc": created_utc,
    }
    _assert_json_round_trip(report)
    return report


def write_study_report(
    report: Mapping[str, Any], directory: str | Path
) -> Path:
    """Atomically write ``study-report.json`` under ``directory``."""

    if not isinstance(report, Mapping):
        raise StudyPlanError(
            "invalid_report",
            f"study report must be an object; got {type(report).__name__}",
        )
    report_value = dict(report)
    _assert_json_round_trip(report_value)
    try:
        text = (
            json.dumps(
                report_value,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise StudyPlanError(
            "invalid_report",
            "study report is not JSON-serializable with finite numbers: "
            f"{exc}",
        ) from exc

    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _REPORT_FILENAME
    # Two concurrent studies may publish into the same directory; a shared
    # temp name would let one process replace the other's half-written file.
    temporary = target_dir / f"{_REPORT_FILENAME}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


__all__ = [
    "MAX_STUDY_POINTS",
    "RUNTIME_EVIDENCE_LEVELS",
    "STATISTIC_REDUCERS",
    "STUDY_AGGREGATE_SCHEMA",
    "STUDY_COMPARABILITY_REASONS",
    "STUDY_KINDS",
    "STUDY_LIMITATIONS",
    "STUDY_POINT_EVIDENCE_LEVEL",
    "STUDY_REPORT_SCHEMA",
    "STUDY_SCHEMA",
    "StudyPlan",
    "StudyPlanError",
    "StudyPoint",
    "StudyPointRecord",
    "aggregate_study",
    "build_study_report",
    "evaluate_periodicity",
    "monte_carlo_draws",
    "monte_carlo_draws_sha256",
    "parse_study_document",
    "statistics_for_point",
    "write_study_report",
]
