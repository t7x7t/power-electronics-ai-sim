"""Shared frequency-domain helpers for capability analysis results.

Three analyses produce a swept response from a linear model: the plant
transfer function, the loop gain, and the input/output impedance.  Keeping the
sweep validation, log spacing, magnitude/decibel projection, and phase
unwrapping here means those rules exist once.  A second copy would let the
same sweep be accepted by one analysis and rejected by another.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


MIN_POINTS = 2
MAX_POINTS = 512


def finite_number(value: Any, label: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    ``bool`` is rejected: a flag is not a frequency or a magnitude.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def validate_sweep(
    start_hz: Any,
    stop_hz: Any,
    points: Any,
    *,
    min_points: int = MIN_POINTS,
    max_points: int = MAX_POINTS,
) -> tuple[float, float, int]:
    """Validate a log-spaced sweep request and return it normalised."""

    start = finite_number(start_hz, "start_hz")
    if start <= 0.0:
        raise ValueError("start_hz must be finite and positive")
    stop = finite_number(stop_hz, "stop_hz")
    if stop <= start:
        raise ValueError("stop_hz must be finite and greater than start_hz")
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError(
            f"points must be an integer in [{min_points}, {max_points}]"
        )
    if not min_points <= points <= max_points:
        raise ValueError(
            f"points must be an integer in [{min_points}, {max_points}], "
            f"got {points}"
        )
    return start, stop, points


def log_frequencies(start_hz: float, stop_hz: float, points: int) -> list[float]:
    """Return ``points`` frequencies spaced evenly on a log10 axis."""

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


def magnitude_series(
    values: Sequence[complex],
    frequencies: Sequence[float],
    *,
    label: str = "transfer function",
) -> tuple[list[float], list[float]]:
    """Return ``(|value|, 20 log10 |value|)`` for a swept response.

    A zero or non-finite magnitude is refused rather than reported as
    ``-inf``: a decibel axis with an infinity in it cannot be published as
    finite evidence.
    """

    if len(values) != len(frequencies):
        raise ValueError("values and frequencies must have equal length")
    magnitudes: list[float] = []
    decibels: list[float] = []
    for value, frequency in zip(values, frequencies):
        magnitude = abs(value)
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            raise ValueError(
                f"{label} magnitude is zero or non-finite at "
                f"{frequency!r} Hz; a finite decibel value cannot be reported"
            )
        magnitudes.append(magnitude)
        decibels.append(20.0 * math.log10(magnitude))
    return magnitudes, decibels


def unwrapped_phase_deg(values: Sequence[complex]) -> list[float]:
    """Return the phase in degrees with 2-pi jumps removed."""

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


__all__ = [
    "MAX_POINTS",
    "MIN_POINTS",
    "finite_number",
    "log_frequencies",
    "magnitude_series",
    "unwrapped_phase_deg",
    "validate_sweep",
]
