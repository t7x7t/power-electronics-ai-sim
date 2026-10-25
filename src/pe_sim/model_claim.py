"""Declared model applicability and parameter provenance.

A finite simulation is not automatically a meaningful one.  This module makes
the *declared* limits of a model first-class, portable, and hashable:

- :class:`ModelClaim` states what the model assumes and where its equations are
  defined (operating ranges, numerical limits, uncheckable rules).
- :class:`ParameterProvenance` states where the numbers came from.

Neither object is part of the model's implementation identity.  A declaration
is evidence about applicability, not a change to the equations.  Adapters opt
in by implementing ``validity_envelope()`` and ``parameter_provenance()``.
Missing declarations are represented as ``unknown``; they are never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Mapping, Sequence

from .artifacts import sha256_bytes
from .jsonutil import stable_sha256


ENVELOPE_SCHEMA_VERSION = "validity-envelope-v1"
PARAMETER_PROVENANCE_SCHEMA_VERSION = "parameter-provenance-v1"
MODEL_CLAIM_RULE_VERSION = "model-envelope-v1"

RULE_STRENGTHS = ("hard", "advisory")

PARAMETER_SOURCES = (
    "datasheet",
    "measurement",
    "assumption",
    "default",
    "reviewed_reference",
    "undeclared",
)

_PARAMETER_REVIEW_STATUSES = ("unreviewed", "reviewed", "not_applicable")

_PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|\.\.?[\\/])|[\\/]")

#: A declaration error is reported verbatim, so it is bounded.  An adapter can
#: raise an arbitrary message, and the manifest must stay readable.
_MAX_DECLARATION_ERROR = 200


def _bounded_error(value: Any) -> str:
    text = str(value)
    if len(text) <= _MAX_DECLARATION_ERROR:
        return text
    return text[: _MAX_DECLARATION_ERROR - 3] + "..."


def _numeric(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array of strings")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class OperatingRange:
    """One declared operating range, including its basis.

    ``basis`` is descriptive, for example ``equation_domain`` (the equation is
    undefined outside it), ``template_domain`` (the generated circuit uses the
    value as an input), or ``declared`` (an engineering statement that is not
    derived from the equations).
    """

    name: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    minimum_exclusive: bool = False
    maximum_exclusive: bool = False
    basis: str = "declared"
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("operating range name must be non-empty")
        minimum = _numeric(self.minimum, f"{self.name}.minimum", allow_none=True)
        maximum = _numeric(self.maximum, f"{self.name}.maximum", allow_none=True)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"operating range {self.name!r} is not ordered")
        if self.minimum is None and self.minimum_exclusive:
            raise ValueError(f"operating range {self.name!r} has an exclusive lower bound with no bound")
        if self.maximum is None and self.maximum_exclusive:
            raise ValueError(f"operating range {self.name!r} has an exclusive upper bound with no bound")

    def contains(self, value: Any, *, relative_tolerance: float = 1e-9) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        number = float(value)
        if not math.isfinite(number):
            return False

        def _lower_ok(bound: float) -> bool:
            tolerance = relative_tolerance * max(1.0, abs(bound))
            return number > bound - tolerance if self.minimum_exclusive else number >= bound - tolerance

        def _upper_ok(bound: float) -> bool:
            tolerance = relative_tolerance * max(1.0, abs(bound))
            return number < bound + tolerance if self.maximum_exclusive else number <= bound + tolerance

        if self.minimum is not None and not _lower_ok(self.minimum):
            return False
        if self.maximum is not None and not _upper_ok(self.maximum):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "minimum_exclusive": bool(self.minimum_exclusive),
            "maximum_exclusive": bool(self.maximum_exclusive),
            "basis": self.basis,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OperatingRange":
        return cls(
            name=str(value.get("name", "")),
            unit=None if value.get("unit") is None else str(value["unit"]),
            minimum=value.get("minimum"),
            maximum=value.get("maximum"),
            minimum_exclusive=bool(value.get("minimum_exclusive", False)),
            maximum_exclusive=bool(value.get("maximum_exclusive", False)),
            basis=str(value.get("basis", "declared")),
            note=None if value.get("note") is None else str(value["note"]),
        )


@dataclass(frozen=True)
class NumericalLimit:
    """One declared numerical-resolution or stability limit."""

    name: str
    value: float | None
    unit: str | None = None
    strength: str = "advisory"
    basis: str = "declared"
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("numerical limit name must be non-empty")
        object.__setattr__(
            self,
            "value",
            _numeric(self.value, f"{self.name}.value", allow_none=True),
        )
        if self.strength not in RULE_STRENGTHS:
            raise ValueError(f"unsupported numerical limit strength: {self.strength!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "strength": self.strength,
            "basis": self.basis,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NumericalLimit":
        return cls(
            name=str(value.get("name", "")),
            value=value.get("value"),
            unit=None if value.get("unit") is None else str(value["unit"]),
            strength=str(value.get("strength", "advisory")),
            basis=str(value.get("basis", "declared")),
            note=None if value.get("note") is None else str(value["note"]),
        )


@dataclass(frozen=True)
class ModelClaim:
    """A portable statement of model assumptions and applicability limits."""

    claim_level: str = "unknown"
    topology: str | None = None
    model_form: str | None = None
    assumptions: tuple[str, ...] = ()
    operating_ranges: tuple[OperatingRange, ...] = ()
    numerical_limits: tuple[NumericalLimit, ...] = ()
    limitations: tuple[str, ...] = ()
    uncheckable_rules: tuple[Mapping[str, str], ...] = ()
    actuator: Mapping[str, str] | None = None
    static_parameters: Mapping[str, float] = field(default_factory=dict)
    status: str = "declared"
    declaration_error: str | None = None
    rule_version: str = MODEL_CLAIM_RULE_VERSION
    schema_version: str = ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in {"declared", "unknown", "declaration_invalid"}:
            raise ValueError(f"unsupported model claim status: {self.status!r}")
        if not isinstance(self.claim_level, str) or not self.claim_level.strip():
            raise ValueError("claim_level must be a non-empty string")

    @classmethod
    def unknown(cls, reason: str) -> "ModelClaim":
        return cls(claim_level="unknown", status="unknown", declaration_error=str(reason))

    @classmethod
    def invalid(cls, error: str) -> "ModelClaim":
        return cls(
            claim_level="unknown",
            status="declaration_invalid",
            declaration_error=str(error),
        )

    def operating_range(self, name: str) -> OperatingRange | None:
        return next((item for item in self.operating_ranges if item.name == name), None)

    def numerical_limit(self, name: str) -> NumericalLimit | None:
        return next((item for item in self.numerical_limits if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "claim_level": self.claim_level,
            "topology": self.topology,
            "model_form": self.model_form,
            "assumptions": list(self.assumptions),
            "operating_ranges": [item.to_dict() for item in self.operating_ranges],
            "numerical_limits": [item.to_dict() for item in self.numerical_limits],
            "limitations": list(self.limitations),
            "uncheckable_rules": [dict(item) for item in self.uncheckable_rules],
            "actuator": dict(self.actuator) if self.actuator is not None else None,
            "static_parameters": {
                str(key): float(value)
                for key, value in self.static_parameters.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
            },
            "status": self.status,
            "declaration_error": self.declaration_error,
        }

    def sha256(self) -> str | None:
        """Return a stable digest of the declaration, or ``None`` if absent."""

        if self.status != "declared":
            return None
        payload = self.to_dict()
        payload.pop("status", None)
        payload.pop("declaration_error", None)
        payload["assumptions"] = sorted(set(payload["assumptions"]))
        payload["limitations"] = sorted(set(payload["limitations"]))
        payload["operating_ranges"] = sorted(payload["operating_ranges"], key=lambda item: item["name"])
        payload["numerical_limits"] = sorted(payload["numerical_limits"], key=lambda item: item["name"])
        payload["uncheckable_rules"] = sorted(
            payload["uncheckable_rules"],
            key=lambda item: (str(item.get("rule_id", "")), str(item.get("reason", ""))),
        )
        return stable_sha256(payload)


def _ranges_from_value(value: Any) -> tuple[OperatingRange, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        result: list[OperatingRange] = []
        for name, item in value.items():
            if isinstance(item, Mapping):
                payload = {"name": name, **dict(item)}
                result.append(OperatingRange.from_mapping(payload))
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
                result.append(OperatingRange(name=str(name), minimum=item[0], maximum=item[1]))
            else:
                raise ValueError(f"operating range {name!r} must be an object or [minimum, maximum]")
        return tuple(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = []
        for item in value:
            if isinstance(item, OperatingRange):
                result.append(item)
            elif isinstance(item, Mapping):
                result.append(OperatingRange.from_mapping(item))
            else:
                raise ValueError("operating ranges must contain objects")
        return tuple(result)
    raise ValueError("operating_ranges must be an array or object")


def _limits_from_value(value: Any) -> tuple[NumericalLimit, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        result: list[NumericalLimit] = []
        for name, item in value.items():
            if isinstance(item, Mapping):
                result.append(NumericalLimit.from_mapping({"name": name, **dict(item)}))
            else:
                result.append(NumericalLimit(name=str(name), value=item))
        return tuple(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = []
        for item in value:
            if isinstance(item, NumericalLimit):
                result.append(item)
            elif isinstance(item, Mapping):
                result.append(NumericalLimit.from_mapping(item))
            else:
                raise ValueError("numerical limits must contain objects")
        return tuple(result)
    raise ValueError("numerical_limits must be an array or object")


def normalise_model_claim(value: Any) -> ModelClaim:
    """Normalise a dataclass or mapping into a :class:`ModelClaim`."""

    if isinstance(value, ModelClaim):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("model claim must be a ModelClaim or a mapping")

    claim_level = str(value.get("claim_level", value.get("level", "declared")))
    static_parameters: dict[str, float] = {}
    raw_static = value.get("static_parameters", {})
    if raw_static is not None:
        if not isinstance(raw_static, Mapping):
            raise ValueError("static_parameters must be an object")
        for key, item in raw_static.items():
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                continue
            number = float(item)
            if math.isfinite(number):
                static_parameters[str(key)] = number

    uncheckable = value.get("uncheckable_rules", ())
    if uncheckable is None:
        uncheckable = ()
    if isinstance(uncheckable, (str, bytes)) or not isinstance(uncheckable, Sequence):
        raise ValueError("uncheckable_rules must be an array")
    uncheckable_rules = tuple(
        {"rule_id": str(item.get("rule_id", "")), "reason": str(item.get("reason", ""))}
        for item in uncheckable
        if isinstance(item, Mapping)
    )

    actuator = value.get("actuator")
    actuator_map = None if actuator is None else {str(k): str(v) for k, v in dict(actuator).items()}

    return ModelClaim(
        claim_level=claim_level,
        topology=None if value.get("topology") is None else str(value["topology"]),
        model_form=None if value.get("model_form") is None else str(value["model_form"]),
        assumptions=_string_tuple(value.get("assumptions"), "assumptions"),
        operating_ranges=_ranges_from_value(value.get("operating_ranges")),
        numerical_limits=_limits_from_value(value.get("numerical_limits")),
        limitations=_string_tuple(value.get("limitations"), "limitations"),
        uncheckable_rules=uncheckable_rules,
        actuator=actuator_map,
        static_parameters=static_parameters,
        status="declared",
    )


@dataclass(frozen=True)
class ParameterRecord:
    """One parameter value and its declared source."""

    name: str
    value: float | None
    unit: str | None
    source: str
    uncertainty: float | None = None
    reference: str | None = None
    reference_sha256: str | None = None
    review_status: str = "unreviewed"
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("parameter name must be non-empty")
        if self.source not in PARAMETER_SOURCES:
            raise ValueError(f"unsupported parameter source: {self.source!r}")
        if self.review_status not in _PARAMETER_REVIEW_STATUSES:
            raise ValueError(f"unsupported parameter review status: {self.review_status!r}")
        value = self.value
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"parameter {self.name!r} value must be a finite number or None"
                )
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"parameter {self.name!r} value must be finite")
        object.__setattr__(self, "value", value)
        uncertainty = self.uncertainty
        if uncertainty is not None:
            if isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)):
                raise ValueError(
                    f"parameter {self.name!r} uncertainty must be a finite number or None"
                )
            uncertainty = float(uncertainty)
            if not math.isfinite(uncertainty):
                raise ValueError(f"parameter {self.name!r} uncertainty must be finite")
        object.__setattr__(self, "uncertainty", uncertainty)
        reference = self.reference
        if reference is not None:
            reference = str(reference)
            if _PATH_LIKE.search(reference):
                object.__setattr__(self, "reference_sha256", sha256_bytes(reference.encode("utf-8")))
                reference = None
        object.__setattr__(self, "reference", reference)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "uncertainty": self.uncertainty,
            "reference": self.reference,
            "reference_sha256": self.reference_sha256,
            "review_status": self.review_status,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ParameterRecord":
        return cls(
            name=str(value.get("name", "")),
            value=value.get("value"),
            unit=None if value.get("unit") is None else str(value["unit"]),
            source=str(value.get("source", "undeclared")),
            uncertainty=value.get("uncertainty"),
            reference=None if value.get("reference") is None else str(value["reference"]),
            reference_sha256=None if value.get("reference_sha256") is None else str(value["reference_sha256"]),
            review_status=str(value.get("review_status", "unreviewed")),
            note=None if value.get("note") is None else str(value["note"]),
        )


@dataclass(frozen=True)
class ParameterProvenance:
    """A set of parameter records with a stable aggregate digest."""

    records: tuple[ParameterRecord, ...] = ()
    status: str = "unknown"
    declaration_error: str | None = None
    schema_version: str = PARAMETER_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in {"declared", "partial", "unknown", "declaration_invalid"}:
            raise ValueError(f"unsupported parameter provenance status: {self.status!r}")

    @classmethod
    def unknown(cls, reason: str) -> "ParameterProvenance":
        return cls(status="unknown", declaration_error=_bounded_error(reason))

    @classmethod
    def invalid(cls, error: str) -> "ParameterProvenance":
        return cls(status="declaration_invalid", declaration_error=_bounded_error(error))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "declaration_error": self.declaration_error,
            "records": [item.to_dict() for item in self.records],
        }

    def sha256(self) -> str | None:
        if self.status not in {"declared", "partial"}:
            return None
        payload = {
            "schema_version": self.schema_version,
            "status": self.status,
            "records": sorted(
                (item.to_dict() for item in self.records),
                key=lambda item: (item["name"], str(item["source"])),
            ),
        }
        return stable_sha256(payload)

    def by_name(self, name: str) -> ParameterRecord | None:
        return next((item for item in self.records if item.name == name), None)

    @property
    def unreviewed(self) -> tuple[ParameterRecord, ...]:
        return tuple(
            item
            for item in self.records
            if item.review_status == "unreviewed" or item.source in {"assumption", "default"}
        )


def normalise_parameter_provenance(value: Any) -> ParameterProvenance:
    if isinstance(value, ParameterProvenance):
        return value
    if value is None:
        raise TypeError("parameter provenance is absent")
    if isinstance(value, Mapping) and "records" in value:
        raw_records = value.get("records")
        status = str(value.get("status", ""))
        declared_error = value.get("declaration_error")
    else:
        raw_records = value
        status = ""
        declared_error = None
    if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
        raise TypeError("parameter provenance records must be an array")
    # A status token is a declaration, not a hint.  Inferring one from the
    # records turned an unregistered token such as ``trusted`` into
    # ``declared`` and hid the malformed declaration behind a clean summary;
    # docs/40 section 2.1 requires ``declaration_invalid`` instead.
    #
    # ``unknown`` stays a legal token: an adapter may declare explicitly that it
    # does not state sources.  0.9.4 accepted that token only when it arrived as
    # a ``ParameterProvenance`` object, so the same declaration changed meaning
    # with the container it came in.
    if status == "declaration_invalid":
        return ParameterProvenance.invalid(
            str(declared_error or "adapter declared an invalid parameter provenance")
        )
    if status and status not in {"declared", "partial", "unknown"}:
        return ParameterProvenance.invalid(
            f"unsupported parameter provenance status: {status!r}"
        )
    try:
        records: list[ParameterRecord] = []
        for item in raw_records:
            if isinstance(item, ParameterRecord):
                records.append(item)
            elif isinstance(item, Mapping):
                records.append(ParameterRecord.from_mapping(item))
            else:
                raise TypeError("parameter provenance records must be objects")
    except ValueError as exc:
        # A record that cannot represent its declared value is a malformed
        # declaration, not a reason to drop the value silently.
        return ParameterProvenance.invalid(f"{type(exc).__name__}: {exc}")
    if not records:
        return ParameterProvenance.unknown("adapter declared an empty parameter provenance")
    if status == "unknown":
        return ParameterProvenance(
            records=tuple(records),
            status="unknown",
            declaration_error=(
                _bounded_error(declared_error) if declared_error is not None else None
            ),
        )
    sources = {item.source for item in records}
    if status in {"declared", "partial"}:
        resolved = status
    elif sources == {"undeclared"}:
        resolved = "unknown"
    elif any(item.value is None or item.source == "undeclared" for item in records):
        resolved = "partial"
    else:
        resolved = "declared"
    return ParameterProvenance(records=tuple(records), status=resolved)


def collect_model_claim(adapter: Any) -> ModelClaim:
    """Read an optional ``validity_envelope``/``model_claim`` declaration."""

    provider = getattr(adapter, "validity_envelope", None)
    if provider is None:
        provider = getattr(adapter, "model_claim", None)
    if provider is None:
        return ModelClaim.unknown("adapter does not declare a validity envelope")
    try:
        value = provider() if callable(provider) else provider
        if value is None:
            return ModelClaim.unknown("adapter returned no validity envelope")
        return normalise_model_claim(value)
    except Exception as exc:  # adapter code is an untrusted extension boundary
        return ModelClaim.invalid(f"{type(exc).__name__}: {exc}")


def collect_parameter_provenance(adapter: Any) -> ParameterProvenance:
    """Read an optional ``parameter_provenance`` declaration."""

    provider = getattr(adapter, "parameter_provenance", None)
    if provider is None:
        return ParameterProvenance.unknown("adapter does not declare parameter sources")
    try:
        value = provider() if callable(provider) else provider
        if value is None:
            return ParameterProvenance.unknown("adapter returned no parameter provenance")
        return normalise_parameter_provenance(value)
    except Exception as exc:
        return ParameterProvenance.invalid(f"{type(exc).__name__}: {exc}")


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "MODEL_CLAIM_RULE_VERSION",
    "PARAMETER_PROVENANCE_SCHEMA_VERSION",
    "PARAMETER_SOURCES",
    "RULE_STRENGTHS",
    "ModelClaim",
    "NumericalLimit",
    "OperatingRange",
    "ParameterProvenance",
    "ParameterRecord",
    "collect_model_claim",
    "collect_parameter_provenance",
    "normalise_model_claim",
    "normalise_parameter_provenance",
]
