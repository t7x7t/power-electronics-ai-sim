"""Read-only parameter view of one published run package.

The view is a projection of ``manifest.json``.  It never imports a Plant or a
Controller, never reads source, and never executes a solver.  Every value,
unit, and source is copied from the recorded package; nothing is rounded,
derived, or inferred from a field name.

A published package is immutable, so the view is rebuilt on demand and never
written back into it.  The parameter-set digest identifies one recorded
parameter set; it is not a model version, a physical-validity claim, or a
safety review.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from .inspection import RunInspectionError, inspect_run
from .jsonutil import stable_sha256


#: Schema tag for the read-only view returned by ``pe-sim parameter-view``.
PARAMETER_VIEW_SCHEMA = "pe-sim.parameter-view.v1"

#: Rule version recorded with every view.
PARAMETER_VIEW_RULE_VERSION = "parameter-view-v1"

#: Fixed order of the groups whose source fields the contract names.  Solver
#: groups follow these, sorted by backend name.
BASE_GROUP_ORDER = ("plant", "controller", "timebase", "action_policy")

_LIMITATIONS = (
    "Values are projected from the recorded manifest; no Plant, controller, or "
    "solver is imported or executed to produce this view.",
    "Units and sources are reported only where a recorded applicability finding "
    "names the parameter; the reader does not infer them from field names.",
    "No parameter range is declared in a 0.2 manifest, so every row range is "
    "null and bounds lists only declared action-policy limits.",
    "The parameter-set digest identifies one recorded parameter set; it is not "
    "a model version, a physical-validity claim, or a safety review.",
)


class ParameterViewError(ValueError):
    """Raised when a run package cannot be projected into a parameter view."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    """Return a recorded text annotation, never a coerced one."""

    return value if isinstance(value, str) and value else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _scalar(value: Any, *, group_id: str, name: str) -> Any:
    """Return a recorded JSON scalar or refuse to drop it silently."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ParameterViewError(
        f"parameter {name!r} in group {group_id!r} is not a JSON scalar: "
        f"{type(value).__name__}"
    )


