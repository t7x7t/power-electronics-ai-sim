"""Versioned, renderer-neutral circuit topology descriptors.

The descriptor is deliberately a semantic graph rather than a drawing format.
Components and nets describe connectivity; ``symbol_id`` is only a rendering
hint and can be replaced by a reviewed or AI-generated symbol asset without
changing the simulated circuit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


TOPOLOGY_SCHEMA = "topology-descriptor-v1"
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects mutation after publication."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("topology descriptor mappings are read-only")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable


def _freeze_json(value: Any) -> Any:
    """Defensively copy JSON values into immutable containers."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("topology JSON object keys must be strings")
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("topology metadata and parameters must be JSON serializable") from exc
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _id(value: str, field_name: str) -> str:
    value = str(value)
    if not value or not _ID_RE.fullmatch(value) or value in {".", ".."} or ".." in value:
        raise ValueError(f"{field_name} must be a safe non-empty identifier")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


@dataclass(frozen=True)
class TopologyNet:
    """A named electrical or control net in a topology graph."""

    net_id: str
    label: str | None = None
    kind: str = "electrical"

    def __post_init__(self) -> None:
        _id(self.net_id, "net_id")
        if self.label is not None and not str(self.label).strip():
            raise ValueError("net label must be non-empty when supplied")
        if self.kind not in {"electrical", "control", "reference", "thermal", "other"}:
            raise ValueError("unsupported net kind")

    def to_dict(self) -> dict[str, Any]:
        result = {"net_id": self.net_id, "kind": self.kind}
        if self.label is not None:
            result["label"] = self.label
        return result


@dataclass(frozen=True)
class TopologyPort:
    """A component terminal bound to one net."""

    port_id: str
    net_id: str
    direction: str = "passive"
    label: str | None = None

    def __post_init__(self) -> None:
        _id(self.port_id, "port_id")
        _id(self.net_id, "net_id")
        if self.direction not in {"input", "output", "bidirectional", "passive", "control"}:
            raise ValueError("unsupported port direction")

    def to_dict(self) -> dict[str, Any]:
        result = {"port_id": self.port_id, "net_id": self.net_id, "direction": self.direction}
        if self.label is not None:
            result["label"] = self.label
        return result


@dataclass(frozen=True)
class TopologyComponent:
    """A semantic component and its renderer-only symbol reference."""

    component_id: str
    kind: str
    ports: tuple[TopologyPort, ...]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    symbol_id: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.component_id, "component_id")
        if not str(self.kind).strip():
            raise ValueError("component kind is required")
        ports = tuple(self.ports)
        if not ports:
            raise ValueError("component must declare at least one port")
        ids = [port.port_id for port in ports]
        if len(ids) != len(set(ids)):
            raise ValueError(f"component {self.component_id} has duplicate port ids")
        object.__setattr__(self, "ports", ports)
        if not isinstance(self.parameters, Mapping):
            raise ValueError("component parameters must be a mapping")
        object.__setattr__(self, "parameters", _freeze_json(dict(self.parameters)))
        if self.symbol_id is not None:
            _id(self.symbol_id, "symbol_id")
        if self.model_id is not None:
            _id(self.model_id, "model_id")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "component_id": self.component_id,
            "kind": self.kind,
            "ports": [port.to_dict() for port in self.ports],
            "parameters": _thaw_json(self.parameters),
        }
        if self.symbol_id is not None:
            result["symbol_id"] = self.symbol_id
        if self.model_id is not None:
            result["model_id"] = self.model_id
        return result


