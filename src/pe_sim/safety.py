"""Fail-closed action and observation safety checks."""

from __future__ import annotations
import math
from typing import Mapping


def project_action(value: float, *, minimum: float = 0.0, maximum: float = 1.0) -> tuple[float, str | None]:
    if not math.isfinite(float(value)):
        raise ValueError("non-finite action")
    if minimum > maximum:
        raise ValueError("invalid action bounds")
    bounded = min(max(float(value), minimum), maximum)
    return bounded, ("lower" if bounded != value and value < minimum else "upper" if bounded != value else None)


def check_observation(measurement: Mapping[str, float]) -> None:
    if any(not math.isfinite(float(v)) for v in measurement.values()):
        raise ValueError("non-finite measurement")
