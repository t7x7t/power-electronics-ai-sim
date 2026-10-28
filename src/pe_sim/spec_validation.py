"""Structured, agent-consumable validation of an experiment document.

The historical entry point (``contracts.validate_experiment_document``) raises
one flat ``ValueError`` string.  An agent cannot map that string back to a
field, cannot branch on it, and cannot tell a typo from a contract mismatch.
This module turns the same checks into ``SpecIssue`` records addressed by JSON
Pointer, so a caller can repair a document instead of guessing.

Two rules shape the design:

- Collecting issues never raises for a bad *document*.  Every problem found is
  reported, not just the first, because an agent that fixes one field per turn
  is slower and no more careful than one that fixes all of them at once.
- The checks are declarations, not a second model.  Parameter shapes come from
  ``plant_parameters`` (the same table ``tests/test_plant_parameters.py``
  pins to the factories) and the schedule vocabulary comes from the plant
  registration, so this module cannot drift into a private opinion about what
  a plant accepts.

Runtime configuration failures (a missing ``jsonschema`` package or a schema
file that cannot be read) still raise: they are defects of the installation,
not of the document, and must not be reported as user-fixable issues.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import plant_registry
from .plant_parameters import PLANT_PARAMETERS, ParameterSpec


#: Schema tag for the envelope ``validation_document`` returns.
SPEC_VALIDATION_SCHEMA = "pe-sim.spec-validation.v1"

#: The closed vocabulary of issue codes.  A new code is a contract event, so
#: ``SpecIssue`` refuses one that is not declared here.  A code is admitted only
#: once something emits it: an unused entry would let a client write a branch
#: that can never run.
SPEC_ISSUE_CODES = frozenset(
    {
        "invalid_document",
        "schema_violation",
        "unknown_plant",
        "unknown_parameter",
        "invalid_parameter_value",
        "unknown_schedule_key",
        "exploratory_contract_default",
    }
)

#: The stages ``collect_issues`` visits, in the order it visits them.  The list
#: is published so a consumer can tell "checked and clean" from "not checked".
CHECKED_STAGES: tuple[str, ...] = (
    "schema",
    "plant_id",
    "plant_config",
    "input_schedule",
    "contracts",
)

_SEVERITIES = frozenset({"error", "warning"})

#: An all-zero contract hash is the documented exploratory default.  It is not
#: a defect; it only caps what the run may be used to claim.
_ZERO_HASH = "0" * 64
_CONTRACT_NAMES = ("safety", "qualification")


@dataclass(frozen=True)
class SpecIssue:
    """One localized problem in an experiment document.

    ``path`` is a JSON Pointer so a caller can navigate the document without
    parsing prose.  ``expected`` and ``suggestion`` are optional because "what
    the field should look like" and "what you probably meant" are different
    answers; an agent should prefer ``suggestion`` when both are present.
    """

    code: str
    path: str
    message: str
    expected: str | None = None
    suggestion: str | None = None
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.code not in SPEC_ISSUE_CODES:
            raise ValueError(
                f"unknown spec issue code {self.code!r}; "
                f"expected one of {sorted(SPEC_ISSUE_CODES)}"
            )
        if self.severity not in _SEVERITIES:
            raise ValueError(
                f"issue severity {self.severity!r} is not one of {sorted(_SEVERITIES)}"
            )
        if self.path and not self.path.startswith("/"):
            raise ValueError(
                f"issue path {self.path!r} must be a JSON Pointer (starting with '/')"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe record; ``severity`` is always present."""

        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "expected": self.expected,
            "suggestion": self.suggestion,
            "severity": self.severity,
        }


class SpecValidationError(ValueError):
    """Raised by ``validate_document`` when at least one error remains.

    The exception carries every issue, not only the first, so a caller that
    catches the error still sees the complete repair list.
    """

    def __init__(self, issues: Sequence[SpecIssue]) -> None:
        ordered = tuple(issues)
        if not ordered:
            raise ValueError("SpecValidationError requires at least one issue")
        self.issues = ordered
        first = ordered[0]
        location = first.path or "<root>"
        # The one-line summary is what a non-structured caller (a log reader)
        # sees, so it must name the first location and the total count.
        super().__init__(f"{len(ordered)} spec issue(s); first at {location}: {first.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"issues": [issue.to_dict() for issue in self.issues]}


def _sort_key(issue: SpecIssue) -> tuple[int, str]:
    # Errors first, then by document position: a reader repairs top-down and
    # an agent diffs two runs of the same document without spurious reordering.
    return (0 if issue.severity == "error" else 1, issue.path)


def _escape(token: str) -> str:
    """Escape one JSON Pointer reference token (RFC 6901)."""

    return token.replace("~", "~0").replace("/", "~1")


def _pointer(parts: Iterable[Any]) -> str:
    return "".join(f"/{_escape(str(part))}" for part in parts)


def _render_number(value: float) -> str:
    return str(value) if isinstance(value, int) else f"{value:g}"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return f"boolean {str(value).lower()}"
    if isinstance(value, str):
        return f"string {value!r}"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__} {value!r}"
    return type(value).__name__