@dataclass(frozen=True)
class TopologyDescriptor:
    """Versioned graph contract consumed by topology renderers.

    A descriptor's ``fingerprint`` excludes no semantic fields: changing a
    symbol hint, connection, or parameter therefore creates a new identity.
    The fingerprint is not a cryptographic signature and does not imply model
    correctness; it only identifies the descriptor bytes.
    """

    topology_id: str
    components: tuple[TopologyComponent, ...]
    nets: tuple[TopologyNet, ...]
    descriptor_version: str = "1.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    _declared_fingerprint: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _id(self.topology_id, "topology_id")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", str(self.descriptor_version)):
            raise ValueError("descriptor_version must be MAJOR.MINOR")
        components, nets = tuple(self.components), tuple(self.nets)
        component_ids = [item.component_id for item in components]
        net_ids = [item.net_id for item in nets]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("component ids must be unique")
        if len(net_ids) != len(set(net_ids)):
            raise ValueError("net ids must be unique")
        known_nets = set(net_ids)
        seen_ports: set[str] = set()
        for component in components:
            for port in component.ports:
                qualified = f"{component.component_id}:{port.port_id}"
                if qualified in seen_ports:
                    raise ValueError(f"duplicate port id: {qualified}")
                seen_ports.add(qualified)
                if port.net_id not in known_nets:
                    raise ValueError(f"port {qualified} references unknown net {port.net_id}")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "nets", nets)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("topology metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze_json(dict(self.metadata)))
        if self._declared_fingerprint is not None:
            if not isinstance(self._declared_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self._declared_fingerprint):
                raise ValueError("topology fingerprint must be a SHA-256 hex digest")
            if self._declared_fingerprint != self.fingerprint:
                raise ValueError("topology descriptor fingerprint does not match contents")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": TOPOLOGY_SCHEMA,
            "descriptor_version": self.descriptor_version,
            "topology_id": self.topology_id,
            "components": [component.to_dict() for component in self.components],
            "nets": [net.to_dict() for net in self.nets],
            "metadata": _thaw_json(self.metadata),
        }
        result["fingerprint"] = self.fingerprint
        return result

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": TOPOLOGY_SCHEMA,
            "descriptor_version": self.descriptor_version,
            "topology_id": self.topology_id,
            "components": [component.to_dict() for component in self.components],
            "nets": [net.to_dict() for net in self.nets],
            "metadata": _thaw_json(self.metadata),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self._identity_payload())).hexdigest()

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, ensure_ascii=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TopologyDescriptor":
        if not isinstance(value, Mapping):
            raise ValueError("topology descriptor must be an object")
        allowed = {"schema", "descriptor_version", "topology_id", "components", "nets", "metadata", "fingerprint"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("topology descriptor contains unexpected fields: " + ", ".join(sorted(unknown)))
        if value.get("schema") != TOPOLOGY_SCHEMA:
            raise ValueError("unsupported topology schema")
        if "fingerprint" not in value:
            raise ValueError("topology descriptor fingerprint is required")
        components = []
        for item in value.get("components", ()):
            ports = tuple(TopologyPort(str(p.get("port_id", "")), str(p.get("net_id", "")), str(p.get("direction", "passive")), p.get("label")) for p in item.get("ports", ()))
            components.append(TopologyComponent(str(item.get("component_id", "")), str(item.get("kind", "")), ports, parameters=dict(item.get("parameters", {})), symbol_id=item.get("symbol_id"), model_id=item.get("model_id")))
        nets = tuple(TopologyNet(str(n.get("net_id", "")), n.get("label"), str(n.get("kind", "electrical"))) for n in value.get("nets", ()))
        return cls(
            str(value.get("topology_id", "")),
            tuple(components),
            nets,
            descriptor_version=str(value.get("descriptor_version", "1.0")),
            metadata=dict(value.get("metadata", {})),
            _declared_fingerprint=value["fingerprint"],
        )


def _component(component_id: str, kind: str, ports: Iterable[tuple[str, str, str]], *, parameters: Mapping[str, Any], symbol_id: str) -> TopologyComponent:
    return TopologyComponent(component_id, kind, tuple(TopologyPort(*port) for port in ports), parameters=parameters, symbol_id=symbol_id)


def buck_topology_descriptor(plant: Any | None = None) -> TopologyDescriptor:
    """Build the semantic graph for the project-owned averaged Buck reference."""

    params = _plant_parameters(plant)
    nets = tuple(TopologyNet(net_id, label=label, kind=kind) for net_id, label, kind in (
        ("vin", "Vin", "electrical"), ("sw", "Switch node", "electrical"),
        ("vout", "Vout", "electrical"), ("gnd", "GND", "reference"), ("duty", "Duty", "control"),
    ))
    components = (
        _component("vin_source", "voltage_source", (("positive", "vin", "output"), ("negative", "gnd", "passive")), parameters={"voltage_v": params.get("input_voltage_v")}, symbol_id="source.dc"),
        _component("switch", "averaged_switch", (("input", "vin", "input"), ("output", "sw", "output"), ("control", "duty", "control")), parameters={}, symbol_id="switch.averaged"),
        _component("inductor", "inductor", (("input", "sw", "passive"), ("output", "vout", "passive")), parameters={"inductance_h": params.get("inductance_h")}, symbol_id="inductor"),
        _component("capacitor", "capacitor", (("positive", "vout", "passive"), ("negative", "gnd", "passive")), parameters={"capacitance_f": params.get("capacitance_f")}, symbol_id="capacitor"),
        _component("load", "resistor_load", (("positive", "vout", "passive"), ("negative", "gnd", "passive")), parameters={"resistance_ohm": params.get("load_resistance_ohm")}, symbol_id="resistor"),
    )
    return TopologyDescriptor("buck", components, nets, metadata={"model_level": "L1", "model_kind": "ideal_averaged_reference"})


def boost_topology_descriptor(plant: Any | None = None) -> TopologyDescriptor:
    """Build the semantic graph for the project-owned averaged Boost reference."""

    params = _plant_parameters(plant)
    nets = tuple(TopologyNet(net_id, label=label, kind=kind) for net_id, label, kind in (
        ("vin", "Vin", "electrical"), ("sw", "Switch node", "electrical"),
        ("vout", "Vout", "electrical"), ("gnd", "GND", "reference"), ("duty", "Duty", "control"),
    ))
    components = (
        _component("vin_source", "voltage_source", (("positive", "vin", "output"), ("negative", "gnd", "passive")), parameters={"voltage_v": params.get("input_voltage_v")}, symbol_id="source.dc"),
        _component("inductor", "inductor", (("input", "vin", "passive"), ("output", "sw", "passive")), parameters={"inductance_h": params.get("inductance_h")}, symbol_id="inductor"),
        _component("switch", "averaged_switch", (("input", "sw", "input"), ("output", "gnd", "passive"), ("control", "duty", "control")), parameters={}, symbol_id="switch.averaged"),
        _component("diode", "diode", (("anode", "sw", "passive"), ("cathode", "vout", "passive")), parameters={}, symbol_id="diode"),
        _component("capacitor", "capacitor", (("positive", "vout", "passive"), ("negative", "gnd", "passive")), parameters={"capacitance_f": params.get("capacitance_f")}, symbol_id="capacitor"),
        _component("load", "resistor_load", (("positive", "vout", "passive"), ("negative", "gnd", "passive")), parameters={"resistance_ohm": params.get("load_resistance_ohm")}, symbol_id="resistor"),
    )
    return TopologyDescriptor("boost", components, nets, metadata={"model_level": "L1", "model_kind": "ideal_averaged_reference"})


def export_topology_descriptor(plant: Any) -> TopologyDescriptor:
    """Export a known reference topology; unknown custom plants fail closed."""

    topology = str(getattr(plant, "topology", ""))
    if topology == "buck":
        return buck_topology_descriptor(plant)
    if topology == "boost":
        return boost_topology_descriptor(plant)
    raise ValueError(f"no reviewed topology exporter for {topology!r}")


def topology_to_dot(descriptor: TopologyDescriptor) -> str:
    """Render a descriptor to dependency-free Graphviz DOT text.

    DOT is an interchange aid, not the semantic source of truth.  Consumers
    can style/lay out nodes independently and retain the descriptor fingerprint.
    """

    lines = ["graph topology {", "  rankdir=LR;", "  graph [label=\"%s\"];" % descriptor.topology_id]
    for component in descriptor.components:
        label = f"{component.component_id}\\n{component.kind}"
        lines.append(f'  "c:{component.component_id}" [shape=box,label="{label}"];')
    for net in descriptor.nets:
        label = net.label or net.net_id
        lines.append(f'  "n:{net.net_id}" [shape=ellipse,label="{label}"];')
    for component in descriptor.components:
        for port in component.ports:
            lines.append(f'  "c:{component.component_id}" -- "n:{port.net_id}" [label="{port.port_id}"];')
    lines.append("}")
    return "\n".join(lines)


def topology_to_adjacency(descriptor: TopologyDescriptor) -> dict[str, list[dict[str, str]]]:
    """Return a compact JSON-friendly adjacency representation for UIs."""

    return {
        component.component_id: [
            {"port_id": port.port_id, "net_id": port.net_id, "direction": port.direction}
            for port in component.ports
        ]
        for component in descriptor.components
    }


def _plant_parameters(plant: Any | None) -> dict[str, Any]:
    if plant is None:
        return {}
    return {name: getattr(plant, name, None) for name in ("input_voltage_v", "inductance_h", "capacitance_f", "load_resistance_ohm")}
