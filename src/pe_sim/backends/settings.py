"""Reviewed numerical settings for the ngspice reference backend.

The settings object is deliberately explicit: the generic infrastructure must
never infer a simulator's numerical configuration.  The same values are
rendered into the netlist, recorded in the run Manifest, and passed to the
provenance adapter so a reviewer can compare them byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


_SUPPORTED_METHODS = frozenset({"gear", "trap", "euler"})


def _finite_positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


@dataclass(frozen=True)
class NgspiceSolverSettings:
    """Explicit numerical configuration for the reviewed ngspice adapter.

    The defaults are conservative SPICE values.  They are not a claim that the
    solver converges for every circuit; convergence remains an adapter-level
    acceptance criterion.
    """

    integration_method: str = "gear"
    reltol: float = 1e-3
    abstol: float = 1e-9
    vntol: float = 1e-6
    chgtol: float = 1e-13
    gmin: float = 1e-12
    max_step_s: float | None = None
    temperature_c: float = 27.0
    num_digits: int = 15

    def __post_init__(self) -> None:
        if self.integration_method not in _SUPPORTED_METHODS:
            raise ValueError(f"unsupported integration_method: {self.integration_method!r}")
        for name in ("reltol", "abstol", "vntol", "chgtol", "gmin"):
            object.__setattr__(self, name, _finite_positive(name, getattr(self, name)))
        if self.max_step_s is not None:
            object.__setattr__(self, "max_step_s", _finite_positive("max_step_s", self.max_step_s))
        if not math.isfinite(float(self.temperature_c)):
            raise ValueError("temperature_c must be finite")
        if isinstance(self.num_digits, bool) or not isinstance(self.num_digits, int) or not 6 <= self.num_digits <= 17:
            raise ValueError("num_digits must be an integer in [6, 17]")

    def options_line(self) -> str:
        """Render the deterministic ``.options`` line used by the netlist."""

        parts = [
            f"reltol={self.reltol:.12g}",
            f"abstol={self.abstol:.12g}",
            f"vntol={self.vntol:.12g}",
            f"chgtol={self.chgtol:.12g}",
            f"gmin={self.gmin:.12g}",
            f"method={self.integration_method}",
        ]
        return ".options " + " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "integration_method": self.integration_method,
            "reltol": self.reltol,
            "abstol": self.abstol,
            "vntol": self.vntol,
            "chgtol": self.chgtol,
            "gmin": self.gmin,
            "max_step_s": self.max_step_s,
            "temperature_c": self.temperature_c,
            "num_digits": self.num_digits,
        }


__all__ = ["NgspiceSolverSettings"]