def _expected_text(spec: ParameterSpec) -> str:
    if spec.kind == "boolean":
        return "boolean"
    text = spec.kind
    if spec.minimum is not None:
        comparator = ">" if spec.exclusive_minimum else ">="
        text += f" {comparator} {_render_number(spec.minimum)}"
    if spec.maximum is not None:
        text += f" and <= {_render_number(spec.maximum)}"
    if spec.nullable:
        text += " (or null)"
    return text


def _value_is_invalid(spec: ParameterSpec, value: Any) -> bool:
    if value is None:
        return not spec.nullable
    if spec.kind == "boolean":
        return not isinstance(value, bool)
    # ``bool`` is an ``int`` subclass, so ``True`` would otherwise pass an
    # integer check; a JSON boolean is never a parameter value.
    if isinstance(value, bool):
        return True
    if spec.kind == "integer":
        if isinstance(value, float):
            # Match JSON Schema, which treats 4.0 as an integer instance.
            if not value.is_integer():
                return True
        elif not isinstance(value, int):
            return True
    elif not isinstance(value, (int, float)):
        return True
    try:
        if spec.minimum is not None:
            if spec.exclusive_minimum:
                if not value > spec.minimum:
                    return True
            elif not value >= spec.minimum:
                return True
        if spec.maximum is not None and not value <= spec.maximum:
            return True
    except TypeError:  # pragma: no cover - guarded by the type checks above
        return True
    return False


def _closest(candidate: Any, allowed: Sequence[str]) -> str | None:
    """Suggest the nearest declared name for one unknown field.

    ``difflib`` compares whole strings, which is the right metric for a
    misspelling such as ``buckk``.  It cannot resolve a short alias such as
    ``vin``: three characters against ``input_voltage_v`` never clear the
    default 0.6 cutoff, and an uncapped whole-string comparison picks
    ``inductance_h`` because it happens to share two letters.  The second pass
    therefore also compares the candidate with each underscore-separated word
    of a declared name, which is where an abbreviation actually comes from, and
    ignores words shorter than three characters so a unit suffix such as ``f``
    cannot attract arbitrary input.
    """

    text = str(candidate)
    names = sorted(allowed)
    if text in names:
        return text
    matches = difflib.get_close_matches(text, names, n=1)
    if matches:
        return matches[0]
    best_name: str | None = None
    best_score = 0.0
    for name in names:
        score = difflib.SequenceMatcher(None, text, name.lower()).ratio()
        for word in name.split("_"):
            if len(word) < 3:
                continue
            score = max(score, difflib.SequenceMatcher(None, text, word).ratio())
        if score > best_score:
            best_name, best_score = name, score
    return best_name if best_score >= 0.5 else None


