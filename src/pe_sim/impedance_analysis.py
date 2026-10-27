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
    log_frequencies,
    magnitude_series,
    unwrapped_phase_deg,
    validate_sweep,
)
from .model_capabilities import SmallSignalModel, capability_provenance


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
    assumptions = list(model.assumptions)
    if (
        model.origin == "caller_registered"
        and "capability_provider_is_caller_registered" not in assumptions
    ):
        assumptions.append("capability_provider_is_caller_registered")
    assumptions.append("load_current_is_injected_at_the_output_node")
    sweep = _sweep_block(start, stop, count)

    return {
        "schema": IMPEDANCE_SCHEMA,
        "status": "available",
        "kind": f"{kind}_impedance",
        "plant_id": model.plant_id,
        "method": model.method,
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
            verified_by=(
                _INPUT_VERIFIED_BY if kind == "input" else _OUTPUT_VERIFIED_BY
            ),
            assumptions=assumptions,
        ),
    }


def _require_response(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping produced by impedance_response")
    if value.get("schema") != IMPEDANCE_SCHEMA or value.get("status") != "available":
        raise ValueError(f"{label} is not an available impedance result")
    for key in ("frequencies_hz", "magnitude_ohm"):
        if key not in value:
            raise ValueError(f"{label} is missing {key!r}")
    return value


def middlebrook_ratio(
    plant_impedance: Mapping[str, Any],
    external_impedance: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    """Compare a converter impedance with the impedance it works against.

    ``kind="input"`` compares ``Zin`` against a source impedance;
    ``kind="output"`` compares a load impedance against ``Zout``.  Both
    responses must share the same frequency axis, because a ratio computed on
    two different grids would silently compare different frequencies.
    """

    kind = _require_kind(kind)
    plant = _require_response(plant_impedance, "plant_impedance")
    external = _require_response(external_impedance, "external_impedance")

    plant_frequencies = list(plant["frequencies_hz"])
    external_frequencies = list(external["frequencies_hz"])
    if plant_frequencies != external_frequencies:
        raise ValueError(
            "plant_impedance and external_impedance must declare the same "
            "frequency axis; recompute both sweeps with the same range and "
            "point count"
        )

    plant_magnitudes = [float(value) for value in plant["magnitude_ohm"]]
    external_magnitudes = [float(value) for value in external["magnitude_ohm"]]

    ratios: list[float | None] = []
    for left, right in zip(plant_magnitudes, external_magnitudes):
        numerator, denominator = (
            (left, right) if kind == "input" else (right, left)
        )
        if (
            not math.isfinite(numerator)
            or not math.isfinite(denominator)
            or denominator == 0.0
            or numerator == 0.0
        ):
            ratios.append(None)
        else:
            ratios.append(numerator / denominator)

    notes: list[str] = []
    if any(value is None for value in ratios):
        notes.append("non_finite_ratio")

    usable = [
        (frequency, value)
        for frequency, value in zip(plant_frequencies, ratios)
        if value is not None
    ]
    min_ratio_db: float | None = None
    worst_frequency: float | None = None
    first_violation: float | None = None
    violation_count = 0
    if usable:
        decibels = [(frequency, 20.0 * math.log10(value)) for frequency, value in usable]
        worst_frequency, min_ratio_db = min(decibels, key=lambda item: item[1])
        for frequency, value in usable:
            if value <= 1.0:
                violation_count += 1
                if first_violation is None:
                    first_violation = frequency

    if any(value is None for value in ratios):
        criterion = "not_evaluable"
    elif violation_count:
        criterion = "violated"
    else:
        criterion = "satisfied"

    inherited = dict(plant.get("provenance") or {})
    inherited_assumptions = list(inherited.get("assumptions") or [])
    if "external_impedance_is_caller_declared" not in inherited_assumptions:
        inherited_assumptions.append("external_impedance_is_caller_declared")
    inherited["assumptions"] = inherited_assumptions
    inherited["controller"] = None
    inherited["verified_by"] = [_MIDDLEBROOK_VERIFIED_BY]

    return {
        "schema": IMPEDANCE_SCHEMA,
        "status": "available",
        "kind": "middlebrook_ratio",
        "direction": kind,
        "criterion": criterion,
        "threshold_db": 0.0,
        "min_ratio_db": min_ratio_db,
        "worst_frequency_hz": worst_frequency,
        "violation_count": violation_count,
        "first_violation_hz": first_violation,
        "notes": notes,
        "criterion_meaning": _CRITERION_MEANING,
        "provenance": inherited,
    }


__all__ = [
    "IMPEDANCE_KINDS",
    "IMPEDANCE_SCHEMA",
    "impedance_response",
    "middlebrook_ratio",
]


