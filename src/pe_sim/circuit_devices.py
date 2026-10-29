"""Closed device vocabulary for circuit topologies a netlist compiler can render.

An agent that writes a topology has two questions the descriptor contract alone
cannot answer: *which* component kinds exist, and *what* each kind must be told
before it can become a circuit.  Answering that from ``pe_sim.topology`` would
mean reading the descriptor builders, which know one specific converter each.
This module is the declared answer instead: a closed list of device kinds, each
with its ordered ports, the net kinds each port may bind, and the parameters it
renders.

The vocabulary is a **device** vocabulary, not a topology vocabulary.  Nothing
here names a converter: ``voltage_source``, ``inductor`` and
``voltage_controlled_switch`` are elements, and the converter an agent builds
out of them is its own invention.  That keeps the kernel naming neither an
engine nor a topology (``docs/43`` invariant I5) while still giving the
compiler a finite, checkable surface.

Three boundaries are deliberate:

- **A device that cannot be rendered says so.**  ``averaged_switch`` is a real
  kind because the project ships averaged descriptors that use it, but it
  declares no SPICE letter: it names a behavioural block, and there is no
  netlist for it.  A compiler asked to render one must refuse rather than
  invent a circuit.
- **Parameters carry units.**  ``ParameterSpec`` is reused from
  ``pe_sim.plant_parameters`` so a circuit parameter and a ``plant_config``
  field are described by one shape; an agent that learned one has learned the
  other.
- **Parameters with a default are optional; the rest are required.**  A device
  whose required parameter is missing cannot be rendered, and the compiler must
  not substitute a plausible-looking number.

Like ``plant_parameters``, this module is stdlib-only: a client can read the
vocabulary without importing an adapter or a simulator binding.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .plant_parameters import ParameterSpec


#: Schema tag for ``describe_devices``; a client branches on this tag rather
#: than on the module layout.
CIRCUIT_DEVICE_SCHEMA = "pe-sim.circuit-devices.v1"

NET_KIND_ELECTRICAL = "electrical"
NET_KIND_CONTROL = "control"
NET_KIND_REFERENCE = "reference"

#: Net kinds a compiled circuit may contain.  ``thermal`` and ``other`` are
#: legitimate descriptor annotations but carry no electrical meaning, so a
#: topology that contains them cannot be turned into a netlist.
COMPILABLE_NET_KINDS = frozenset(
    {NET_KIND_ELECTRICAL, NET_KIND_CONTROL, NET_KIND_REFERENCE}
)

#: Port directions, mirroring ``TopologyPort.direction`` exactly.  Ports are
#: ordered: the order is the node order a SPICE element card must emit.
PORT_DIRECTIONS = frozenset({"input", "output", "bidirectional", "passive", "control"})

#: Per-instance metadata a component may carry that is not a device parameter
#: and is never rendered.  ``phase_index`` says which phase of a multiphase
#: converter an instance belongs to, which a display needs and a netlist does
#: not: the phase is expressed by the instance's own nodes.
DEVICE_ANNOTATIONS = frozenset({"phase_index"})

_ELECTRICAL = frozenset({NET_KIND_ELECTRICAL})
_ELECTRICAL_OR_REFERENCE = frozenset({NET_KIND_ELECTRICAL, NET_KIND_REFERENCE})
_CONTROL = frozenset({NET_KIND_CONTROL})


@dataclass(frozen=True)
class PortSpec:
    """One declared terminal of a device kind.

    ``net_kinds`` is a set because the same physical pin legitimately lands on
    an electrical node in one circuit and on the reference node in another: the
    low-side switch of a synchronous converter returns to the reference node,
    and the capacitor's negative plate does the same.
    """

    port_id: str
    net_kinds: frozenset[str]
    direction: str
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.port_id, str) or not self.port_id.strip():
            raise ValueError("port_id must be a non-empty string")
        if not isinstance(self.net_kinds, frozenset) or not self.net_kinds:
            raise ValueError(f"port {self.port_id!r} must declare at least one net kind")
        unknown = set(self.net_kinds) - set(COMPILABLE_NET_KINDS)
        if unknown:
            raise ValueError(
                f"port {self.port_id!r} declares unknown net kinds: {sorted(unknown)}"
            )
        if self.direction not in PORT_DIRECTIONS:
            raise ValueError(
                f"port {self.port_id!r} declares unknown direction {self.direction!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "net_kinds": sorted(self.net_kinds),
            "direction": self.direction,
            "description": self.description,
        }


@dataclass(frozen=True)
class DeviceSpec:
    """One declared component kind and everything needed to render it.

    ``spicetype`` is the SPICE element letter (``v``, ``l``, ``c``, ``r``, ``s``,
    ``d``) or ``None`` when the kind has no netlist rendering.  ``aliases``
    exists so a descriptor written against an older spelling still resolves to
    one canonical kind instead of appearing as an unknown device.
    """

    kind: str
    ports: tuple[PortSpec, ...]
    parameters: tuple[ParameterSpec, ...] = ()
    spicetype: str | None = None
    model_card: str | None = None
    aliases: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("device kind must be a non-empty string")
        ports = tuple(self.ports)
        if not ports:
            raise ValueError(f"device kind {self.kind!r} must declare at least one port")
        port_ids = [port.port_id for port in ports]
        if len(port_ids) != len(set(port_ids)):
            raise ValueError(f"device kind {self.kind!r} declares duplicate port ids")
        object.__setattr__(self, "ports", ports)
        parameters = tuple(self.parameters)
        names = [spec.name for spec in parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"device kind {self.kind!r} declares duplicate parameters")
        object.__setattr__(self, "parameters", parameters)
        if self.spicetype is not None:
            if not isinstance(self.spicetype, str) or len(self.spicetype) != 1:
                raise ValueError(
                    f"device kind {self.kind!r} must declare a one-letter spicetype"
                )
            if self.spicetype != self.spicetype.lower():
                raise ValueError(
                    f"device kind {self.kind!r} spicetype must be lower case"
                )
        if self.spicetype is None and self.model_card is not None:
            raise ValueError(
                f"device kind {self.kind!r} declares a model card but no spicetype"
            )
        aliases = tuple(self.aliases)
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError(f"device kind {self.kind!r} declares an empty alias")
        if self.kind in aliases:
            raise ValueError(
                f"device kind {self.kind!r} cannot alias itself"
            )
        object.__setattr__(self, "aliases", aliases)

    @property
    def renderable(self) -> bool:
        """Whether a netlist compiler can emit this device at all."""

        return self.spicetype is not None

    @property
    def required_parameters(self) -> tuple[str, ...]:
        """Parameters with no default, which a descriptor must therefore supply."""

        return tuple(
            spec.name
            for spec in self.parameters
            if spec.default is None and not spec.nullable
        )

    @property
    def parameter_names(self) -> frozenset[str]:
        return frozenset(spec.name for spec in self.parameters)

    @property
    def accepted_parameter_keys(self) -> frozenset[str]:
        """Keys a component of this kind may carry in ``parameters``.

        Declared device parameters plus the reserved annotations.  A key outside
        this set is a descriptor error rather than something the compiler may
        silently drop.
        """

        return self.parameter_names | DEVICE_ANNOTATIONS

    @property
    def port_ids(self) -> tuple[str, ...]:
        return tuple(port.port_id for port in self.ports)

    def port(self, port_id: str) -> PortSpec | None:
        for candidate in self.ports:
            if candidate.port_id == port_id:
                return candidate
        return None

    def parameter(self, name: str) -> ParameterSpec | None:
        for candidate in self.parameters:
            if candidate.name == name:
                return candidate
        return None


def _p(
    name: str,
    unit: str | None,
    default: Any,
    description: str,
    *,
    kind: str = "number",
    minimum: float | None = None,
    exclusive_minimum: bool = False,
    maximum: float | None = None,
) -> ParameterSpec:
    return ParameterSpec(
        name=name,
        kind=kind,
        unit=unit,
        minimum=minimum,
        exclusive_minimum=exclusive_minimum,
        maximum=maximum,
        default=default,
        description=description,
    )


def _positive(
    name: str, unit: str | None, default: Any, description: str
) -> ParameterSpec:
    return _p(
        name,
        unit,
        default,
        description,
        minimum=0.0,
        exclusive_minimum=True,
    )


def _quantity(name: str, unit: str | None, description: str) -> ParameterSpec:
    """A required, strictly positive physical quantity."""

    return _positive(name, unit, None, description)


_VOLTAGE_SOURCE = DeviceSpec(
    kind="voltage_source",
    ports=(
        PortSpec(
            "positive",
            _ELECTRICAL,
            "output",
            "Driven electrical node; the element card puts this node first.",
        ),
        PortSpec(
            "negative",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Return node; usually the reference net, but a floating source is legal.",
        ),
    ),
    parameters=(
        _quantity("voltage_v", "V", "DC value the source holds for the whole window."),
    ),
    spicetype="v",
    description="Ideal independent DC voltage source with zero output impedance.",
)

_PULSE_SOURCE = DeviceSpec(
    kind="pulse_source",
    ports=(
        PortSpec(
            "positive",
            _CONTROL,
            "output",
            "Control net the carrier drives; gates of every actuator bind this net.",
        ),
        PortSpec(
            "negative",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Carrier reference node; a high-side gate drive may float on the switch node.",
        ),
    ),
    parameters=(
        _quantity(
            "switching_frequency_hz",
            "Hz",
            "Carrier frequency; the switching period is its reciprocal.",
        ),
        _positive(
            "amplitude_v",
            "V",
            1.0,
            "High level of the carrier; a gate threshold below this value turns the switch on.",
        ),
        _positive(
            "rise_time_s",
            "s",
            1e-9,
            "Carrier rise time; made short because the element is an ideal gate driver.",
        ),
        _positive(
            "fall_time_s",
            "s",
            1e-9,
            "Carrier fall time.",
        ),
        ParameterSpec(
            name="dead_time_s",
            kind="number",
            unit="s",
            minimum=0.0,
            default=None,
            nullable=True,
            description=(
                "Dead time recorded on the carrier itself.  It is never "
                "rendered: ``metadata.control.dead_time_s`` is the single "
                "authority the compiler renders, and the compiler refuses a "
                "carrier value that disagrees with it.  Kept so a descriptor "
                "can record what a reviewed template states."
            ),
        ),
    ),
    spicetype="v",
    description=(
        "Periodic gate-drive carrier.  The pulse width is not a device parameter: "
        "it is derived from the duty ratio supplied at compile time, so the same "
        "descriptor can be compiled for any commanded duty."
    ),
)

_INDUCTOR = DeviceSpec(
    kind="inductor",
    ports=(
        PortSpec(
            "input",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "First node of the element card.  An inductor is a two-terminal path "
            "and either end may be the reference, as in a choke returned to ground.",
        ),
        PortSpec(
            "output",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Second node of the element card.",
        ),
    ),
    parameters=(
        _quantity("inductance_h", "H", "Inductance; must be finite and positive."),
        _p(
            "initial_current_a",
            "A",
            0.0,
            "Current through the inductor at the start of the solve window.",
            minimum=0.0,
        ),
    ),
    spicetype="l",
    description="Ideal linear inductor of the declared inductance.",
)

_CAPACITOR = DeviceSpec(
    kind="capacitor",
    ports=(
        PortSpec(
            "positive",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Plate node.  The element is electrically symmetric, so binding this "
            "terminal to the reference net expresses a negative output rail; an "
            "electrical-only rule here carried no information.",
        ),
        PortSpec(
            "negative",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Negative plate node; the reference net is allowed.",
        ),
    ),
    parameters=(
        _quantity("capacitance_f", "F", "Capacitance; must be finite and positive."),
        _p(
            "initial_voltage_v",
            "V",
            0.0,
            "Voltage across the capacitor at the start of the solve window.",
            minimum=0.0,
        ),
    ),
    spicetype="c",
    description="Ideal linear capacitor of the declared capacitance.",
)
_RESISTOR_LOAD = DeviceSpec(
    kind="resistor_load",
    ports=(
        PortSpec(
            "positive",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Load terminal.  The element is electrically symmetric, so binding it "
            "to the reference net expresses a negative output rail; an "
            "electrical-only rule here carried no information.",
        ),
        PortSpec(
            "negative",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Load return node; the reference net is allowed.",
        ),
    ),
    parameters=(
        _quantity("resistance_ohm", "Ohm", "Load resistance; must be finite and positive."),
    ),
    spicetype="r",
    description=(
        "Resistive load.  This is the graph's only output sink, so a topology "
        "without one declares no observable output."
    ),
)

_VOLTAGE_CONTROLLED_SWITCH = DeviceSpec(
    kind="voltage_controlled_switch",
    ports=(
        PortSpec(
            "input",
            _ELECTRICAL_OR_REFERENCE,
            "input",
            "Conducting path, first terminal.  A switch is a two-terminal path and "
            "the element card is symmetric, so either end may be the reference; "
            "the low-side orientation ``gnd -> node`` is a legal declaration.",
        ),
        PortSpec(
            "output",
            _ELECTRICAL_OR_REFERENCE,
            "output",
            "Conducting path, return side; a low-side switch returns to the reference net.",
        ),
        PortSpec(
            "control",
            _CONTROL,
            "control",
            "Control net whose voltage decides conduction.",
        ),
    ),
    parameters=(
        _positive(
            "on_resistance_ohm",
            "Ohm",
            1e-3,
            "Closed-state resistance; small but non-zero so the node matrix stays regular.",
        ),
        _positive(
            "off_resistance_ohm",
            "Ohm",
            1e9,
            "Open-state resistance; large but finite so the node does not float numerically.",
        ),
        _p(
            "threshold_v",
            "V",
            0.5,
            "Control voltage at which the switch changes state.",
        ),
        _p(
            "hysteresis_v",
            "V",
            0.0,
            "Half-width of the switching band; zero makes the transition abrupt.",
            minimum=0.0,
        ),
    ),
    spicetype="s",
    model_card="switch",
    description=(
        "Ideal voltage-controlled two-terminal switch.  It is the actuator: the "
        "kernel's single commanded duty ratio becomes this control net."
    ),
)

_FREEWHEELING_DIODE = DeviceSpec(
    kind="freewheeling_diode",
    ports=(
        PortSpec(
            "anode",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Anode node; a freewheeling diode's anode usually binds the reference net.",
        ),
        PortSpec(
            "cathode",
            _ELECTRICAL_OR_REFERENCE,
            "passive",
            "Cathode node; a diode returned to ground is a legal orientation too.",
        ),
    ),
    parameters=(
        _positive(
            "saturation_current_a",
            "A",
            1e-12,
            "Shockley saturation current; small values approximate an ideal diode.",
        ),
        _positive(
            "emission_coefficient",
            None,
            0.02,
            "Dimensionless emission coefficient; a small value steepens the knee.",
        ),
    ),
    spicetype="d",
    model_card="diode",
    aliases=("diode",),
    description="Shockley diode used as a freewheeling element.",
)

_AVERAGED_SWITCH = DeviceSpec(
    kind="averaged_switch",
    ports=(
        PortSpec("input", _ELECTRICAL, "input", "Node the averaged block draws from."),
        PortSpec(
            "output",
            _ELECTRICAL_OR_REFERENCE,
            "output",
            "Node the averaged block delivers to.",
        ),
        PortSpec("control", _CONTROL, "control", "Duty-ratio control net."),
    ),
    parameters=(),
    spicetype=None,
    description=(
        "Averaged behavioural switch.  It is a real descriptor kind because the "
        "project ships averaged reference models that declare it, but it names a "
        "cycle-averaged block and has no element card: a compiler must refuse it "
        "rather than invent a circuit that was never reviewed."
    ),
)


#: The closed vocabulary.  Adding a kind is a contract event: it widens what an
#: agent may write and what a compiler must render.
CIRCUIT_DEVICES: Mapping[str, DeviceSpec] = MappingProxyType(
    {
        spec.kind: spec
        for spec in (
            _VOLTAGE_SOURCE,
            _PULSE_SOURCE,
            _INDUCTOR,
            _CAPACITOR,
            _RESISTOR_LOAD,
            _VOLTAGE_CONTROLLED_SWITCH,
            _FREEWHEELING_DIODE,
            _AVERAGED_SWITCH,
        )
    }
)

#: Alias lookup, including the canonical names, so one call resolves any
#: spelling a descriptor may carry.
_ALIAS_TO_KIND: Mapping[str, str] = MappingProxyType(
    {
        **{kind: kind for kind in CIRCUIT_DEVICES},
        **{
            alias: spec.kind
            for spec in CIRCUIT_DEVICES.values()
            for alias in spec.aliases
        },
    }
)

#: The kinds a compiler can emit, in a stable order.
RENDERABLE_DEVICE_KINDS: tuple[str, ...] = tuple(
    kind for kind, spec in CIRCUIT_DEVICES.items() if spec.renderable
)


def canonical_device_kind(kind: str) -> str | None:
    """Resolve a descriptor kind to its canonical name, or ``None`` if unknown."""

    return _ALIAS_TO_KIND.get(str(kind))


def device_spec(kind: str) -> DeviceSpec:
    """Return one device declaration, failing with the whole known list."""

    canonical = canonical_device_kind(kind)
    if canonical is None:
        raise KeyError(
            f"unknown device kind {kind!r}; known kinds: {sorted(CIRCUIT_DEVICES)}"
        )
    return CIRCUIT_DEVICES[canonical]


def _json_type(spec: ParameterSpec) -> str | list[str]:
    if spec.kind == "boolean":
        return "boolean"
    if spec.kind == "integer":
        return "integer"
    return ["number", "null"] if spec.nullable else "number"


def device_parameter_schema(kind: str) -> dict[str, Any]:
    """Return the closed JSON Schema fragment for one device's parameters.

    The object is closed for the same reason a ``plant_config`` schema is: the
    compiler refuses unknown keys, so an open schema would let a descriptor
    validate here and fail at compile time.
    """

    spec = device_spec(kind)
    properties: dict[str, Any] = {}
    for parameter in sorted(spec.parameters, key=lambda item: item.name):
        entry: dict[str, Any] = {"type": _json_type(parameter)}
        if parameter.minimum is not None:
            if parameter.exclusive_minimum:
                entry["exclusiveMinimum"] = parameter.minimum
            else:
                entry["minimum"] = parameter.minimum
        if parameter.maximum is not None:
            entry["maximum"] = parameter.maximum
        entry["default"] = parameter.default
        entry["description"] = parameter.description
        if parameter.unit is not None:
            entry["x-unit"] = parameter.unit
        properties[parameter.name] = entry
    return {"type": "object", "additionalProperties": False, "properties": properties}


def describe_device(kind: str) -> dict[str, Any]:
    """Return the agent-facing declaration of one device kind, deterministically."""

    spec = device_spec(kind)
    return {
        "schema": CIRCUIT_DEVICE_SCHEMA,
        "kind": spec.kind,
        "aliases": list(spec.aliases),
        "renderable": spec.renderable,
        "spicetype": spec.spicetype,
        "model_card": spec.model_card,
        "ports": [port.as_dict() for port in spec.ports],
        "parameters": [
            parameter.as_dict()
            for parameter in sorted(spec.parameters, key=lambda item: item.name)
        ],
        "required_parameters": list(spec.required_parameters),
        "parameter_schema": device_parameter_schema(spec.kind),
        "unknown_keys": "rejected",
        "description": spec.description,
    }


def describe_devices() -> dict[str, Any]:
    """Return the whole closed vocabulary as one JSON-safe document."""

    return {
        "schema": CIRCUIT_DEVICE_SCHEMA,
        "device_kinds": [describe_device(kind) for kind in sorted(CIRCUIT_DEVICES)],
        "renderable_kinds": list(RENDERABLE_DEVICE_KINDS),
        "net_kinds": sorted(COMPILABLE_NET_KINDS),
        "unknown_kinds": "rejected",
        "notes": [
            "Only renderable kinds can be compiled into a netlist; a descriptor "
            "that declares an averaged block is a valid descriptor but not a "
            "compilable circuit.",
            "A parameter with a default may be omitted; every other parameter "
            "must be supplied because the compiler never substitutes a value.",
        ],
    }


def closest_device_kind(kind: str, candidates: Iterable[str] | None = None) -> str | None:
    """Return the nearest known kind, or ``None`` when nothing is close enough.

    The same discipline as the specification validator applies: a suggestion is
    a hint that must be safe to follow.  Two gates keep it honest - a very
    short input is more likely to be invented junk than a typo, and a weak
    match is not evidence of intent.  Suggesting a real kind for either would
    silently turn a wrong descriptor into a plausible circuit.
    """

    text = str(kind)
    if len(text) < 5:
        return None
    pool = sorted(CIRCUIT_DEVICES if candidates is None else candidates)
    best: tuple[float, str] | None = None
    for candidate in pool:
        score = difflib.SequenceMatcher(None, text.lower(), candidate.lower()).ratio()
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is None or best[0] < 0.75:
        return None
    return best[1]
