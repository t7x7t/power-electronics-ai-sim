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
  ratio.  A descriptor may declare how several gate signals derive from it.
  Without a declaration the compiler requires exactly one actuator and exactly
  one carrier; with ``metadata.control.mode = "single_duty_complementary"`` it
  renders a high-side and a low-side gate from the same duty, with
  ``"single_duty_interleaved"`` it renders ``phases`` gates spread evenly across
  one switching period, and with ``"paired_complementary"`` it renders several
  legs that are each complementary and separated by a declared shift.  Either
  way the pulse width is derived from the commanded duty, never read out of the
  descriptor.

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
    RENDERABLE_DEVICE_KINDS,
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
        "ambiguous_complementary_control",
        "ambiguous_reference_net",
        "invalid_carrier_phase",
        "invalid_control_declaration",
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

#: Declared relations between several gate signals and the commanded duty.
#: The declaration lives in ``descriptor.metadata.control`` and is validated
#: here rather than in the topology validator, because "which switch is the low
#: side" and "which carrier is phase two" are rendering questions, not
#: graph-shape questions.
#:
#: ``single_duty_complementary`` renders a high-side and a low-side gate from
#: one duty with dead time between them.
#:
#: ``single_duty_interleaved`` renders ``phases`` gates from one duty, each on
#: its own carrier delayed by a whole fraction of the switching period.  It is
#: the multiphase counterpart: the same command produces N pulses that are
#: spread evenly across one period, so the input current ripples at N times the
#: per-phase switching frequency without any phase having a different duty.
#:
#: ``paired_complementary`` renders N *legs*, each with a high-side and a
#: low-side gate that are complementary to each other, all driven by the one
#: duty.  It is the composed relation: a bridge is a set of legs, and no
#: structural rule in the graph says which switch belongs to which leg — only
#: the declaration does.  Legs may be shifted relative to each other, which is
#: how a synchronous multiphase converter is written.
CONTROL_MODE_COMPLEMENTARY = "single_duty_complementary"
CONTROL_MODE_INTERLEAVED = "single_duty_interleaved"
CONTROL_MODE_PAIRED = "paired_complementary"
SUPPORTED_CONTROL_MODES = frozenset(
    {CONTROL_MODE_COMPLEMENTARY, CONTROL_MODE_INTERLEAVED, CONTROL_MODE_PAIRED}
)

#: Declaration keys each mode admits.  A key outside its mode's set is refused
#: rather than ignored: ``dead_time_s`` on an interleaved declaration would
#: silently mean "no dead time", which is a different circuit than the one the
#: author believed they wrote.
_CONTROL_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        CONTROL_MODE_COMPLEMENTARY: frozenset({"mode", "dead_time_s"}),
        CONTROL_MODE_INTERLEAVED: frozenset({"mode", "phases", "gate_order"}),
        CONTROL_MODE_PAIRED: frozenset(
            {"mode", "pairs", "dead_time_s", "leg_phase_shift_s"}
        ),
    }
)

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
    carrier_phase_s: float = 0.0
    carrier_tail_s: float = 0.0
    #: ``single_duty`` for one commanded gate, ``single_duty_complementary`` for
    #: the declared high-side/low-side pair, ``single_duty_interleaved`` for a
    #: declared set of phase-shifted gates, ``paired_complementary`` for a
    #: declared set of legs that are each complementary.
    control_mode: str = "single_duty"
    #: The remaining on-time rendered as a finite tail, per control net.  Empty
    #: when no gate was already on at the window start.
    carrier_tails_s: tuple[tuple[str, float], ...] = ()
    #: The first rising edge of every rendered gate inside the compiled window,
    #: in seconds from the window start.  Interleaved phases differ here.  A gate
    #: that is already conducting at the window start reports its *next* rise,
    #: and the pulse that straddles the start is rendered as a finite tail; a
    #: gate that never turns on is absent from the record.
    gate_first_rise_s: tuple[tuple[str, float], ...] = ()
    #: Number of switching phases the one commanded duty drove.  A single gate
    #: and a complementary pair are both one phase: they are one switching leg
    #: whose gates are derived from each other, not several evenly spaced legs.
    phases: int = 1
    #: Component id to SPICE element name, in descriptor order.  A consumer that
    #: holds the descriptor needs this to name the saved current/voltage of an
    #: element it did not render itself.
    element_names: tuple[tuple[str, str], ...] = ()
    #: The net bound to the load's positive port when the descriptor declares
    #: exactly one ``resistor_load``.  ``None`` means the descriptor does not
    #: define a single observable output.
    output_net: str | None = None

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
            "carrier_phase_s": self.carrier_phase_s,
            "carrier_tail_s": self.carrier_tail_s,
            "control_mode": self.control_mode,
            "gate_first_rise_s": {
                net_id: value for net_id, value in self.gate_first_rise_s
            },
            "phases": self.phases,
            "carrier_tails_s": {
                net_id: tail for net_id, tail in self.carrier_tails_s
            },
            "element_names": {
                component_id: name for component_id, name in self.element_names
            },
            "output_net": self.output_net,
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


def _synthetic_node(base: str, taken: set[str]) -> str:
    """Return a node name that no declared net uses.

    The suffix grows until it is free rather than raising: a collision is a
    naming accident, not a circuit defect, and appending an underscore is a
    deterministic, readable resolution.
    """

    candidate = base
    while candidate in taken:
        candidate += "_"
    return candidate


@dataclass(frozen=True)
class _ComponentView:
    """One component with per-window parameter values applied.

    Only ``_parameter`` and ``_bound_net`` read a component, so the view carries
    exactly the fields they use.  Building a view is what lets a caller change
    an input voltage or load resistance for one window without rebuilding the
    descriptor (which would change the identity the caller is citing).
    """

    component_id: str
    kind: str
    ports: tuple[Any, ...]
    parameters: Mapping[str, Any]