def _parameter_annotations(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index per-parameter annotations recorded by applicability findings.

    Only findings whose evidence is a mapping with a string ``parameter`` key
    annotate a row.  The first finding in recorded order wins, so a later
    finding never overwrites an earlier recorded unit or source.
    """

    annotations: dict[str, dict[str, Any]] = {}
    checks = _mapping(_mapping(manifest.get("applicability")).get("checks"))
    findings = checks.get("findings")
    if not isinstance(findings, list):
        return annotations
    for finding in findings:
        evidence = _mapping(_mapping(finding).get("evidence"))
        name = evidence.get("parameter")
        if not isinstance(name, str) or not name or name in annotations:
            continue
        annotations[name] = {
            "unit": _optional_text(evidence.get("unit")),
            "source": _optional_text(evidence.get("source")),
            "review_status": _optional_text(evidence.get("review_status")),
        }
    return annotations


def _make_rows(
    values: Mapping[str, Any],
    *,
    group_id: str,
    annotations: Mapping[str, Mapping[str, Any]],
    default_unit: str | None = None,
    unit_for_numbers_only: bool = False,
) -> list[dict[str, Any]]:
    """Build the sorted parameter rows of one group."""

    rows: list[dict[str, Any]] = []
    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str):
            raise ParameterViewError(
                f"group {group_id!r} has a non-string parameter name: {raw_name!r}"
            )
        value = _scalar(raw_value, group_id=group_id, name=raw_name)
        annotation = annotations.get(raw_name)
        unit = annotation["unit"] if annotation is not None else None
        if unit is None and (not unit_for_numbers_only or _is_number(value)):
            unit = default_unit
        rows.append(
            {
                "name": raw_name,
                "value": value,
                "unit": unit,
                "source": annotation["source"] if annotation is not None else None,
                "review_status": (
                    annotation["review_status"] if annotation is not None else None
                ),
                "provenance_source": (
                    "applicability_finding" if annotation is not None else "not_recorded"
                ),
                "range": None,
            }
        )
    rows.sort(key=lambda row: row["name"])
    return rows


def _plant_group(
    manifest: Mapping[str, Any], annotations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    plant = _mapping(manifest.get("plant"))
    identity = _mapping(plant.get("identity"))
    parameters = identity.get("parameters")
    if not isinstance(parameters, Mapping) or not parameters:
        return None
    applicability = manifest.get("applicability")
    provenance = (
        {
            "status": applicability.get("parameter_provenance_status"),
            "sha256": applicability.get("parameter_provenance_sha256"),
        }
        if isinstance(applicability, Mapping)
        else None
    )
    return {
        "id": "plant",
        "component": {
            "id": plant.get("id") or identity.get("topology"),
            "kind": identity.get("kind"),
            "level": identity.get("level"),
            "identity_sha256": plant.get("hash"),
        },
        "provenance": provenance,
        "parameters": _make_rows(
            parameters, group_id="plant", annotations=annotations
        ),
    }


def _controller_group(
    manifest: Mapping[str, Any], annotations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    controller = _mapping(manifest.get("controller"))
    identity = _mapping(controller.get("identity"))
    values = dict(_mapping(identity.get("parameters")))
    # The runner records the effective configuration next to the identity; a
    # top-level copy is accepted too.  The run configuration is what actually
    # configured this run, so it wins over an identity default on a collision.
    for candidate in (
        controller.get("run_configuration"),
        identity.get("run_configuration"),
    ):
        if isinstance(candidate, Mapping):
            values.update(candidate)
    if not values:
        return None
    return {
        "id": "controller",
        "component": {
            "id": controller.get("id"),
            "kind": identity.get("kind"),
            "class": identity.get("class"),
        },
        "provenance": None,
        "parameters": _make_rows(
            values, group_id="controller", annotations=annotations
        ),
    }


def _timebase_group(
    manifest: Mapping[str, Any], annotations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    timebase = _mapping(manifest.get("timebase"))
    values = {name: value for name, value in timebase.items() if name != "unit"}
    if not values:
        return None
    return {
        "id": "timebase",
        "component": None,
        "provenance": None,
        # The unit is a property of the timebase, not of a row: only the
        # numeric rows inherit it, and the declaration is never copied into
        # the rows as a parameter of its own.
        "parameters": _make_rows(
            values,
            group_id="timebase",
            annotations=annotations,
            default_unit=_optional_text(timebase.get("unit")),
            unit_for_numbers_only=True,
        ),
    }


def _action_policy_identity(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(manifest.get("action_policy")).get("identity"))


def _action_policy_group(
    manifest: Mapping[str, Any], annotations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    identity = _action_policy_identity(manifest)
    values = {name: identity[name] for name in ("minimum", "maximum") if name in identity}
    if not values:
        return None
    return {
        "id": "action_policy",
        "component": {"name": identity.get("name"), "module": identity.get("module")},
        "provenance": None,
        "parameters": _make_rows(
            values, group_id="action_policy", annotations=annotations
        ),
    }


def _solver_groups(
    manifest: Mapping[str, Any], annotations: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    backends = _mapping(manifest.get("environment")).get("backends")
    if not isinstance(backends, list):
        return []
    selected: list[Mapping[str, Any]] = []
    for backend in backends:
        if not isinstance(backend, Mapping):
            raise ParameterViewError(
                "environment.backends contains a non-object entry"
            )
        name = backend.get("name")
        if not isinstance(name, str) or not name:
            raise ParameterViewError(
                "environment.backends has an entry without a usable backend name"
            )
        settings = backend.get("solver_settings")
        if settings is None or settings == {}:
            continue
        if not isinstance(settings, Mapping):
            raise ParameterViewError(
                f"solver_settings for backend {name!r} is not a mapping"
            )
        selected.append(backend)
    selected.sort(key=lambda item: str(item["name"]))
    groups: list[dict[str, Any]] = []
    for backend in selected:
        name = str(backend["name"])
        groups.append(
            {
                "id": "solver:" + name,
                "component": {
                    "name": name,
                    "version": backend.get("version"),
                    "status": backend.get("status"),
                },
                "provenance": None,
                "parameters": _make_rows(
                    _mapping(backend.get("solver_settings")),
                    group_id="solver:" + name,
                    annotations=annotations,
                ),
            }
        )
    return groups


def _bounds(
    manifest: Mapping[str, Any], *, policy_group_present: bool
) -> list[dict[str, Any]]:
    if not policy_group_present:
        return []
    identity = _action_policy_identity(manifest)
    minimum = identity.get("minimum")
    maximum = identity.get("maximum")
    if not (_is_finite_number(minimum) and _is_finite_number(maximum)):
        return []
    return [
        {
            "name": identity.get("name"),
            "minimum": minimum,
            "maximum": maximum,
            "source": "action_policy",
        }
    ]


def _qualification_section(
    manifest: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    checks = _mapping(_mapping(manifest.get("qualification")).get("checks"))
    findings = checks.get("findings")
    finding_list = findings if isinstance(findings, list) else []
    categories = sorted(
        {
            finding["category"]
            for finding in finding_list
            if isinstance(finding, Mapping)
            and isinstance(finding.get("category"), str)
            and finding["category"]
        }
    )
    return {
        "level": summary.get("qualification_level"),
        "finding_count": len(finding_list),
        "categories": categories,
    }


def _package_section(
    manifest: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    hashes = summary.get("hashes")
    hashes_map = hashes if isinstance(hashes, Mapping) else {}
    compatibility = summary.get("compatibility")
    compatibility_map = compatibility if isinstance(compatibility, Mapping) else {}
    return {
        "manifest_sha256": hashes_map.get("manifest_sha256")
        or manifest.get("manifest_sha256"),
        "package_sha256": hashes_map.get("package_sha256")
        or manifest.get("package_sha256"),
        "source_commit": manifest.get("source_commit"),
        "working_tree_status": manifest.get("working_tree_status"),
        "compatibility": {
            "status": compatibility_map.get("status"),
            "reader_schema": compatibility_map.get("reader_schema"),
            "package_schema": compatibility_map.get("package_schema"),
        },
    }


def build_parameter_view(run_dir: str | Path) -> dict[str, Any]:
    """Project one verified, published run package into the parameter view.

    Integrity is verified by :func:`pe_sim.inspection.inspect_run` first; a
    package that fails verification is reported as a :class:`ParameterViewError`
    instead of being projected.  The manifest is then read directly, because
    every displayed value must be copied from the recorded document.
    """

    try:
        inspection = inspect_run(Path(run_dir))
    except RunInspectionError as exc:
        raise ParameterViewError(str(exc)) from exc
    if inspection.status != "ok":
        detail = "; ".join(inspection.problems)
        raise ParameterViewError(
            f"run package integrity status is {inspection.status!r}"
            + (f": {detail}" if detail else "")
        )

    manifest_path = Path(inspection.run_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParameterViewError(
            f"manifest.json is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise ParameterViewError("manifest.json must be a JSON object")

    summary = inspection.summary
    annotations = _parameter_annotations(manifest)

    groups: list[dict[str, Any]] = []
    for group_id in BASE_GROUP_ORDER:
        builder = {
            "plant": _plant_group,
            "controller": _controller_group,
            "timebase": _timebase_group,
            "action_policy": _action_policy_group,
        }[group_id]
        group = builder(manifest, annotations)
        if group is not None:
            groups.append(group)
    groups.extend(_solver_groups(manifest, annotations))

    bounds = _bounds(
        manifest,
        policy_group_present=any(group["id"] == "action_policy" for group in groups),
    )
    all_rows = [row for group in groups for row in group["parameters"]]
    counts = {
        "groups": len(groups),
        "parameters": len(all_rows),
        "without_unit": sum(1 for row in all_rows if row["unit"] is None),
        "without_source": sum(1 for row in all_rows if row["source"] is None),
    }

    parameter_set: dict[str, Any] = {
        "digest": "",
        "groups": groups,
        "bounds": bounds,
        "counts": counts,
    }
    parameter_set["digest"] = parameter_set_digest(parameter_set)

    claim = summary.get("claim")
    if not isinstance(claim, Mapping):
        raise ParameterViewError("run inspection did not report a claim")

    return {
        "schema_version": PARAMETER_VIEW_SCHEMA,
        "rule_version": PARAMETER_VIEW_RULE_VERSION,
        "run": {
            "run_id": manifest.get("run_id"),
            "run_mode": manifest.get("run_mode"),
            "status": manifest.get("status"),
            "evidence_level": summary.get("evidence_level"),
            "qualification_level": summary.get("qualification_level"),
        },
        "package": _package_section(manifest, summary),
        "parameter_set": parameter_set,
        "qualification": _qualification_section(manifest, summary),
        "claim": dict(claim),
        "limitations": list(_LIMITATIONS),
    }


def _parameter_set_of(view_or_parameter_set: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(view_or_parameter_set, Mapping):
        raise ParameterViewError("a parameter view or parameter-set mapping is required")
    if "parameter_set" in view_or_parameter_set:
        parameter_set = view_or_parameter_set["parameter_set"]
    else:
        parameter_set = view_or_parameter_set
    if not isinstance(parameter_set, Mapping):
        raise ParameterViewError("parameter_set must be a mapping")
    return parameter_set


def _component_id(group_id: Any, component: Mapping[str, Any]) -> Any:
    if group_id in {"plant", "controller"}:
        return component.get("id")
    if group_id == "action_policy" or (
        isinstance(group_id, str) and group_id.startswith("solver:")
    ):
        return component.get("name")
    return None


def parameter_set_digest(view_or_parameter_set: Mapping[str, Any]) -> str:
    """Return the canonical digest of one recorded parameter set.

    The payload is exactly ``groups`` (id, component id, name/value/unit rows)
    and ``bounds`` (name/minimum/maximum).  Review status, source, and
    provenance are excluded because they describe where a number came from,
    not which number was used.  Rows are hashed in emitted order; the view
    sorts them deterministically, so two packages with the same recorded
    values produce the same digest.
    """

    parameter_set = _parameter_set_of(view_or_parameter_set)
    raw_groups = parameter_set.get("groups")
    if raw_groups is None:
        raw_groups = ()
    if not isinstance(raw_groups, (list, tuple)):
        raise ParameterViewError("parameter_set.groups must be an array")
    groups: list[dict[str, Any]] = []
    for group in raw_groups:
        if not isinstance(group, Mapping):
            raise ParameterViewError("parameter_set.groups entries must be objects")
        component = _mapping(group.get("component"))
        raw_rows = group.get("parameters")
        if raw_rows is None:
            raw_rows = ()
        if not isinstance(raw_rows, (list, tuple)):
            raise ParameterViewError("group parameters must be an array")
        rows: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, Mapping):
                raise ParameterViewError("parameter rows must be objects")
            rows.append(
                {
                    "name": row.get("name"),
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                }
            )
        group_id = group.get("id")
        groups.append(
            {
                "id": group_id,
                "component_id": _component_id(group_id, component),
                "parameters": rows,
            }
        )

    raw_bounds = parameter_set.get("bounds")
    if raw_bounds is None:
        raw_bounds = ()
    if not isinstance(raw_bounds, (list, tuple)):
        raise ParameterViewError("parameter_set.bounds must be an array")
    bounds: list[dict[str, Any]] = []
    for bound in raw_bounds:
        if not isinstance(bound, Mapping):
            raise ParameterViewError("parameter_set.bounds entries must be objects")
        bounds.append(
            {
                "name": bound.get("name"),
                "minimum": bound.get("minimum"),
                "maximum": bound.get("maximum"),
            }
        )
    return stable_sha256({"groups": groups, "bounds": bounds})


def parameter_rows(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten the view into confirmation rows, in emitted order."""

    parameter_set = _parameter_set_of(view)
    groups = parameter_set.get("groups")
    if not isinstance(groups, (list, tuple)):
        raise ParameterViewError("parameter_set.groups must be an array")
    rows: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            raise ParameterViewError("parameter_set.groups entries must be objects")
        group_id = group.get("id")
        parameters = group.get("parameters")
        if not isinstance(parameters, (list, tuple)):
            raise ParameterViewError("group parameters must be an array")
        for row in parameters:
            if not isinstance(row, Mapping):
                raise ParameterViewError("parameter rows must be objects")
            rows.append(
                {
                    "group": group_id,
                    "name": row.get("name"),
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                }
            )
    return rows


__all__ = [
    "BASE_GROUP_ORDER",
    "PARAMETER_VIEW_RULE_VERSION",
    "PARAMETER_VIEW_SCHEMA",
    "ParameterViewError",
    "build_parameter_view",
    "parameter_rows",
    "parameter_set_digest",
]
