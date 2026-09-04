"""Self-owned L1 averaged reference plants.

These models are intentionally small and deterministic.  They exercise the
public PlantAdapter contract; they are not device, thermal, or product models.
"""

from __future__ import annotations

from typing import Any, Mapping
import math

from .contracts import PlantObservation


def _positive(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


class _AveragedConverter:
    """Shared deterministic integrator for ideal averaged converters."""

    topology = "averaged_converter"

    def __init__(
        self,
        *,
        input_voltage_v: float,
        inductance_h: float,
        capacitance_f: float,
        load_resistance_ohm: float,
        integration_step_s: float = 1e-6,
    ) -> None:
        self.input_voltage_v = _positive("input_voltage_v", input_voltage_v)
        self.inductance_h = _positive("inductance_h", inductance_h)
        self.capacitance_f = _positive("capacitance_f", capacitance_f)
        self.load_resistance_ohm = _positive("load_resistance_ohm", load_resistance_ohm)
        self.integration_step_s = _positive("integration_step_s", integration_step_s)
        self.time_s = 0.0
        self.inductor_current_a = 0.0
        self.output_voltage_v = 0.0

    def capabilities(self) -> set[str]:
        return {"continuous_time", "external_input", "snapshot"}

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        state = dict(initial_state or {})
        self.time_s = 0.0
        self.inductor_current_a = float(state.get("inductor_current_a", state.get("current_a", 0.0)))
        self.output_voltage_v = float(state.get("output_voltage_v", state.get("vout", 0.0)))
        self._validate_state()
        return self._state_dict()

    def observe(self) -> PlantObservation:
        self._validate_state()
        return PlantObservation(
            time_s=self.time_s,
            measurement={
                "vout": self.output_voltage_v,
                "inductor_current": self.inductor_current_a,
            },
            truth={
                "vout": self.output_voltage_v,
                "inductor_current": self.inductor_current_a,
                "duty": getattr(self, "last_duty", 0.0),
                "input_voltage": self.input_voltage_v,
                "load_resistance": self.load_resistance_ohm,
            },
            age_steps=0,
            measurement_units={"vout": "V", "inductor_current": "A"},
        )

    def advance(
        self,
        action: float,
        duration: float,
        external_input: Mapping[str, Any] | None = None,
    ) -> PlantObservation:
        duty = float(action)
        duration = float(duration)
        if not math.isfinite(duty) or not 0.0 <= duty <= 1.0:
            raise ValueError("action duty must be finite and in [0, 1]")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be finite and positive")
        self._apply_external_input(external_input)
        self.last_duty = duty
        remaining = duration
        # Fixed substeps keep the reference model deterministic while avoiding
        # a control-period-dependent Euler instability for common L/C values.
        while remaining > 0.0:
            step = min(remaining, self.integration_step_s)
            self._rk4_step(duty, step)
            self.time_s += step
            remaining -= step
        self._validate_state()
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        self._validate_state()
        return {
            "topology": self.topology,
            "time_s": self.time_s,
            "inductor_current_a": self.inductor_current_a,
            "output_voltage_v": self.output_voltage_v,
            "input_voltage_v": self.input_voltage_v,
            "load_resistance_ohm": self.load_resistance_ohm,
            "last_duty": getattr(self, "last_duty", 0.0),
        }

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(snapshot.get("topology", self.topology)) != self.topology:
            raise ValueError("snapshot topology does not match plant")
        self.time_s = float(snapshot["time_s"])
        self.inductor_current_a = float(snapshot["inductor_current_a"])
        self.output_voltage_v = float(snapshot["output_voltage_v"])
        self.input_voltage_v = _positive("input_voltage_v", snapshot.get("input_voltage_v", self.input_voltage_v))
        self.load_resistance_ohm = _positive("load_resistance_ohm", snapshot.get("load_resistance_ohm", self.load_resistance_ohm))
        self.last_duty = float(snapshot.get("last_duty", 0.0))
        self._validate_state()
        return self._state_dict()

    def _apply_external_input(self, external_input: Mapping[str, Any] | None) -> None:
        values = dict(external_input or {})
        if "vin_v" in values or "input_voltage_v" in values:
            value = values["vin_v"] if "vin_v" in values else values["input_voltage_v"]
            self.input_voltage_v = _positive("input_voltage_v", value)
        if "load_ohm" in values or "load_resistance_ohm" in values:
            value = values["load_ohm"] if "load_ohm" in values else values["load_resistance_ohm"]
            self.load_resistance_ohm = _positive("load_resistance_ohm", value)

    def _state_dict(self) -> dict[str, float]:
        return {
            "time_s": self.time_s,
            "inductor_current_a": self.inductor_current_a,
            "output_voltage_v": self.output_voltage_v,
        }

    def _validate_state(self) -> None:
        for name in ("time_s", "inductor_current_a", "output_voltage_v"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must remain finite")
        if self.time_s < 0:
            raise ValueError("time_s must remain non-negative")

    def _derivatives(self, duty: float, current: float, voltage: float) -> tuple[float, float]:
        raise NotImplementedError

    def _rk4_step(self, duty: float, step: float) -> None:
        i0, v0 = self.inductor_current_a, self.output_voltage_v
        k1i, k1v = self._derivatives(duty, i0, v0)
        k2i, k2v = self._derivatives(duty, i0 + 0.5 * step * k1i, v0 + 0.5 * step * k1v)
        k3i, k3v = self._derivatives(duty, i0 + 0.5 * step * k2i, v0 + 0.5 * step * k2v)
        k4i, k4v = self._derivatives(duty, i0 + step * k3i, v0 + step * k3v)
        self.inductor_current_a = i0 + step * (k1i + 2 * k2i + 2 * k3i + k4i) / 6.0
        self.output_voltage_v = v0 + step * (k1v + 2 * k2v + 2 * k3v + k4v) / 6.0


class BuckPlant(_AveragedConverter):
    """Ideal averaged buck converter in continuous-conduction approximation."""

    topology = "buck"

    def _derivatives(self, duty: float, current: float, voltage: float) -> tuple[float, float]:
        return (
            (duty * self.input_voltage_v - voltage) / self.inductance_h,
            (current - voltage / self.load_resistance_ohm) / self.capacitance_f,
        )


class BoostPlant(_AveragedConverter):
    """Ideal averaged boost converter in continuous-conduction approximation."""

    topology = "boost"

    def _derivatives(self, duty: float, current: float, voltage: float) -> tuple[float, float]:
        conduction = 1.0 - duty
        return (
            (self.input_voltage_v - conduction * voltage) / self.inductance_h,
            (conduction * current - voltage / self.load_resistance_ohm) / self.capacitance_f,
        )