def _component_view(
    component: Any,
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> Any:
    if not overrides:
        return component
    override = overrides.get(component.component_id)
    if not override:
        return component
    return _ComponentView(
        component_id=component.component_id,
        kind=component.kind,
        ports=component.ports,
        parameters={**dict(component.parameters), **dict(override)},
    )


def _bound_net(component: Any, port_id: str) -> str:
    """Return the net one declared port binds, with a compilable refusal.

    The compiler can be called directly, without the validator having run, so a
    missing port must become a refusal rather than a ``KeyError``.
    """

    for bound in component.ports:
        if bound.port_id == port_id:
            return bound.net_id
    raise CircuitCompileError(
        "not_compilable",
        f"component {component.component_id!r} has no {port_id!r} port",
        component_id=component.component_id,
    )


def _control_declaration(
    descriptor: TopologyDescriptor,
) -> "_ControlDeclaration | None":
    """Read ``metadata.control`` and validate the declaration's shape.

    A descriptor without the key is a single-actuator circuit.  A descriptor
    with the key has promised that several gates derive from one duty, so an
    unreadable declaration is a refusal: silently falling back to one actuator
    would render a different circuit than the descriptor declares.

    Only the shape is checked here.  Whether the declaration *fits the graph*
    (two switches and two carriers, three phases and three carriers) needs the
    rendered components, so it is decided where those are known.
    """

    metadata = descriptor.metadata
    if "control" not in metadata:
        return None
    declaration = metadata["control"]
    if not isinstance(declaration, Mapping):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control must be an object declaring how the commanded duty "
            "maps to the gate signals",
            expected="an object with 'mode' and the keys that mode declares",
        )
    mode = declaration.get("mode")
    if not isinstance(mode, str) or mode not in SUPPORTED_CONTROL_MODES:
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"unsupported control mode {mode!r}",
            supported=sorted(SUPPORTED_CONTROL_MODES),
        )
    allowed = _CONTROL_KEYS[mode]
    unknown = sorted(set(declaration) - allowed)
    if unknown:
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"metadata.control declares {unknown} which mode {mode!r} does not "
            "understand; an unrecognised key would change the circuit silently",
            mode=mode,
            expected="only " + ", ".join(sorted(allowed)),
        )
    if mode == CONTROL_MODE_COMPLEMENTARY:
        return _ControlDeclaration(mode, dead_time_s=_dead_time(declaration))
    if mode == CONTROL_MODE_PAIRED:
        return _ControlDeclaration(
            mode,
            dead_time_s=_dead_time(declaration),
            pairs=_pairs(declaration),
            leg_phase_shift_s=_leg_phase_shift(declaration),
        )
    return _ControlDeclaration(
        mode,
        phases=_phase_count(declaration),
        gate_order=_gate_order(declaration),
    )


@dataclass(frozen=True)
class _ControlDeclaration:
    """One parsed ``metadata.control`` record.

    The mode decides which fields carry meaning; the other fields keep their
    "not declared" values.  ``dead_time_s`` belongs to the complementary and
    paired relations, ``pairs`` and ``leg_phase_shift_s`` to the paired one,
    and ``phases`` / ``gate_order`` to the interleaved one.
    """

    mode: str
    dead_time_s: float = 0.0
    phases: int | None = None
    gate_order: tuple[str, ...] | None = None
    pairs: tuple[tuple[str, str], ...] | None = None
    leg_phase_shift_s: float = 0.0