def _load_schema(schema_path: str | Path | None) -> Mapping[str, Any]:
    """Load the same contract ``contracts`` validates against.

    The packaged copy is the default so an installed wheel does not depend on
    the repository checkout layout; ``tests/test_packaging.py`` keeps the
    repository-level copy byte-identical.
    """

    if schema_path is not None:
        path = Path(schema_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"experiment schema could not be loaded: {path}") from exc
    else:
        try:
            text = (
                resources.files("pe_sim.schemas")
                .joinpath("experiment.schema.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, ModuleNotFoundError) as exc:
            raise RuntimeError("packaged experiment schema could not be loaded") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("experiment schema is not valid JSON") from exc


def _expected_from_error(error: Any) -> str:
    value = error.validator_value
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    return f"{error.validator}: {rendered}"


def _schema_issues(
    document: Mapping[str, Any], schema_path: str | Path | None
) -> list[SpecIssue]:
    try:
        # Lazy on purpose: the runtime package itself has no mandatory
        # third-party dependency, and importing the document checker must stay
        # cheap for callers that never validate.
        import jsonschema
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise RuntimeError(
            "experiment schema validation requires the 'jsonschema' package"
        ) from exc
    validator = jsonschema.Draft202012Validator(_load_schema(schema_path))
    issues = [
        SpecIssue(
            code="schema_violation",
            path=_pointer(error.absolute_path),
            message=error.message,
            expected=_expected_from_error(error),
        )
        for error in validator.iter_errors(document)
    ]
    # ``iter_errors`` order is an implementation detail; sorting by pointer
    # makes repeated runs byte-identical for a caller that diffs them.
    issues.sort(key=lambda issue: issue.path)
    return issues


def _unknown_plant_issue(document: Mapping[str, Any]) -> SpecIssue:
    declared = document.get("plant_id")
    # The declared table covers the shipped plants; the registry adds anything a
    # distribution registered, because discovery has already run by the time
    # this issue is produced for an unresolvable id.
    known = sorted(set(PLANT_PARAMETERS) | set(plant_registry.plant_ids()))
    if isinstance(declared, str):
        message = f"plant_id {declared!r} is not a declared plant"
        suggestion = _closest(declared, known)
    else:
        message = f"plant_id must name a declared plant, got {_render_value(declared)}"
        suggestion = None
    return SpecIssue(
        code="unknown_plant",
        path="/plant_id",
        message=message,
        expected="one of: " + ", ".join(known),
        suggestion=suggestion,
    )


def _plant_config_key_issues(
    plant_id: str,
    allowed: Sequence[str],
    plant_config: Any,
) -> list[SpecIssue]:
    """Report ``plant_config`` keys the plant does not declare.

    ``allowed`` is passed in rather than read from ``PLANT_PARAMETERS`` so the
    same message shape serves a plugin plant that publishes a JSON Schema
    instead of a ``ParameterSpec`` table: one class of mistake should not need
    two vocabularies.
    """

    if not allowed or not isinstance(plant_config, Mapping):
        return []
    ordered = sorted(allowed)
    known = set(ordered)
    expected = "one of: " + ", ".join(ordered)
    issues: list[SpecIssue] = []
    for key in plant_config:
        if key in known:
            continue
        issues.append(
            SpecIssue(
                code="unknown_parameter",
                path=f"/plant_config/{_escape(str(key))}",
                message=f"plant {plant_id!r} does not accept plant_config field {key!r}",
                expected=expected,
                suggestion=_closest(key, ordered),
            )
        )
    return issues


def _plant_config_value_issues(
    parameters: Mapping[str, ParameterSpec],
    plant_config: Any,
) -> list[SpecIssue]:
    if not parameters or not isinstance(plant_config, Mapping):
        return []
    issues: list[SpecIssue] = []
    for name in sorted(parameters):
        if name not in plant_config:
            continue
        spec = parameters[name]
        value = plant_config[name]
        if not _value_is_invalid(spec, value):
            continue
        expected = _expected_text(spec)
        issues.append(
            SpecIssue(
                code="invalid_parameter_value",
                path=f"/plant_config/{_escape(name)}",
                message=f"{name} must be {expected}, got {_render_value(value)}",
                expected=expected,
            )
        )
    return issues


def _schedule_key_issues(plant_id: str, input_schedule: Any) -> list[SpecIssue]:
    if not isinstance(input_schedule, list) or not input_schedule:
        # Nothing to check, and loading the composition root is deferred until
        # a document actually carries schedule entries: the checker must stay
        # importable without pulling in adapters and topology.
        return []
    try:
        from .builtin_plants import load_builtin_plants

        # Idempotent by contract; a caller that already loaded the shipped
        # plants gets the registrations it asked for.
        load_builtin_plants()
        schedule_keys = plant_registry.resolve(plant_id).schedule_keys
    except (ImportError, KeyError, ValueError):
        # A plant that is declared but not registered here has no schedule
        # vocabulary to check against; the document is still reported by the
        # schema and parameter checks rather than failing collection.
        return []
    allowed = sorted(schedule_keys)
    expected = "one of: " + ", ".join(allowed)
    issues: list[SpecIssue] = []
    for index, entry in enumerate(input_schedule):
        if not isinstance(entry, Mapping):
            continue
        for key in entry:
            if key in schedule_keys:
                continue
            issues.append(
                SpecIssue(
                    code="unknown_schedule_key",
                    path=f"/input_schedule/{index}/{_escape(str(key))}",
                    message=f"plant {plant_id!r} does not accept input_schedule field {key!r}",
                    expected=expected,
                    suggestion=_closest(key, allowed),
                )
            )
    return issues


def _contract_warnings(document: Mapping[str, Any]) -> list[SpecIssue]:
    contracts = document.get("contracts")
    if not isinstance(contracts, Mapping):
        return []
    zero = [
        name
        for name in _CONTRACT_NAMES
        if isinstance(contracts.get(name), Mapping)
        and contracts[name].get("hash") == _ZERO_HASH
    ]
    if not zero:
        return []
    return [
        SpecIssue(
            code="exploratory_contract_default",
            path="/contracts",
            message=(
                f"contracts {' and '.join(zero)} use the all-zero exploratory hash, so this "
                "run can only be reported as exploratory; formal_comparison requires "
                "explicitly reviewed contract hashes. This is a declaration gap, not a defect."
            ),
            expected="a non-zero reviewed hash for each contract",
            severity="warning",
        )
    ]


def _resolve_registration(plant_id: str | None) -> Any:
    """Return the registered plant for ``plant_id``, or ``None``.

    A plant can exist without a ``PLANT_PARAMETERS`` entry: a third-party
    distribution registers through the ``pe_sim.plants`` entry point, and the
    kernel is explicitly not supposed to know its name.  Reporting such a
    plant as unknown would tell an agent to rename something that is installed
    and working, so the registry gets the final say.

    ``resolve`` only triggers entry-point discovery on a miss, so a built-in
    lookup never pays for it.  A broken plugin can raise while loading, which
    is also treated as "not resolvable": the document still gets the
    ``unknown_plant`` report rather than a traceback.
    """

    if plant_id is None:
        return None
    try:
        return plant_registry.resolve(plant_id)
    except (KeyError, TypeError, ValueError):
        return None


def _declared_properties(registration: Any) -> Mapping[str, Any]:
    """Return the JSON Schema properties a registration publishes, if any."""

    schema = getattr(registration, "parameter_schema", None)
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _plugin_value_issues(
    plant_id: str,
    properties: Mapping[str, Any],
    plant_config: Any,
) -> list[SpecIssue]:
    """Check a plugin plant's values against the schema it publishes.

    The declared properties are applied without ``additionalProperties``: an
    unknown key is already reported once by ``_plant_config_key_issues``, and
    repeating it here would ask an agent to repair the same typo twice.
    """

    if not properties or not isinstance(plant_config, Mapping):
        return []
    known = {key: value for key, value in plant_config.items() if key in properties}
    if not known:
        return []
    import jsonschema

    validator = jsonschema.Draft202012Validator(
        {
            "type": "object",
            "properties": {
                name: dict(schema) for name, schema in properties.items()
            },
        }
    )
    issues = [
        SpecIssue(
            code="invalid_parameter_value",
            path="/plant_config" + _pointer(error.absolute_path),
            message=f"{plant_id!r} plant_config {error.message}",
            expected=_expected_from_error(error),
        )
        for error in validator.iter_errors(known)
    ]
    issues.sort(key=lambda issue: issue.path)
    return issues


def _drop_shadowed_schema_issues(issues: Sequence[SpecIssue]) -> list[SpecIssue]:
    """Remove a generic ``additionalProperties`` failure a specific issue explains.

    ``jsonschema`` reports a misspelled ``input_schedule`` field twice: once as
    ``additionalProperties`` at ``/input_schedule/0`` and once as
    ``unknown_schedule_key`` at ``/input_schedule/0/vinn_v``.  Reporting both
    makes an agent repair the same mistake twice and buries the one record that
    carries a ``suggestion``.

    The generic record survives whenever no specific issue covers it, so an
    unknown plant still reports the schema failure instead of going silent.  A
    root-level failure is never dropped: its path is the empty pointer, which
    every other path would appear to descend from.
    """

    specific_paths = {
        issue.path for issue in issues if issue.code != "schema_violation"
    }
    kept: list[SpecIssue] = []
    for issue in issues:
        shadowed = (
            issue.code == "schema_violation"
            and bool(issue.path)
            and (issue.expected or "").startswith("additionalProperties")
            and any(path.startswith(issue.path + "/") for path in specific_paths)
        )
        if not shadowed:
            kept.append(issue)
    return kept


def collect_issues(
    document: Mapping[str, Any], *, schema_path: str | Path | None = None
) -> tuple[SpecIssue, ...]:
    """Return every issue found in ``document`` without raising for bad input.

    The five checks are: the shared JSON Schema, plant resolution, unknown
    ``plant_config`` keys, ``plant_config`` value shapes, and the schedule
    vocabulary.  A zero-hash contract adds a non-blocking warning.  Only a
    misconfigured installation (missing ``jsonschema``, unreadable schema)
    raises.
    """

    if not isinstance(document, Mapping):
        return (
            SpecIssue(
                code="invalid_document",
                path="",
                message=(
                    "experiment document must be a JSON object, got "
                    f"{type(document).__name__}"
                ),
                expected="object",
            ),
        )

    issues = _schema_issues(document, schema_path)

    declared = document.get("plant_id")
    plant_id = declared if isinstance(declared, str) else None
    parameters = PLANT_PARAMETERS.get(plant_id) if plant_id is not None else None
    plant_config = document.get("plant_config")

    if parameters is not None:
        issues.extend(
            _plant_config_key_issues(plant_id, sorted(parameters), plant_config)
        )
        issues.extend(_plant_config_value_issues(parameters, plant_config))
        issues.extend(_schedule_key_issues(plant_id, document.get("input_schedule")))
    else:
        registration = _resolve_registration(plant_id)
        if registration is None:
            issues.append(_unknown_plant_issue(document))
        else:
            # A registered plant with no declaration table still gets the
            # checks its own schema supports.  Skipping them silently would
            # make ``validate`` answer "ok" for a document the factory is
            # about to reject.
            properties = _declared_properties(registration)
            issues.extend(
                _plant_config_key_issues(plant_id, sorted(properties), plant_config)
            )
            issues.extend(_plugin_value_issues(plant_id, properties, plant_config))
            issues.extend(
                _schedule_key_issues(plant_id, document.get("input_schedule"))
            )
    issues.extend(_contract_warnings(document))
    issues = _drop_shadowed_schema_issues(issues)
    issues.sort(key=_sort_key)
    return tuple(issues)


def _checked_stages(
    document: Mapping[str, Any], plant_id: str | None
) -> tuple[str, ...]:
    """Report which stages actually ran for this document.

    ``CHECKED_STAGES`` is the vocabulary and the order; this function decides
    membership.  A static list would tell a consumer that ``plant_config`` was
    examined even when the plant id did not resolve and there was nothing to
    examine against.  "Clean" and "not examined" must not look the same, or
    the envelope's ``ok`` becomes a claim the checker did not earn.
    """

    if not isinstance(document, Mapping):
        return ()
    stages = ["schema"]
    resolvable = plant_id is not None and (
        plant_id in PLANT_PARAMETERS or _resolve_registration(plant_id) is not None
    )
    if resolvable:
        # ``input_schedule`` counts as examined even when the array is empty:
        # the plant's vocabulary was available and an empty array cannot
        # violate it.  What must not be reported is the case where no
        # vocabulary could be resolved at all.
        stages.extend(("plant_id", "plant_config", "input_schedule"))
    stages.append("contracts")
    return tuple(stages)


def validation_document(
    document: Mapping[str, Any],
    *,
    issues: Sequence[SpecIssue],
    plant_id: str | None = None,
) -> dict[str, Any]:
    """Wrap already-collected issues in the versioned validation envelope."""

    ordered = tuple(sorted(issues, key=_sort_key))
    error_count = sum(1 for issue in ordered if issue.severity == "error")
    warning_count = len(ordered) - error_count
    if plant_id is None and isinstance(document, Mapping):
        declared = document.get("plant_id")
        plant_id = declared if isinstance(declared, str) else None
    return {
        "schema": SPEC_VALIDATION_SCHEMA,
        "plant_id": plant_id,
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": [issue.to_dict() for issue in ordered],
        "checked": list(_checked_stages(document, plant_id)),
    }


def validate_document(
    document: Mapping[str, Any], *, schema_path: str | Path | None = None
) -> dict[str, Any]:
    """Validate a document and raise ``SpecValidationError`` on any error.

    This is the agent's compile-time check: it validates the document, the
    plant's declared parameter surface, the schedule vocabulary, and the
    contract hashes **without starting a solver, creating a run directory, or
    requiring ngspice**.  A document that passes here is *well formed*, not
    numerically or physically validated; that only happens during a run.
    """

    issues = collect_issues(document, schema_path=schema_path)
    if any(issue.severity == "error" for issue in issues):
        raise SpecValidationError(issues)
    return validation_document(document, issues=issues)


__all__ = [
    "CHECKED_STAGES",
    "SPEC_ISSUE_CODES",
    "SPEC_VALIDATION_SCHEMA",
    "SpecIssue",
    "SpecValidationError",
    "collect_issues",
    "validate_document",
    "validation_document",
]
