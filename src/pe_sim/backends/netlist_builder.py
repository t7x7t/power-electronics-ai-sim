"""Optional PySpice circuit-model builders for the reviewed Buck reference.

PySpice 1.5 is used **only to construct the circuit and render its netlist**.
Execution deliberately stays on the verified external ngspice process boundary:
PySpice 1.5 officially supports ngspice <= 34, while the verified local
executable is ngspice 41.  Both native PySpice execution paths (shared library
and its subprocess server) were observed to fail against ngspice 41; see
``docs/34_pyspice_optional_backend_v0.md``.

PySpice is GPLv3 and is never a mandatory dependency.  Import is lazy and the
module raises a clear error when the optional extra is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol

from .netlist import (
    NETLIST_TEMPLATE_VERSION,
    BuckNetlistParameters,
    default_sample_step_s,
    render_buck_netlist,
)
from .settings import NgspiceSolverSettings


class NetlistBuilderError(ValueError):
    """Raised when a netlist builder cannot render a reviewed circuit."""


class PyspiceUnavailableError(NetlistBuilderError):
    """Raised when PySpice is not installed in the active environment."""


# PySpice renders ``ic=0.45A``.  SPICE interprets the trailing ``A`` as the
# atto scale factor (1e-18), which silently zeroes the initial condition.  Our
# builder always supplies base units (A, V), so removing the suffix is correct
# and unambiguous for these two fields.
_INITIAL_CONDITION_RE = re.compile(r"\b(ic=)([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)([A-Za-z]+)")


class NetlistBuilder(Protocol):
    """Strategy that renders one zero-order-hold solve segment."""

    name: str

    def render(
        self,
        parameters: BuckNetlistParameters,
        *,
        duty: float,
        duration_s: float,
        solver: NgspiceSolverSettings,
        sample_step_s: float | None = None,
        carrier_phase_s: float = 0.0,
    ) -> str: ...

    def provenance(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NativeBuckNetlistBuilder:
    """Project-owned deterministic netlist renderer (default)."""

    name: str = "native-netlist"

    def render(
        self,
        parameters: BuckNetlistParameters,
        *,
        duty: float,
        duration_s: float,
        solver: NgspiceSolverSettings,
        sample_step_s: float | None = None,
        carrier_phase_s: float = 0.0,
    ) -> str:
        return render_buck_netlist(
            parameters,
            duty=duty,
            duration_s=duration_s,
            solver=solver,
            sample_step_s=sample_step_s,
            carrier_phase_s=carrier_phase_s,
        )

    def provenance(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "template": NETLIST_TEMPLATE_VERSION,
            "execution": "external-ngspice-process",
        }


def _import_pyspice():
    try:
        import PySpice  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on optional install
        raise PyspiceUnavailableError(
            "PySpice is not installed; install the optional 'pyspice' extra "
            "or use the native netlist builder"
        ) from exc
    return PySpice


def pyspice_version() -> str | None:
    """Return the installed PySpice version without making it mandatory."""

    try:
        return str(_import_pyspice().__version__)
    except PyspiceUnavailableError:
        return None


def _strip_initial_condition_units(text: str) -> str:
    return _INITIAL_CONDITION_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}", text)


@dataclass(frozen=True)
class PyspiceBuckNetlistBuilder:
    """Build the reviewed Buck circuit through the PySpice object API.

    The resulting netlist is executed by the reviewed ngspice process adapter,
    not by PySpice's own simulator backend.
    """

    name: str = "pyspice-model"

    def check_available(self) -> None:
        _import_pyspice()

    def render(
        self,
        parameters: BuckNetlistParameters,
        *,
        duty: float,
        duration_s: float,
        solver: NgspiceSolverSettings,
        sample_step_s: float | None = None,
        carrier_phase_s: float = 0.0,
    ) -> str:
        _import_pyspice()
        from PySpice.Spice.Netlist import Circuit
        from PySpice.Unit import u_A, u_F, u_H, u_Ohm, u_s, u_V

        duty_value = float(duty)
        if not 0.0 <= duty_value <= 1.0:
            raise NetlistBuilderError("duty must be in [0, 1]")
        duration = float(duration_s)
        if not duration > 0:
            raise NetlistBuilderError("duration_s must be positive")
        phase = float(carrier_phase_s)
        switching_period = parameters.switching_period_s
        phase %= switching_period
        if switching_period - phase <= 1e-12 * max(1.0, switching_period):
            phase = 0.0
        pulse_delay = (-phase) % switching_period
        if switching_period - pulse_delay <= 1e-12 * max(1.0, switching_period):
            pulse_delay = 0.0
        step = (
            default_sample_step_s(parameters, duration)
            if sample_step_s is None
            else float(sample_step_s)
        )
        step = min(step, duration / 10.0)
        pulse_width = max(duty_value * switching_period, 1e-15)

        circuit = Circuit("pe-sim pyspice buck reference")
        circuit.V("input", "vin", circuit.gnd, parameters.input_voltage_v @ u_V)
        circuit.PulseVoltageSource(
            "pwm",
            "pwm",
            circuit.gnd,
            initial_value=0 @ u_V,
            pulsed_value=1 @ u_V,
            pulse_width=pulse_width @ u_s,
            period=parameters.switching_period_s @ u_s,
            delay_time=pulse_delay @ u_s,
            rise_time=1e-9 @ u_s,
            fall_time=1e-9 @ u_s,
        )
        circuit.S("1", "vin", "sw", "pwm", circuit.gnd, model="SWMOD")
        circuit.D("1", circuit.gnd, "sw", model="DMOD")
        circuit.L(
            "1",
            "sw",
            "vout",
            parameters.inductance_h @ u_H,
            initial_condition=parameters.initial_inductor_current_a @ u_A,
        )
        circuit.C(
            "1",
            "vout",
            circuit.gnd,
            parameters.capacitance_f @ u_F,
            initial_condition=parameters.initial_output_voltage_v @ u_V,
        )
        circuit.R("1", "vout", circuit.gnd, parameters.load_resistance_ohm @ u_Ohm)
        circuit.model(
            "SWMOD",
            "SW",
            Ron=parameters.switch_on_resistance_ohm,
            Roff=parameters.switch_off_resistance_ohm,
            Vt=0.5,
            Vh=0,
        )
        circuit.model(
            "DMOD",
            "D",
            Is=parameters.diode_saturation_current_a,
            N=parameters.diode_emission_coefficient,
        )

        body = _strip_initial_condition_units(circuit.str()).rstrip()
        control = "\n".join(
            (
                solver.options_line(),
                f".temp {solver.temperature_c:.12e}",
                f".tran {step:.12e} {duration:.12e} 0 {step:.12e} uic",
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
            )
        )
        return body + "\n\n" + control

    def provenance(self) -> dict[str, Any]:
        version = _import_pyspice().__version__
        return {
            "name": self.name,
            "pyspice_version": str(version),
            "execution": "external-ngspice-process",
            "native_pyspice_execution": False,
            "limitations": [
                "PySpice is used for circuit construction only; execution uses the reviewed ngspice process adapter",
                "PySpice is GPLv3 and is not a core dependency",
            ],
        }


__all__ = [
    "NativeBuckNetlistBuilder",
    "NetlistBuilder",
    "NetlistBuilderError",
    "PyspiceBuckNetlistBuilder",
    "PyspiceUnavailableError",
    "pyspice_version",
]
