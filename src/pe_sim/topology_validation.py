"""Structured, agent-consumable validation of a circuit topology document.

``TopologyDescriptor.from_dict`` raises one flat ``ValueError`` string and
requires a fingerprint the author cannot easily compute.  This module is the
authoring gate in front of it: it reads a document that names only the schema,
the identifiers, the nets, and the components, and reports every problem as a
``TopologyIssue`` addressed by JSON Pointer, so a caller can repair the document
instead of guessing at a message.

Three rules shape the design, mirroring ``pe_sim.spec_validation``:

- Collecting issues never raises for a bad *document*.  A descriptor that is
  well formed but not a usable circuit (an isolated net, a loop of ideal
  voltage sources) is still a document, and the caller must see all of its
  problems at once rather than the first one that happened to be checked.
- The checks are declarations, not a second model.  Component kinds, port net
  kinds, and parameter domains all come from ``pe_sim.circuit_devices``, and the
  identifier rule is the pattern ``pe_sim.topology`` already enforces.
  ``tests/test_topology_validation.py`` pins that pattern so the two cannot
  drift apart.
- ``checked`` reports what actually ran.  "Clean" and "not examined" must not
  look the same, or the envelope's ``ok`` becomes a claim the checker did not
  earn.

Nothing here starts a solver, reads a schema file, or writes anything: a
document that passes is a well-formed *graph*, not an electrically correct
circuit.  Compiling it, and deciding whether the result is trustworthy, are
different questions answered elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .circuit_devices import (
    CIRCUIT_DEVICES,
    COMPILABLE_NET_KINDS,
    NET_KIND_CONTROL,
    NET_KIND_REFERENCE,
    DeviceSpec,
    canonical_device_kind,
    closest_device_kind,
    device_spec,
)
from .plant_parameters import ParameterSpec
from .topology import TOPOLOGY_SCHEMA, TopologyComponent, TopologyDescriptor, TopologyNet, TopologyPort


#: Schema tag for ``validate_topology``; a client branches on this tag rather
#: than on the module layout.
TOPOLOGY_VALIDATION_SCHEMA = "pe-sim.topology-validation.v1"

#: The closed vocabulary of topology issue codes.  A new code is a contract
#: event: a client that branches on this set must be told it changed.
TOPOLOGY_ISSUE_CODES = frozenset(
    {
        "actuator_missing",
        "control_net_multiple_drivers",
        "control_net_undriven",
        "disconnected_graph",
        "duplicate_component_id",
        "duplicate_net_id",
        "duplicate_port_id",
        "fingerprint_mismatch",
        "invalid_document",
        "invalid_parameter_value",
        "isolated_net",
        "missing_parameter",
        "missing_port",
        "missing_reference_net",
        "multiple_control_nets",
        "multiple_reference_nets",
        "no_output_load",
        "not_compilable",
        "port_net_kind_mismatch",
        "schema_violation",
        "unknown_net",
        "unknown_parameter",
        "unknown_port",
        "unsupported_device_kind",
        "unsupported_net_kind",
        "voltage_source_loop",
    }
)

#: Issue codes that are reported at ``warning`` severity.  They describe a
#: document that is well formed as a graph but is not (yet) a circuit the
#: compiler can render, which is a different answer from "this document is
#: wrong".  ``tests/test_topology_validation.py`` pins this set, so a code
#: cannot silently change severity.
WARNING_ISSUE_CODES = frozenset(
    {
        "actuator_missing",
        "multiple_control_nets",
        "no_output_load",
        "not_compilable",
    }
)

#: Component kinds that turn a control net into a commanded duty ratio.  The
#: averaged switch counts because a behavioural averaged model *is* an actuator;
#: it simply has no element card to render.
ACTUATOR_KINDS = frozenset({"voltage_controlled_switch", "averaged_switch"})

#: Gate relations a descriptor may declare in ``metadata.control``.  Mirrors
#: ``pe_sim.circuit_compile.SUPPORTED_CONTROL_MODES``; a test asserts the two
#: sets are equal, so the validator cannot bless a mode the compiler refuses
#: (or warn about one it understands).
DECLARED_CONTROL_MODES = frozenset(
    {"single_duty_complementary", "single_duty_interleaved"}
)

#: The component kind whose presence makes a circuit's output observable.  A
#: topology without one is valid but has nothing to measure.
OUTPUT_SINK_KIND = "resistor_load"

#: Check groups reported in ``checked``.  A group is listed only when its
#: preconditions held, so a caller can tell "examined and clean" from "never
#: looked at".
CHECK_GROUPS = (
    "document_shape",
    "nets",
    "components",
    "connectivity",
    "control",
    "sources",
    "fingerprint",
)

_SEVERITIES = frozenset({"error", "warning"})

#: Mirrors ``pe_sim.topology._ID_RE``.  Duplicated rather than imported because
#: this module must not depend on a private name; a test asserts the two
#: patterns are equal, so a change in either place fails the suite.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "descriptor_version",
        "topology_id",
        "components",
        "nets",
        "metadata",
        "fingerprint",
    }
)
_NET_KEYS = frozenset({"net_id", "label", "kind"})
_COMPONENT_KEYS = frozenset(
    {"component_id", "kind", "ports", "parameters", "symbol_id", "model_id"}
)
_PORT_KEYS = frozenset({"port_id", "net_id", "direction", "label"})


@dataclass(frozen=True)
class TopologyIssue:
    """One repairable finding, addressed by a JSON Pointer.

    ``expected`` answers "what shape is legal here" and ``suggestion`` answers
    "what did you probably mean".  They are separate answers, and a caller
    should prefer the suggestion only when it is present: a suggestion is a
    hint that must be safe to follow, so the producer omits it when the match
    is weak.
    """

    code: str
    path: str
    message: str
    expected: str | None = None
    suggestion: str | None = None
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.code not in TOPOLOGY_ISSUE_CODES:
            raise ValueError(
                f"unknown topology issue code {self.code!r}; "
                f"expected one of {sorted(TOPOLOGY_ISSUE_CODES)}"
            )
        if self.severity not in _SEVERITIES:
            raise ValueError(
                f"issue severity {self.severity!r} is not one of {sorted(_SEVERITIES)}"
            )
        if not self.path or not self.path.startswith("/"):
            raise ValueError(
                f"issue path {self.path!r} must be a JSON Pointer starting with '/'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "expected": self.expected,
            "suggestion": self.suggestion,
            "severity": self.severity,
        }


class TopologyValidationError(ValueError):
    """Raised when a topology document still has at least one error."""

    def __init__(self, issues: Sequence[TopologyIssue]) -> None:
        ordered = tuple(issues)
        if not ordered:
            raise ValueError("TopologyValidationError requires at least one issue")
        self.issues = ordered
        first = ordered[0]
        super().__init__(
            f"{len(ordered)} topology issue(s); first at {first.path}: {first.message}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"issues": [issue.to_dict() for issue in self.issues]}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _escape(token: Any) -> str:
    """Escape one JSON Pointer reference token (RFC 6901)."""

    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(*tokens: Any) -> str:
    return "".join("/" + _escape(token) for token in tokens)


def _sort_key(issue: TopologyIssue) -> tuple[int, str, str]:
    return (0 if issue.severity == "error" else 1, issue.path, issue.code)


def _finalize(issues: Iterable[TopologyIssue]) -> tuple[TopologyIssue, ...]:
    """Sort deterministically and drop exact duplicates.

    One defect must not appear twice under two codes: a caller that fixes the
    first report and re-runs should see the issue disappear, not change code.
    """

    seen: set[tuple[str, str]] = set()
    unique: list[TopologyIssue] = []
    for issue in issues:
        key = (issue.code, issue.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return tuple(sorted(unique, key=_sort_key))


def _closest(
    text: Any,
    pool: Iterable[str],
    *,
    minimum_length: int = 6,
    threshold: float = 0.75,
) -> str | None:
    """Return the nearest candidate, or ``None`` when the match is not safe.

    Two gates keep a suggestion honest.  A very short key is far more likely to
    be an invented field than a typo, and a weak match is not evidence of
    intent.  Suggesting a real name for either would convert junk into a live
    field, which is worse than saying nothing.
    """

    candidate_text = str(text)
    if len(candidate_text) < minimum_length:
        return None
    best: tuple[float, str] | None = None
    for candidate in sorted(pool):
        score = difflib.SequenceMatcher(
            None, candidate_text.lower(), candidate.lower()
        ).ratio()
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is None or best[0] < threshold:
        return None
    return best[1]


def _safe_identifier(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if not value or not _SAFE_ID_RE.fullmatch(value):
        return False
    return value not in {".", ".."} and ".." not in value


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _invalid_value_reason(spec: ParameterSpec, value: Any) -> str | None:
    """Return why ``value`` violates ``spec``, or ``None`` when it is legal."""

    if value is None:
        return None if spec.nullable else "a null is not an accepted value here"
    if spec.kind == "boolean":
        return None if isinstance(value, bool) else "expected a JSON boolean"
    if not _is_number(value):
        return "expected a finite JSON number"
    number = float(value)
    if spec.kind == "integer" and not number.is_integer():
        return "expected an integer"
    if spec.minimum is not None:
        if spec.exclusive_minimum and not number > spec.minimum:
            return f"expected a number greater than {spec.minimum}"
        if not spec.exclusive_minimum and not number >= spec.minimum:
            return f"expected a number at least {spec.minimum}"
    if spec.maximum is not None and not number <= spec.maximum:
        return f"expected a number at most {spec.maximum}"
    return None


@dataclass
class _ParsedComponent:
    index: int
    component_id: str
    device: DeviceSpec | None
    ports: dict[str, str] = field(default_factory=dict)


@dataclass
class _Result:
    issues: list[TopologyIssue] = field(default_factory=list)
    checked: set[str] = field(default_factory=set)
    descriptor: TopologyDescriptor | None = None

    @property
    def has_error(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect_topology_issues(document: Any) -> tuple[TopologyIssue, ...]:
    """Return every finding for ``document`` without raising on bad input."""

    return _finalize(_collect(document).issues)


def _collect(document: Any) -> _Result:
    result = _Result()

    if not isinstance(document, Mapping):
        result.issues.append(
            TopologyIssue(
                code="invalid_document",
                path="/",
                message="a topology document must be a JSON object",
                expected="a JSON object with a 'schema' field",
            )
        )
        return result

    if document.get("schema") != TOPOLOGY_SCHEMA:
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/schema",
                message=(
                    f"unsupported topology schema {document.get('schema')!r}; "
                    "the rest of the document was not examined"
                ),
                expected=f"{TOPOLOGY_SCHEMA!r}",
            )
        )
        return result

    result.checked.add("document_shape")
    _check_top_level(result, document)

    net_kind_by_id = _check_nets(result, document.get("nets"))
    parsed = _check_components(result, document.get("components"), net_kind_by_id)

    if result.checked.issuperset({"nets", "components"}) and net_kind_by_id and parsed:
        behavioural = _behavioural_nets(parsed)
        control_nets = sorted(
            net_id
            for net_id, kind in net_kind_by_id.items()
            if kind == NET_KIND_CONTROL
        )
        _check_renderability(result, net_kind_by_id, parsed)
        _check_connectivity(result, net_kind_by_id, parsed, behavioural)
        _check_control(
            result,
            net_kind_by_id,
            parsed,
            behavioural,
            *_control_declaration_state(document, control_nets),
        )
        _check_sources(result, parsed)

    _check_fingerprint(result, document)
    _build_quietly(result, document)
    return result

def _check_top_level(result: _Result, document: Mapping[str, Any]) -> None:
    for key in sorted(set(document) - _TOP_LEVEL_KEYS):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path=_pointer(key),
                message=f"unexpected top-level field {key!r}",
                expected="one of: " + ", ".join(sorted(_TOP_LEVEL_KEYS)),
                suggestion=_closest(key, _TOP_LEVEL_KEYS),
            )
        )

    topology_id = document.get("topology_id")
    if not _safe_identifier(topology_id):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/topology_id",
                message=f"topology_id {topology_id!r} is not a safe identifier",
                expected=(
                    "a non-empty string of letters, digits, underscore, dot, "
                    "colon or hyphen that is not '.' or '..' and contains no '..'"
                ),
            )
        )

    version = document.get("descriptor_version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/descriptor_version",
                message=f"descriptor_version {version!r} is not MAJOR.MINOR",
                expected="a string such as '1.0'",
            )
        )

    metadata = document.get("metadata", {})
    if not isinstance(metadata, Mapping):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/metadata",
                message="metadata must be a JSON object",
                expected="a JSON object",
            )
        )


def _check_nets(result: _Result, nets: Any) -> dict[str, str]:
    """Return ``net_id -> kind`` for every well-formed net, in document order."""

    if not isinstance(nets, list):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/nets",
                message="nets must be a JSON array",
                expected="an array of net objects",
            )
        )
        return {}
    result.checked.add("nets")

    kinds: dict[str, str] = {}
    for index, item in enumerate(nets):
        path = _pointer("nets", index)
        if not isinstance(item, Mapping):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=path,
                    message="a net must be a JSON object",
                    expected="an object with 'net_id' and 'kind'",
                )
            )
            continue
        for key in sorted(set(item) - _NET_KEYS):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("nets", index, key),
                    message=f"unexpected net field {key!r}",
                    expected="one of: " + ", ".join(sorted(_NET_KEYS)),
                    suggestion=_closest(key, _NET_KEYS),
                )
            )
        net_id = item.get("net_id")
        if not _safe_identifier(net_id):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("nets", index, "net_id"),
                    message=f"net_id {net_id!r} is not a safe identifier",
                    expected="a non-empty identifier without '..'",
                )
            )
            continue
        assert isinstance(net_id, str)
        if net_id in kinds:
            result.issues.append(
                TopologyIssue(
                    code="duplicate_net_id",
                    path=_pointer("nets", index, "net_id"),
                    message=f"net id {net_id!r} is declared more than once",
                    expected="every net_id appears exactly once",
                )
            )
            continue
        kind = item.get("kind", "electrical")
        if kind not in COMPILABLE_NET_KINDS:
            result.issues.append(
                TopologyIssue(
                    code="unsupported_net_kind",
                    path=_pointer("nets", index, "kind"),
                    message=(
                        f"net {net_id!r} declares kind {kind!r}, which has no "
                        "electrical meaning and cannot be compiled"
                    ),
                    expected="one of: " + ", ".join(sorted(COMPILABLE_NET_KINDS)),
                )
            )
            continue
        kinds[net_id] = str(kind)
    return kinds


def _check_components(
    result: _Result, components: Any, net_kind_by_id: Mapping[str, str]
) -> list[_ParsedComponent]:
    if not isinstance(components, list):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path="/components",
                message="components must be a JSON array",
                expected="an array of component objects",
            )
        )
        return []
    result.checked.add("components")

    parsed: list[_ParsedComponent] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(components):
        path = _pointer("components", index)
        if not isinstance(item, Mapping):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=path,
                    message="a component must be a JSON object",
                    expected="an object with 'component_id', 'kind' and 'ports'",
                )
            )
            continue
        for key in sorted(set(item) - _COMPONENT_KEYS):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("components", index, key),
                    message=f"unexpected component field {key!r}",
                    expected="one of: " + ", ".join(sorted(_COMPONENT_KEYS)),
                    suggestion=_closest(key, _COMPONENT_KEYS),
                )
            )

        component_id = item.get("component_id")
        if not _safe_identifier(component_id):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("components", index, "component_id"),
                    message=f"component_id {component_id!r} is not a safe identifier",
                    expected="a non-empty identifier without '..'",
                )
            )
            continue
        assert isinstance(component_id, str)
        if component_id in seen_ids:
            result.issues.append(
                TopologyIssue(
                    code="duplicate_component_id",
                    path=_pointer("components", index, "component_id"),
                    message=f"component id {component_id!r} is declared more than once",
                    expected="every component_id appears exactly once",
                )
            )
            continue
        seen_ids.add(component_id)

        raw_kind = item.get("kind")
        canonical = canonical_device_kind(raw_kind) if isinstance(raw_kind, str) else None
        device: DeviceSpec | None = None
        if canonical is None:
            result.issues.append(
                TopologyIssue(
                    code="unsupported_device_kind",
                    path=_pointer("components", index, "kind"),
                    message=f"component {component_id!r} declares unknown kind {raw_kind!r}",
                    expected="one of: " + ", ".join(sorted(CIRCUIT_DEVICES)),
                    suggestion=(
                        closest_device_kind(raw_kind) if isinstance(raw_kind, str) else None
                    ),
                )
            )
        else:
            device = device_spec(canonical)

        ports = _check_ports(result, index, component_id, item.get("ports"), device, net_kind_by_id)
        if device is not None:
            _check_component_parameters(result, index, component_id, item.get("parameters", {}), device)

        parsed.append(_ParsedComponent(index, component_id, device, ports))
    return parsed


def _check_ports(
    result: _Result,
    index: int,
    component_id: str,
    ports: Any,
    device: DeviceSpec | None,
    net_kind_by_id: Mapping[str, str],
) -> dict[str, str]:
    """Return ``port_id -> net_id`` for the ports that could be read."""

    bound: dict[str, str] = {}
    if not isinstance(ports, list):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path=_pointer("components", index, "ports"),
                message=f"component {component_id!r} must declare a ports array",
                expected="an array of port objects",
            )
        )
        return bound

    seen: set[str] = set()
    unknown_port_reported = False
    for port_index, item in enumerate(ports):
        path = _pointer("components", index, "ports", port_index)
        if not isinstance(item, Mapping):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=path,
                    message="a port must be a JSON object",
                    expected="an object with 'port_id' and 'net_id'",
                )
            )
            continue
        for key in sorted(set(item) - _PORT_KEYS):
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("components", index, "ports", port_index, key),
                    message=f"unexpected port field {key!r}",
                    expected="one of: " + ", ".join(sorted(_PORT_KEYS)),
                    suggestion=_closest(key, _PORT_KEYS),
                )
            )
        port_id = item.get("port_id")
        net_id = item.get("net_id")
        if not isinstance(port_id, str) or not port_id:
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path=_pointer("components", index, "ports", port_index, "port_id"),
                    message="a port must declare a non-empty port_id",
                    expected="a non-empty string",
                )
            )
            continue
        if port_id in seen:
            result.issues.append(
                TopologyIssue(
                    code="duplicate_port_id",
                    path=_pointer("components", index, "ports", port_index, "port_id"),
                    message=(
                        f"component {component_id!r} declares port {port_id!r} more than once"
                    ),
                    expected="every port_id is unique inside its component",
                )
            )
            continue
        seen.add(port_id)

        if device is not None and device.port(port_id) is None:
            unknown_port_reported = True
            result.issues.append(
                TopologyIssue(
                    code="unknown_port",
                    path=_pointer("components", index, "ports", port_index, "port_id"),
                    message=(
                        f"device kind {device.kind!r} has no port {port_id!r}"
                    ),
                    expected="one of: " + ", ".join(device.port_ids),
                    suggestion=_closest(port_id, device.port_ids, minimum_length=4),
                )
            )
            continue

        if not isinstance(net_id, str) or net_id not in net_kind_by_id:
            result.issues.append(
                TopologyIssue(
                    code="unknown_net",
                    path=_pointer("components", index, "ports", port_index, "net_id"),
                    message=(
                        f"port {component_id}.{port_id} binds undeclared net {net_id!r}"
                    ),
                    expected="one of the declared net ids",
                    suggestion=(
                        _closest(net_id, net_kind_by_id) if isinstance(net_id, str) else None
                    ),
                )
            )
            continue

        if device is not None:
            spec = device.port(port_id)
            assert spec is not None
            actual_kind = net_kind_by_id[net_id]
            if actual_kind not in spec.net_kinds:
                result.issues.append(
                    TopologyIssue(
                        code="port_net_kind_mismatch",
                        path=_pointer("components", index, "ports", port_index, "net_id"),
                        message=(
                            f"port {component_id}.{port_id} binds {actual_kind!r} net "
                            f"{net_id!r}, but that port accepts only "
                            f"{', '.join(sorted(spec.net_kinds))}"
                        ),
                        expected="a net of kind: " + ", ".join(sorted(spec.net_kinds)),
                    )
                )
                continue
        bound[port_id] = net_id

    if device is not None and not unknown_port_reported:
        missing = [port_id for port_id in device.port_ids if port_id not in seen]
        if missing:
            result.issues.append(
                TopologyIssue(
                    code="missing_port",
                    path=_pointer("components", index, "ports"),
                    message=(
                        f"component {component_id!r} of kind {device.kind!r} does not "
                        f"declare required port(s): {', '.join(missing)}"
                    ),
                    expected="ports: " + ", ".join(device.port_ids),
                )
            )
    return bound


def _check_component_parameters(
    result: _Result,
    index: int,
    component_id: str,
    parameters: Any,
    device: DeviceSpec,
) -> None:
    if not isinstance(parameters, Mapping):
        result.issues.append(
            TopologyIssue(
                code="schema_violation",
                path=_pointer("components", index, "parameters"),
                message=f"component {component_id!r} parameters must be a JSON object",
                expected="a JSON object of declared parameters",
            )
        )
        return

    accepted = device.accepted_parameter_keys
    for key in sorted(parameters):
        spec = device.parameter(key)
        if key not in accepted:
            result.issues.append(
                TopologyIssue(
                    code="unknown_parameter",
                    path=_pointer("components", index, "parameters", key),
                    message=(
                        f"device kind {device.kind!r} has no parameter {key!r}"
                    ),
                    expected="one of: " + ", ".join(sorted(accepted)),
                    suggestion=_closest(key, accepted),
                )
            )
            continue
        if spec is None:
            # A declared annotation such as ``phase_index`` carries no domain.
            continue
        reason = _invalid_value_reason(spec, parameters[key])
        if reason is not None:
            result.issues.append(
                TopologyIssue(
                    code="invalid_parameter_value",
                    path=_pointer("components", index, "parameters", key),
                    message=f"parameter {key!r} of {component_id!r} is invalid: {reason}",
                    expected=_expected_domain(spec),
                )
            )

    missing = [
        name for name in device.required_parameters if name not in parameters
    ]
    if missing:
        result.issues.append(
            TopologyIssue(
                code="missing_parameter",
                path=_pointer("components", index, "parameters"),
                message=(
                    f"component {component_id!r} of kind {device.kind!r} is missing "
                    f"required parameter(s): {', '.join(missing)}"
                ),
                expected=(
                    "no default exists for these, so the compiler will not guess: "
                    + ", ".join(missing)
                ),
            )
        )


def _expected_domain(spec: ParameterSpec) -> str:
    if spec.kind == "boolean":
        return "a JSON boolean"
    parts = ["a finite number"]
    if spec.kind == "integer":
        parts = ["an integer"]
    if spec.minimum is not None:
        relation = "greater than" if spec.exclusive_minimum else "at least"
        parts.append(f"{relation} {spec.minimum}")
    if spec.maximum is not None:
        parts.append(f"at most {spec.maximum}")
    if spec.nullable:
        parts.append("or null")
    described = ", ".join(parts)
    if spec.unit is not None:
        described += f" (unit: {spec.unit})"
    return described


def _behavioural_nets(parsed: Sequence[_ParsedComponent]) -> frozenset[str]:
    """Nets whose every bound port belongs to a device that cannot be rendered.

    An averaged behavioural block owns its control net: the duty ratio is an
    input of the block, not a node another element drives.  Treating such a net
    like a gate-drive net would report a shipped, reviewed averaged reference as
    broken.
    """

    devices_by_net: dict[str, list[DeviceSpec | None]] = {}
    for component in parsed:
        for net_id in component.ports.values():
            devices_by_net.setdefault(net_id, []).append(component.device)
    behavioural: set[str] = set()
    for net_id, devices in devices_by_net.items():
        if devices and all(
            device is not None and not device.renderable for device in devices
        ):
            behavioural.add(net_id)
    return frozenset(behavioural)


def _check_renderability(
    result: _Result,
    net_kind_by_id: Mapping[str, str],
    parsed: Sequence[_ParsedComponent],
) -> None:
    """Report a document that is a valid descriptor but not a compilable circuit."""

    blocked = sorted(
        {
            component.device.kind
            for component in parsed
            if component.device is not None and not component.device.renderable
        }
    )
    if blocked:
        result.issues.append(
            TopologyIssue(
                code="not_compilable",
                path="/components",
                message=(
                    "this descriptor declares behavioural block(s) "
                    f"({', '.join(blocked)}) that have no element card, so it is a "
                    "display descriptor rather than a circuit a compiler can render"
                ),
                expected=(
                    "only renderable device kinds when the descriptor is meant to be "
                    "compiled; run 'pe-sim describe-devices' for the renderable set"
                ),
                severity="warning",
            )
        )

    kinds = {component.device.kind for component in parsed if component.device}
    if OUTPUT_SINK_KIND not in kinds:
        result.issues.append(
            TopologyIssue(
                code="no_output_load",
                path="/components",
                message=(
                    "the topology declares no load, so the circuit has no output "
                    "sink and nothing meaningful to observe"
                ),
                expected=f"at least one component of kind {OUTPUT_SINK_KIND!r}",
                severity="warning",
            )
        )


def _check_connectivity(
    result: _Result,
    net_kind_by_id: Mapping[str, str],
    parsed: Sequence[_ParsedComponent],
    behavioural: frozenset[str],
) -> None:
    result.checked.add("connectivity")

    bound_counts: dict[str, int] = {net_id: 0 for net_id in net_kind_by_id}
    for component in parsed:
        for net_id in component.ports.values():
            bound_counts[net_id] = bound_counts.get(net_id, 0) + 1

    for position, net_id in enumerate(net_kind_by_id):
        if net_id in behavioural:
            continue
        if net_kind_by_id[net_id] == NET_KIND_CONTROL:
            # A control net always gets a more specific diagnosis: either it is
            # driven (and fine) or ``control_net_undriven`` names the real
            # problem.  Reporting it as a dangling terminal as well would make
            # one defect look like two.
            continue
        count = bound_counts.get(net_id, 0)
        if count >= 2:
            continue
        result.issues.append(
            TopologyIssue(
                code="isolated_net",
                path=_pointer("nets", position, "net_id"),
                message=(
                    f"net {net_id!r} is declared but bound by no port, so it defines "
                    "nothing"
                    if count == 0
                    else f"net {net_id!r} is bound by exactly one port, so it is a "
                    "dangling terminal rather than a connection"
                ),
                expected="every declared net is bound by at least two ports",
            )
        )

    reference_nets = [
        net_id for net_id, kind in net_kind_by_id.items() if kind == NET_KIND_REFERENCE
    ]
    if not reference_nets:
        result.issues.append(
            TopologyIssue(
                code="missing_reference_net",
                path="/nets",
                message="the topology declares no reference net",
                expected=f"exactly one net of kind {NET_KIND_REFERENCE!r}",
            )
        )
    elif len(reference_nets) > 1:
        positions = list(net_kind_by_id)
        for net_id in reference_nets[1:]:
            result.issues.append(
                TopologyIssue(
                    code="multiple_reference_nets",
                    path=_pointer("nets", positions.index(net_id), "kind"),
                    message=(
                        f"net {net_id!r} is an extra reference net; a circuit has one "
                        "reference node, and a second one would silently split the "
                        "return path"
                    ),
                    expected=f"exactly one net of kind {NET_KIND_REFERENCE!r}",
                )
            )

    islands = _disconnected_islands(parsed)
    if islands:
        described = "; ".join(", ".join(sorted(island)) for island in islands)
        result.issues.append(
            TopologyIssue(
                code="disconnected_graph",
                path="/nets",
                message=(
                    "the circuit is not connected: these nets form separate islands: "
                    f"{described}"
                ),
                expected="one connected component over every component and net",
            )
        )


def _disconnected_islands(parsed: Sequence[_ParsedComponent]) -> list[list[str]]:
    """Return the net ids of every connected component except the largest.

    The graph is bipartite (components and nets are both nodes); a circuit is
    connected exactly when that graph is.  Nets with no binding never appear
    here because they are already reported as isolated.
    """

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def node(kind: str, name: str) -> tuple[str, str]:
        return (kind, name)

    def link(left: tuple[str, str], right: tuple[str, str]) -> None:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    for component in parsed:
        component_node = node("c", component.component_id)
        for net_id in component.ports.values():
            link(component_node, node("n", net_id))
    if not adjacency:
        return []

    seen: set[tuple[str, str]] = set()
    groups: list[list[tuple[str, str]]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        group: list[tuple[str, str]] = []
        seen.add(start)
        while stack:
            current = stack.pop()
            group.append(current)
            for neighbour in sorted(adjacency[current]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        groups.append(group)
    if len(groups) <= 1:
        return []

    groups.sort(key=lambda group: (-len(group), sorted(group)))
    islands: list[list[str]] = []
    for group in groups[1:]:
        nets = sorted(name for kind, name in group if kind == "n")
        if nets:
            islands.append(nets)
    return islands


def _control_declaration_state(
    document: Mapping[str, Any], control_nets: Sequence[str]
) -> tuple[str | None, str | None]:
    """Return ``(effective_mode, declared_mode)``.

    ``effective_mode`` is the declared relation when it fits this graph and
    ``None`` otherwise.  ``declared_mode`` is what the document says, so a
    declaration that does not fit can be named in the warning instead of
    silently reverting to "no relation declared".

    Only the declaration's *shape* is checked here.  Whether the gates can be
    rendered is the compiler's question, but a declaration that contradicts the
    graph must not silence the ``multiple_control_nets`` warning: a document
    saying ``{"mode": "single_duty_interleaved", "phases": 4}`` while declaring
    three control nets is not "checked and clean", it is unchecked, and the
    author would only learn that at compile time.
    """

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return None, None
    declaration = metadata.get("control")
    if not isinstance(declaration, Mapping):
        return None, None
    mode = declaration.get("mode")
    if not isinstance(mode, str) or mode not in DECLARED_CONTROL_MODES:
        return None, mode if isinstance(mode, str) else None
    if mode == "single_duty_interleaved":
        phases = declaration.get("phases")
        if isinstance(phases, bool) or not isinstance(phases, int):
            return None, mode
        if phases < 2 or phases != len(control_nets):
            return None, mode
    return mode, mode


def _check_control(
    result: _Result,
    net_kind_by_id: Mapping[str, str],
    parsed: Sequence[_ParsedComponent],
    behavioural: frozenset[str],
    control_mode: str | None = None,
    declared_mode: str | None = None,
) -> None:
    result.checked.add("control")

    actuators = [
        component
        for component in parsed
        if component.device is not None and component.device.kind in ACTUATOR_KINDS
    ]
    if not actuators:
        result.issues.append(
            TopologyIssue(
                code="actuator_missing",
                path="/components",
                message=(
                    "the topology declares no actuator, so a commanded duty ratio has "
                    "nothing to act on"
                ),
                expected=(
                    "at least one component of kind "
                    + " or ".join(repr(kind) for kind in sorted(ACTUATOR_KINDS))
                ),
                severity="warning",
            )
        )

    control_nets = [
        net_id for net_id, kind in net_kind_by_id.items() if kind == NET_KIND_CONTROL
    ]
    if len(control_nets) > 1 and control_mode not in DECLARED_CONTROL_MODES:
        # A declaration that exists but does not fit the graph is a different
        # answer from "nothing was declared", and the author needs to hear the
        # difference: the second one is a missing sentence, the first one is a
        # sentence that contradicts the drawing.
        unfit = (
            f"The document declares metadata.control.mode = {declared_mode!r}, but "
            "that declaration does not fit this graph (for "
            "'single_duty_interleaved', 'phases' must be an integer of at least 2 "
            "equal to the number of control nets), so it is not counted as a "
            "stated relation.  "
            if declared_mode is not None and declared_mode in DECLARED_CONTROL_MODES
            else ""
        )
        result.issues.append(
            TopologyIssue(
                code="multiple_control_nets",
                path="/nets",
                message=(
                    "the topology declares "
                    f"{len(control_nets)} control nets ({', '.join(control_nets)}); the "
                    "kernel commands one dimensionless duty ratio and the current "
                    "compiler renders exactly one control net, so the extra nets are "
                    "a declaration the compiler cannot honour yet.  "
                    + unfit
                ).strip(),
                expected=(
                    f"exactly one net of kind {NET_KIND_CONTROL!r}, or none when the "
                    "descriptor is display-only, or metadata.control declaring how "
                    "the nets derive from one duty "
                    f"({', '.join(sorted(DECLARED_CONTROL_MODES))})"
                ),
                severity="warning",
            )
        )
    if not control_nets:
        return

    for control_net in control_nets:
        if control_net in behavioural:
            continue
        position = list(net_kind_by_id).index(control_net)
        drivers = [
            component
            for component in parsed
            if component.device is not None
            and _drives(component, control_net)
        ]
        if not drivers:
            result.issues.append(
                TopologyIssue(
                    code="control_net_undriven",
                    path=_pointer("nets", position, "net_id"),
                    message=f"control net {control_net!r} has no driving source",
                    expected="exactly one source whose output port binds the control net",
                )
            )
        elif len(drivers) > 1:
            result.issues.append(
                TopologyIssue(
                    code="control_net_multiple_drivers",
                    path=_pointer("nets", position, "net_id"),
                    message=(
                        f"control net {control_net!r} is driven by "
                        f"{len(drivers)} sources ({', '.join(c.component_id for c in drivers)}); "
                        "the voltage on the net would be the sum of two carriers"
                    ),
                    expected="exactly one source whose output port binds the control net",
                )
            )


def _drives(component: _ParsedComponent, net_id: str) -> bool:
    assert component.device is not None
    for port_id, bound in component.ports.items():
        spec = component.device.port(port_id)
        if spec is not None and spec.direction == "output" and bound == net_id:
            return True
    return False


def _check_sources(result: _Result, parsed: Sequence[_ParsedComponent]) -> None:
    result.checked.add("sources")

    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> bool:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return False
        parent[right_root] = left_root
        return True

    for component in parsed:
        if component.device is None or component.device.kind != "voltage_source":
            continue
        positive = component.ports.get("positive")
        negative = component.ports.get("negative")
        if positive is None or negative is None:
            continue
        if not union(positive, negative):
            result.issues.append(
                TopologyIssue(
                    code="voltage_source_loop",
                    path=_pointer("components", component.index),
                    message=(
                        f"ideal voltage source {component.component_id!r} closes a loop "
                        "through other ideal voltage sources; that loop fixes the node "
                        "voltages twice and is a short circuit in the model"
                    ),
                    expected="no cycle of ideal voltage sources",
                )
            )
            return


def _check_fingerprint(result: _Result, document: Mapping[str, Any]) -> None:
    if "fingerprint" not in document:
        return
    if result.has_error:
        # Editing a recorded descriptor both breaks its fingerprint and creates
        # the real defect the author is looking at.  Reporting the mismatch
        # alongside would double-count one edit; it reappears the moment the
        # document is otherwise clean, which is when it actually matters.
        return
    declared = document.get("fingerprint")
    descriptor = _build_descriptor_quiet(document)
    if descriptor is None:
        # The document could not be built, so there is nothing to compare
        # against; reporting a mismatch would be a guess.
        return
    result.checked.add("fingerprint")
    if not isinstance(declared, str) or not _FINGERPRINT_RE.fullmatch(declared):
        result.issues.append(
            TopologyIssue(
                code="fingerprint_mismatch",
                path="/fingerprint",
                message=f"fingerprint {declared!r} is not a lowercase SHA-256 hex digest",
                expected=descriptor.fingerprint,
            )
        )
        return
    if declared != descriptor.fingerprint:
        result.issues.append(
            TopologyIssue(
                code="fingerprint_mismatch",
                path="/fingerprint",
                message="the declared fingerprint does not match the document contents",
                expected=descriptor.fingerprint,
            )
        )


def _build_quietly(result: _Result, document: Mapping[str, Any]) -> None:
    """Attach the built descriptor, or explain why it could not be built.

    The explanation is only emitted when nothing else was found.  Otherwise the
    constructor's message would repeat a problem that already has a precise
    path and a specific code, and a caller would see one defect twice.
    """

    try:
        result.descriptor = _build_descriptor_quiet(document)
    except ValueError as exc:
        if not result.has_error:
            result.issues.append(
                TopologyIssue(
                    code="schema_violation",
                    path="/",
                    message=str(exc),
                    expected="a document the descriptor contract accepts",
                )
            )


def _build_descriptor_quiet(document: Mapping[str, Any]) -> TopologyDescriptor | None:
    try:
        return _construct(document)
    except (ValueError, TypeError, AttributeError):
        return None


def _construct(document: Mapping[str, Any]) -> TopologyDescriptor:
    """Build a descriptor, tolerating a document that omits ``fingerprint``.

    ``TopologyDescriptor.from_dict`` requires a fingerprint because a *recorded*
    descriptor must prove it was not edited.  An *authored* descriptor has no
    such history yet, and making an agent implement SHA-256 over canonical JSON
    would be a trap rather than a gate.  This builder therefore accepts either
    shape; ``collect_topology_issues`` verifies the fingerprint when one is
    present.  ``tests/test_topology_validation.py`` asserts this builder and
    ``from_dict`` agree whenever both apply.
    """

    components: list[TopologyComponent] = []
    for item in document.get("components", ()):
        ports = tuple(
            TopologyPort(
                str(port.get("port_id", "")),
                str(port.get("net_id", "")),
                str(port.get("direction", "passive")),
                port.get("label"),
            )
            for port in item.get("ports", ())
        )
        components.append(
            TopologyComponent(
                str(item.get("component_id", "")),
                str(item.get("kind", "")),
                ports,
                parameters=dict(item.get("parameters", {})),
                symbol_id=item.get("symbol_id"),
                model_id=item.get("model_id"),
            )
        )
    nets = tuple(
        TopologyNet(
            str(net.get("net_id", "")),
            net.get("label"),
            str(net.get("kind", "electrical")),
        )
        for net in document.get("nets", ())
    )
    return TopologyDescriptor(
        str(document.get("topology_id", "")),
        tuple(components),
        nets,
        descriptor_version=str(document.get("descriptor_version", "1.0")),
        metadata=dict(document.get("metadata", {})),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_descriptor(document: Any) -> TopologyDescriptor:
    """Build the descriptor for ``document``; the fingerprint is optional."""

    if not isinstance(document, Mapping):
        raise TopologyValidationError(
            [
                TopologyIssue(
                    code="invalid_document",
                    path="/",
                    message="a topology document must be a JSON object",
                    expected="a JSON object with a 'schema' field",
                )
            ]
        )
    try:
        return _construct(document)
    except (ValueError, TypeError, AttributeError) as exc:
        raise TopologyValidationError(
            [
                TopologyIssue(
                    code="schema_violation",
                    path="/",
                    message=str(exc),
                    expected="a document the descriptor contract accepts",
                )
            ]
        ) from exc


def topology_validation_document(
    document: Any,
    *,
    issues: Sequence[TopologyIssue] | None = None,
    topology_id: str | None = None,
    result: _Result | None = None,
) -> dict[str, Any]:
    """Return the ``pe-sim.topology-validation.v1`` envelope for one document."""

    collected = result if result is not None else _collect(document)
    found = tuple(issues) if issues is not None else _finalize(collected.issues)
    declared = document.get("fingerprint") if isinstance(document, Mapping) else None
    if "document_shape" in collected.checked:
        descriptor = (
            collected.descriptor
            if collected.descriptor is not None
            else build_descriptor_quietly(document)
        )
    else:
        # A document this module did not even recognise has no fingerprint it
        # could legitimately claim, so reporting one would be an invention.
        descriptor = None
    error_count = sum(1 for issue in found if issue.severity == "error")
    warning_count = sum(1 for issue in found if issue.severity == "warning")
    identifier = topology_id
    if identifier is None and isinstance(document, Mapping):
        raw = document.get("topology_id")
        identifier = raw if isinstance(raw, str) else None
    return {
        "schema": TOPOLOGY_VALIDATION_SCHEMA,
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "topology_id": identifier,
        "declared_fingerprint": declared if isinstance(declared, str) else None,
        "computed_fingerprint": None if descriptor is None else descriptor.fingerprint,
        "checked": sorted(collected.checked),
        "issues": [issue.to_dict() for issue in found],
    }


def build_descriptor_quietly(document: Any) -> TopologyDescriptor | None:
    """Return the built descriptor, or ``None`` when it cannot be built."""

    if not isinstance(document, Mapping):
        return None
    return _build_descriptor_quiet(document)


def validate_topology(document: Any) -> dict[str, Any]:
    """Validate one topology document and return its envelope.

    Raises ``TopologyValidationError`` when at least one error remains, so a
    caller cannot mistake a rejected document for an accepted one by ignoring
    the return value.
    """

    collected = _collect(document)
    found = _finalize(collected.issues)
    if any(issue.severity == "error" for issue in found):
        raise TopologyValidationError(found)
    return topology_validation_document(document, issues=found, result=collected)