def _pairs(declaration: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Read the declared complementary pairs.

    The pairing is the one fact the graph cannot supply.  Two switches sharing a
    node are not necessarily a leg (two low-side switches sharing ground are
    not), and two switches on opposite rails are not necessarily a pair.  So the
    author names the legs, and the compiler never guesses one.
    """

    raw = declaration.get("pairs")
    if raw is None:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "paired_complementary requires 'pairs', the list of switch pairs that "
            "form each leg",
            expected='a list such as [["high_1", "low_1"], ["high_2", "low_2"]]',
        )
    if not isinstance(raw, (list, tuple)) or not raw:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.pairs must be a non-empty array of two-element arrays",
            expected='a list such as [["high_1", "low_1"]]',
        )
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or not all(isinstance(name, str) for name in entry)
        ):
            raise CircuitCompileError(
                "invalid_control_declaration",
                f"metadata.control.pairs[{index}] must be two component ids",
                index=index,
                expected='a two-element list such as ["high_1", "low_1"]',
            )
        high, low = str(entry[0]), str(entry[1])
        if high == low:
            raise CircuitCompileError(
                "invalid_control_declaration",
                f"metadata.control.pairs[{index}] names {high!r} twice; a leg is "
                "two different switches",
                index=index,
                expected='two different component ids',
            )
        for name in (high, low):
            if name in seen:
                raise CircuitCompileError(
                    "invalid_control_declaration",
                    f"component {name!r} appears in more than one pair; a switch "
                    "belongs to exactly one leg",
                    component_id=name,
                    expected="every switch named exactly once",
                )
            seen.add(name)
        pairs.append((high, low))
    return tuple(pairs)


def _leg_phase_shift(declaration: Mapping[str, Any]) -> float:
    raw = declaration.get("leg_phase_shift_s", 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.leg_phase_shift_s must be a number when supplied",
        )
    shift = float(raw)
    if not math.isfinite(shift) or shift < 0.0:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.leg_phase_shift_s must be finite and non-negative",
            leg_phase_shift_s=raw,
        )
    return shift


def _dead_time(declaration: Mapping[str, Any]) -> float:
    raw = declaration.get("dead_time_s", 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.dead_time_s must be a number when supplied",
        )
    dead_time = float(raw)
    if not math.isfinite(dead_time) or dead_time < 0.0:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.dead_time_s must be finite and non-negative",
        )
    return dead_time


def _phase_count(declaration: Mapping[str, Any]) -> int:
    """Read the declared phase count.

    The count is required even though it is derivable from the graph.  A
    document that says "three phases" but wires two carriers is not a two-phase
    circuit: it is a document whose author and whose netlist disagree, and the
    refusal names both numbers instead of quietly rendering the smaller one.
    """

    raw = declaration.get("phases")
    if raw is None:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "single_duty_interleaved requires 'phases', the number of gate "
            "signals the one duty drives",
            expected="an integer of at least 2",
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.phases must be an integer",
            phases=raw,
        )
    if isinstance(raw, float) and not float(raw).is_integer():
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.phases must be a whole number of phases",
            phases=raw,
        )
    phases = int(raw)
    if phases < 2:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.phases must be at least 2; a one-phase "
            "declaration is a single-actuator circuit and needs no control "
            "declaration at all",
            phases=phases,
        )
    return phases


def _gate_order(declaration: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = declaration.get("gate_order")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(name, str) for name in raw
    ):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.gate_order must be an array of control net ids",
            expected="a list of net ids, one per phase",
        )
    order = tuple(str(name) for name in raw)
    if len(set(order)) != len(order):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.gate_order names the same control net twice; "
            "each phase needs its own gate",
            gate_order=list(order),
        )
    return order


def _complementary_switches(
    actuators: Sequence[Any], reference_net: str
) -> tuple[Any, Any]:
    """Classify the two switches into the high side and the low side.

    The only structural fact the compiler uses is which switch's conducting
    path reaches the reference net.  That is a declared connection, not a name:
    a descriptor may call its components anything.
    """

    high: list[Any] = []
    low: list[Any] = []
    for component in actuators:
        ends = {
            _bound_net(component, "input"),
            _bound_net(component, "output"),
        }
        (low if reference_net in ends else high).append(component)
    if len(high) != 1 or len(low) != 1:
        raise CircuitCompileError(
            "ambiguous_complementary_control",
            "single-duty complementary control needs exactly one switch whose "
            "conducting path reaches the reference net (the low side) and one "
            "that does not (the high side); the descriptor declares "
            f"{len(high)} high-side and {len(low)} low-side candidates",
            high_side=[component.component_id for component in high],
            low_side=[component.component_id for component in low],
        )
    return high[0], low[0]


def _paired_gates(
    *,
    declaration: _ControlDeclaration,
    actuators: Sequence[Any],
    carrier_by_net: Mapping[str, Any],
    duty_value: float,
    reference_net: str,
) -> tuple[dict[str, tuple[float, float]], float, dict[str, float]]:
    """Derive one gate window per gate of every declared leg.

    Returns ``(gate_windows, switching_period, delay_by_net)``.

    The pairing is the fact the graph cannot supply.  Two switches that share a
    node are not necessarily a leg (two low-side switches both touching ground
    are not one), and two switches on the same leg are not necessarily adjacent
    in the component list.  So the author names the pairs, and within a pair the
    compiler still decides which switch is the low side from the one structural
    fact it trusts: which conducting path reaches the reference net.

    Leg ``k`` is delayed by ``k * leg_phase_shift_s``, so a shift of ``Tsw/2``
    makes a two-leg synchronous converter interleaved while every leg stays
    complementary inside itself.
    """

    pairs = declaration.pairs
    assert pairs is not None
    by_id = {component.component_id: component for component in actuators}
    named = {name for pair in pairs for name in pair}
    unpaired = sorted(set(by_id) - named)
    if unpaired:
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"{len(unpaired)} switch(es) are not named in metadata.control.pairs: "
            f"{unpaired}; a switch the declaration does not place in a leg has no "
            "gate to drive",
            components=unpaired,
            expected="every switch of the descriptor appears in exactly one pair",
        )
    unknown = sorted(named - set(by_id))
    if unknown:
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"metadata.control.pairs names {unknown}, which are not switches of "
            "this descriptor",
            components=unknown,
            actuators=sorted(by_id),
        )

    gate_windows: dict[str, tuple[float, float]] = {}
    leg_frequency: list[float] = []
    leg_nets: list[tuple[str, str]] = []
    # Two legs that share a gate net would render two switches switching
    # together, and the second leg's window would overwrite the first one's.
    # The set is filled in this loop because ``gate_windows`` is only populated
    # after every leg has been classified; checking that dict here would read an
    # empty mapping and never fire.
    claimed_gate_nets: set[str] = set()
    for pair_index, (first, second) in enumerate(pairs):
        members = [by_id[first], by_id[second]]
        low = [
            component
            for component in members
            if reference_net
            in {_bound_net(component, "input"), _bound_net(component, "output")}
        ]
        if len(low) != 1:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                f"leg {pair_index} ({first!r}, {second!r}) has {len(low)} switches "
                "whose conducting path reaches the reference net; a leg is exactly "
                "one high side and one low side",
                pair=[first, second],
                low_side=[component.component_id for component in low],
                expected=(
                    "one switch of the pair tied to the reference net; a leg whose "
                    "two switches are both floating or both grounded is not a "
                    "half bridge"
                ),
            )
        high = [
            component
            for component in members
            if component.component_id != low[0].component_id
        ]
        high_net = _bound_net(high[0], "control")
        low_net = _bound_net(low[0], "control")
        if high_net == low_net:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                f"the two switches of leg {pair_index} share control net "
                f"{high_net!r}, so they switch together instead of complementarily",
                control_net=high_net,
                pair=[first, second],
            )
        re_used = sorted({high_net, low_net} & claimed_gate_nets)
        if re_used:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                f"leg {pair_index} reuses control net(s) {re_used} of an earlier "
                "leg; two legs on one gate net switch together, and the second "
                "leg's window would overwrite the first one's",
                pair=[first, second],
                gate_nets=re_used,
                expected="each leg gets its own two control nets",
            )
        claimed_gate_nets.update({high_net, low_net})
        for net in (high_net, low_net):
            carrier = carrier_by_net.get(net)
            if carrier is None:
                raise CircuitCompileError(
                    "no_carrier",
                    f"leg {pair_index} drives {net!r}, which has no carrier",
                    net=net,
                    expected="one pulse_source per gate net, its 'positive' port on it",
                )
            frequency = _parameter(carrier, "switching_frequency_hz", required=True)
            assert frequency is not None
            leg_frequency.append(frequency)
        leg_nets.append((high_net, low_net))

    reference_frequency = leg_frequency[0]
    for frequency in leg_frequency:
        if abs(frequency - reference_frequency) > 1e-12 * max(
            1.0, abs(reference_frequency)
        ):
            raise CircuitCompileError(
                "invalid_control_declaration",
                "every leg of a paired set must declare the same switching "
                f"frequency; found {sorted(set(leg_frequency))}",
                frequencies=sorted(set(leg_frequency)),
                expected="one switching_frequency_hz on every carrier",
            )
    switching_period = 1.0 / reference_frequency
    dead_time = declaration.dead_time_s
    if dead_time >= switching_period / 2.0:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.dead_time_s must be smaller than half the "
            "switching period",
            dead_time_s=dead_time,
            switching_period_s=switching_period,
            expected=(
                f"dead_time_s below {switching_period / 2.0:.6g} s, or a lower "
                "switching_frequency_hz"
            ),
        )
    shift = declaration.leg_phase_shift_s
    if shift >= switching_period:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.leg_phase_shift_s must be smaller than one "
            "switching period; a whole period would put a leg back in phase and "
            "make the shift meaningless",
            leg_phase_shift_s=shift,
            switching_period_s=switching_period,
            expected=f"a shift below {switching_period:.6g} s",
        )
    # The declaration is the single authority for the dead time, exactly as in
    # the single-leg mode: a carrier's own ``dead_time_s`` is recorded, never
    # rendered, so a *disagreeing* value would be a second authority the author
    # believes is in force.
    for high_net, low_net in leg_nets:
        for net in (high_net, low_net):
            declared_on_carrier = _parameter(carrier_by_net[net], "dead_time_s")
            if declared_on_carrier is None:
                continue
            if abs(declared_on_carrier - dead_time) > 1e-12:
                raise CircuitCompileError(
                    "invalid_control_declaration",
                    f"carrier on {net!r} declares dead_time_s="
                    f"{declared_on_carrier!r} but metadata.control declares "
                    f"{dead_time!r}; the declaration is the only authority the "
                    "compiler renders, so two different values cannot both hold",
                    net=net,
                    carrier_dead_time_s=declared_on_carrier,
                    declaration_dead_time_s=dead_time,
                    expected=(
                        "either delete dead_time_s from the carrier, or make the "
                        "two values equal, or delete it from metadata.control"
                    ),
                )

    # The cycle is the same one the single-leg complementary mode renders: high
    # side on [0, D*Tsw - dead_time], low side on [D*Tsw, Tsw - dead_time], and
    # both dead-time intervals have both gates low.
    high_width = max(0.0, duty_value * switching_period - dead_time)
    delays: dict[str, float] = {}
    for pair_index, (high_net, low_net) in enumerate(leg_nets):
        gate_windows[high_net] = (0.0, high_width)
        gate_windows[low_net] = (high_width + dead_time, switching_period - dead_time)
        delays[high_net] = pair_index * shift
        delays[low_net] = pair_index * shift
    return gate_windows, switching_period, delays


def _interleaved_gates(
    *,
    declaration: _ControlDeclaration,
    actuators: Sequence[Any],
    carrier_by_net: Mapping[str, Any],
    duty_value: float,
) -> tuple[dict[str, tuple[float, float]], float, dict[str, float]]:
    """Derive one gate window and one carrier offset per interleaved phase.

    Returns ``(gate_windows, switching_period, delay_by_net)``.

    Every phase carries the *same* on-width, because the kernel commands one
    duty ratio; what differs is when each phase's carrier starts.  Phase ``k``
    of ``N`` is delayed by ``k*Tsw/N``, so the pulses tile the switching period
    and the sum of the phase currents ripples at ``N`` times the per-phase
    frequency.

    The choice of which phase is "first" is a labelling choice, not a physical
    one: spreading the same offsets over the same legs in a different order
    describes the same aggregate behaviour, only with the legs renamed.  The
    compiler therefore uses the declared ``gate_order`` when the author gave
    one and the sorted control-net ids otherwise, then reports the resolved
    assignment so a reader never has to guess which leg leads.
    """

    phases = declaration.phases
    assert phases is not None
    gate_nets: list[str] = []
    for component in actuators:
        net = _bound_net(component, "control")
        if net not in gate_nets:
            gate_nets.append(net)
    if len(gate_nets) != phases:
        raise CircuitCompileError(
            "invalid_control_declaration",
            "single_duty_interleaved declares "
            f"{phases} phases but the switches bind {len(gate_nets)} distinct "
            f"control nets ({', '.join(gate_nets) or 'none'}).  Each phase is "
            "exactly one gate signal, and every phase gets the same duty; if "
            "your legs have two gates each (a high side and a low side) this "
            "mode cannot express them, because it has no word for which gate "
            "is complementary to which",
            phases=phases,
            gate_nets=sorted(gate_nets),
            expected=(
                "one control net per phase with 'phases' equal to their count, "
                "and one switch per control net; a leg whose two gates are "
                "complementary to each other uses 'paired_complementary' "
                "(docs/64 section 5.8)"
            ),
        )
    order = declaration.gate_order
    if order is None:
        order = tuple(sorted(gate_nets))
    elif set(order) != set(gate_nets):
        raise CircuitCompileError(
            "invalid_control_declaration",
            "metadata.control.gate_order must list exactly the control nets the "
            "switches bind",
            gate_order=list(order),
            gate_nets=sorted(gate_nets),
        )
    missing = [net for net in order if net not in carrier_by_net]
    if missing:
        raise CircuitCompileError(
            "no_carrier",
            f"phase control nets {missing} have no carrier; the commanded duty "
            "has no pulse source to derive a pulse width from",
            nets=missing,
            expected="one pulse_source per phase, its 'positive' port on that net",
        )
    extra = sorted(set(carrier_by_net) - set(order))
    if extra:
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"carriers drive {extra}, which are not phase control nets; a "
            "carrier on an unrelated net would be a second independent gate",
            carrier_nets=extra,
            gate_nets=sorted(order),
            expected="exactly one carrier per phase control net, and none elsewhere",
        )
    frequencies: list[float] = []
    for net in order:
        frequency = _parameter(
            carrier_by_net[net], "switching_frequency_hz", required=True
        )
        assert frequency is not None
        frequencies.append(frequency)
    reference = frequencies[0]
    for net, frequency in zip(order, frequencies):
        if abs(frequency - reference) > 1e-12 * max(1.0, abs(reference)):
            raise CircuitCompileError(
                "invalid_control_declaration",
                "every phase of an interleaved set must declare the same "
                "switching frequency; phase "
                f"{net!r} declares {frequency!r} against {reference!r}",
                frequencies={
                    name: value for name, value in zip(order, frequencies)
                },
                expected="one switching_frequency_hz for every phase",
            )
    switching_period = 1.0 / reference
    # No lower floor: ``duty == 0`` renders a gate that never turns on, so no
    # phase reports a first rise and none writes a femtosecond-wide pulse.
    pulse_width = duty_value * switching_period
    gate_windows = {net: (0.0, pulse_width) for net in order}
    offset_by_net = {
        net: index * switching_period / phases for index, net in enumerate(order)
    }
    return gate_windows, switching_period, offset_by_net


def _render_gate(
    *,
    name: str,
    component: Any,
    control_node: str,
    negative_node: str,
    start_s: float,
    stop_s: float,
    phase_s: float,
    switching_period_s: float,
    taken_nodes: set[str],
    counters: dict[str, int],
) -> tuple[list[str], float]:
    """Render one gate whose on-interval is ``[start_s, stop_s)`` in the cycle.

    The periodic source covers every complete on-interval.  A window that
    starts inside one needs the remainder as a finite PWL tail summed onto the
    control net: a shifted rising edge alone would silently drop the energy of
    the pulse that began before the window.  A gate that never turns on is a
    zero-volt source rather than an absent one, so the control net stays driven.
    """

    amplitude = _parameter(component, "amplitude_v")
    rise_time = _parameter(component, "rise_time_s")
    fall_time = _parameter(component, "fall_time_s")
    assert amplitude is not None and rise_time is not None and fall_time is not None
    if stop_s <= start_s:
        return [f"{name} {control_node} {negative_node} DC 0"], 0.0
    width = stop_s - start_s
    first_rise = (start_s - phase_s) % switching_period_s
    if switching_period_s - first_rise <= 1e-12 * max(1.0, switching_period_s):
        first_rise = 0.0
    tail_s = stop_s - phase_s if start_s < phase_s < stop_s else 0.0
    if tail_s <= 1e-12:
        tail_s = 0.0
    pulse_card = (
        f"0 {_format(amplitude)} {_format(first_rise)} "
        f"{_format(rise_time)} {_format(fall_time)} "
        f"{_format(width)} {_format(switching_period_s)})"
    )
    if tail_s <= 0.0:
        return [f"{name} {control_node} {negative_node} PULSE({pulse_card}"], 0.0
    pulse_node = _synthetic_node(f"{control_node}_pulse", taken_nodes)
    tail_node = _synthetic_node(f"{control_node}_tail", taken_nodes)
    taken_nodes.update({pulse_node, tail_node})
    counters["V"] = counters.get("V", 0) + 1
    counters["B"] = counters.get("B", 0) + 1
    return (
        [
            f"{name} {pulse_node} {negative_node} PULSE({pulse_card}",
            f"V{counters['V']} {tail_node} {negative_node} PWL(0 "
            f"{_format(amplitude)} {_format(tail_s)} {_format(amplitude)} "
            f"{_format(tail_s + 1e-9)} 0)",
            f"B{counters['B']} {control_node} {negative_node} "
            f"V = V({pulse_node}) + V({tail_node})",
        ],
        tail_s,
    )


def _phase_total(
    control_mode: str, declaration: "_ControlDeclaration | None", gate_windows: Mapping[str, Any]
) -> int:
    """How many switching phases the one commanded duty drove.

    A single gate and a complementary pair are both one phase: they are one leg
    whose gates derive from each other.  Interleaved phases and paired legs are
    genuinely several, and the count is what a reader needs to tell a one-leg
    circuit from a bridge.
    """

    if control_mode == CONTROL_MODE_INTERLEAVED:
        return len(gate_windows)
    if control_mode == CONTROL_MODE_PAIRED and declaration is not None:
        assert declaration.pairs is not None
        return len(declaration.pairs)
    return 1


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
    carrier_phase_s: float = 0.0,
    temperature_c: float = 27.0,
    initial_conditions: Mapping[str, float] | None = None,
    parameter_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledCircuit:
    """Render ``descriptor`` as one ngspice netlist.

    ``duty`` is held constant for the whole window.  The carrier's pulse width is
    derived from it, so a descriptor declares the switching frequency and the
    caller declares the command - the two are never the same field.

    A descriptor may declare ``metadata.control = {"mode":
    "single_duty_complementary", "dead_time_s": ...}``.  The compiler then maps
    one duty to a high-side on-interval ``[0, duty*Tsw - dead_time]`` and a
    low-side on-interval ``[duty*Tsw, Tsw - dead_time]``, which is the timing
    the reviewed synchronous reference renders explicitly.  Without the
    declaration exactly one actuator and one carrier are accepted.

    ``carrier_phase_s`` is where the switching carrier stands at the window
    start: the window's start time modulo the switching period.  A zero-order
    hold consumer that solves one window at a time must pass it, or every
    window would restart the carrier at a rising edge and silently change the
    energy delivered.

    ``initial_conditions`` overrides the ``IC`` of an inductor (amperes) or a
    capacitor (volts) by component id; it is how a stepped executor resumes a
    multi-window circuit from its previous endpoint without restoring solver
    internals.  ``parameter_overrides`` replaces named component parameters for
    this one window (an external input voltage or load change).  Neither
    changes ``descriptor_fingerprint``: the descriptor is still the identity the
    caller cites, and the netlist digest identifies the concrete window.
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
    try:
        temperature = float(temperature_c)
    except (TypeError, ValueError) as exc:
        raise CircuitCompileError(
            "invalid_option", "temperature_c must be a number"
        ) from exc
    if not math.isfinite(temperature):
        raise CircuitCompileError("invalid_option", "temperature_c must be finite")

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
                expected="one of " + ", ".join(sorted(RENDERABLE_DEVICE_KINDS)),
            )
        if not device_spec(canonical).renderable:
            raise CircuitCompileError(
                "not_compilable",
                f"component {component.component_id!r} of kind {canonical!r} is a "
                "behavioural block with no element card, so it cannot be rendered "
                "as a circuit",
                component_id=component.component_id,
                kind=canonical,
                expected=(
                    "a renderable kind: "
                    + ", ".join(sorted(RENDERABLE_DEVICE_KINDS))
                ),
            )

    components_by_id = {
        component.component_id: component for component in descriptor.components
    }

    # The compiler can be called without the validator having run, so it repeats
    # the one structural rule whose violation is invisible in the rendered
    # netlist: a port bound to a net of a kind its device does not accept.  A
    # resistor load wired between a gate net and ground renders as a perfectly
    # legal ``R`` card, every other card looks fine, and the circuit is not the
    # one the author declared.
    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        if canonical is None:  # already refused above
            continue
        spec = device_spec(canonical)
        for port in component.ports:
            declaration = spec.port(port.port_id)
            if declaration is None:
                continue
            net_kind = nets.get(port.net_id)
            if net_kind is None:
                continue  # an unknown net is the caller's bug, refused by _bound_net
            if net_kind not in declaration.net_kinds:
                raise CircuitCompileError(
                    "unsupported_net_kind",
                    f"component {component.component_id!r} binds its "
                    f"{port.port_id!r} port to net {port.net_id!r} of kind "
                    f"{net_kind!r}, which that port does not accept",
                    component_id=component.component_id,
                    port_id=port.port_id,
                    net_id=port.net_id,
                    net_kind=net_kind,
                    expected="one of " + ", ".join(sorted(declaration.net_kinds)),
                )
    if initial_conditions:
        unknown = sorted(set(initial_conditions) - set(components_by_id))
        if unknown:
            raise CircuitCompileError(
                "invalid_parameter",
                "initial_conditions names components the descriptor does not "
                f"declare: {unknown}",
                components=unknown,
            )
        for component_id, value in sorted(initial_conditions.items()):
            kind = canonical_device_kind(components_by_id[component_id].kind)
            if kind not in {"inductor", "capacitor"}:
                raise CircuitCompileError(
                    "invalid_parameter",
                    f"component {component_id!r} of kind {kind!r} has no initial "
                    "state to set; only an inductor current or a capacitor "
                    "voltage can be resumed",
                    component_id=component_id,
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CircuitCompileError(
                    "invalid_parameter",
                    f"initial state of {component_id!r} must be a number",
                    component_id=component_id,
                )
            if not math.isfinite(float(value)):
                raise CircuitCompileError(
                    "invalid_parameter",
                    f"initial state of {component_id!r} must be finite",
                    component_id=component_id,
                )
    if parameter_overrides:
        unknown = sorted(set(parameter_overrides) - set(components_by_id))
        if unknown:
            raise CircuitCompileError(
                "invalid_parameter",
                "parameter_overrides names components the descriptor does not "
                f"declare: {unknown}",
                components=unknown,
            )
        for component_id, override in sorted(parameter_overrides.items()):
            if not isinstance(override, Mapping):
                raise CircuitCompileError(
                    "invalid_parameter",
                    f"parameter override for {component_id!r} must be an object",
                    component_id=component_id,
                )
            canonical = canonical_device_kind(components_by_id[component_id].kind)
            assert canonical is not None
            device = device_spec(canonical)
            unknown_names = sorted(
                name for name in override if device.parameter(str(name)) is None
            )
            if unknown_names:
                raise CircuitCompileError(
                    "invalid_parameter",
                    f"component {component_id!r} declares no parameter "
                    f"{unknown_names[0]!r}",
                    component_id=component_id,
                    parameter=unknown_names[0],
                )

    declaration = _control_declaration(descriptor)
    actuators = [
        component
        for component in descriptor.components
        if canonical_device_kind(component.kind) == "voltage_controlled_switch"
    ]
    carriers = [
        component
        for component in descriptor.components
        if canonical_device_kind(component.kind) == "pulse_source"
    ]
    # One carrier per control net: the same net driven twice would sum two
    # pulse trains, which is not a gate signal.
    carrier_by_net: dict[str, Any] = {}
    for carrier in carriers:
        carrier_net = _bound_net(carrier, "positive")
        if carrier_net in carrier_by_net:
            raise CircuitCompileError(
                "multiple_carriers",
                f"two carriers drive control net {carrier_net!r}; one commanded "
                "duty buys exactly one pulse width per control net",
                net=carrier_net,
                carriers=[
                    carrier_by_net[carrier_net].component_id,
                    carrier.component_id,
                ],
            )
        carrier_by_net[carrier_net] = _component_view(carrier, parameter_overrides)

    delays: dict[str, float] = {}
    if declaration is None:
        if len(actuators) != 1:
            raise CircuitCompileError(
                "multiple_actuators",
                "without a declared control relation the compiler renders exactly "
                "one actuator because the kernel commands one duty ratio; the "
                f"descriptor declares {len(actuators)}.  Two switches on one "
                "control net would render complementary devices as simultaneously "
                "on, and two control nets have no defined relation yet.  Declare "
                "metadata.control to state that relation.",
                actuators=len(actuators),
            )
        if not carriers:
            raise CircuitCompileError(
                "no_carrier",
                "the descriptor declares no carrier, so there is no gate signal to "
                "derive from the commanded duty",
                expected=(
                    "a pulse_source whose 'positive' port binds the switch's "
                    "control net, with switching_frequency_hz set"
                ),
            )
        if len(carriers) > 1:
            raise CircuitCompileError(
                "multiple_carriers",
                "the descriptor declares more than one carrier; the commanded duty "
                "buys exactly one pulse width",
                carriers=len(carriers),
                expected=(
                    "one carrier, or metadata.control declaring how several "
                    "carriers derive from the one duty "
                    f"({', '.join(sorted(SUPPORTED_CONTROL_MODES))})"
                ),
            )
        actuator = actuators[0]
        actuator_net = _bound_net(actuator, "control")
        carrier = carrier_by_net.get(actuator_net)
        if carrier is None:
            raise CircuitCompileError(
                "no_carrier",
                f"the actuator's control net {actuator_net!r} has no carrier; a "
                "carrier elsewhere in the descriptor does not drive the switch",
                control_net=actuator_net,
                carrier_nets=sorted(carrier_by_net),
                expected=(
                    f"a pulse_source whose 'positive' port binds {actuator_net!r}"
                ),
            )
        switching_frequency = _parameter(
            carrier, "switching_frequency_hz", required=True
        )
        assert switching_frequency is not None
        switching_period = 1.0 / switching_frequency
        # No lower floor on the width: a commanded duty of zero must render a
        # gate that never turns on (``DC 0``) rather than a pulse one femtosecond
        # wide, which is a turn-on the author did not ask for and a first rise
        # the record would then have to report.
        pulse_width = duty_value * switching_period
        gate_windows: dict[str, tuple[float, float]] = {
            actuator_net: (0.0, pulse_width)
        }
        control_mode = "single_duty"
    elif declaration.mode == CONTROL_MODE_COMPLEMENTARY:
        dead_time = declaration.dead_time_s
        if len(actuators) != 2:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                "single-duty complementary control needs exactly two switches, "
                f"and the descriptor declares {len(actuators)}",
                actuators=len(actuators),
                expected=(
                    "exactly two switches for a complementary pair; N evenly "
                    "spaced single-switch phases use 'single_duty_interleaved'; "
                    "several legs that are each complementary use "
                    "'paired_complementary' (docs/64 section 5.8)"
                ),
            )
        high_side, low_side = _complementary_switches(actuators, reference_net)
        high_net = _bound_net(high_side, "control")
        low_net = _bound_net(low_side, "control")
        if high_net == low_net:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                "the two switches share one control net, so they switch together "
                "instead of complementarily",
                control_net=high_net,
            )
        expected_nets = {high_net, low_net}
        if set(carrier_by_net) != expected_nets:
            raise CircuitCompileError(
                "ambiguous_complementary_control",
                "complementary control needs exactly one carrier on each gate net",
                gate_nets=sorted(expected_nets),
                carrier_nets=sorted(carrier_by_net),
            )
        high_frequency = _parameter(
            carrier_by_net[high_net], "switching_frequency_hz", required=True
        )
        low_frequency = _parameter(
            carrier_by_net[low_net], "switching_frequency_hz", required=True
        )
        assert high_frequency is not None and low_frequency is not None
        if abs(low_frequency - high_frequency) > 1e-12 * max(
            1.0, abs(high_frequency)
        ):
            raise CircuitCompileError(
                "invalid_control_declaration",
                "both carriers of a complementary pair must declare the same "
                "switching frequency",
                frequencies=[high_frequency, low_frequency],
            )
        switching_period = 1.0 / high_frequency
        if dead_time >= switching_period / 2.0:
            raise CircuitCompileError(
                "invalid_control_declaration",
                "metadata.control.dead_time_s must be smaller than half the "
                "switching period",
                dead_time_s=dead_time,
                switching_period_s=switching_period,
                expected=(
                    f"dead_time_s below {switching_period / 2.0:.6g} s, or a "
                    "lower switching_frequency_hz"
                ),
            )
        # The declaration is the single authority for the dead time.  A carrier
        # that carries its own ``dead_time_s`` is recorded rather than rendered,
        # so a *disagreeing* value would be a second authority the author
        # believes is in force.  Agreement is accepted; disagreement is not.
        for net in (high_net, low_net):
            declared_on_carrier = _parameter(
                carrier_by_net[net], "dead_time_s"
            )
            if declared_on_carrier is None:
                continue
            if abs(declared_on_carrier - dead_time) > 1e-12:
                raise CircuitCompileError(
                    "invalid_control_declaration",
                    f"carrier on {net!r} declares dead_time_s="
                    f"{declared_on_carrier!r} but metadata.control declares "
                    f"{dead_time!r}; the declaration is the only authority the "
                    "compiler renders, so two different values cannot both hold",
                    net=net,
                    carrier_dead_time_s=declared_on_carrier,
                    declaration_dead_time_s=dead_time,
                    expected=(
                        "either delete dead_time_s from the carrier, or make the "
                        "two values equal, or delete it from metadata.control"
                    ),
                )
        # The cycle is: high side on [0, duty*Tsw - dead_time], low side on
        # [duty*Tsw, Tsw - dead_time].  The two dead-time intervals have both
        # gates low, exactly as the reviewed synchronous reference renders them.
        high_width = max(0.0, duty_value * switching_period - dead_time)
        gate_windows = {
            high_net: (0.0, high_width),
            low_net: (high_width + dead_time, switching_period - dead_time),
        }
        control_mode = declaration.mode
    elif declaration.mode == CONTROL_MODE_INTERLEAVED:
        gate_windows, switching_period, delays = _interleaved_gates(
            declaration=declaration,
            actuators=actuators,
            carrier_by_net=carrier_by_net,
            duty_value=duty_value,
        )
        control_mode = declaration.mode
    elif declaration.mode == CONTROL_MODE_PAIRED:
        gate_windows, switching_period, delays = _paired_gates(
            declaration=declaration,
            actuators=actuators,
            carrier_by_net=carrier_by_net,
            duty_value=duty_value,
            reference_net=reference_net,
        )
        control_mode = declaration.mode
    else:  # pragma: no cover - guarded by ``_control_declaration``
        raise CircuitCompileError(
            "invalid_control_declaration",
            f"mode {declaration.mode!r} parsed but has no rendering branch",
            supported=sorted(SUPPORTED_CONTROL_MODES),
        )

    try:
        phase = float(carrier_phase_s)
    except (TypeError, ValueError) as exc:
        raise CircuitCompileError(
            "invalid_carrier_phase", "carrier_phase_s must be a number"
        ) from exc
    if not math.isfinite(phase):
        raise CircuitCompileError(
            "invalid_carrier_phase", "carrier_phase_s must be finite"
        )
    phase %= switching_period
    # A phase within floating-point noise of one period is zero phase; leaving
    # it as a whole period would make the carrier wait an extra period.
    if switching_period - phase <= 1e-12 * max(1.0, switching_period):
        phase = 0.0

    # The declared base phase applies to every gate; an interleaved phase adds
    # its own share of a period on top.  The renderer *advances* a carrier by
    # the phase it is given, so a phase that must fire later is written as a
    # negative advance.  Each value is normalised here because ``_render_gate``
    # compares the phase against the on-window to decide whether a pulse spans
    # the window start, and that comparison is only meaningful inside one
    # period.
    phase_by_net: dict[str, float] = {}
    for control_net in gate_windows:
        value = (phase - delays.get(control_net, 0.0)) % switching_period
        if switching_period - value <= 1e-12 * max(1.0, switching_period):
            value = 0.0
        phase_by_net[control_net] = value

    # The record answers "when does this gate next rise inside the compiled
    # window", which is the fact a reader can check against the netlist.  A
    # phase value would answer the same question backwards - the renderer
    # advances a carrier by the phase, so its first rising edge is one period
    # later minus that advance - and a record a reader has to invert is a record
    # that gets misread.  A gate with an empty on-window is absent: it never
    # turns on, so it has no first rise to report.
    gate_first_rise_s: dict[str, float] = {}
    for control_net, (start_s, stop_s) in gate_windows.items():
        if stop_s <= start_s:
            continue
        first_rise = (start_s - phase_by_net[control_net]) % switching_period
        if switching_period - first_rise <= 1e-12 * max(1.0, switching_period):
            first_rise = 0.0
        gate_first_rise_s[control_net] = first_rise

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
    carrier_tails: dict[str, float] = {}
    # Synthetic carrier nodes must not shadow a declared net: a descriptor that
    # happens to use the same name is a legitimate descriptor, and silently
    # merging the two nodes would change the circuit.
    taken_nodes: set[str] = set(nets)

    for component in descriptor.components:
        canonical = canonical_device_kind(component.kind)
        assert canonical is not None
        spec = device_spec(canonical)
        letter = spec.spicetype.upper()
        assert letter is not None
        counters[letter] = counters.get(letter, 0) + 1
        name = f"{letter}{counters[letter]}"
        element_names[component.component_id] = name
        view = _component_view(component, parameter_overrides)
        nodes = {
            port.port_id: _node(_bound_net(view, port.port_id), reference_net)
            for port in spec.ports
        }

        if canonical == "voltage_source":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} DC "
                f"{_format(_parameter(view, 'voltage_v', required=True))}"
            )
            # Its current is what makes an energy balance possible: without it a
            # solved window cannot report input power at all, and "no measurement"
            # would look like "no loss".
            saved.append(f"i({name})")
        elif canonical == "pulse_source":
            control_net = _bound_net(view, "positive")
            window = gate_windows.get(control_net)
            if window is None:
                raise CircuitCompileError(
                    "ambiguous_complementary_control",
                    f"carrier {component.component_id!r} drives {control_net!r}, "
                    "which is not one of the declared gate nets",
                    net=control_net,
                    gate_nets=sorted(gate_windows),
                )
            start_s, stop_s = window
            gate_cards, tail_s = _render_gate(
                name=name,
                component=view,
                control_node=nodes["positive"],
                negative_node=nodes["negative"],
                start_s=start_s,
                stop_s=stop_s,
                phase_s=phase_by_net[control_net],
                switching_period_s=switching_period,
                taken_nodes=taken_nodes,
                counters=counters,
            )
            cards.extend(gate_cards)
            if tail_s > 0.0:
                carrier_tails[control_net] = tail_s
            saved.append(f"i({name})")
        elif canonical == "inductor":
            initial = _parameter(view, "initial_current_a")
            if initial_conditions and component.component_id in initial_conditions:
                initial = float(initial_conditions[component.component_id])
            cards.append(
                f"{name} {nodes['input']} {nodes['output']} "
                f"{_format(_parameter(view, 'inductance_h', required=True))} "
                f"IC={_format(0.0 if initial is None else initial)}"
            )
            saved.append(f"i({name})")
        elif canonical == "capacitor":
            initial = _parameter(view, "initial_voltage_v")
            if initial_conditions and component.component_id in initial_conditions:
                initial = float(initial_conditions[component.component_id])
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} "
                f"{_format(_parameter(view, 'capacitance_f', required=True))} "
                f"IC={_format(0.0 if initial is None else initial)}"
            )
        elif canonical == "resistor_load":
            cards.append(
                f"{name} {nodes['positive']} {nodes['negative']} "
                f"{_format(_parameter(view, 'resistance_ohm', required=True))}"
            )
        elif canonical == "voltage_controlled_switch":
            # The switch measures V(control) - V(control reference).  When the
            # carrier that drives this gate is referenced to something other
            # than the circuit reference (a floating or high-side drive), that
            # node is the switch's control reference; using 0 would compare the
            # gate against the wrong node while every card still looked legal.
            control_net = _bound_net(view, "control")
            driver = carrier_by_net.get(control_net)
            control_reference = (
                _node(_bound_net(driver, "negative"), reference_net)
                if driver is not None
                else _REFERENCE_NODE
            )
            cards.append(
                f"{name} {nodes['input']} {nodes['output']} {nodes['control']} "
                f"{control_reference} {name}"
            )
            on_resistance = _parameter(view, "on_resistance_ohm")
            off_resistance = _parameter(view, "off_resistance_ohm")
            threshold = _parameter(view, "threshold_v")
            hysteresis = _parameter(view, "hysteresis_v")
            model_cards.append(
                f".model {name} SW(Ron={_format(1e-3 if on_resistance is None else on_resistance)} "
                f"Roff={_format(1e9 if off_resistance is None else off_resistance)} "
                f"Vt={_format(0.5 if threshold is None else threshold)} "
                f"Vh={_format(0.0 if hysteresis is None else hysteresis)})"
            )
        elif canonical == "freewheeling_diode":
            cards.append(f"{name} {nodes['anode']} {nodes['cathode']} {name}")
            saturation = _parameter(view, "saturation_current_a")
            emission = _parameter(view, "emission_coefficient")
            model_cards.append(
                f".model {name} D(Is={_format(1e-12 if saturation is None else saturation)} "
                f"N={_format(0.02 if emission is None else emission)})"
            )
        else:  # pragma: no cover - guarded above by ``renderable``
            raise CircuitCompileError(
                "not_compilable",
                f"no element card is defined for device kind {canonical!r}",
                component_id=component.component_id,
            )

    saved = [f"v({net_id})" for net_id in nets if net_id != reference_net] + saved
    loads = [
        component
        for component in descriptor.components
        if canonical_device_kind(component.kind) == "resistor_load"
    ]
    output_net = _bound_net(loads[0], "positive") if len(loads) == 1 else None

    lines = [
        "* pe-sim compiled circuit",
        f"* template: {CIRCUIT_NETLIST_TEMPLATE_VERSION}",
        f"* topology: {descriptor.topology_id}",
        f"* descriptor_fingerprint: {descriptor.fingerprint}",
        f"* duty: {_format(duty_value)}",
        "",
        _render_options(options, integrator),
        f".temp {_format(temperature)}",
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
        carrier_phase_s=phase,
        carrier_tail_s=max(carrier_tails.values(), default=0.0),
        control_mode=control_mode,
        carrier_tails_s=tuple(sorted(carrier_tails.items())),
        gate_first_rise_s=tuple(sorted(gate_first_rise_s.items())),
        phases=_phase_total(control_mode, declaration, gate_windows),
        element_names=tuple(element_names.items()),
        output_net=output_net,
    )


def circuit_netlist_digest(text: str) -> str:
    """Return the stable SHA-256 identity of one compiled netlist."""

    if not isinstance(text, str) or not text:
        raise ValueError("netlist text must be a non-empty string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "CIRCUIT_COMPILE_CODES",
    "CIRCUIT_NETLIST_TEMPLATE_VERSION",
    "CONTROL_MODE_COMPLEMENTARY",
    "CONTROL_MODE_INTERLEAVED",
    "CONTROL_MODE_PAIRED",
    "DEFAULT_CIRCUIT_OPTIONS",
    "SUPPORTED_CONTROL_MODES",
    "SUPPORTED_INTEGRATORS",
    "CircuitCompileError",
    "CompiledCircuit",
    "circuit_netlist_digest",
    "compile_topology",
    "default_sample_step_s",
]
