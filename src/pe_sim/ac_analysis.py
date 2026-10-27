"""AC sweeps, loop gain, and stability margins over small-signal models.

This is the AC half of the ``docs/60`` C4 capability route.  It consumes a
:class:`pe_sim.model_capabilities.SmallSignalModel` and a controller that
declares discrete coefficients; it never runs a Plant, a Runner, or a solver.

Three boundaries are load-bearing:

* **A frequency sweep is not a simulation.**  The result is the frequency
  response of a declared linear model at one operating point.  It says nothing
  about large-signal behaviour, switching ripple, or a transient run.
* **Margins are numbers, not verdicts.**  :func:`stability_margins` reports
  crossover frequencies and signed margins.  It deliberately contains no
  ``stable`` / ``unstable`` field.  ``margin_positive`` is only a three-state
  sign at that linearization point under the declared sweep resolution:
  ``True`` when both margins are present and positive, ``False`` when both are
  present and at least one is non-positive, and ``None`` when a crossover was
  not found so the pair cannot be evaluated.  ``None`` must not be read as
  "stable".
* **The controller mapping is declared.**  ``loop_gain_response`` uses the
  Tustin (bilinear) continuous equivalent ``s = (2/T)(z-1)/(z+1)`` and records
  ``mapping`` plus the assumption ``tustin_continuous_equivalent``.  Sampling
  delay, zero-order hold phase, and discrete-time aliasing are **not** modeled;
  the assumption ``loop_gain_excludes_sampling_delay`` is recorded for the same
  reason.

``phase_deg`` is unwrapped across the sweep and uses ``atan2(Im, Re)`` with
degrees increasing counter-clockwise.  With this convention a first-order
integrator loop crosses 0 dB at ``-90 deg`` and therefore reports a phase
margin of ``+90 deg``.

Only the Python standard library is used.  The frequency grid is uniform in
``log10(f)``; every reported value is finite, and a zero or non-finite
magnitude raises ``ValueError`` instead of being clamped or reported as
``-inf`` dB.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .model_capabilities import CapabilityUnavailable, SmallSignalModel


AC_RESULT_SCHEMA = "pe-sim.ac-analysis.v1"

_MIN_POINTS = 2
_MAX_POINTS = 512
_CONTROLLER_REASON = "controller_transfer_function_not_declared"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _validate_sweep(start_hz: Any, stop_hz: Any, points: Any) -> tuple[float, float, int]:
    start = _finite_number(start_hz, "start_hz")
    if start <= 0.0:
        raise ValueError("start_hz must be finite and positive")
    stop = _finite_number(stop_hz, "stop_hz")
    if stop <= start:
        raise ValueError("stop_hz must be finite and greater than start_hz")
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError(
            f"points must be an integer in [{_MIN_POINTS}, {_MAX_POINTS}]"
        )
    if not _MIN_POINTS <= points <= _MAX_POINTS:
        raise ValueError(
            f"points must be an integer in [{_MIN_POINTS}, {_MAX_POINTS}], got {points}"
        )
    return start, stop, points


def _log_frequencies(start_hz: float, stop_hz: float, points: int) -> list[float]:
    if points == 2:
        return [start_hz, stop_hz]
    log_start = math.log10(start_hz)
    log_stop = math.log10(stop_hz)
    step = (log_stop - log_start) / (points - 1)
    frequencies = [10.0 ** (log_start + step * index) for index in range(points)]
    # Pin the endpoints so the declared sweep and the returned axis agree.
    frequencies[0] = start_hz
    frequencies[-1] = stop_hz
    return frequencies


def _complex_values(
    model: SmallSignalModel, frequencies: Sequence[float]
) -> list[complex]:
    values: list[complex] = []
    for frequency in frequencies:
        value = model.transfer_function(frequency)
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise ValueError(
                f"transfer function is not finite at {frequency!r} Hz"
            )
        values.append(value)
    return values


def _unwrapped_phase_deg(values: Sequence[complex]) -> list[float]:
    if not values:
        raise ValueError("at least one complex value is required")
    raw = [math.atan2(value.imag, value.real) for value in values]
    unwrapped = [raw[0]]
    for angle in raw[1:]:
        previous = unwrapped[-1]
        while angle - previous > math.pi:
            angle -= 2.0 * math.pi
        while angle - previous < -math.pi:
            angle += 2.0 * math.pi
        unwrapped.append(angle)
    phases = [math.degrees(angle) for angle in unwrapped]
    if not all(math.isfinite(phase) for phase in phases):
        raise ValueError("phase response contains a non-finite value")
    return phases


def _magnitude_series(values: Sequence[complex]) -> tuple[list[float], list[float]]:
    magnitudes: list[float] = []
    decibels: list[float] = []
    for value in values:
        magnitude = abs(value)
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            raise ValueError(
                "transfer function magnitude is zero or non-finite at a sweep "
                "point; a finite Bode magnitude cannot be reported"
            )
        magnitudes.append(magnitude)
        decibels.append(20.0 * math.log10(magnitude))
    return magnitudes, decibels


def _response_payload(
    model: SmallSignalModel,
    *,
    kind: str,
    start_hz: float,
    stop_hz: float,
    points: int,
    frequencies: list[float],
    values: Sequence[complex],
) -> dict[str, Any]:
    magnitudes, magnitude_db = _magnitude_series(values)
    return {
        "schema": AC_RESULT_SCHEMA,
        "status": "available",
        "kind": kind,
        "plant_id": model.plant_id,
        "method": model.method,
        "linearization_point": dict(model.operating_point),
        "assumptions": list(model.assumptions),
        "verified_by": model.verified_by,
        "sweep": {
            "start_hz": start_hz,
            "stop_hz": stop_hz,
            "points": points,
            "spacing": "log10",
        },
        "frequencies_hz": list(frequencies),
        "magnitude": magnitudes,
        "magnitude_db": magnitude_db,
        "phase_deg": _unwrapped_phase_deg(values),
    }


# ---------------------------------------------------------------------------
# Plant response
# ---------------------------------------------------------------------------


def frequency_response(
    model: SmallSignalModel,
    *,
    start_hz: float,
    stop_hz: float,
    points: int,
) -> dict[str, Any]:
    """Return the plant transfer function on a ``log10``-spaced frequency grid.

    ``magnitude`` is the linear gain and ``magnitude_db`` is
    ``20 log10(|G|)`` in absolute dB, **not** normalized to the DC gain.
    ``phase_deg`` is unwrapped.  Every value is finite; a singular model or a
    zero magnitude raises ``ValueError``.
    """

    if not isinstance(model, SmallSignalModel):
        raise TypeError("model must be a SmallSignalModel")
    start, stop, count = _validate_sweep(start_hz, stop_hz, points)
    frequencies = _log_frequencies(start, stop, count)
    values = _complex_values(model, frequencies)
    return _response_payload(
        model,
        kind="plant_transfer_function",
        start_hz=start,
        stop_hz=stop,
        points=count,
        frequencies=frequencies,
        values=values,
    )


# ---------------------------------------------------------------------------
# Controller / loop gain
# ---------------------------------------------------------------------------


def _coefficient_list(value: Any, label: str, *, allow_empty: bool = False) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a sequence of finite numbers")
    items = tuple(_finite_number(item, f"{label}[{index}]") for index, item in enumerate(value))
    if not items and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    return items


def _controller_polynomials(
    controller: Any,
) -> tuple[tuple[float, ...], tuple[float, ...], float]:
    """Return z-domain descending-power coefficients and the sample period.

    Accepted shapes:

    * ``{"numerator": [...], "denominator": [...]}`` -- both descending powers
      of ``z`` (the contract shape);
    * ``{"b": [...], "a": [...]}`` -- the difference-equation shape exposed by
      ``DiscreteCompensator``: ``b`` is ``b0..bN`` and ``a`` is ``a1..aN`` with
      an implicit leading ``1``.  Both lists are already descending powers of
      ``z`` once the implicit ``1`` is prepended to ``a``.

    A missing mapping or a missing ``sample_period_s`` is a capability gap and
    raises :class:`CapabilityUnavailable`.  A declared but malformed
    coefficient list is a caller error and raises ``ValueError``.
    """

    coefficients = getattr(controller, "coefficients", None)
    if not isinstance(coefficients, Mapping):
        raise CapabilityUnavailable(
            _CONTROLLER_REASON,
            f"controller {type(controller).__name__} does not declare a "
            "'coefficients' mapping; the loop gain cannot be evaluated",
        )
    sample_period = getattr(controller, "sample_period_s", None)
    if sample_period is None:
        raise CapabilityUnavailable(
            _CONTROLLER_REASON,
            f"controller {type(controller).__name__} does not declare "
            "'sample_period_s'; the Tustin equivalent cannot be evaluated",
        )
    sample_period = _finite_number(sample_period, "sample_period_s")
    if sample_period <= 0.0:
        raise ValueError("sample_period_s must be finite and positive")

    if "numerator" in coefficients or "denominator" in coefficients:
        if "numerator" not in coefficients or "denominator" not in coefficients:
            raise ValueError(
                "coefficients must declare both 'numerator' and 'denominator'"
            )
        numerator = _coefficient_list(
            coefficients["numerator"], "coefficients['numerator']"
        )
        denominator = _coefficient_list(
            coefficients["denominator"], "coefficients['denominator']"
        )
    elif "b" in coefficients or "a" in coefficients:
        if "b" not in coefficients or "a" not in coefficients:
            raise ValueError("coefficients must declare both 'b' and 'a'")
        numerator = _coefficient_list(coefficients["b"], "coefficients['b']")
        denominator = (
            1.0,
            *_coefficient_list(
                coefficients["a"], "coefficients['a']", allow_empty=True
            ),
        )
    else:
        raise CapabilityUnavailable(
            _CONTROLLER_REASON,
            f"controller {type(controller).__name__} declares coefficients "
            "without 'numerator'/'denominator' or 'b'/'a'",
        )

    leading = denominator[0]
    if leading == 0.0:
        raise ValueError("controller denominator has a zero leading coefficient")
    # Normalize both sides by the leading denominator coefficient so the
    # polynomial ratio is unchanged.
    numerator = tuple(value / leading for value in numerator)
    denominator = tuple(value / leading for value in denominator)
    if not all(math.isfinite(value) for value in numerator + denominator):
        raise ValueError("controller coefficients must remain finite after normalization")
    return numerator, denominator, sample_period


def _polyval_desc(coefficients: Sequence[float], z: complex) -> complex:
    value = 0j
    for coefficient in coefficients:
        value = value * z + coefficient
    return value


def _controller_value(
    numerator: Sequence[float],
    denominator: Sequence[float],
    sample_period: float,
    frequency_hz: float,
) -> complex:
    s = complex(0.0, 2.0 * math.pi * frequency_hz)
    z = (1.0 + s * sample_period / 2.0) / (1.0 - s * sample_period / 2.0)
    denominator_value = _polyval_desc(denominator, z)
    if denominator_value == 0.0:
        raise ValueError(
            f"controller transfer function is singular at {frequency_hz!r} Hz"
        )
    value = _polyval_desc(numerator, z) / denominator_value
    if not math.isfinite(value.real) or not math.isfinite(value.imag):
        raise ValueError(
            f"controller transfer function is not finite at {frequency_hz!r} Hz"
        )
    return value


def loop_gain_response(
    model: SmallSignalModel,
    controller: Any,
    *,
    start_hz: float,
    stop_hz: float,
    points: int,
) -> dict[str, Any]:
    """Return ``L(jw) = G_plant(jw) * C_tustin(jw)`` on the declared sweep.

    The controller's discrete coefficients are evaluated with the Tustin
    continuous equivalent ``s = (2/T)(z-1)/(z+1)``.  Sampling delay is excluded
    and the response records both facts in ``assumptions``.  ``controller``
    carries the class name, ``sample_period_s``, and the literal ``mapping``
    value so a reader can tell an analytic plant model from a mapped discrete
    controller.
    """

    if not isinstance(model, SmallSignalModel):
        raise TypeError("model must be a SmallSignalModel")
    start, stop, count = _validate_sweep(start_hz, stop_hz, points)
    numerator, denominator, sample_period = _controller_polynomials(controller)

    frequencies = _log_frequencies(start, stop, count)
    plant_values = _complex_values(model, frequencies)
    loop_values = [
        plant_value
        * _controller_value(numerator, denominator, sample_period, frequency)
        for plant_value, frequency in zip(plant_values, frequencies)
    ]

    payload = _response_payload(
        model,
        kind="loop_gain",
        start_hz=start,
        stop_hz=stop,
        points=count,
        frequencies=frequencies,
        values=loop_values,
    )
    assumptions = list(payload["assumptions"])
    for item in (
        "tustin_continuous_equivalent",
        "loop_gain_excludes_sampling_delay",
    ):
        if item not in assumptions:
            assumptions.append(item)
    payload["assumptions"] = assumptions
    payload["controller"] = {
        "class": type(controller).__name__,
        "sample_period_s": sample_period,
        "mapping": "tustin_continuous_equivalent",
    }
    return payload


# ---------------------------------------------------------------------------
# Stability margins
# ---------------------------------------------------------------------------


def _number_sequence(value: Any, label: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a sequence of finite numbers")
    return [
        _finite_number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


def _log_interpolate(low_hz: float, high_hz: float, fraction: float) -> float:
    return 10.0 ** (
        math.log10(low_hz)
        + fraction * (math.log10(high_hz) - math.log10(low_hz))
    )


def _interpolate_at(values: Sequence[float], index: int, fraction: float) -> float:
    if index >= len(values) - 1:
        return values[-1]
    return values[index] + fraction * (values[index + 1] - values[index])


def _first_crossing(
    frequencies: Sequence[float], values: Sequence[float], target: float
) -> tuple[float, float, int] | None:
    for index in range(len(values) - 1):
        low = values[index]
        high = values[index + 1]
        if low == target:
            return frequencies[index], 0.0, index
        if (low - target) * (high - target) < 0.0:
            fraction = (target - low) / (high - low)
            return _log_interpolate(frequencies[index], frequencies[index + 1], fraction), fraction, index
    if values[-1] == target:
        return frequencies[-1], 0.0, len(values) - 1
    return None


def _count_crossings(values: Sequence[float], target: float) -> int:
    count = 0
    for index in range(len(values) - 1):
        low = values[index]
        high = values[index + 1]
        if low == target:
            count += 1
        elif (low - target) * (high - target) < 0.0:
            count += 1
    if values[-1] == target:
        count += 1
    return count


def stability_margins(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return crossover frequencies and signed margins for a loop-gain sweep.

    Only ``kind == "loop_gain"`` responses are accepted.  Crossings are located
    by linear interpolation between adjacent samples: frequency is interpolated
    in ``log10``, magnitude in dB, and phase in degrees.  The **first**
    crossover in increasing frequency is reported; a note records when a second
    one exists.

    ``phase_margin_deg = 180 + phase_deg`` at the gain crossover and
    ``gain_margin_db = -magnitude_db`` at the ``-180 deg`` phase crossover,
    using the unwrapped phase produced by :func:`loop_gain_response`.

    ``margin_positive`` is a three-state sign, not a stability verdict: see the
    module docstring.  Missing crossings set the corresponding fields to
    ``None`` and add ``"no_gain_crossover"`` / ``"no_phase_crossover"`` to
    ``notes``.
    """

    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    if response.get("kind") != "loop_gain":
        raise ValueError(
            "stability_margins requires a response with kind='loop_gain'"
        )

    frequencies = _number_sequence(response.get("frequencies_hz"), "frequencies_hz")
    magnitude_db = _number_sequence(response.get("magnitude_db"), "magnitude_db")
    phases = _number_sequence(response.get("phase_deg"), "phase_deg")
    if len(frequencies) < 2:
        raise ValueError("frequencies_hz must contain at least two samples")
    if len(magnitude_db) != len(frequencies) or len(phases) != len(frequencies):
        raise ValueError(
            "frequencies_hz, magnitude_db, and phase_deg must have equal length"
        )
    if frequencies[0] <= 0.0:
        raise ValueError("frequencies_hz must be strictly positive")
    for lower, upper in zip(frequencies, frequencies[1:]):
        if not upper > lower:
            raise ValueError("frequencies_hz must be strictly increasing")

    method = response.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError("response['method'] must be a non-empty string")
    raw_assumptions = response.get("assumptions")
    if isinstance(raw_assumptions, (str, bytes)) or not isinstance(
        raw_assumptions, (list, tuple)
    ):
        raise ValueError("response['assumptions'] must be a sequence of strings")
    if any(not isinstance(item, str) for item in raw_assumptions):
        raise ValueError("response['assumptions'] must contain only strings")
    assumptions = list(raw_assumptions)

    notes: list[str] = []

    gain_crossing = _first_crossing(frequencies, magnitude_db, 0.0)
    if gain_crossing is None:
        gain_crossover_hz = None
        phase_margin_deg = None
        notes.append("no_gain_crossover")
    else:
        gain_crossover_hz, fraction, index = gain_crossing
        phase_at_crossover = _interpolate_at(phases, index, fraction)
        phase_margin_deg = 180.0 + phase_at_crossover
        if _count_crossings(magnitude_db, 0.0) > 1:
            notes.append("multiple_gain_crossovers_first_reported")

    phase_crossing = _first_crossing(frequencies, phases, -180.0)
    if phase_crossing is None:
        phase_crossover_hz = None
        gain_margin_db = None
        notes.append("no_phase_crossover")
    else:
        phase_crossover_hz, fraction, index = phase_crossing
        magnitude_at_crossover = _interpolate_at(magnitude_db, index, fraction)
        gain_margin_db = -magnitude_at_crossover
        if _count_crossings(phases, -180.0) > 1:
            notes.append("multiple_phase_crossovers_first_reported")

    if phase_margin_deg is None or gain_margin_db is None:
        margin_positive: bool | None = None
    else:
        margin_positive = phase_margin_deg > 0.0 and gain_margin_db > 0.0

    for value in (gain_crossover_hz, phase_margin_deg, phase_crossover_hz, gain_margin_db):
        if value is not None and not math.isfinite(value):
            raise ValueError("stability margin interpolation produced a non-finite value")

    return {
        "schema": AC_RESULT_SCHEMA,
        "status": "available",
        "kind": "stability_margins",
        "gain_crossover_hz": gain_crossover_hz,
        "phase_margin_deg": phase_margin_deg,
        "phase_crossover_hz": phase_crossover_hz,
        "gain_margin_db": gain_margin_db,
        "margin_positive": margin_positive,
        "notes": notes,
        "method": method,
        "assumptions": assumptions,
    }


__all__ = [
    "AC_RESULT_SCHEMA",
    "frequency_response",
    "loop_gain_response",
    "stability_margins",
]
