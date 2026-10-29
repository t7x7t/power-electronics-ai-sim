"""Import a SPICE netlist subset as a topology descriptor.

An engineer who already has a netlist should not have to retype it as a graph.
This module reads the part of SPICE that the closed device vocabulary
(``pe_sim.circuit_devices``) can express and returns the *same* descriptor
document an author would have written by hand.  That is the whole claim: the
result is an ordinary descriptor, so it goes through
``pe_sim.topology_validation`` and ``pe_sim.circuit_compile`` like any other.
There is no second execution path and no second renderer, and an imported
circuit cannot reach a solver except through the reviewed compile step.

Four boundaries are deliberate.

**A whitelist, not a parser.**  Six element letters are understood - ``V``
(DC or ``PULSE``), ``L``, ``C``, ``R``, ``S`` and ``D`` - and every other
letter is ``unsupported_element`` rather than a silent omission.  In
particular a ``B`` source cannot be imported even though this project's own
compiler emits one for a window that starts inside a carrier pulse: that source
is a rendering detail of a window, not an element of the vocabulary.

**Execution intent is dropped, and said so.**  ``.tran``, ``.options``,
``.temp``, probe and analysis directives, and a whole ``.control`` block say
*how* a netlist is run; the compiled topology owns that instead.  The importer
discards them and lists every discard in ``ignored_directives`` with a reason.
Nothing is dropped silently, and nothing in that list changes the circuit.

**Indirection is refused.**  ``.include``, ``.lib``, ``.subckt``, ``.func``,
``.global``, ``.ic`` and parameter *expressions* would make the imported circuit
depend on text the importer never read, so they are refusals.  ``.param
NAME=<number>`` is accepted: substituting a literal number that is right there
in the file adds no information the importer does not have.

**One answer per question.**  The importer answers "did the netlist translate",
not "is this a sensible circuit".  ``spice_import_document`` therefore runs the
topology validator on the descriptor it produced and reports the two answers
separately; merging them into one boolean would make an unmappable element and
an unconnected node look like the same problem.

Like the rest of the authoring layer this module is stdlib-only and never
imports ``pe_sim.backends``: importing a netlist must not need a simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .circuit_devices import (
    NET_KIND_CONTROL,
    NET_KIND_ELECTRICAL,
    NET_KIND_REFERENCE,
    canonical_device_kind,
    device_spec,
)
from .topology import TOPOLOGY_SCHEMA


#: Schema tag for the import envelope; a client branches on this tag rather than
#: on the module layout.
SPICE_IMPORT_SCHEMA = "pe-sim.spice-import.v1"

#: Every issue code this module can produce.  Closed: a client may switch on it.
IMPORT_ISSUE_CODES = frozenset(
    {
        # document shape
        "empty_netlist",
        "missing_title",
        "malformed_card",
        # elements and values
        "unsupported_element",
        "unsupported_directive",
        "unsupported_expression",
        "unsupported_source_function",
        "invalid_value",
        "duplicate_element",
        # models
        "unknown_model",
        "unsupported_model_type",
        "unsupported_parameter",
        "duplicate_model",
        "unused_model",
        # topology-level facts the importer must establish before validation
        "missing_ground",
        "unsupported_control_reference",
        # information the descriptor cannot carry
        "pulse_width_not_imported",
        "complementary_control_undeclared",
    }
)

#: The subset of :data:`IMPORT_ISSUE_CODES` that never makes an import fail.
WARNING_IMPORT_CODES = frozenset(
    {
        "unused_model",
        "pulse_width_not_imported",
        "complementary_control_undeclared",
    }
)

#: Element letters the vocabulary can express, mapped to the canonical device
#: kind.  ``V`` is refined to ``pulse_source`` when its value is a ``PULSE``
#: specification; every other letter maps one to one.
ELEMENT_LETTERS: Mapping[str, str] = MappingProxyType(
    {
        "v": "voltage_source",
        "l": "inductor",
        "c": "capacitor",
        "r": "resistor_load",
        "s": "voltage_controlled_switch",
        "d": "freewheeling_diode",
    }
)

#: Descriptor port bound by each SPICE node position, in card order.  ``None``
#: marks a node the card carries but the vocabulary does not model as a port:
#: an ``S`` element's fourth node is the voltage its control node is measured
#: against, which the compiler derives from the driving carrier instead.
ELEMENT_NODES: Mapping[str, tuple[str | None, ...]] = MappingProxyType(
    {
        "v": ("positive", "negative"),
        "l": ("input", "output"),
        "c": ("positive", "negative"),
        "r": ("positive", "negative"),
        "s": ("input", "output", "control", None),
        "d": ("anode", "cathode"),
    }
)

#: Element letters whose card ends with a model name instead of a value.
MODEL_LETTERS = frozenset({"s", "d"})

#: The node name SPICE reserves for the reference node.
GROUND_NODE = "0"

#: Net id the ground node becomes.  Fixed rather than derived so an imported
#: circuit and a hand-written one name the reference net the same way.
GROUND_NET_ID = "gnd"

#: Model types that map onto a device kind.  ``D`` is a diode and ``SW`` is a
#: voltage-controlled switch; anything else would need a device the vocabulary
#: does not have.
MODEL_TYPES: Mapping[str, str] = MappingProxyType(
    {"d": "freewheeling_diode", "sw": "voltage_controlled_switch"}
)

#: Model parameter names, per device kind, in the spelling ngspice uses.
MODEL_PARAMETERS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "freewheeling_diode": MappingProxyType(
            {"is": "saturation_current_a", "n": "emission_coefficient"}
        ),
        "voltage_controlled_switch": MappingProxyType(
            {
                "ron": "on_resistance_ohm",
                "roff": "off_resistance_ohm",
                "vt": "threshold_v",
                "vh": "hysteresis_v",
            }
        ),
    }
)

#: Directives that describe an execution the compiled topology re-derives, so the
#: importer drops them.  The reason is reported with every drop.
IGNORED_DIRECTIVES: Mapping[str, str] = MappingProxyType(
    {
        ".tran": "the solve window is a compile argument, not a circuit property",
        ".options": "solver options are compile arguments",
        ".opt": "solver options are compile arguments",
        ".temp": "the analysis temperature is a compile argument",
        ".save": "stored vectors are chosen by the compiler",
        ".print": "printed output is chosen by the compiler",
        ".plot": "plotted output is chosen by the compiler",
        ".op": "operating-point analysis is a separate capability, not a circuit",
        ".dc": "sweep analysis is a separate capability, not a circuit",
        ".ac": "small-signal analysis is a separate capability, not a circuit",
        ".four": "Fourier analysis is post-processing, not a circuit",
        ".fourier": "Fourier analysis is post-processing, not a circuit",
        ".measure": "measurement statements are post-processing, not a circuit",
        ".meas": "measurement statements are post-processing, not a circuit",
        ".width": "output formatting is a run setting",
        ".title": "the title is recorded separately",
        ".nodeset": "an initial guess for the solver; use IC= on the element",
    }
)

#: Directives that would make the circuit depend on text this module never read.
REFUSED_DIRECTIVES: Mapping[str, str] = MappingProxyType(
    {
        ".include": "an included file would be read by the solver but never by the importer",
        ".inc": "an included file would be read by the solver but never by the importer",
        ".lib": "a library section pulls in a circuit the importer never reads",
        ".subckt": "subcircuit instantiation is not part of the closed device vocabulary",
        ".ends": "subcircuit instantiation is not part of the closed device vocabulary",
        ".func": "a user function is evaluated by the solver, not by the importer",
        ".global": "a global net changes net identity between files",
        ".ic": "use IC= on the inductor or capacitor card, which is unambiguous",
        ".if": "conditional netlist sections are not evaluated by the importer",
        ".else": "conditional netlist sections are not evaluated by the importer",
        ".elseif": "conditional netlist sections are not evaluated by the importer",
        ".endif": "conditional netlist sections are not evaluated by the importer",
        ".step": "a parameter sweep is a study, not a circuit",
        ".csparam": "parameter expressions are not evaluated by the importer",
        ".data": "data blocks are not part of the closed device vocabulary",
        ".enddata": "data blocks are not part of the closed device vocabulary",
    }
)

#: SPICE scale factors.  The closed list is on purpose: ``a`` (atto) and other
#: factors no power-electronic value ever needs are absent, so ``ic=0.45A`` is an
#: error instead of 4.5e-19.  See ``docs/34`` for the incident that motivates it.
_SCALE_FACTORS: Mapping[str, float] = MappingProxyType(
    {
        "t": 1e12,
        "g": 1e9,
        "meg": 1e6,
        "mil": 25.4e-6,
        "k": 1e3,
        "m": 1e-3,
        "u": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
        "f": 1e-15,
    }
)
_SCALE_BY_LENGTH = tuple(
    sorted(_SCALE_FACTORS, key=lambda key: (-len(key), key))
)

_NUMBER_RE = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([A-Za-z]*)$")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CARD_NAME_RE = re.compile(r"^[A-Za-z]\d[A-Za-z0-9_]*$")
_BRACE_TOKEN_RE = re.compile(r"^\{([^{}]*)\}$")

_SEVERITIES = frozenset({"error", "warning"})


class SpiceImportError(ValueError):
    """Raised when a netlist cannot be translated into a descriptor."""

    def __init__(self, issues: Iterable["SpiceImportIssue"]) -> None:
        self.issues = _finalize(issues)
        errors = [issue for issue in self.issues if issue.severity == "error"]
        first = errors[0] if errors else None
        message = (
            f"netlist import failed with {len(errors)} error(s)"
            if first is None
            else f"netlist import failed: {first.message}"
        )
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SPICE_IMPORT_SCHEMA,
            "error_count": len(
                [issue for issue in self.issues if issue.severity == "error"]
            ),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class SpiceImportIssue:
    """One reported netlist defect or data-loss warning.

    ``line`` is the physical line in the source text (1-based); ``path`` is the
    locator a client should print, kept in the same ``/line/<n>`` shape the
    other authoring envelopes use so a client needs one formatter.
    """

    code: str
    line: int
    message: str
    expected: str | None = None
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.code not in IMPORT_ISSUE_CODES:
            raise ValueError(f"unknown import issue code {self.code!r}")
        if self.severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if (self.code in WARNING_IMPORT_CODES) != (self.severity == "warning"):
            raise ValueError(
                f"code {self.code!r} cannot carry severity {self.severity!r}"
            )
        if not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1:
            raise ValueError("issue line must be a positive integer")
        if not str(self.message).strip():
            raise ValueError("issue message must not be empty")

    @property
    def path(self) -> str:
        return f"/line/{self.line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "expected": self.expected,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ImportedNetlist:
    """A translated netlist: one descriptor document plus what was dropped.

    ``document`` has no ``fingerprint``: it is a descriptor being authored, and
    requiring the caller to compute a canonical SHA-256 first would be a trap
    rather than a gate (``docs/64`` section 4.6).
    """

    document: Mapping[str, Any]
    title: str
    warnings: tuple[SpiceImportIssue, ...] = ()
    ignored_directives: tuple[Mapping[str, Any], ...] = ()
    net_name_map: Mapping[str, str] = MappingProxyType({})
    component_name_map: Mapping[str, str] = MappingProxyType({})
    observed_duty: Mapping[str, float] = MappingProxyType({})
    element_kinds: Mapping[str, int] = MappingProxyType({})
    netlist_sha256: str = ""


def _finalize(issues: Iterable[SpiceImportIssue]) -> tuple[SpiceImportIssue, ...]:
    """Deduplicate on ``(code, line)`` and sort errors first, then by position.

    One line may only report one of each code: a card whose value is missing
    should not also be reported as an unknown parameter three times.
    """

    seen: set[tuple[str, int]] = set()
    unique: list[SpiceImportIssue] = []
    for issue in issues:
        key = (issue.code, issue.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    unique.sort(key=lambda item: (0 if item.severity == "error" else 1, item.line, item.code))
    return tuple(unique)


@dataclass
class _Context:
    """Collects issues instead of raising, so one pass reports every defect."""

    issues: list[SpiceImportIssue] = field(default_factory=list)

    def error(
        self,
        code: str,
        line: int,
        message: str,
        *,
        expected: str | None = None,
    ) -> None:
        self.issues.append(
            SpiceImportIssue(
                code=code, line=line, message=message, expected=expected
            )
        )

    def warning(
        self,
        code: str,
        line: int,
        message: str,
        *,
        expected: str | None = None,
    ) -> None:
        self.issues.append(
            SpiceImportIssue(
                code=code,
                line=line,
                message=message,
                expected=expected,
                severity="warning",
            )
        )

    @property
    def failed(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class _Line:
    number: int
    text: str


@dataclass
class _Element:
    """One parsed element card, before its model and net ids are resolved."""

    line: int
    name: str
    letter: str
    kind: str
    nodes: tuple[str, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None
    observed_duty: float | None = None


@dataclass
class _Model:
    line: int
    name: str
    kind: str
    parameters: dict[str, Any] = field(default_factory=dict)


def _logical_lines(text: str) -> list[_Line]:
    """Split into logical lines, joining ``+`` continuations.

    A continuation keeps the line number of the card it continues, so an issue
    on a three-line card points at the card rather than at a fragment.
    """

    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[_Line] = []
    for index, line in enumerate(raw, start=1):
        stripped = line.strip()
        if stripped.startswith("+"):
            continuation = stripped[1:].strip()
            if result:
                previous = result[-1]
                result[-1] = _Line(previous.number, f"{previous.text} {continuation}")
            else:
                result.append(_Line(index, continuation))
            continue
        result.append(_Line(index, line))
    return result


def _strip_comment(text: str) -> str:
    """Remove an inline ``;`` comment.

    ``$`` is not treated as a comment start: it is a legal character in a node
    name, and silently truncating a card at one would delete an element.
    """

    return text.split(";", 1)[0]


def _split_tokens(text: str) -> list[str]:
    """Split on whitespace, but never inside parentheses.

    ``PULSE(0 1 0 1e-9 1e-9 5e-5 1e-5)`` must stay one token; otherwise every
    parenthesised argument list would need its own reassembly rule.
    """

    tokens: list[str] = []
    buffer: list[str] = []
    depth = 0
    for character in text:
        if character == "(":
            depth += 1
            buffer.append(character)
        elif character == ")":
            depth = depth - 1 if depth else 0
            buffer.append(character)
        elif character.isspace() and depth == 0:
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
        else:
            buffer.append(character)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _paren_args(text: str) -> tuple[str, list[str]] | None:
    """Split ``NAME(a b c)`` into ``("NAME", ["a", "b", "c"])``."""

    start = text.find("(")
    stripped = text.rstrip()
    if start < 0 or not stripped.endswith(")"):
        return None
    head = text[:start].strip()
    inside = stripped[start + 1 : -1]
    if "(" in inside or ")" in inside:
        return None
    return head, inside.split()


def _normalise(value: float) -> float:
    """Round a parsed value to 15 significant decimal digits.

    ``100u`` is exactly 1e-4, but ``100 * 1e-6`` evaluates to
    ``9.999999999999999e-05``.  Fifteen digits is coarser than double precision
    and finer than the 13 significant digits the compiled netlist renders, so
    this removes representation noise without losing a value the circuit could
    have carried.
    """

    return float(f"{value:.15g}")


def _parse_number(text: str) -> float | None:
    """Parse a SPICE number, or return ``None`` when it is not one."""

    match = _NUMBER_RE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    suffix = match.group(2).lower()
    if suffix:
        factor = None
        for key in _SCALE_BY_LENGTH:
            if suffix.startswith(key):
                factor = _SCALE_FACTORS[key]
                break
        if factor is None:
            return None
        value *= factor
    if not math.isfinite(value):
        return None
    return _normalise(value)


def _number(
    text: str, ctx: _Context, line: int, *, what: str
) -> float | None:
    value = _parse_number(text)
    if value is None:
        ctx.error(
            "invalid_value",
            line,
            f"{what} {text!r} is not a number this importer understands",
            expected=(
                "a decimal number with an optional scale suffix from "
                + ", ".join(sorted(_SCALE_FACTORS))
            ),
        )
    return value


def _parameter_map(tokens: Iterable[str]) -> dict[str, str] | None:
    """Split ``key=value`` tokens into a mapping, or ``None`` if malformed."""

    result: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            return None
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if not key or not value.strip():
            return None
        result[key] = value.strip()
    return result


def _looks_like_a_card(text: str) -> bool:
    """Whether a would-be title is in fact an element card or a directive.

    SPICE always reads the first line as the title.  Pasting a netlist without
    one therefore deletes the first element while every remaining line still
    looks right, which is the worst possible failure: the circuit simulates and
    the answer is wrong.  Two shapes are unambiguous enough to refuse - a
    directive, and a conventional element name such as ``V1`` - and nothing else
    is guessed at.
    """

    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("."):
        return True
    first = stripped.split()[0]
    if not _CARD_NAME_RE.fullmatch(first):
        return False
    return first[0].lower() in ELEMENT_LETTERS


def _sanitise_identifier(value: str) -> str:
    """Turn a SPICE name into a descriptor-safe identifier."""

    text = _UNSAFE_ID_CHARS.sub("_", value)
    if not text:
        text = "_"
    while ".." in text:
        text = text.replace("..", "_")
    if text in {".", ".."}:
        text = "_"
    return text


def _unique_identifier(base: str, taken: dict[str, Any]) -> str:
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _resolve_value(
    token: str, parameters: Mapping[str, float], ctx: _Context, line: int, *, what: str
) -> float | None:
    """Resolve ``{name}`` against numeric ``.param`` entries, else parse a number."""

    text = token.strip()
    if "{" in text or "}" in text:
        match = _BRACE_TOKEN_RE.fullmatch(text)
        if match is None:
            ctx.error(
                "unsupported_expression",
                line,
                f"{what} {token!r} is a parameter expression; only a single "
                "{{name}} referring to a numeric .param is accepted",
            )
            return None
        name = match.group(1).strip().lower()
        if name not in parameters:
            ctx.error(
                "unsupported_expression",
                line,
                f"{what} refers to {{{match.group(1)}}}, which no .param declares "
                "as a number",
                expected="a numeric .param with that name",
            )
            return None
        return parameters[name]
    return _number(text, ctx, line, what=what)


def _parse_pulse(
    token: str,
    parameters: Mapping[str, float],
    ctx: _Context,
    line: int,
    name: str,
) -> tuple[dict[str, Any], float | None] | None:
    """Translate a ``PULSE(...)`` specification into carrier parameters.

    The pulse *width* is deliberately not a parameter: the compiler derives it
    from the duty ratio it is commanded, so the width found in a netlist can only
    be reported as an observation.
    """

    parsed = _paren_args(token)
    if parsed is None:
        ctx.error(
            "malformed_card",
            line,
            f"carrier {name} has a malformed PULSE specification {token!r}",
            expected="PULSE(v_initial v_pulsed t_delay t_rise t_fall t_width t_period)",
        )
        return None
    head, args = parsed
    if head.lower() != "pulse":
        ctx.error(
            "unsupported_source_function",
            line,
            f"source {name} uses the {head!r} waveform, which this importer does "
            "not translate",
            expected="PULSE(...)",
        )
        return None
    if len(args) != 7:
        ctx.error(
            "malformed_card",
            line,
            f"carrier {name} declares {len(args)} PULSE argument(s) instead of 7",
            expected="PULSE(v_initial v_pulsed t_delay t_rise t_fall t_width t_period)",
        )
        return None
    values = [
        _resolve_value(arg, parameters, ctx, line, what=f"PULSE argument {index + 1}")
        for index, arg in enumerate(args)
    ]
    if any(value is None for value in values):
        return None
    initial, pulsed, _delay, rise, fall, width, period = (float(v) for v in values)  # type: ignore[arg-type]
    if initial != 0.0:
        ctx.error(
            "unsupported_source_function",
            line,
            f"carrier {name} starts its pulse at {initial:g} V; the vocabulary's "
            "carrier is low at the start of the window",
            expected="a PULSE specification whose initial value is 0",
        )
        return None
    if pulsed <= 0.0:
        ctx.error(
            "invalid_value",
            line,
            f"carrier {name} has a non-positive pulse amplitude {pulsed:g} V",
            expected="a positive amplitude",
        )
        return None
    if period <= 0.0:
        ctx.error(
            "invalid_value",
            line,
            f"carrier {name} has a non-positive period {period:g} s",
            expected="a positive period",
        )
        return None
    if width < 0.0 or width > period:
        ctx.error(
            "invalid_value",
            line,
            f"carrier {name} has a pulse width {width:g} s outside [0, period]",
            expected="a width between zero and the period",
        )
        return None
    result: dict[str, Any] = {
        "switching_frequency_hz": _normalise(1.0 / period),
        "amplitude_v": pulsed,
    }
    # A zero rise or fall time means "instantaneous", which the vocabulary
    # expresses as its own default.  Omitting the parameter is not inventing a
    # value: it is leaving an optional field to its declared default.
    if rise > 0.0:
        result["rise_time_s"] = rise
    if fall > 0.0:
        result["fall_time_s"] = fall
    return result, (width / period if period else None)


def _parse_element(
    line: int,
    tokens: list[str],
    parameters: Mapping[str, float],
    ctx: _Context,
) -> _Element | None:
    """Parse one element card into a device-agnostic record."""

    name = tokens[0]
    letter = name[0].lower()
    kind = ELEMENT_LETTERS.get(letter)
    if kind is None:
        ctx.error(
            "unsupported_element",
            line,
            f"element {name!r} uses the letter {name[0]!r}, which is outside the "
            "closed device vocabulary",
            expected="one of: " + ", ".join(sorted(key.upper() for key in ELEMENT_LETTERS)),
        )
        return None
    shape = ELEMENT_NODES[letter]
    node_count = len(shape)
    shape_text = " ".join(
        f"<{port_id}>" if port_id is not None else "<control-reference>"
        for port_id in shape
    )
    if len(tokens) < node_count + 2:
        ctx.error(
            "malformed_card",
            line,
            f"element {name!r} declares {max(len(tokens) - 1, 0)} token(s) but a "
            f"{kind} needs {node_count} node(s) and a "
            + ("model" if letter in MODEL_LETTERS else "value"),
            expected=f"{name} "
            + shape_text
            + (" <model>" if letter in MODEL_LETTERS else " <value>"),
        )
        return None

    nodes = tuple(tokens[1 : node_count + 1])
    rest = tokens[node_count + 1 :]
    element = _Element(
        line=line, name=name, letter=letter, kind=kind, nodes=nodes
    )

    if kind == "voltage_source":
        if not rest:
            ctx.error(
                "malformed_card",
                line,
                f"source {name} declares no value",
                expected="a DC value or PULSE(...)",
            )
            return None
        head = rest[0]
        if "(" in head:
            parsed = _parse_pulse(head, parameters, ctx, line, name)
            if parsed is None:
                return None
            element.kind = "pulse_source"
            element.parameters, element.observed_duty = parsed
            return element
        if head.lower() == "ac":
            ctx.error(
                "unsupported_source_function",
                line,
                f"source {name} declares an AC specification, which is not a "
                "time-domain circuit property",
                expected="a DC value or PULSE(...)",
            )
            return None
        values = rest[1:] if head.lower() == "dc" else rest
        if len(values) != 1:
            ctx.error(
                "malformed_card",
                line,
                f"source {name} declares {len(values)} value token(s) instead of 1",
                expected="a single DC value",
            )
            return None
        value = _resolve_value(values[0], parameters, ctx, line, what=f"value of {name}")
        if value is None:
            return None
        element.parameters = {"voltage_v": value}
        return element

    if kind in {"inductor", "capacitor", "resistor_load"}:
        value = _resolve_value(rest[0], parameters, ctx, line, what=f"value of {name}")
        if value is None:
            return None
        quantity = {
            "inductor": "inductance_h",
            "capacitor": "capacitance_f",
            "resistor_load": "resistance_ohm",
        }[kind]
        element.parameters = {quantity: value}
        for extra in rest[1:]:
            key, separator, raw = extra.partition("=")
            key_lower = key.strip().lower()
            if separator and key_lower == "ic":
                initial_name = {
                    "inductor": "initial_current_a",
                    "capacitor": "initial_voltage_v",
                }.get(kind)
                if initial_name is None:
                    ctx.error(
                        "unsupported_parameter",
                        line,
                        f"element {name} declares IC=, but a {kind} has no initial state",
                        expected="IC= only on an inductor or a capacitor",
                    )
                    continue
                initial = _resolve_value(
                    raw, parameters, ctx, line, what=f"IC= of {name}"
                )
                if initial is not None:
                    element.parameters[initial_name] = initial
                continue
            ctx.error(
                "unsupported_parameter",
                line,
                f"element {name} declares the parameter {extra!r}, which a {kind} "
                "does not have",
                expected="the element value, optionally followed by IC="
                if kind in {"inductor", "capacitor"}
                else "the element value only",
            )
        return element

    if letter in MODEL_LETTERS:
        if len(rest) != 1:
            ctx.error(
                "malformed_card",
                line,
                f"element {name} declares {len(rest)} token(s) after its nodes "
                "instead of a model name",
                expected="a .model name",
            )
            return None
        element.model_name = rest[0]
        return element

    ctx.error(
        "unsupported_element",
        line,
        f"element {name!r} of kind {kind!r} cannot be imported",
        expected="one of: " + ", ".join(sorted(ELEMENT_LETTERS)),
    )
    return None


def _parse_model(
    line: int,
    tokens: list[str],
    parameters: Mapping[str, float],
    ctx: _Context,
) -> _Model | None:
    """Parse one ``.model`` card for a diode or a switch."""

    if len(tokens) < 3:
        ctx.error(
            "malformed_card",
            line,
            ".model needs a name and a typed parameter list",
            expected=".model NAME D(...) or .model NAME SW(...)",
        )
        return None
    name = tokens[1]
    body = "".join(tokens[2:])
    parsed = _paren_args(body)
    if parsed is None:
        ctx.error(
            "malformed_card",
            line,
            f"model {name!r} has a malformed parameter list {body!r}",
            expected=".model NAME TYPE(key=value ...)",
        )
        return None
    type_name, args = parsed
    kind = MODEL_TYPES.get(type_name.strip().lower())
    if kind is None:
        ctx.error(
            "unsupported_model_type",
            line,
            f"model {name!r} is of type {type_name!r}, which no device kind in the "
            "vocabulary renders from",
            expected="one of: " + ", ".join(sorted(key.upper() for key in MODEL_TYPES)),
        )
        return None
    mapping = MODEL_PARAMETERS[kind]
    values: dict[str, str] | None = _parameter_map(args)
    if values is None:
        ctx.error(
            "unsupported_parameter",
            line,
            f"model {name!r} declares an argument that is not a key=value pair",
            expected="parameters of the form name=value",
        )
        return None
    model = _Model(line=line, name=name, kind=kind)
    for key, raw in values.items():
        target = mapping.get(key)
        if target is None:
            ctx.error(
                "unsupported_parameter",
                line,
                f"model {name!r} declares {key!r}, which has no counterpart in the "
                f"{kind} vocabulary",
                expected="one of: " + ", ".join(sorted(mapping)),
            )
            continue
        value = _resolve_value(raw, parameters, ctx, line, what=f"parameter {key} of {name}")
        if value is None:
            continue
        if target in model.parameters:
            ctx.error(
                "malformed_card",
                line,
                f"model {name!r} declares {key!r} more than once",
            )
            continue
        model.parameters[target] = value
    return model


def _collect_parameters(
    line: int,
    tokens: list[str],
    parameters: dict[str, float],
    ctx: _Context,
) -> None:
    """Add the numeric declarations of one ``.param`` card."""

    for token in tokens[1:]:
        key, separator, raw = token.partition("=")
        key_lower = key.strip().lower()
        if not separator or not _PARAM_NAME_RE.fullmatch(key_lower):
            ctx.error(
                "malformed_card",
                line,
                f".param argument {token!r} is not NAME=value",
                expected=".param NAME=<number>",
            )
            continue
        if "{" in raw or "}" in raw:
            ctx.error(
                "unsupported_expression",
                line,
                f".param {key.strip()} is defined by the expression {raw!r}; "
                "only a numeric literal is accepted",
                expected="a numeric literal",
            )
            continue
        value = _number(raw, ctx, line, what=f".param {key.strip()}")
        if value is None:
            continue
        if key_lower in parameters:
            ctx.error(
                "malformed_card",
                line,
                f".param {key.strip()} is declared more than once",
            )
            continue
        parameters[key_lower] = value


def _prescan_parameters(
    lines: list[_Line], parameters: dict[str, float], ctx: _Context
) -> None:
    """Collect every ``.param`` before any card is parsed.

    SPICE resolves ``{name}`` against the whole file, not against what has been
    read so far, so a parameter declared *below* the card that uses it is legal.
    Collecting them first makes the importer order-independent instead of
    reporting a confusing "no .param declares this name" for a legal netlist.

    The control and subcircuit spans are skipped for the same reason the main
    pass skips them: a ``.param`` inside them is not part of this circuit.
    """

    in_control = False
    subcircuit_depth = 0
    for line in lines[1:]:
        raw = _strip_comment(line.text).strip()
        if not raw or raw.startswith("*"):
            continue
        tokens = _split_tokens(raw)
        if not tokens:
            continue
        keyword = tokens[0].lower()
        if in_control:
            if keyword == ".endc":
                in_control = False
            continue
        if subcircuit_depth:
            if keyword == ".subckt":
                subcircuit_depth += 1
            elif keyword == ".ends":
                subcircuit_depth -= 1
            continue
        if keyword == ".control":
            in_control = True
            continue
        if keyword == ".subckt":
            subcircuit_depth = 1
            continue
        if keyword == ".end":
            return
        if keyword == ".param":
            _collect_parameters(line.number, tokens, parameters, ctx)


def _handle_directive(
    line: int,
    tokens: list[str],
    parameters: dict[str, float],
    models: dict[str, _Model],
    model_lines: dict[str, int],
    ignored: list[dict[str, Any]],
    ctx: _Context,
) -> None:
    """Route one dot-directive: accept, ignore-and-record, or refuse."""

    keyword = tokens[0].lower()

    if keyword == ".model":
        model = _parse_model(line, tokens, parameters, ctx)
        if model is None:
            return
        key = model.name.lower()
        if key in models:
            ctx.error(
                "duplicate_model",
                line,
                f"model {model.name!r} is declared twice (first on line "
                f"{model_lines[key]})",
            )
            return
        models[key] = model
        model_lines[key] = line
        return

    reason = IGNORED_DIRECTIVES.get(keyword)
    if reason is not None:
        ignored.append({"directive": keyword, "line": line, "reason": reason})
        return

    refusal = REFUSED_DIRECTIVES.get(keyword)
    if refusal is not None:
        ctx.error(
            "unsupported_directive",
            line,
            f"{tokens[0]!r} cannot be imported: {refusal}",
            expected=(
                "ignored directives: "
                + ", ".join(sorted(IGNORED_DIRECTIVES))
                + "; or .param/.model"
            ),
        )
        return

    ctx.error(
        "unsupported_directive",
        line,
        f"directive {tokens[0]!r} is not part of the accepted netlist subset",
        expected="one of: " + ", ".join(sorted(set(IGNORED_DIRECTIVES) | {".param", ".model", ".end", ".control"})),
    )


@dataclass(frozen=True)
class _Built:
    """A descriptor document and the two name maps that explain it."""

    document: dict[str, Any]
    net_name_map: dict[str, str]
    component_name_map: dict[str, str]
    observed_duty: dict[str, float]


@dataclass
class _Analysis:
    """Everything one pass over the netlist established."""

    title: str
    document: dict[str, Any]
    ignored: list[dict[str, Any]]
    net_name_map: dict[str, str]
    component_name_map: dict[str, str]
    observed_duty: dict[str, float]
    element_kinds: dict[str, int]
    netlist_sha256: str


def _build_document(
    title: str,
    topology_id: str,
    elements: list[_Element],
    models: dict[str, _Model],
    ctx: _Context,
) -> _Built | None:
    """Turn parsed elements into a descriptor document, or ``None``."""

    # Models first: an element referencing a model that does not exist cannot be
    # rendered, and the model also supplies parameters the card does not state.
    used_models: set[str] = set()
    for element in elements:
        if element.model_name is None:
            continue
        key = element.model_name.lower()
        model = models.get(key)
        if model is None:
            ctx.error(
                "unknown_model",
                element.line,
                f"element {element.name} references the model "
                f"{element.model_name!r}, which no .model card declares",
                expected="a .model card for this name",
            )
            continue
        if model.kind != element.kind:
            ctx.error(
                "unsupported_model_type",
                element.line,
                f"model {element.model_name!r} describes a {model.kind}, but "
                f"element {element.name} is a {element.kind}",
                expected=f"a model of type {element.kind}",
            )
            continue
        used_models.add(key)
        element.parameters = {**model.parameters, **element.parameters}

    for key, model in sorted(models.items()):
        if key not in used_models:
            ctx.warning(
                "unused_model",
                model.line,
                f"model {model.name!r} is declared but no element references it",
            )

    if not elements:
        ctx.error("empty_netlist", 1, "the netlist declares no elements")
        return None

    nodes_in_order: list[str] = []
    seen_nodes: set[str] = set()
    for element in elements:
        for node in element.nodes:
            if node not in seen_nodes:
                seen_nodes.add(node)
                nodes_in_order.append(node)

    if GROUND_NODE not in seen_nodes:
        ctx.error(
            "missing_ground",
            1,
            "no element binds the reference node '0', so the circuit has no "
            "reference net",
            expected="at least one element connected to node 0",
        )
        return None

    carriers: dict[str, list[str]] = {}
    for element in elements:
        if element.kind == "pulse_source":
            carriers.setdefault(element.nodes[0], []).append(element.nodes[1])

    for element in elements:
        if element.kind == "pulse_source" and element.nodes[0] == GROUND_NODE:
            ctx.error(
                "unsupported_control_reference",
                element.line,
                f"carrier {element.name} drives the reference node; a carrier must "
                "drive a control net from its positive terminal",
                expected="a control node as the carrier's first node",
            )
        if element.kind != "voltage_controlled_switch":
            continue
        control_net, control_reference = element.nodes[2], element.nodes[3]
        if control_net == GROUND_NODE:
            ctx.error(
                "unsupported_control_reference",
                element.line,
                f"switch {element.name} is controlled by the reference node",
                expected="a control net as the switch's third node",
            )
            continue
        references = carriers.get(control_net)
        if references is None:
            if control_reference != GROUND_NODE:
                ctx.error(
                    "unsupported_control_reference",
                    element.line,
                    f"switch {element.name} measures its control voltage against "
                    f"node {control_reference!r}, but no carrier drives "
                    f"{control_net!r} from that node",
                    expected=(
                        "a control reference of node 0, or the negative node of "
                        "the carrier that drives this control net"
                    ),
                )
            continue
        if control_reference not in references:
            ctx.error(
                "unsupported_control_reference",
                element.line,
                f"switch {element.name} measures its control voltage against node "
                f"{control_reference!r}, but the carrier driving {control_net!r} "
                f"returns to {references[0]!r}",
                expected="the negative node of the driving carrier",
            )

    if ctx.failed:
        return None

    control_nets = set(carriers)
    control_nets.update(
        element.nodes[2]
        for element in elements
        if element.kind == "voltage_controlled_switch"
    )
    control_nets.discard(GROUND_NODE)

    net_ids: dict[str, str] = {GROUND_NODE: GROUND_NET_ID}
    taken_nets: dict[str, Any] = {GROUND_NET_ID: GROUND_NODE}
    for node in nodes_in_order:
        if node in net_ids:
            continue
        identifier = _unique_identifier(_sanitise_identifier(node), taken_nets)
        net_ids[node] = identifier
        taken_nets[identifier] = node

    nets = []
    for node in nodes_in_order:
        kind = NET_KIND_REFERENCE if node == GROUND_NODE else (
            NET_KIND_CONTROL if node in control_nets else NET_KIND_ELECTRICAL
        )
        nets.append({"net_id": net_ids[node], "kind": kind})

    components = []
    component_name_map: dict[str, str] = {}
    observed_duty: dict[str, float] = {}
    taken_components: dict[str, Any] = {}
    seen_element_names: dict[str, int] = {}
    for element in elements:
        key = element.name.lower()
        if key in seen_element_names:
            ctx.error(
                "duplicate_element",
                element.line,
                f"element {element.name!r} is declared twice (first on line "
                f"{seen_element_names[key]}); SPICE names are case-insensitive",
            )
            continue
        seen_element_names[key] = element.line
        component_id = _unique_identifier(_sanitise_identifier(key), taken_components)
        taken_components[component_id] = element.name
        component_name_map[element.name] = component_id
        if element.observed_duty is not None:
            observed_duty[component_id] = element.observed_duty
        spec = device_spec(element.kind)
        ports = []
        for index, port_id in enumerate(ELEMENT_NODES[element.letter]):
            if port_id is None:
                continue
            port = spec.port(port_id)
            assert port is not None, (element.kind, port_id)
            ports.append(
                {
                    "port_id": port_id,
                    "net_id": net_ids[element.nodes[index]],
                    "direction": port.direction,
                }
            )
        components.append(
            {
                "component_id": component_id,
                "kind": element.kind,
                "ports": ports,
                "parameters": dict(element.parameters),
            }
        )

    if ctx.failed:
        return None

    if len(carriers) >= 2 and len(components) >= 2:
        switches = [
            element for element in elements
            if element.kind == "voltage_controlled_switch"
        ]
        if len(switches) >= 2:
            ctx.warning(
                "complementary_control_undeclared",
                1,
                f"the netlist drives {len(switches)} switches from {len(carriers)} "
                "carriers but the imported descriptor declares no relation "
                "between them, so the compiler will refuse it with "
                "multiple_carriers",
                expected=(
                    'metadata.control = {"mode": "single_duty_complementary", '
                    '"dead_time_s": <seconds>} when the two gates are complementary'
                ),
            )

    for element in elements:
        if element.kind != "pulse_source" or element.observed_duty is None:
            continue
        ctx.warning(
            "pulse_width_not_imported",
            element.line,
            f"carrier {element.name} implies a duty ratio of "
            f"{element.observed_duty:.6g}, but a duty ratio is a compile-time "
            "command in this project, so the width is recorded and not imported",
            expected="duty comes from the compile call, not from the descriptor",
        )

    return _Built(
        document={
            "schema": TOPOLOGY_SCHEMA,
            "descriptor_version": "1.0",
            "topology_id": topology_id,
            "components": components,
            "nets": nets,
            "metadata": {
                "source": {"format": "spice-netlist", "title": title},
            },
        },
        net_name_map=net_ids,
        component_name_map=component_name_map,
        observed_duty=observed_duty,
    )


def _analyse(text: str, topology_id: str, ctx: _Context) -> _Analysis | None:
    """Run the whole import, reporting every defect it can see."""

    if not isinstance(text, str):
        ctx.error("empty_netlist", 1, "the netlist must be text")
        return None
    if not text.strip():
        ctx.error("empty_netlist", 1, "the netlist is empty")
        return None

    lines = _logical_lines(text)
    title_line = lines[0]
    if _looks_like_a_card(title_line.text):
        ctx.error(
            "missing_title",
            title_line.number,
            "SPICE reads the first line as the title, and this one looks like an "
            "element card or a directive, which would be dropped",
            expected="a title line above the first element",
        )
        return None
    title = title_line.text.strip().lstrip("*").strip()

    parameters: dict[str, float] = {}
    _prescan_parameters(lines, parameters, ctx)
    models: dict[str, _Model] = {}
    model_lines: dict[str, int] = {}
    elements: list[_Element] = []
    ignored: list[dict[str, Any]] = []
    in_control = False
    subcircuit_depth = 0

    for line in lines[1:]:
        raw = _strip_comment(line.text).strip()
        if not raw or raw.startswith("*"):
            continue
        tokens = _split_tokens(raw)
        if not tokens:
            continue
        keyword = tokens[0].lower()
        if in_control:
            if keyword == ".endc":
                in_control = False
            continue
        if subcircuit_depth:
            # A refused subcircuit is one unsupported construct, not one defect
            # per line inside it: report it once and stay silent until it ends.
            if keyword == ".subckt":
                subcircuit_depth += 1
            elif keyword == ".ends":
                subcircuit_depth -= 1
            continue
        if keyword == ".subckt":
            subcircuit_depth = 1
            _handle_directive(
                line.number,
                tokens,
                parameters,
                models,
                model_lines,
                ignored,
                ctx,
            )
            continue
        if keyword == ".control":
            in_control = True
            ignored.append(
                {
                    "directive": ".control",
                    "line": line.number,
                    "reason": (
                        "a control block is a run script; the compiled topology "
                        "chooses the analysis and the stored signals, so the "
                        "block's intent is not modelled"
                    ),
                }
            )
            continue
        if keyword == ".endc":
            ctx.error(
                "malformed_card",
                line.number,
                ".endc appears without an opening .control",
            )
            continue
        if keyword == ".end":
            ignored.append(
                {
                    "directive": ".end",
                    "line": line.number,
                    "reason": "the netlist ends here; later lines are not read",
                }
            )
            break
        if keyword.startswith("."):
            if keyword == ".param":
                # Already collected by _prescan_parameters, so a card may use a
                # parameter declared below it.
                continue
            _handle_directive(
                line.number,
                tokens,
                parameters,
                models,
                model_lines,
                ignored,
                ctx,
            )
            continue
        element = _parse_element(line.number, tokens, parameters, ctx)
        if element is not None:
            elements.append(element)

    if in_control:
        ctx.error(
            "malformed_card",
            lines[-1].number,
            "the .control block is never closed by .endc",
            expected="a .endc line",
        )

    if ctx.failed:
        return None

    built = _build_document(title, topology_id, elements, models, ctx)
    if built is None:
        return None

    element_kinds: dict[str, int] = {}
    for element in elements:
        element_kinds[element.kind] = element_kinds.get(element.kind, 0) + 1

    return _Analysis(
        title=title,
        document=built.document,
        ignored=ignored,
        net_name_map=built.net_name_map,
        component_name_map=built.component_name_map,
        observed_duty=built.observed_duty,
        element_kinds=element_kinds,
        netlist_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def import_spice_netlist(text: str, *, topology_id: str = "imported") -> ImportedNetlist:
    """Translate ``text`` into a descriptor document, raising on any defect."""

    ctx = _Context()
    analysis = _analyse(text, topology_id, ctx)
    issues = _finalize(ctx.issues)
    if analysis is None or any(issue.severity == "error" for issue in issues):
        raise SpiceImportError(issues)
    return ImportedNetlist(
        document=analysis.document,
        title=analysis.title,
        warnings=tuple(issue for issue in issues if issue.severity == "warning"),
        ignored_directives=tuple(analysis.ignored),
        net_name_map=MappingProxyType(dict(analysis.net_name_map)),
        component_name_map=MappingProxyType(dict(analysis.component_name_map)),
        observed_duty=MappingProxyType(dict(analysis.observed_duty)),
        element_kinds=MappingProxyType(dict(analysis.element_kinds)),
        netlist_sha256=analysis.netlist_sha256,
    )


def spice_import_document(text: str, *, topology_id: str = "imported") -> dict[str, Any]:
    """Translate ``text`` and return one self-describing envelope.

    Two answers are reported separately: ``import_ok`` says whether the netlist
    translated, and ``validation`` carries the topology validator's verdict on
    the descriptor that came out.  ``ok`` is the conjunction, so a caller that
    only wants "is this usable" has one field, and a caller that needs to know
    which layer objected has two.
    """

    ctx = _Context()
    analysis = _analyse(text, topology_id, ctx)
    issues = _finalize(ctx.issues)
    import_ok = analysis is not None and not any(
        issue.severity == "error" for issue in issues
    )
    document = analysis.document if analysis is not None else None
    validation = None
    fingerprint = None
    if document is not None:
        from .topology_validation import (
            build_descriptor_quietly,
            topology_validation_document,
        )

        validation = topology_validation_document(document)
        built = build_descriptor_quietly(document)
        fingerprint = None if built is None else built.fingerprint

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    ok = import_ok and (validation is None or validation["ok"] is True)
    return {
        "schema": SPICE_IMPORT_SCHEMA,
        "ok": ok,
        "import_ok": import_ok,
        "topology_id": topology_id,
        "title": None if analysis is None else analysis.title,
        "netlist_sha256": None if analysis is None else analysis.netlist_sha256,
        "descriptor": document,
        "descriptor_fingerprint": fingerprint,
        "counts": {
            "components": 0 if document is None else len(document["components"]),
            "nets": 0 if document is None else len(document["nets"]),
            "ignored_directives": 0 if analysis is None else len(analysis.ignored),
        },
        "element_kinds": {} if analysis is None else dict(analysis.element_kinds),
        "net_name_map": {} if analysis is None else dict(analysis.net_name_map),
        "component_name_map": (
            {} if analysis is None else dict(analysis.component_name_map)
        ),
        "observed_duty": {} if analysis is None else dict(analysis.observed_duty),
        "ignored_directives": [] if analysis is None else list(analysis.ignored),
        "issues": [issue.to_dict() for issue in issues],
        "error_count": len(errors),
        "warning_count": len(warnings),
        "validation": validation,
        "notes": [
            "The duty ratio is a compile-time command, not a descriptor field; "
            "run compile-topology with --duty to render this circuit.",
            "Ignored directives describe how a netlist is executed; the compiled "
            "topology re-derives them, so none of them is silently lost.",
            "validation reports the topology validator's verdict on the "
            "descriptor; run validate-topology on the written document for the "
            "same answer later.",
        ],
    }
