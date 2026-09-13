"""Versioned, renderer-neutral simulation output contracts.

The output layer is deliberately separate from the Runner and from any
plotting toolkit.  Producers must register an output definition and publish a
validated value explicitly.  No object introspection is performed here, and
the resulting dataset is an immutable, read-only representation suitable for
tables, scalar cards, curves, or comparison views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

from .traces import TraceProvenance


OUTPUT_SCHEMA_VERSION = "1.0"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DTYPES = {"float64", "int64", "bool", "string"}
_TOP_LEVEL_FIELDS = {"schema_version", "provenance", "metadata", "outputs", "fingerprint"}
_ENTRY_FIELDS = {
    "output_id",
    "name",
    "kind",
    "unit",
    "source",
    "dtype",
    "definition_version",
    "description",
    "metadata",
    "fields",
    "payload",
}


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects all mutating operations."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("output dataset mappings are read-only")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("output metadata and payloads must be JSON serializable") from exc
    return value


def _freeze_json(value: Any) -> Any:
    """Defensively copy JSON values into immutable, JSON-compatible objects."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """Return an ordinary mutable JSON tree for serialization boundaries."""

    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_provenance(value: TraceProvenance) -> TraceProvenance:
    """Clone provenance so caller-owned metadata cannot mutate a dataset."""

    return TraceProvenance(
        run_id=value.run_id,
        manifest_sha256=value.manifest_sha256,
        package_sha256=value.package_sha256,
        source_commit=value.source_commit,
        producer=value.producer,
        metadata=_freeze_json(dict(value.metadata)),
    )


def _provenance_to_dict(value: TraceProvenance) -> dict[str, Any]:
    result = {
        "producer": value.producer,
    }
    for key in ("run_id", "manifest_sha256", "package_sha256", "source_commit"):
        item = getattr(value, key)
        if item is not None:
            result[key] = item
    if value.metadata:
        result["metadata"] = _thaw_json(value.metadata)
    return result


def _version(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or not _VERSION_RE.fullmatch(value):
        raise ValueError(f"{field_name} must contain only letters, numbers, '.', '_', or '-' characters")
    return value


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must contain only letters, numbers, '.', '_', ':', or '-' characters")
    return value


def _dtype(value: str, field_name: str = "dtype") -> str:
    if value not in _DTYPES:
        raise ValueError(f"unsupported {field_name}: {value}")
    return value


def _unit(value: str | None, field_name: str = "unit", *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} requires a finite numeric value")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} requires a finite numeric value")
    return float(value)


def _typed_value(value: Any, dtype: str, field_name: str) -> Any:
    if dtype == "float64":
        return _finite_number(value, field_name)
    if dtype == "int64":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} requires an integer value")
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError(f"{field_name} is outside int64 range")
        return int(value)
    if dtype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} requires a boolean value")
        return bool(value)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} requires a string value")
    return value


@dataclass(frozen=True)
class OutputFieldDefinition:
    """Definition for one column in a tabular output."""

    field_id: str
    name: str
    unit: str
    dtype: str = "float64"
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.field_id, "field_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("field name must be non-empty")
        _unit(self.unit, "field unit")
        _dtype(self.dtype)
        if not isinstance(self.description, str):
            raise ValueError("field description must be a string")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("field metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "field_id": self.field_id,
            "name": self.name,
            "unit": self.unit,
            "dtype": self.dtype,
        }
        if self.description:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = _thaw_json(self.metadata)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputFieldDefinition":
        if not isinstance(value, Mapping):
            raise ValueError("output field definition must be an object")
        allowed = {"field_id", "name", "unit", "dtype", "description", "metadata"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("output field definition contains unexpected fields: " + ", ".join(sorted(unknown)))
        required = {"field_id", "name", "unit", "dtype"}
        missing = required - set(value)
        if missing:
            raise ValueError("output field definition missing fields: " + ", ".join(sorted(missing)))
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("field metadata must be a mapping")
        return cls(
            field_id=value["field_id"],
            name=value["name"],
            unit=value["unit"],
            dtype=value["dtype"],
            description=value.get("description", ""),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class OutputDefinition:
    """Stable identity and validation metadata for one published output."""

    output_id: str
    name: str
    kind: str
    source: str
    unit: str | None = None
    dtype: str | None = None
    definition_version: str = "1.0"
    description: str = ""
    fields: tuple[OutputFieldDefinition, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.output_id, "output_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("output name must be non-empty")
        if self.kind not in {"scalar", "series", "table"}:
            raise ValueError("output kind must be scalar, series, or table")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("output source must be non-empty")
        _version(self.definition_version, "definition_version")
        if self.kind in {"scalar", "series"}:
            _unit(self.unit)
            object.__setattr__(self, "dtype", _dtype(self.dtype or "float64"))
            if self.fields:
                raise ValueError("scalar and series outputs cannot declare table fields")
        else:
            if self.unit is not None:
                raise ValueError("table output units belong to its declared fields")
            if self.dtype is not None:
                raise ValueError("table output dtype belongs to its declared fields")
            if not self.fields:
                raise ValueError("table output requires at least one field")
            ids = [field.field_id for field in self.fields]
            if len(ids) != len(set(ids)):
                raise ValueError("table field IDs must be unique")
        if not isinstance(self.description, str):
            raise ValueError("output description must be a string")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("output metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)
        if not all(isinstance(item, OutputFieldDefinition) for item in self.fields):
            raise TypeError("fields must contain OutputFieldDefinition objects")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "output_id": self.output_id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source,
            "definition_version": self.definition_version,
        }
        if self.kind in {"scalar", "series"}:
            result["unit"] = self.unit
            result["dtype"] = self.dtype
        else:
            result["fields"] = [field.to_dict() for field in self.fields]
        if self.description:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = _thaw_json(self.metadata)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputDefinition":
        if not isinstance(value, Mapping):
            raise ValueError("output definition must be an object")
        allowed = {
            "output_id",
            "name",
            "kind",
            "unit",
            "source",
            "dtype",
            "definition_version",
            "description",
            "fields",
            "metadata",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("output definition contains unexpected fields: " + ", ".join(sorted(unknown)))
        required = {"output_id", "name", "kind", "source", "definition_version"}
        missing = required - set(value)
        if missing:
            raise ValueError("output definition missing fields: " + ", ".join(sorted(missing)))
        kind = value["kind"]
        if kind in {"scalar", "series"}:
            required_shape = {"unit", "dtype"}
            prohibited_shape = {"fields"}
        elif kind == "table":
            required_shape = {"fields"}
            prohibited_shape = {"unit", "dtype"}
        else:
            raise ValueError("output kind must be scalar, series, or table")
        missing_shape = required_shape - set(value)
        if missing_shape:
            raise ValueError("output definition missing fields: " + ", ".join(sorted(missing_shape)))
        present_prohibited = prohibited_shape & set(value)
        if present_prohibited:
            raise ValueError("output definition contains fields invalid for its kind: " + ", ".join(sorted(present_prohibited)))
        fields_value = value.get("fields", [])
        if not isinstance(fields_value, list):
            raise ValueError("output fields must be an array")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("output metadata must be a mapping")
        return cls(
            output_id=value["output_id"],
            name=value["name"],
            kind=kind,
            source=value["source"],
            unit=value.get("unit"),
            dtype=value.get("dtype"),
            definition_version=value["definition_version"],
            description=value.get("description", ""),
            fields=tuple(OutputFieldDefinition.from_dict(item) for item in fields_value),
            metadata=dict(metadata),
        )


class OutputRegistry:
    """Explicit registration boundary for output definitions."""

    def __init__(self, definitions: Iterable[OutputDefinition] = ()) -> None:
        self._definitions: dict[str, OutputDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: OutputDefinition) -> OutputDefinition:
        if not isinstance(definition, OutputDefinition):
            raise TypeError("definition must be an OutputDefinition")
        if definition.output_id in self._definitions:
            raise ValueError(f"output_id already registered: {definition.output_id}")
        self._definitions[definition.output_id] = definition
        return definition

    def get(self, output_id: str) -> OutputDefinition:
        try:
            return self._definitions[output_id]
        except KeyError as exc:
            raise KeyError(f"unknown output_id: {output_id}") from exc

    def __contains__(self, output_id: object) -> bool:
        return output_id in self._definitions

    def __iter__(self):
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)

    @property
    def definitions(self) -> tuple[OutputDefinition, ...]:
        return tuple(self._definitions.values())

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.definitions]

    @classmethod
    def from_list(cls, values: Iterable[Mapping[str, Any]]) -> "OutputRegistry":
        return cls(OutputDefinition.from_dict(item) for item in values)


def _validate_payload(definition: OutputDefinition, payload: Any) -> Any:
    if definition.kind == "scalar":
        return _typed_value(payload, definition.dtype, f"output {definition.output_id}")
    if not isinstance(payload, Mapping):
        raise ValueError(f"output {definition.output_id} payload must be an object")
    if definition.kind == "series":
        if set(payload) != {"time_s", "values"}:
            raise ValueError(f"output {definition.output_id} series payload must contain only time_s and values")
        times = payload["time_s"]
        values = payload["values"]
        if not isinstance(times, list) or not isinstance(values, list):
            raise ValueError(f"output {definition.output_id} series time_s and values must be arrays")
        if len(times) != len(values):
            raise ValueError(f"output {definition.output_id} series time_s and values must have the same length")
        clean_times: list[float] = []
        previous: float | None = None
        for index, item in enumerate(times):
            current = _finite_number(item, f"output {definition.output_id} time_s[{index}]")
            if current < 0:
                raise ValueError(f"output {definition.output_id} time_s must be non-negative")
            if previous is not None and current < previous:
                raise ValueError(f"output {definition.output_id} series time_s must be monotonic")
            clean_times.append(current)
            previous = current
        clean_values = [_typed_value(item, definition.dtype, f"output {definition.output_id} values[{index}]") for index, item in enumerate(values)]
        return {"time_s": clean_times, "values": clean_values}
    if set(payload) != {"rows"}:
        raise ValueError(f"output {definition.output_id} table payload must contain only rows")
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError(f"output {definition.output_id} table rows must be an array")
    field_by_id = {field.field_id: field for field in definition.fields}
    clean_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"output {definition.output_id} table row {index} must be an object")
        keys = set(row)
        missing = set(field_by_id) - keys
        extra = keys - set(field_by_id)
        if missing:
            raise ValueError(f"output {definition.output_id} table row {index} has missing columns: {', '.join(sorted(missing))}")
        if extra:
            raise ValueError(f"output {definition.output_id} table row {index} has unexpected columns: {', '.join(sorted(extra))}")
        clean_rows.append(
            {
                field_id: _typed_value(row[field_id], field_by_id[field_id].dtype, f"output {definition.output_id} row {index} column {field_id}")
                for field_id in field_by_id
            }
        )
    return {"rows": clean_rows}


@dataclass(frozen=True)
class OutputEntry:
    definition: OutputDefinition
    payload: Any

    def __post_init__(self) -> None:
        if not isinstance(self.definition, OutputDefinition):
            raise TypeError("definition must be an OutputDefinition")
        clean = _validate_payload(self.definition, self.payload)
        object.__setattr__(self, "payload", _freeze_json(clean))

    @property
    def output_id(self) -> str:
        return self.definition.output_id

    def to_dict(self) -> dict[str, Any]:
        return {**self.definition.to_dict(), "payload": _thaw_json(self.payload)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputEntry":
        if not isinstance(value, Mapping):
            raise ValueError("output entry must be an object")
        unknown = set(value) - _ENTRY_FIELDS
        if unknown:
            raise ValueError("output entry contains unexpected fields: " + ", ".join(sorted(unknown)))
        if "payload" not in value:
            raise ValueError("output entry payload is required")
        definition = OutputDefinition.from_dict({key: item for key, item in value.items() if key != "payload"})
        return cls(definition, value["payload"])


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class OutputDataset:
    """Immutable validated collection of structured simulation outputs."""

    outputs: tuple[OutputEntry, ...] = ()
    provenance: TraceProvenance = field(default_factory=TraceProvenance)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = OUTPUT_SCHEMA_VERSION
    _declared_fingerprint: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != OUTPUT_SCHEMA_VERSION:
            raise ValueError(f"unsupported output schema_version: {self.schema_version}")
        if not isinstance(self.provenance, TraceProvenance):
            raise TypeError("provenance must be a TraceProvenance")
        object.__setattr__(self, "provenance", _freeze_provenance(self.provenance))
        if not isinstance(self.metadata, Mapping):
            raise ValueError("output dataset metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)
        seen: set[str] = set()
        for entry in self.outputs:
            if not isinstance(entry, OutputEntry):
                raise TypeError("outputs must contain OutputEntry objects")
            if entry.output_id in seen:
                raise ValueError(f"output_id already published: {entry.output_id}")
            seen.add(entry.output_id)
        if self._declared_fingerprint is not None:
            if not isinstance(self._declared_fingerprint, str) or not _HASH_RE.fullmatch(self._declared_fingerprint):
                raise ValueError("fingerprint must be a SHA-256 hex digest")
            if self._declared_fingerprint != self._compute_fingerprint():
                raise ValueError("output dataset fingerprint does not match contents")

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provenance": _provenance_to_dict(self.provenance),
            "metadata": _thaw_json(self.metadata),
            "outputs": [entry.to_dict() for entry in self.outputs],
        }

    def _compute_fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self._body())).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._compute_fingerprint()

    @property
    def registry(self) -> OutputRegistry:
        return OutputRegistry(entry.definition for entry in self.outputs)

    def get(self, output_id: str) -> OutputEntry:
        for entry in self.outputs:
            if entry.output_id == output_id:
                return entry
        raise KeyError(f"unknown output_id: {output_id}")

    def to_dict(self) -> dict[str, Any]:
        return {**self._body(), "fingerprint": self.fingerprint}

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputDataset":
        if not isinstance(value, Mapping):
            raise ValueError("output dataset must be an object")
        unknown = set(value) - _TOP_LEVEL_FIELDS
        if unknown:
            raise ValueError("output dataset contains unexpected fields: " + ", ".join(sorted(unknown)))
        missing = _TOP_LEVEL_FIELDS - set(value)
        if missing:
            raise ValueError("output dataset missing fields: " + ", ".join(sorted(missing)))
        outputs = value["outputs"]
        if not isinstance(outputs, list):
            raise ValueError("output dataset outputs must be an array")
        metadata = value["metadata"]
        if not isinstance(metadata, Mapping):
            raise ValueError("output dataset metadata must be a mapping")
        provenance = value["provenance"]
        if not isinstance(provenance, Mapping):
            raise ValueError("output dataset provenance must be an object")
        return cls(
            schema_version=value["schema_version"],
            provenance=TraceProvenance.from_dict(provenance),
            metadata=dict(metadata),
            outputs=tuple(OutputEntry.from_dict(item) for item in outputs),
            _declared_fingerprint=value["fingerprint"],
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "OutputDataset":
        return cls.from_dict(json.loads(value))


class OutputCollector:
    """Explicit, single-publication collector for output values."""

    def __init__(
        self,
        registry: OutputRegistry,
        *,
        provenance: TraceProvenance | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(registry, OutputRegistry):
            raise TypeError("registry must be an OutputRegistry")
        self.registry = registry
        self.provenance = provenance or TraceProvenance()
        self.metadata = dict(metadata or {})
        _json_safe(self.metadata)
        self._entries: list[OutputEntry] = []

    @property
    def entries(self) -> tuple[OutputEntry, ...]:
        return tuple(self._entries)

    def publish(self, output_id: str, payload: Any) -> OutputEntry:
        definition = self.registry.get(output_id)
        if any(entry.output_id == output_id for entry in self._entries):
            raise ValueError(f"output_id already published: {output_id}")
        entry = OutputEntry(definition, payload)
        self._entries.append(entry)
        return entry

    def dataset(self) -> OutputDataset:
        return OutputDataset(tuple(self._entries), provenance=self.provenance, metadata=self.metadata)


__all__ = [
    "OUTPUT_SCHEMA_VERSION",
    "OutputFieldDefinition",
    "OutputDefinition",
    "OutputRegistry",
    "OutputEntry",
    "OutputDataset",
    "OutputCollector",
]
