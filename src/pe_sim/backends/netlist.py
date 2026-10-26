"""Deterministic ngspice netlist construction for the reference Buck converter.

The netlist is generated, never hand-edited.  Formatting is fixed and contains
no timestamp, host name, or absolute path, so the SHA-256 digest of the text is
a stable identity for the simulated circuit and its numerical settings.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Any

from .settings import NgspiceSolverSettings


NETLIST_TEMPLATE_VERSION = "ngspice-buck-v2"


class NetlistParameterError(ValueError):
    """Raised when a circuit or solver parameter cannot be rendered safely."""


def _positive(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise NetlistParameterError(f"{name} must be finite and positive")
    return number


def _non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise NetlistParameterError(f"{name} must be finite and non-negative")
    return number


def _format(value: float) -> str:
    """Stable scientific formatting used by every generated netlist line."""

    return f"{float(value):.12e}"


@dataclass(frozen=True)
class BuckNetlistParameters:
    """Electrical parameters of the reviewed asynchronous Buck reference.

    The switch is an ideal voltage-controlled switch with explicit on/off
    resistance.  The diode is a Shockley model with a deliberately small
    emission coefficient to approximate an ideal freewheeling diode.  These are
    reference-model choices, not device or thermal models.
    """

    input_voltage_v: float
    inductance_h: float
    capacitance_f: float
    load_resistance_ohm: float
    switching_frequency_hz: float
    switch_on_resistance_ohm: float = 1e-3
    switch_off_resistance_ohm: float = 1e9
    diode_saturation_current_a: float = 1e-12
    diode_emission_coefficient: float = 0.02
    initial_inductor_current_a: float = 0.0
    initial_output_voltage_v: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_voltage_v", _positive("input_voltage_v", self.input_voltage_v))
        object.__setattr__(self, "inductance_h", _positive("inductance_h", self.inductance_h))
        object.__setattr__(self, "capacitance_f", _positive("capacitance_f", self.capacitance_f))
        object.__setattr__(self, "load_resistance_ohm", _positive("load_resistance_ohm", self.load_resistance_ohm))
        object.__setattr__(self, "switching_frequency_hz", _positive("switching_frequency_hz", self.switching_frequency_hz))
        object.__setattr__(self, "switch_on_resistance_ohm", _positive("switch_on_resistance_ohm", self.switch_on_resistance_ohm))
        object.__setattr__(self, "switch_off_resistance_ohm", _positive("switch_off_resistance_ohm", self.switch_off_resistance_ohm))
        object.__setattr__(self, "diode_saturation_current_a", _positive("diode_saturation_current_a", self.diode_saturation_current_a))
        object.__setattr__(self, "diode_emission_coefficient", _positive("diode_emission_coefficient", self.diode_emission_coefficient))
        object.__setattr__(self, "initial_inductor_current_a", _non_negative("initial_inductor_current_a", self.initial_inductor_current_a))
        object.__setattr__(self, "initial_output_voltage_v", _non_negative("initial_output_voltage_v", self.initial_output_voltage_v))

    @property
    def switching_period_s(self) -> float:
        return 1.0 / self.switching_frequency_hz

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_voltage_v": self.input_voltage_v,
            "inductance_h": self.inductance_h,
            "capacitance_f": self.capacitance_f,
            "load_resistance_ohm": self.load_resistance_ohm,
            "switching_frequency_hz": self.switching_frequency_hz,
            "switch_on_resistance_ohm": self.switch_on_resistance_ohm,
            "switch_off_resistance_ohm": self.switch_off_resistance_ohm,
            "diode_saturation_current_a": self.diode_saturation_current_a,
            "diode_emission_coefficient": self.diode_emission_coefficient,
            "initial_inductor_current_a": self.initial_inductor_current_a,
            "initial_output_voltage_v": self.initial_output_voltage_v,
        }


def default_sample_step_s(parameters: BuckNetlistParameters, duration_s: float) -> float:
    """Choose a bounded output step without exceeding the requested window."""

    duration = _positive("duration_s", duration_s)
    candidate = min(duration / 1000.0, parameters.switching_period_s / 200.0)
    candidate = max(candidate, 1e-12)
    if candidate > duration / 10.0:
        candidate = duration / 10.0
    return candidate


def render_buck_netlist(
    parameters: BuckNetlistParameters,
    *,
    duty: float,
    duration_s: float,
    solver: NgspiceSolverSettings,
    sample_step_s: float | None = None,
    carrier_phase_s: float = 0.0,
) -> str:
    """Render one zero-order-hold solve segment as an ngspice netlist.

    ``duty`` is held constant for the whole segment.  ``carrier_phase_s`` is the
    phase of the PWM carrier at the segment start (the segment's start time
    modulo the switching period).  The global carrier starts its on-interval at
    absolute multiples of the switching period, so the PULSE delay is the time
    to the *next* multiple, ``(-carrier_phase_s) mod Tsw``; a zero phase renders
    a zero delay and reproduces the previous window netlist apart from the
    explicit parameter.
    """

    duty_value = float(duty)
    if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
        raise NetlistParameterError("duty must be finite and in [0, 1]")
    duration = _positive("duration_s", duration_s)
    if not isinstance(solver, NgspiceSolverSettings):
        raise NetlistParameterError("solver must be NgspiceSolverSettings")

    switching_period = parameters.switching_period_s
    phase = float(carrier_phase_s)
    if not math.isfinite(phase):
        raise NetlistParameterError("carrier_phase_s must be finite")
    phase %= switching_period
    # A phase within floating-point noise of one period is zero phase; leaving
    # it as Tsw would make PULSE wait a whole extra period before switching.
    if switching_period - phase <= 1e-12 * max(1.0, switching_period):
        phase = 0.0
    pulse_delay = (-phase) % switching_period
    if switching_period - pulse_delay <= 1e-12 * max(1.0, switching_period):
        pulse_delay = 0.0

    step = default_sample_step_s(parameters, duration) if sample_step_s is None else _positive("sample_step_s", sample_step_s)
    step = min(step, duration / 10.0)
    pulse_width = max(duty_value * switching_period, 1e-15)

    lines = [
        "* pe-sim ngspice buck reference",
        f"* template: {NETLIST_TEMPLATE_VERSION}",
        f".param Vin={_format(parameters.input_voltage_v)}",
        f".param Tsw={_format(parameters.switching_period_s)}",
        f".param D={_format(duty_value)}",
        f".param TD={_format(pulse_delay)}",
        f".param L={_format(parameters.inductance_h)}",
        f".param C={_format(parameters.capacitance_f)}",
        f".param Rload={_format(parameters.load_resistance_ohm)}",
        f".param Ron={_format(parameters.switch_on_resistance_ohm)}",
        f".param Roff={_format(parameters.switch_off_resistance_ohm)}",
        f".param Isat={_format(parameters.diode_saturation_current_a)}",
        f".param N={_format(parameters.diode_emission_coefficient)}",
        f".param IL0={_format(parameters.initial_inductor_current_a)}",
        f".param VC0={_format(parameters.initial_output_voltage_v)}",
        "",
        "V1 vin 0 DC {Vin}",
        f"Vpwm pwm 0 PULSE(0 1 {{TD}} 1e-9 1e-9 {_format(pulse_width)} {{Tsw}})",
        "S1 vin sw pwm 0 SWMOD",
        "D1 0 sw DMOD",
        "L1 sw vout {L} IC={IL0}",
        "C1 vout 0 {C} IC={VC0}",
        "R1 vout 0 {Rload}",
        ".model SWMOD SW(Ron={Ron} Roff={Roff} Vt=0.5 Vh=0)",
        ".model DMOD D(Is={Isat} N={N})",
        solver.options_line(),
        f".temp {_format(solver.temperature_c)}",
        f".tran {_format(step)} {_format(duration)} 0 {_format(step)} uic",
        ".control",
        "set filetype=ascii",
        "set wr_singlescale",
        "set wr_vecnames",
        f"set numdgt={solver.num_digits}",
        "run",
        "wrdata output.dat v(vout) i(L1) v(sw)",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(lines)


def netlist_digest(text: str) -> str:
    """Return the stable SHA-256 identity of a rendered netlist."""

    if not isinstance(text, str) or not text:
        raise NetlistParameterError("netlist text must be a non-empty string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "NETLIST_TEMPLATE_VERSION",
    "BuckNetlistParameters",
    "NetlistParameterError",
    "default_sample_step_s",
    "netlist_digest",
    "render_buck_netlist",
]
