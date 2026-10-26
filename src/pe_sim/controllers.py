"""Discrete-time compensators expressed as one explicit difference equation.

The kernel only requires a controller object that produces an
``ActionRequest``; it never branches on which design produced the
coefficients.  This module therefore contains one runtime implementation,
``DiscreteCompensator``, plus small factories that translate reviewed designs
(Type II, Type III, or a directly supplied biquad) into that difference
equation.

The difference equation is the control law::

    y[n] = b0*x[n] + b1*x[n-1] + ... - a1*y[n-1] - a2*y[n-2] - a3*y[n-3]

where ``x[n] = vref - measurement``.  There is no hidden integrator: the
``1/s`` in a Type II or Type III design is part of the discretised
coefficients below.

Output limits are optional and paired.  When the output saturates, the delay
line stores the *applied* (clamped) value rather than the unclamped request.
That keeps the compensator's memory bounded by the actuator range and prevents
it from accumulating an action the actuator never executed.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .contracts import ActionRequest


TYPE2_CONFIG_KEYS = frozenset(
    {"gain", "zero_hz", "pole_hz", "output_min", "output_max", "measurement_key"}
)
TYPE3_CONFIG_KEYS = frozenset(
    {
        "gain",
        "zero1_hz",
        "zero2_hz",
        "pole1_hz",
        "pole2_hz",
        "output_min",
        "output_max",
        "measurement_key",
    }
)
BIQUAD_CONFIG_KEYS = frozenset(
    {"b0", "b1", "b2", "a1", "a2", "output_min", "output_max", "measurement_key"}
)


def _finite_number(value: Any, *, field: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _positive_number(value: Any, *, field: str) -> float:
    """Return ``value`` as a finite positive float or raise ``ValueError``."""

    number = _finite_number(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be finite and positive")
    return number


def _optional_limits(
    values: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    """Read an optional, paired ``output_min``/``output_max`` declaration."""

    has_min = "output_min" in values
    has_max = "output_max" in values
    if has_min != has_max:
        raise ValueError("output_min and output_max must be provided together")
    if not has_min:
        return None, None
    output_min = _finite_number(values["output_min"], field="output_min")
    output_max = _finite_number(values["output_max"], field="output_max")
    if not output_min < output_max:
        raise ValueError("output_min must be smaller than output_max")
    return output_min, output_max


def _poly_mul(left: Sequence[float], right: Sequence[float]) -> list[float]:
    """Multiply two coefficient lists in ascending powers of ``z^-1``."""

    result = [0.0] * (len(left) + len(right) - 1)
    for left_index, left_value in enumerate(left):
        for right_index, right_value in enumerate(right):
            result[left_index + right_index] += left_value * right_value
    return result


def _normalised_tustin(
    numerator: Sequence[float],
    denominator: Sequence[float],
    *,
    design: str,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Normalise a Tustin-mapped transfer function to ``a0 == 1``.

    ``denominator`` is the full polynomial in ascending powers of ``z^-1``;
    the returned denominator omits its leading ``1`` because the difference
    equation treats it implicitly.
    """

    leading = float(denominator[0])
    if not math.isfinite(leading) or leading == 0.0:
        raise ValueError(
            f"{design} discrete denominator has no finite non-zero leading coefficient"
        )
    b = tuple(float(value) / leading for value in numerator)
    a = tuple(float(value) / leading for value in denominator[1:])
    if not all(math.isfinite(value) for value in b + a):
        raise ValueError(f"{design} coefficients must be finite")
    return b, a


