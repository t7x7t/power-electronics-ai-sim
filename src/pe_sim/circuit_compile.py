"""Deterministic netlist compilation for a declared circuit topology.

Everything upstream of this module answers a question about a *document*: is it
a graph the kernel can hold, does it name real devices, is it connected.  This
module answers the one remaining authoring question - can that graph become a
circuit a solver can run - and it answers by producing the netlist.

Three properties are deliberate:

- **Deterministic text.**  Element cards follow descriptor order, values use one
  fixed format, and nothing time-, host-, or path-dependent is written.  The
  SHA-256 of the result is therefore a stable identity for the compiled circuit,
  which is what makes a compiled netlist citable at all.
- **Refusal over invention.**  A descriptor the compiler cannot render exactly
  is rejected with a code, never approximated.  An averaged behavioural block
  has no element card; a gate net driving two switches would render
  complementary devices as simultaneously on; a missing parameter has no value
  the compiler is entitled to pick.  Each of those is a distinct refusal.
- **One commanded scalar.**  The kernel commands exactly one dimensionless duty
  ratio.  The compiler therefore requires exactly one actuator and exactly one
  carrier, and derives the pulse width from the duty it is given instead of
  reading one out of the descriptor.

The compiler does not execute anything and imports no backend: the text it
returns is handed to whatever process adapter the caller trusts.  A compiled
netlist is a *well-formed circuit*, not a validated model - no reviewer has
accepted it, so nothing built from it may claim comparable evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .circuit_devices import (
    COMPILABLE_NET_KINDS,
    NET_KIND_REFERENCE,
    canonical_device_kind,
    device_spec,
)
from .topology import TopologyDescriptor


#: Template tag written into every compiled netlist.  Bumping it is a visible
#: change to the compiled-circuit identity.
CIRCUIT_NETLIST_TEMPLATE_VERSION = "circuit-netlist-v1"

#: The closed vocabulary of compile refusals.
CIRCUIT_COMPILE_CODES = frozenset(
    {
        "ambiguous_reference_net",
        "invalid_duration",
        "invalid_duty",
        "invalid_integrator",
        "invalid_option",
        "invalid_parameter",
        "invalid_sample_step",
        "missing_parameter",
        "multiple_actuators",
        "multiple_carriers",
        "no_carrier",
        "not_compilable",
        "unsupported_net_kind",
    }
)

#: Integrators the compiled ``.tran`` line may request.  Both are ngspice's own
#: names; the project does not reinterpret them.
SUPPORTED_INTEGRATORS = frozenset({"trap", "gear", "euler"})

#: Numerical options written when the caller supplies none.  They are the
#: conservative values a switching solve needs, and they are *written into the
#: netlist* so the digest identifies them along with the circuit.
DEFAULT_CIRCUIT_OPTIONS: Mapping[str, Any] = MappingProxyType(
    {
        "abstol": 1e-12,
        "gmin": 1e-12,
        "reltol": 1e-3,
        "vntol": 1e-6,
    }
)

_REFERENCE_NODE = "0"


class CircuitCompileError(ValueError):
    """A descriptor that cannot be compiled, with a code a caller can branch on."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if code not in CIRCUIT_COMPILE_CODES:
            raise ValueError(f"unknown circuit compile code: {code!r}")
        self.code = code
        self.details = dict(details)
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": dict(self.details)}


@dataclass(frozen=True)
class CompiledCircuit:
    """One rendered netlist plus the facts a caller needs to cite it."""

    netlist: str
    digest: str
    topology_id: str
    descriptor_fingerprint: str
    template: str
    duty: float
    duration_s: float
    sample_step_s: float
    reference_net: str
    saved_signals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "digest": self.digest,
            "topology_id": self.topology_id,
            "descriptor_fingerprint": self.descriptor_fingerprint,
            "duty": self.duty,
            "duration_s": self.duration_s,
            "sample_step_s": self.sample_step_s,
            "reference_net": self.reference_net,
            "saved_signals": list(self.saved_signals),
        }


def _format(value: float) -> str:
    """The one numeric format used by every compiled line."""

    return f"{float(value):.12e}"


