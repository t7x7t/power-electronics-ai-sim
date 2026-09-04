"""Reusable contract checks for PlantAdapter and ControllerAdapter plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
import math

from .contracts import ActionRequest, snapshot_digest


@dataclass(frozen=True)
class ConformanceReport:
    component: str
    checks: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def check_plant_adapter(
    factory: Callable[[], Any],
    *,
    initial_state: Mapping[str, Any] | None = None,
    action: float = 0.5,
    duration_s: float = 1e-5,
    action_bounds: tuple[float, float] | None = None,
) -> ConformanceReport:
    """Run deterministic lifecycle and fail-closed checks on a plant factory.

    ``action_bounds`` is model-specific and therefore opt-in.  The public
    PlantAdapter contract does not require a normalized action range.
    """
    checks: list[str] = []
    failures: list[str] = []
    try:
        plant = factory()
        if not callable(getattr(plant, "capabilities", None)):
            raise TypeError("missing capabilities()")
        checks.append("capabilities")
        plant.reset(dict(initial_state or {}))
        first = plant.observe()
        checks.append("reset_observe")
        if not math.isfinite(first.time_s):
            raise ValueError("initial observation time is not finite")
        if set(first.controller_measurement()) != set(first.measurement):
            raise ValueError("controller measurement projection changed fields")
        checks.append("measurement_projection")
        if action_bounds is not None:
            minimum, maximum = (float(action_bounds[0]), float(action_bounds[1]))
            if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
                raise ValueError("action_bounds must be finite and ordered")
            if not minimum <= float(action) <= maximum:
                raise ValueError("test action lies outside action_bounds")
        before = plant.snapshot()
        after = plant.advance(action, duration_s)
        if after.time_s <= first.time_s:
            raise ValueError("time did not advance")
        checks.append("advance_monotonic")
        plant.restore(before)
        restored = plant.snapshot()
        if snapshot_digest(restored) != snapshot_digest(before):
            raise ValueError("snapshot restore is not deterministic")
        checks.append("snapshot_restore")
        try:
            plant.advance(float("nan"), duration_s)
        except (TypeError, ValueError):
            checks.append("nonfinite_action_rejected")
        else:
            raise ValueError("non-finite action was accepted")
        if action_bounds is not None:
            out_of_range = maximum + max(1.0, abs(maximum) * 0.1)
            try:
                plant.advance(out_of_range, duration_s)
            except (TypeError, ValueError):
                checks.append("out_of_range_action_rejected")
            else:
                raise ValueError("out-of-range action was accepted")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    return ConformanceReport("plant", tuple(checks), tuple(failures))


def check_controller_adapter(
    factory: Callable[[], Any],
    *,
    measurement: Mapping[str, float] | None = None,
    command: Mapping[str, Any] | None = None,
) -> ConformanceReport:
    """Check reset and ActionRequest production for a controller factory."""
    checks: list[str] = []
    failures: list[str] = []
    try:
        controller = factory()
        state = controller.reset(None, 0)
        checks.append("reset")
        request = controller.observe(
            dict(measurement or {"vout": 0.0}),
            dict(command or {"vref": 1.0}),
            {"sample_time_s": 0.0, "control_period_s": 1e-3, "age_steps": 0},
            state,
        )
        if not isinstance(request, ActionRequest):
            raise TypeError("controller did not return ActionRequest")
        if not math.isfinite(request.value):
            raise ValueError("controller produced non-finite action")
        checks.append("action_request")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    return ConformanceReport("controller", tuple(checks), tuple(failures))