class DiscreteCompensator:
    """Order-1..3 compensator driven by an explicit difference equation.

    ``numerator`` is ``b0..bN`` with two to four entries; ``denominator`` is
    ``a1..aN`` and must have exactly ``len(numerator) - 1`` entries.  Empty
    histories start at zero, so the first sample is ``b0 * x[0]``.
    """

    def __init__(
        self,
        numerator: Sequence[float],
        denominator: Sequence[float],
        *,
        output_min: float | None = None,
        output_max: float | None = None,
        measurement_key: str = "vout",
        name: str = "discrete-compensator",
    ):
        b = tuple(
            _finite_number(value, field=f"numerator[{index}]")
            for index, value in enumerate(numerator)
        )
        if not 2 <= len(b) <= 4:
            raise ValueError("numerator must contain between 2 and 4 coefficients")
        a = tuple(
            _finite_number(value, field=f"denominator[{index}]")
            for index, value in enumerate(denominator)
        )
        if len(a) != len(b) - 1:
            raise ValueError(
                "denominator must contain exactly len(numerator) - 1 coefficients"
            )
        if (output_min is None) != (output_max is None):
            raise ValueError("output_min and output_max must be provided together")
        if output_min is not None:
            output_min = _finite_number(output_min, field="output_min")
            output_max = _finite_number(output_max, field="output_max")
            if not math.isfinite(output_min) or not math.isfinite(output_max):
                raise ValueError("compensator output limits must be finite")
            if not output_min < output_max:
                raise ValueError("output_min must be smaller than output_max")
        if not isinstance(measurement_key, str) or not measurement_key:
            raise ValueError("measurement_key must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")

        self.numerator = b
        self.denominator = a
        self.coefficients = {"b": list(b), "a": list(a)}
        self.output_min = output_min
        self.output_max = output_max
        self.measurement_key = measurement_key
        self.name = name
        order = len(b) - 1
        # Most recent first.  They are private so ``_component_manifest`` does
        # not record mutable runtime state as part of the controller identity.
        self._x_history: list[float] = [0.0] * order
        self._y_history: list[float] = [0.0] * order

    def capabilities(self) -> set[str]:
        return {"discrete_control"}

    def observe(
        self,
        measurement: Mapping[str, float],
        command: Mapping[str, Any],
        timing: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> ActionRequest:
        """Apply the stored difference equation to one measurement sample.

        The returned ``state_update`` mirrors the delay line as ``x1..xN``
        (most recent input first) and ``y1..yN`` (most recent applied output
        first), plus ``output``.  ``state`` is accepted for protocol
        compatibility; ``snapshot()``/``restore()`` own the authoritative
        history.
        """

        del state  # The returned state_update is authoritative for the Runner.
        key = self.measurement_key
        if key not in measurement:
            raise ValueError(f"measurement does not contain {key!r}")
        measured = float(measurement[key])
        vref = float(command.get("vref", 1.0))
        now = float(timing["sample_time_s"])
        if not math.isfinite(measured) or not math.isfinite(vref) or not math.isfinite(now):
            raise ValueError("controller input must be finite")

        error = vref - measured
        output = self.numerator[0] * error
        for index in range(1, len(self.numerator)):
            output += self.numerator[index] * self._x_history[index - 1]
            output -= self.denominator[index - 1] * self._y_history[index - 1]
        if not math.isfinite(output):
            raise ValueError("compensator produced a non-finite output")

        applied = output
        if self.output_min is not None:
            applied = min(max(output, self.output_min), self.output_max)
        # Store the applied value, not the unclamped request: an actuator that
        # cannot execute the request must not leave a phantom term in the
        # delay line that later drives the compensator out of saturation
        # slowly or the wrong way.
        self._x_history = [error, *self._x_history[:-1]]
        self._y_history = [applied, *self._y_history[:-1]]
        return ActionRequest(
            value=applied,
            produced_time_s=now,
            target_time_s=now,
            state_update=self._state_update(applied),
        )

    def reset(
        self, controller_state: Mapping[str, Any] | None, seed: int
    ) -> Mapping[str, Any]:
        """Restore a serialized state, using zero for every missing key."""

        del seed
        self._load_history(dict(controller_state or {}))
        return self._state_update(self._y_history[0])

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe checkpoint of the coefficients and history."""

        return {
            "coefficients": {"b": list(self.numerator), "a": list(self.denominator)},
            "history": {"x": list(self._x_history), "y": list(self._y_history)},
            "limits": {"output_min": self.output_min, "output_max": self.output_max},
            "measurement_key": self.measurement_key,
            "name": self.name,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        """Restore a snapshot, refusing one produced by a different law."""

        state = dict(snapshot or {})
        coefficients = state.get("coefficients")
        if not isinstance(coefficients, Mapping):
            raise ValueError("compensator snapshot must declare coefficients")
        stored_b = list(coefficients.get("b", ()))
        stored_a = list(coefficients.get("a", ()))
        if stored_b != list(self.numerator) or stored_a != list(self.denominator):
            raise ValueError(
                "compensator snapshot coefficients do not match this controller"
            )
        self._load_history(state)
        return self._state_update(self._y_history[0])

    def _state_update(self, output: float) -> dict[str, Any]:
        update: dict[str, Any] = {
            f"x{index + 1}": value for index, value in enumerate(self._x_history)
        }
        update.update(
            {f"y{index + 1}": value for index, value in enumerate(self._y_history)}
        )
        update["output"] = float(output)
        return update

    def _load_history(self, state: Mapping[str, Any]) -> None:
        """Load history from a snapshot shape or a ``state_update`` shape."""

        history = state.get("history")
        if isinstance(history, Mapping):
            x_values = list(history.get("x", ()))
            y_values = list(history.get("y", ()))
        else:
            x_values = [
                state.get(f"x{index + 1}", 0.0) for index in range(len(self._x_history))
            ]
            y_values = [
                state.get(f"y{index + 1}", 0.0) for index in range(len(self._y_history))
            ]
        if len(x_values) != len(self._x_history) or len(y_values) != len(self._y_history):
            raise ValueError("compensator history length does not match the coefficients")
        x_loaded = [
            _finite_number(value, field=f"x{index + 1}")
            for index, value in enumerate(x_values)
        ]
        y_loaded = [
            _finite_number(value, field=f"y{index + 1}")
            for index, value in enumerate(y_values)
        ]
        self._x_history = x_loaded
        self._y_history = y_loaded


class Type2Controller(DiscreteCompensator):
    """Type II compensator ``K (1 + s/wz) / (s (1 + s/wp))``.

    The design is Tustin-mapped at the control period.  Exactly one zero and
    one non-zero pole are exposed; the ``1/s`` is carried by the denominator
    of the discretised law.
    """

    _CONFIG_KEYS = TYPE2_CONFIG_KEYS

    def __init__(
        self,
        *,
        gain: float,
        zero_hz: float,
        pole_hz: float,
        control_period_s: float,
        output_min: float | None = None,
        output_max: float | None = None,
        measurement_key: str = "vout",
    ):
        self.gain = _positive_number(gain, field="gain")
        self.zero_hz = _positive_number(zero_hz, field="zero_hz")
        self.pole_hz = _positive_number(pole_hz, field="pole_hz")
        self.sample_period_s = _positive_number(
            control_period_s, field="control_period_s"
        )
        self.zero_rad_s = 2.0 * math.pi * self.zero_hz
        self.pole_rad_s = 2.0 * math.pi * self.pole_hz
        k = 2.0 / self.sample_period_s
        az = 1.0 + k / self.zero_rad_s
        bz = 1.0 - k / self.zero_rad_s
        ap = 1.0 + k / self.pole_rad_s
        bp = 1.0 - k / self.pole_rad_s
        numerator = [self.gain / k * value for value in _poly_mul([az, bz], [1.0, 1.0])]
        denominator = _poly_mul([ap, bp], [1.0, -1.0])
        b, a = _normalised_tustin(numerator, denominator, design="type2")
        super().__init__(
            b,
            a,
            output_min=output_min,
            output_max=output_max,
            measurement_key=measurement_key,
            name="type2-controller",
        )

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any] | None, *, control_period_s: float
    ) -> "Type2Controller":
        values = dict(config or {})
        unknown = set(values) - cls._CONFIG_KEYS
        if unknown:
            raise ValueError(f"unsupported type2 fields: {sorted(unknown)}")
        for field in ("gain", "zero_hz", "pole_hz"):
            if field not in values:
                raise ValueError(f"type2 requires {field}")
        output_min, output_max = _optional_limits(values)
        return cls(
            gain=values["gain"],
            zero_hz=values["zero_hz"],
            pole_hz=values["pole_hz"],
            control_period_s=control_period_s,
            output_min=output_min,
            output_max=output_max,
            measurement_key=str(values.get("measurement_key", "vout")),
        )


class Type3Controller(DiscreteCompensator):
    """Type III compensator with two zeros and two non-zero poles.

    ``G(s) = K (1 + s/wz1)(1 + s/wz2) / (s (1 + s/wp1)(1 + s/wp2))`` is
    Tustin-mapped at the control period, producing a third-order difference
    equation.
    """

    _CONFIG_KEYS = TYPE3_CONFIG_KEYS

    def __init__(
        self,
        *,
        gain: float,
        zero1_hz: float,
        zero2_hz: float,
        pole1_hz: float,
        pole2_hz: float,
        control_period_s: float,
        output_min: float | None = None,
        output_max: float | None = None,
        measurement_key: str = "vout",
    ):
        self.gain = _positive_number(gain, field="gain")
        self.zero1_hz = _positive_number(zero1_hz, field="zero1_hz")
        self.zero2_hz = _positive_number(zero2_hz, field="zero2_hz")
        self.pole1_hz = _positive_number(pole1_hz, field="pole1_hz")
        self.pole2_hz = _positive_number(pole2_hz, field="pole2_hz")
        self.sample_period_s = _positive_number(
            control_period_s, field="control_period_s"
        )
        self.zero1_rad_s = 2.0 * math.pi * self.zero1_hz
        self.zero2_rad_s = 2.0 * math.pi * self.zero2_hz
        self.pole1_rad_s = 2.0 * math.pi * self.pole1_hz
        self.pole2_rad_s = 2.0 * math.pi * self.pole2_hz
        k = 2.0 / self.sample_period_s
        zeros = _poly_mul(
            [1.0 + k / self.zero1_rad_s, 1.0 - k / self.zero1_rad_s],
            [1.0 + k / self.zero2_rad_s, 1.0 - k / self.zero2_rad_s],
        )
        poles = _poly_mul(
            [1.0 + k / self.pole1_rad_s, 1.0 - k / self.pole1_rad_s],
            [1.0 + k / self.pole2_rad_s, 1.0 - k / self.pole2_rad_s],
        )
        numerator = [
            self.gain / k * value for value in _poly_mul(zeros, [1.0, 1.0])
        ]
        denominator = _poly_mul(poles, [1.0, -1.0])
        b, a = _normalised_tustin(numerator, denominator, design="type3")
        super().__init__(
            b,
            a,
            output_min=output_min,
            output_max=output_max,
            measurement_key=measurement_key,
            name="type3-controller",
        )

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any] | None, *, control_period_s: float
    ) -> "Type3Controller":
        values = dict(config or {})
        unknown = set(values) - cls._CONFIG_KEYS
        if unknown:
            raise ValueError(f"unsupported type3 fields: {sorted(unknown)}")
        for field in ("gain", "zero1_hz", "zero2_hz", "pole1_hz", "pole2_hz"):
            if field not in values:
                raise ValueError(f"type3 requires {field}")
        output_min, output_max = _optional_limits(values)
        return cls(
            gain=values["gain"],
            zero1_hz=values["zero1_hz"],
            zero2_hz=values["zero2_hz"],
            pole1_hz=values["pole1_hz"],
            pole2_hz=values["pole2_hz"],
            control_period_s=control_period_s,
            output_min=output_min,
            output_max=output_max,
            measurement_key=str(values.get("measurement_key", "vout")),
        )


class BiquadController(DiscreteCompensator):
    """Second-order compensator whose coefficients are supplied directly."""

    _CONFIG_KEYS = BIQUAD_CONFIG_KEYS

    def __init__(
        self,
        *,
        b0: float,
        b1: float,
        b2: float,
        a1: float,
        a2: float,
        output_min: float | None = None,
        output_max: float | None = None,
        measurement_key: str = "vout",
    ):
        super().__init__(
            [b0, b1, b2],
            [a1, a2],
            output_min=output_min,
            output_max=output_max,
            measurement_key=measurement_key,
            name="biquad-controller",
        )

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any] | None, *, control_period_s: float
    ) -> "BiquadController":
        # The coefficients are already discrete, so the control period is not
        # part of this design.  The keyword keeps every factory callable
        # identical at the registry boundary.
        del control_period_s
        values = dict(config or {})
        unknown = set(values) - cls._CONFIG_KEYS
        if unknown:
            raise ValueError(f"unsupported biquad fields: {sorted(unknown)}")
        for field in ("b0", "b1", "b2", "a1", "a2"):
            if field not in values:
                raise ValueError(f"biquad requires {field}")
        output_min, output_max = _optional_limits(values)
        return cls(
            b0=values["b0"],
            b1=values["b1"],
            b2=values["b2"],
            a1=values["a1"],
            a2=values["a2"],
            output_min=output_min,
            output_max=output_max,
            measurement_key=str(values.get("measurement_key", "vout")),
        )


__all__ = [
    "BIQUAD_CONFIG_KEYS",
    "BiquadController",
    "DiscreteCompensator",
    "TYPE2_CONFIG_KEYS",
    "TYPE3_CONFIG_KEYS",
    "Type2Controller",
    "Type3Controller",
]