def _finite_positive(name: str, value: Any, code: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise CircuitCompileError(code, f"{name} must be finite and positive")
    return number


def _parameter(
    component: Any,
    name: str,
    *,
    required: bool = False,
) -> float | None:
    """Read one declared parameter, falling back to its declared default."""

    device = device_spec(component.kind)
    spec = device.parameter(name)
    if spec is None:  # pragma: no cover - guarded by the device vocabulary
        raise CircuitCompileError(
            "invalid_parameter",
            f"device kind {device.kind!r} does not declare {name!r}",
        )
    if name in component.parameters:
        raw = component.parameters[name]
    elif spec.default is not None:
        raw = spec.default
    elif required:
        raise CircuitCompileError(
            "missing_parameter",
            f"component {component.component_id!r} has no {name!r}, and the "
            "compiler never substitutes a value for a parameter without a default",
            component_id=component.component_id,
            parameter=name,
        )
    else:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CircuitCompileError(
            "invalid_parameter",
            f"parameter {name!r} of {component.component_id!r} must be a number",
            component_id=component.component_id,
            parameter=name,
        )
    number = float(raw)
    if not math.isfinite(number):
        raise CircuitCompileError(
            "invalid_parameter",
            f"parameter {name!r} of {component.component_id!r} must be finite",
            component_id=component.component_id,
            parameter=name,
        )
    if spec.minimum is not None:
        if spec.exclusive_minimum and not number > spec.minimum:
            raise CircuitCompileError(
                "invalid_parameter",
                f"parameter {name!r} of {component.component_id!r} must be greater "
                f"than {spec.minimum}",
                component_id=component.component_id,
                parameter=name,
            )
        if not spec.exclusive_minimum and not number >= spec.minimum:
            raise CircuitCompileError(
                "invalid_parameter",
                f"parameter {name!r} of {component.component_id!r} must be at least "
                f"{spec.minimum}",
                component_id=component.component_id,
                parameter=name,
            )
    if spec.maximum is not None and not number <= spec.maximum:
        raise CircuitCompileError(
            "invalid_parameter",
            f"parameter {name!r} of {component.component_id!r} must be at most "
            f"{spec.maximum}",
            component_id=component.component_id,
            parameter=name,
        )
    return number


def default_sample_step_s(switching_period_s: float, duration_s: float) -> float:
    """Choose an output step that resolves the carrier without exceeding the window.

    This mirrors the reviewed Buck template's rule so a compiled circuit and the
    project-owned one are sampled on the same schedule for the same inputs.
    """

    period = _finite_positive("switching_period_s", switching_period_s, "invalid_parameter")
    duration = _finite_positive("duration_s", duration_s, "invalid_duration")
    candidate = min(duration / 1000.0, period / 200.0)
    candidate = max(candidate, 1e-12)
    if candidate > duration / 10.0:
        candidate = duration / 10.0
    return candidate


def _node(net_id: str, reference_net: str) -> str:
    return _REFERENCE_NODE if net_id == reference_net else net_id


def _render_options(
    options: Mapping[str, Any] | None, integrator: str
) -> str:
    merged: dict[str, str] = {
        str(key): _option_value(str(key), value)
        for key, value in (options or DEFAULT_CIRCUIT_OPTIONS).items()
    }
    merged["method"] = integrator
    tokens = " ".join(f"{key}={merged[key]}" for key in sorted(merged))
    return f".options {tokens}"


def _option_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        raise CircuitCompileError(
            "invalid_option",
            f"option {name!r} must be a number or a short name, not a boolean",
        )
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise CircuitCompileError(
                "invalid_option",
                f"option {name!r} must be finite and positive",
            )
        return _format(number)
    text = str(value).strip()
    if not text or any(character.isspace() for character in text):
        raise CircuitCompileError(
            "invalid_option",
            f"option {name!r} must be a single non-empty token",
        )
    return text


def compile_topology(
    descriptor: TopologyDescriptor,
    *,
    duty: float,
    duration_s: float,
    sample_step_s: float | None = None,
    integrator: str = "trap",
    options: Mapping[str, Any] | None = None,
    num_digits: int = 12,
) -> CompiledCircuit:
    """Render ``descriptor`` as one ngspice netlist.

    ``duty`` is held constant for the whole window.  The carrier's pulse width is
    derived from it, so a descriptor declares the switching frequency and the
    caller declares the command - the two are never the same field.
    """

    duty_value = float(duty)
    if not math.isfinite(duty_value) or not 0.0 <= duty_value <= 1.0:
        raise CircuitCompileError("invalid_duty", "duty must be finite and in [0, 1]")
    if integrator not in SUPPORTED_INTEGRATORS:
        # Checked before any rendering so a bad argument cannot fail halfway
        # through a netlist that then looks like a circuit defect.
        raise CircuitCompileError(
            "invalid_integrator",
            f"unsupported integrator {integrator!r}",
            supported=sorted(SUPPORTED_INTEGRATORS),
        )
    duration = _finite_positive("duration_s", duration_s, "invalid_duration")
    if not isinstance(num_digits, int) or isinstance(num_digits, bool) or num_digits < 1:
        raise CircuitCompileError(
            "invalid_option", "num_digits must be a positive integer"
        )

    nets: dict[str, str] = {}
    for net in descriptor.nets:
        if net.kind not in COMPILABLE_NET_KINDS:
            raise CircuitCompileError(
                "unsupported_net_kind",
                f"net {net.net_id!r} has kind {net.kind!r}, which has no electrical "
                "meaning",
                net_id=net.net_id,
            )
        nets[net.net_id] = net.kind

    reference_nets = sorted(
        net_id for net_id, kind in nets.items() if kind == NET_KIND_REFERENCE
    )
    if len(reference_nets) != 1:
        raise CircuitCompileError(
            "ambiguous_reference_net",
            "a compiled circuit needs exactly one reference net, found "
            f"{len(reference_nets)}",
            reference_nets=reference_nets,
        )
    reference_net = reference_nets[0]

    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        if canonical is None:
            raise CircuitCompileError(
                "not_compilable",
                f"component {component.component_id!r} declares unknown device kind "
                f"{component.kind!r}",
                component_id=component.component_id,
            )
        if not device_spec(canonical).renderable:
            raise CircuitCompileError(
                "not_compilable",
                f"component {component.component_id!r} of kind {canonical!r} is a "
                "behavioural block with no element card, so it cannot be rendered "
                "as a circuit",
                component_id=component.component_id,
                kind=canonical,
            )

    actuators = [
        component
        for component in descriptor.components
        if canonical_device_kind(component.kind) == "voltage_controlled_switch"
    ]
    if len(actuators) != 1:
        raise CircuitCompileError(
            "multiple_actuators",
            "the compiler renders exactly one actuator because the kernel commands "
            f"one duty ratio; the descriptor declares {len(actuators)}.  Two switches "
            "on one control net would render complementary devices as simultaneously "
            "on, and two control nets have no defined relation yet.",
            actuators=len(actuators),
        )

    carriers = [
        component
        for component in descriptor.components
        if canonical_device_kind(component.kind) == "pulse_source"
    ]
    if not carriers:
        raise CircuitCompileError(
            "no_carrier",
            "the descriptor declares no carrier, so there is no gate signal to "
            "derive from the commanded duty",
        )
    if len(carriers) > 1:
        raise CircuitCompileError(
            "multiple_carriers",
            "the descriptor declares more than one carrier; the commanded duty "
            "buys exactly one pulse width",
            carriers=len(carriers),
        )
    carrier = carriers[0]

    switching_frequency = _parameter(carrier, "switching_frequency_hz", required=True)
    assert switching_frequency is not None
    amplitude = _parameter(carrier, "amplitude_v") or 1.0
    rise_time = _parameter(carrier, "rise_time_s") or 1e-9
    fall_time = _parameter(carrier, "fall_time_s") or 1e-9
    switching_period = 1.0 / switching_frequency
    pulse_width = max(duty_value * switching_period, 1e-15)

    step = (
        default_sample_step_s(switching_period, duration)
        if sample_step_s is None
        else _finite_positive("sample_step_s", sample_step_s, "invalid_sample_step")
    )
    step = min(step, duration / 10.0)

    counters: dict[str, int] = {}
    element_names: dict[str, str] = {}
    cards: list[str] = []
    model_cards: list[str] = []
    saved: list[str] = []

    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        assert canonical is not None
        spec = device_spec(canonical)
        letter = spec.spicetype.upper()
        assert letter is not None
        counters[letter] = counters.get(letter, 0) + 1
        name = f"{letter}{counters[letter]}"
        element_names[component.component_id] = name
        nodes = {
            port.port_id: _node(
                next(
                    bound.net_id
                    for bound in component.ports
                    if bound.port_id == port.port_id
                ),
                reference_net,
            )
            for port in component.ports
        }

        if canonical == "voltage_source":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} DC "
                f"{_format(_parameter(component, 'voltage_v', required=True))}"
            )
        elif canonical == "pulse_source":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} PULSE("
                f"0 {_format(amplitude)} 0 {_format(rise_time)} {_format(fall_time)} "
                f"{_format(pulse_width)} {_format(switching_period)})"
            )
        elif canonical == "inductor":
            cards.append(
                f"{name} {nodes['input']} {nodes['output']} "
                f"{_format(_parameter(component, 'inductance_h', required=True))} "
                f"IC={_format(_parameter(component, 'initial_current_a') or 0.0)}"
            )
            saved.append(f"i({name})")
        elif canonical == "capacitor":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} "
                f"{_format(_parameter(component, 'capacitance_f', required=True))} "
                f"IC={_format(_parameter(component, 'initial_voltage_v') or 0.0)}"
            )
        elif canonical == "resistor_load":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} "
                f"{_format(_parameter(component, 'resistance_ohm', required=True))}"
            )
        elif canonical == "voltage_controlled_switch":
            cards.append(
                f"{name} {nodes['input']} {nodes['output']} {nodes['control']} "
                f"{_REFERENCE_NODE} {name}"
            )
            model_cards.append(
                f".model {name} SW(Ron="
                f"{_format(_parameter(component, 'on_resistance_ohm') or 1e-3)} "
                f"Roff={_format(_parameter(component, 'off_resistance_ohm') or 1e9)} "
                f"Vt={_format(_parameter(component, 'threshold_v') or 0.5)} "
                f"Vh={_format(_parameter(component, 'hysteresis_v') or 0.0)})"
            )
        elif canonical == "freewheeling_diode":
            cards.append(f"{name} {nodes['anode']} {nodes['cathode']} {name}")
            model_cards.append(
                f".model {name} D(Is="
                f"{_format(_parameter(component, 'saturation_current_a') or 1e-12)} "
                f"N={_format(_parameter(component, 'emission_coefficient') or 0.02)})"
            )
        else:  # pragma: no cover - guarded above by ``renderable``
            raise CircuitCompileError(
                "not_compilable",
                f"no element card is defined for device kind {canonical!r}",
                component_id=component.component_id,
            )

    saved = [f"v({net_id})" for net_id in nets if net_id != reference_net] + saved

    lines = [
        "* pe-sim compiled circuit",
        f"* template: {CIRCUIT_NETLIST_TEMPLATE_VERSION}",
        f"* topology: {descriptor.topology_id}",
        f"* descriptor_fingerprint: {descriptor.fingerprint}",
        f"* duty: {_format(duty_value)}",
        "",
        _render_options(options, integrator),
        f".temp {_format(27.0)}",
        f".tran {_format(step)} {_format(duration)} 0 {_format(step)} uic",
        "",
        *cards,
        *model_cards,
        "",
        ".control",
        "set filetype=ascii",
        "set wr_singlescale",
        "set wr_vecnames",
        f"set numdgt={num_digits}",
        "run",
        "wrdata output.dat " + " ".join(saved),
        ".endc",
        ".end",
        "",
    ]
    netlist = "\n".join(lines)
    return CompiledCircuit(
        netlist=netlist,
        digest=circuit_netlist_digest(netlist),
        topology_id=descriptor.topology_id,
        descriptor_fingerprint=descriptor.fingerprint,
        template=CIRCUIT_NETLIST_TEMPLATE_VERSION,
        duty=duty_value,
        duration_s=duration,
        sample_step_s=step,
        reference_net=reference_net,
        saved_signals=tuple(saved),
    )


def circuit_netlist_digest(text: str) -> str:
    """Return the stable SHA-256 identity of one compiled netlist."""

    if not isinstance(text, str) or not text:
        raise ValueError("netlist text must be a non-empty string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "CIRCUIT_COMPILE_CODES",
    "CIRCUIT_NETLIST_TEMPLATE_VERSION",
    "DEFAULT_CIRCUIT_OPTIONS",
    "SUPPORTED_INTEGRATORS",
    "CircuitCompileError",
    "CompiledCircuit",
    "circuit_netlist_digest",
    "compile_topology",
    "default_sample_step_s",
]
