"""Qualification is deliberately independent from performance metrics."""

from __future__ import annotations
from typing import Iterable, Mapping, Any
import math


def qualify_samples(samples: Iterable[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    rows = list(samples)
    reasons: list[str] = []
    if not rows:
        reasons.append("no_samples")
    for index, row in enumerate(rows):
        for key, value in row.items():
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                reasons.append(f"non_finite:{index}:{key}")
    return not reasons, reasons
