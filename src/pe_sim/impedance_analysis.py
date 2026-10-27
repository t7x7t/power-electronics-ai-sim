"""Input/output impedance and the Middlebrook ratio, from one linearization.

Both quantities are small-signal ratios of the *same* analytic model that
:mod:`pe_sim.ac_analysis` sweeps.  Nothing here re-derives physics, runs a
Plant, or invents an evidence level: each result carries the model's
self-contained ``provenance`` block and states the sign convention it used.

Conventions
-----------
``load_current`` is a current *injected into the output node* (positive into
the node).  Injecting current raises the node voltage, so a passive output
impedance is ``Z_out = v_out / i_load`` with a positive real part at low
frequency.  (The common ``-Δv/Δi`` form assumes ``i`` is the current *drawn by*
the load; here the sign convention is stated explicitly instead of being
implied.)  ``input_current`` is the converter's own input current, so
``Z_in = v_in / i_in``.

What the Middlebrook ratio is not
---------------------------------
``criterion="satisfied"`` means the magnitude of the chosen impedance ratio
stays above unity at every sampled frequency.  That is a necessary screen, not
a stability proof: it says nothing about phase, about frequencies between the
samples, or about the converter's own loop.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .frequency_domain import (
    finite_number as _finite_number,
    log_frequencies,
    magnitude_series,
    unwrapped_phase_deg,
    validate_sweep,
)
from .model_capabilities import (
    SmallSignalModel,
    capability_provenance,
    validate_capability_provenance,
)


IMPEDANCE_SCHEMA = "pe-sim.impedance-analysis.v1"
IMPEDANCE_KINDS = ("input", "output")

_INPUT_VERIFIED_BY = (
    "tests/test_impedance_analysis.py::test_buck_input_impedance_matches_analytic_dc",
)
_OUTPUT_VERIFIED_BY = (
    "tests/test_impedance_analysis.py::test_buck_output_impedance_matches_parallel_expression",
)
_MIDDLEBROOK_VERIFIED_BY = (
    "tests/test_impedance_analysis.py::test_middlebrook_ratio_satisfied_and_violated",
)

_CRITERION_MEANING = (
    "the small-signal impedance-ratio magnitude stays above unity at every "
    "sampled frequency; a necessary screen, not a stability proof"
)


def _require_kind(kind: Any) -> str:
    if kind not in IMPEDANCE_KINDS:
        raise ValueError(
            f"kind must be one of {list(IMPEDANCE_KINDS)}, got {kind!r}"
        )
    return kind


def _require_model(model: Any) -> SmallSignalModel:
    if not isinstance(model, SmallSignalModel):
        raise TypeError("model must be a SmallSignalModel")
    return model


def _port(model: SmallSignalModel, name: str, *, role: str) -> str:
    declared = model.input_names if role == "input" else model.output_names
    if name not in declared:
        raise ValueError(
            f"model does not declare {role} {name!r}; declared names are "
            f"{list(declared)}"
        )
    return name


def _values(model: SmallSignalModel, frequencies: Sequence[float], kind: str) -> list[complex]:
    values: list[complex] = []
    for frequency in frequencies:
        if kind == "input":
            _port(model, "vin", role="input")
            _port(model, "input_current", role="output")
            admittance = model.transfer_function_for(
                frequency, output="input_current", input="vin"
            )
            if admittance == 0.0:
                raise ValueError(
                    "input admittance is zero at "
                    f"{frequency!r} Hz; the input impedance is undefined"
                )
            value = 1.0 / admittance
        else:
            _port(model, "load_current", role="input")
            _port(model, "vout", role="output")
            value = model.transfer_function_for(
                frequency, output="vout", input="load_current"
            )
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise ValueError(
                f"{kind} impedance is not finite at {frequency!r} Hz"
            )
        values.append(value)
    return values


def _sweep_block(start_hz: float, stop_hz: float, points: int) -> dict[str, Any]:
    return {
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        "points": points,
        "spacing": "log10",
    }


def impedance_response(
    model: SmallSignalModel,
    *,
    kind: str,
    start_hz: float,
    stop_hz: float,
    points: int,
) -> dict[str, Any]:
    """Return the input or output impedance of ``model`` over a log sweep."""

    model = _require_model(model)
    kind = _require_kind(kind)
    start, stop, count = validate_sweep(start_hz, stop_hz, points)
    frequencies = log_frequencies(start, stop, count)
    values = _values(model, frequencies, kind)

    magnitudes, magnitude_db = magnitude_series(
        values, frequencies, label=f"{kind} impedance"
    )
    assumptions = [
        value
        for value in model.assumptions
        if kind != "input"
        or value != "load_current_is_injected_at_the_output_node"
    ]
    if model.origin == "caller_registered":
        assumptions.append("capability_provider_is_caller_registered")
    if (
        kind == "output"
        and "load_current_is_injected_at_the_output_node" not in assumptions
    ):
        assumptions.append("load_current_is_injected_at_the_output_node")
    assumptions = _deduplicate(assumptions)
    sweep = _sweep_block(start, stop, count)
    verified_by = list(
        _INPUT_VERIFIED_BY if kind == "input" else _OUTPUT_VERIFIED_BY
    )

    return {
        "schema": IMPEDANCE_SCHEMA,
        "status": "available",
        "kind": f"{kind}_impedance",
        "plant_id": model.plant_id,
        "method": model.method,
        "assumptions": list(assumptions),
        "verified_by": verified_by,
        "plant_verified_by": model.verified_by,
        "convention": (
            "Zin = v_in / i_in"
            if kind == "input"
            else "Zout = v_out / i_load (load current injected into the output node)"
        ),
        "frequencies_hz": frequencies,
        "magnitude_ohm": magnitudes,
        "magnitude_db_ohm": magnitude_db,
        "phase_deg": unwrapped_phase_deg(values),
        "provenance": capability_provenance(
            model,
            sweep=sweep,
            verified_by=verified_by,
            assumptions=assumptions,
        ),
    }


def _deduplicate(values: Sequence[str]) -> list[str]:
    """Return ``values`` without duplicates, preserving first-seen order."""

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _require_frequency_axis(value: Any, label: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a sequence of finite numbers")
    frequencies = [
        _finite_number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    if not frequencies:
        raise ValueError(f"{label} must not be empty")
    if frequencies[0] <= 0.0:
        raise ValueError(f"{label} must contain strictly positive frequencies")
    for index, (lower, upper) in enumerate(zip(frequencies, frequencies[1:])):
        if not upper > lower:
            raise ValueError(
                f"{label} must be strictly increasing; index {index + 1} is "
                f"not greater than index {index}"
            )
    return frequencies


def _require_magnitude_axis(value: Any, label: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a sequence of numbers")
    magnitudes: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{label}[{index}] must be a number")
        number = float(item)
        if math.isfinite(number) and number < 0.0:
            raise ValueError(
                f"{label}[{index}] must not be negative; a magnitude is "
                f"non-negative, or non-finite when it cannot be evaluated, "
                f"got {number!r}"
            )
        magnitudes.append(number)
    if not magnitudes:
        raise ValueError(f"{label} must not be empty")
    return magnitudes


def _require_response(
    value: Any, label: str, *, require_provenance: bool
) -> tuple[Mapping[str, Any], list[float], list[float], dict[str, Any] | None]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping produced by impedance_response")
    if value.get("schema") != IMPEDANCE_SCHEMA:
        raise ValueError(
            f"{label}['schema'] must equal {IMPEDANCE_SCHEMA!r}, "
            f"got {value.get('schema')!r}"
        )
    if value.get("status") != "available":
        raise ValueError(
            f"{label}['status'] must be 'available', "
            f"got {value.get('status')!r}"
        )
    for key in ("frequencies_hz", "magnitude_ohm"):
        if key not in value:
            raise ValueError(f"{label} is missing {key!r}")
    frequencies = _require_frequency_axis(
        value["frequencies_hz"], f"{label}['frequencies_hz']"
    )
    magnitudes = _require_magnitude_axis(
        value["magnitude_ohm"], f"{label}['magnitude_ohm']"
    )
    provenance_value = value.get("provenance")
    if provenance_value is None:
        if require_provenance:
            raise ValueError(
                f"{label} must carry a provenance mapping; an impedance "
                "result without provenance cannot be used as the plant side"
            )
        provenance = None
    else:
        provenance = validate_capability_provenance(
            provenance_value, label=f"{label}['provenance']"
        )
    return value, frequencies, magnitudes, provenance


def _ratio_or_none(numerator: float, denominator: float) -> float | None:
    """Apply IEEE ratio semantics to one sampled point.

    ``+inf`` is a determinate pass (a finite positive numerator over a zero
    denominator), ``0.0`` is a determinate violation, and ``0/0`` or any
    non-finite operand is not evaluable.  A non-finite magnitude is therefore
    never allowed to pass the screen merely because the other side is zero.
    """

    if not math.isfinite(numerator) or not math.isfinite(denominator):
        return None
    if denominator == 0.0:
        if numerator > 0.0:
            return math.inf
        return None
    if numerator == 0.0:
        if denominator > 0.0:
            return 0.0
        return None
    return numerator / denominator


def _ratio_db(value: float) -> float:
    """Return ``20 log10(value)``, preserving the ``0 -> -inf`` limit."""

    if value == 0.0:
        return -math.inf
    if value > 0.0 and math.isfinite(value):
        return 20.0 * math.log10(value)
    if value > 0.0:
        return math.inf
    return -math.inf


def middlebrook_ratio(
    plant_impedance: Mapping[str, Any],
    external_impedance: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    """Compare a converter impedance with the impedance it works against.

    ``kind="input"`` compares ``Zin`` against a source impedance;
    ``kind="output"`` compares a load impedance against ``Zout``.  Both
    responses must share the same frequency axis and declare matching
    ``frequencies_hz``/``magnitude_ohm`` lengths, because a ratio computed on
    two different grids would silently compare different frequencies or drop
    samples.
    """

    kind = _require_kind(kind)
    plant, plant_frequencies, plant_magnitudes, plant_provenance = (
        _require_response(
            plant_impedance, "plant_impedance", require_provenance=True
        )
    )
    external, external_frequencies, external_magnitudes, external_provenance = (
        _require_response(
            external_impedance, "external_impedance", require_provenance=False
        )
    )

    expected_plant_kind = f"{kind}_impedance"
    if plant.get("kind") != expected_plant_kind:
        raise ValueError(
            f"plant_impedance['kind'] must be {expected_plant_kind!r} for "
            f"kind={kind!r}, got {plant.get('kind')!r}"
        )
    external_kind = external.get("kind")
    if not isinstance(external_kind, str) or not external_kind.strip():
        raise ValueError(
            "external_impedance['kind'] must be a non-empty string, got "
            f"{external_kind!r}"
        )

    lengths = (
        len(plant_frequencies),
        len(plant_magnitudes),
        len(external_frequencies),
        len(external_magnitudes),
    )
    if len(set(lengths)) != 1:
        raise ValueError(
            "plant_impedance and external_impedance must have matching "
            "frequencies_hz and magnitude_ohm lengths; got "
            f"plant frequencies={lengths[0]}, plant magnitude={lengths[1]}, "
            f"external frequencies={lengths[2]}, external magnitude={lengths[3]}"
        )
    if plant_frequencies != external_frequencies:
        raise ValueError(
            "plant_impedance and external_impedance must declare the same "
            "frequency axis; recompute both sweeps with the same range and "
            "point count"
        )

    ratios: list[float | None] = []
    for left, right in zip(plant_magnitudes, external_magnitudes):
        numerator, denominator = (
            (left, right) if kind == "input" else (right, left)
        )
        ratios.append(_ratio_or_none(numerator, denominator))

    evaluated = [
        (frequency, value)
        for frequency, value in zip(plant_frequencies, ratios)
        if value is not None
    ]
    undefined_count = len(ratios) - len(evaluated)

    notes: list[str] = []
    if undefined_count:
        notes.append("non_finite_ratio")
    min_ratio_scope = (
        "all_sampled_points" if undefined_count == 0 else "evaluable_subset"
    )
    if min_ratio_scope == "evaluable_subset":
        notes.append("minimum_covers_evaluable_points_only")

    min_ratio_db: float | None = None
    min_ratio_db_bound = "undefined"
    worst_frequency: float | None = None
    first_violation: float | None = None
    violation_count = 0
    if evaluated:
        decibels = [
            (frequency, _ratio_db(value)) for frequency, value in evaluated
        ]
        worst_frequency, worst_db = min(decibels, key=lambda item: item[1])
        # A zero ratio is a certain violation and its exact decibel value is
        # minus infinity, which JSON cannot carry.  Keep the number finite by
        # reporting the bound instead of writing an infinity into an artifact.
        if worst_db == -math.inf:
            min_ratio_db = None
            min_ratio_db_bound = "negative_infinity"
        elif worst_db == math.inf:
            min_ratio_db = None
            min_ratio_db_bound = "positive_infinity"
        else:
            min_ratio_db = worst_db
            min_ratio_db_bound = "finite"
        for frequency, value in evaluated:
            if value <= 1.0:
                violation_count += 1
                if first_violation is None:
                    first_violation = frequency

    if violation_count:
        criterion = "violated"
    elif undefined_count:
        criterion = "not_evaluable"
    else:
        criterion = "satisfied"

    inherited = dict(plant["provenance"])
    inherited_assumptions = _deduplicate(
        [
            value
            for value in plant_provenance["assumptions"]
            if kind != "input"
            or value != "load_current_is_injected_at_the_output_node"
        ]
    )
    if "external_impedance_is_caller_declared" not in inherited_assumptions:
        inherited_assumptions.append("external_impedance_is_caller_declared")
    inherited["assumptions"] = inherited_assumptions
    inherited["controller"] = None
    inherited["verified_by"] = list(_MIDDLEBROOK_VERIFIED_BY)

    if external_provenance is not None:
        external_plant_id = external_provenance["plant_id"]
        external_method = external_provenance["method"]
        external_assumptions = list(external_provenance["assumptions"])
        external_verified_by = list(external_provenance["verified_by"])
    else:
        external_plant_id = None
        external_method = None
        external_assumptions = []
        external_verified_by = []

    return {
        "schema": IMPEDANCE_SCHEMA,
        "status": "available",
        "kind": "middlebrook_ratio",
        "direction": kind,
        "criterion": criterion,
        "threshold_db": 0.0,
        "min_ratio_db": min_ratio_db,
        "min_ratio_db_bound": min_ratio_db_bound,
        "worst_frequency_hz": worst_frequency,
        "violation_count": violation_count,
        "first_violation_hz": first_violation,
        "total_points": len(plant_frequencies),
        "evaluated_points": len(evaluated),
        "min_ratio_scope": min_ratio_scope,
        "notes": notes,
        "criterion_meaning": _CRITERION_MEANING,
        "method": plant_provenance["method"],
        "assumptions": list(inherited_assumptions),
        "verified_by": list(_MIDDLEBROOK_VERIFIED_BY),
        "plant_verified_by": plant_provenance["plant_verified_by"],
        "external": {
            "kind": external_kind,
            "plant_id": external_plant_id,
            "method": external_method,
            "assumptions": external_assumptions,
            "verified_by": external_verified_by,
        },
        "provenance": inherited,
    }


__all__ = [
    "IMPEDANCE_KINDS",
    "IMPEDANCE_SCHEMA",
    "impedance_response",
    "middlebrook_ratio",
]


