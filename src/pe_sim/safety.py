"""Fail-closed action and observation safety checks."""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Mapping, Protocol


class ActionPolicy(Protocol):
    """Policy used to validate or project a controller action.

    Implementations return the action to apply and an optional reason when it
    was changed.  A policy is deliberately separate from the Runner so a
    plant can declare its own actuator semantics without imposing them on
    unrelated models.
    """

    def project(self, value: float) -> tuple[float, str | None]: ...


@dataclass(frozen=True)
class FiniteActionPolicy:
    """Generic default: require a finite action and preserve its value."""

    name: str = "finite"

    def project(self, value: float) -> tuple[float, str | None]:
        action = float(value)
        if not math.isfinite(action):
            raise ValueError("non-finite action")
        return action, None


@dataclass(frozen=True)
class BoundedActionPolicy:
    """Explicit bounded actuator policy with deterministic clamping."""

    minimum: float
    maximum: float
    name: str = "bounded"

    def __post_init__(self) -> None:
        minimum, maximum = float(self.minimum), float(self.maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
            raise ValueError("action bounds must be finite and ordered")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def project(self, value: float) -> tuple[float, str | None]:
        action = float(value)
        if not math.isfinite(action):
            raise ValueError("non-finite action")
        bounded = min(max(action, self.minimum), self.maximum)
        reason = "lower" if bounded != action and action < self.minimum else "upper" if bounded != action else None
        return bounded, reason


def project_action(value: float, *, minimum: float | None = None, maximum: float | None = None) -> tuple[float, str | None]:
    """Compatibility helper using finite-only validation unless bounds exist."""
    if minimum is None and maximum is None:
        return FiniteActionPolicy().project(value)
    if minimum is None or maximum is None:
        raise ValueError("minimum and maximum must be provided together")
    return BoundedActionPolicy(minimum, maximum).project(value)


def check_observation(measurement: Mapping[str, float]) -> None:
    if any(not math.isfinite(float(v)) for v in measurement.values()):
        raise ValueError("non-finite measurement")
