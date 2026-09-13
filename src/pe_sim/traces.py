"""Versioned signal/trace contracts for simulation observation.

The trace layer is deliberately independent from the Runner and from any
plotting toolkit.  A model or a Runner integration can publish values through
``TraceCollector``; consumers can then serialize, stream, or visualize the
result without guessing units, signal identity, or run provenance.

Trace data is observational evidence.  It does not grant a controller access
to plant truth and it does not make a physical or product-level claim about a
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol
import json
import math
import re

TRACE_SCHEMA_VERSION = "1.0"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_DTYPES = {"float64", "int64", "bool", "string"}


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects mutation after publication."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("trace dataset mappings are read-only")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable


def _json_safe(value: Any) -> Any:
    """Return ``value`` after checking that it is JSON serializable."""

    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("trace metadata and values must be JSON serializable") from exc
    return value


def _freeze_json(value: Any) -> Any:
    """Defensively copy nested JSON values into immutable containers."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """Return a mutable JSON tree at serialization boundaries."""

    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_provenance(value: "TraceProvenance") -> "TraceProvenance":
    """Clone provenance so caller-owned metadata cannot mutate a dataset."""

    return TraceProvenance(
        run_id=value.run_id,
        manifest_sha256=value.manifest_sha256,
        package_sha256=value.package_sha256,
        source_commit=value.source_commit,
        producer=value.producer,
        metadata=_freeze_json(dict(value.metadata)),
    )


def _validate_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must contain only letters, numbers, '.', '_', ':', or '-' characters")
    return value


def _validate_hash(value: str | None, field_name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not _HASH_RE.fullmatch(value)):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return value


@dataclass(frozen=True)
class SignalDefinition:
    """Stable identity and display metadata for one observable signal.

    ``source`` is an extensible provenance label (for example ``plant``,
    ``controller``, ``solver``, or ``user``), not a permission boundary.  A
    signal becomes usable by a consumer only after the consumer explicitly
    selects it.
    """

    signal_id: str
    name: str
    unit: str
    source: str = "user"
    dtype: str = "float64"
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_identifier(self.signal_id, "signal_id")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("signal name must be non-empty")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("signal unit must be non-empty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("signal source must be non-empty")
        if self.dtype not in _DTYPES:
            raise ValueError(f"unsupported signal dtype: {self.dtype}")
        if not isinstance(self.description, str):
            raise ValueError("signal description must be a string")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("signal metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "signal_id": self.signal_id,
            "name": self.name,
            "unit": self.unit,
            "source": self.source,
            "dtype": self.dtype,
        }
        if self.description:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = _thaw_json(self.metadata)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SignalDefinition":
        if not isinstance(value, Mapping):
            raise ValueError("signal definition must be an object")
        return cls(
            signal_id=str(value.get("signal_id", "")),
            name=str(value.get("name", "")),
            unit=str(value.get("unit", "")),
            source=str(value.get("source", "user")),
            dtype=str(value.get("dtype", "float64")),
            description=str(value.get("description", "")),
            metadata=dict(value.get("metadata", {})),
        )


class SignalRegistry:
    """Mutable registration boundary for signal definitions.

    Registration is intentionally explicit and duplicate IDs are rejected.
    This catches accidental collisions when a custom topology and a backend
    publish similarly named values.
    """

    def __init__(self, definitions: Iterable[SignalDefinition] = ()) -> None:
        self._definitions: dict[str, SignalDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SignalDefinition) -> SignalDefinition:
        if not isinstance(definition, SignalDefinition):
            raise TypeError("definition must be a SignalDefinition")
        if definition.signal_id in self._definitions:
            raise ValueError(f"signal_id already registered: {definition.signal_id}")
        self._definitions[definition.signal_id] = definition
        return definition

    def register_signal(
        self,
        signal_id: str,
        *,
        name: str | None = None,
        unit: str,
        source: str = "user",
        dtype: str = "float64",
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SignalDefinition:
        return self.register(
            SignalDefinition(
                signal_id=signal_id,
                name=name if name is not None else signal_id,
                unit=unit,
                source=source,
                dtype=dtype,
                description=description,
                metadata=dict(metadata or {}),
            )
        )

    def get(self, signal_id: str) -> SignalDefinition:
        try:
            return self._definitions[signal_id]
        except KeyError as exc:
            raise KeyError(f"unknown signal_id: {signal_id}") from exc

    def __contains__(self, signal_id: object) -> bool:
        return signal_id in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)

    def __iter__(self):
        return iter(self._definitions)

    @property
    def definitions(self) -> tuple[SignalDefinition, ...]:
        return tuple(self._definitions.values())

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.definitions]

    @classmethod
    def from_list(cls, values: Iterable[Mapping[str, Any]]) -> "SignalRegistry":
        return cls(SignalDefinition.from_dict(item) for item in values)


@dataclass(frozen=True)
class SamplingPolicy:
    """Bound the rate and size of trace collection.

    ``every_step`` records every accepted call, ``fixed_interval`` records the
    first point and then points at least ``interval_s`` apart, ``event`` records
    only calls with an event label, and ``manual`` requires ``force=True``.
    The collector never silently overwrites an existing sample when
    ``max_points`` is reached; it reports the dropped count instead.
    """

    mode: str = "every_step"
    interval_s: float | None = None
    max_points: int | None = None
    include_initial: bool = True
    event_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"every_step", "fixed_interval", "event", "manual"}:
            raise ValueError("unsupported sampling mode")
        if self.mode == "fixed_interval" and (self.interval_s is None or not math.isfinite(float(self.interval_s)) or float(self.interval_s) <= 0):
            raise ValueError("fixed_interval sampling requires a finite positive interval_s")
        if self.interval_s is not None and (not math.isfinite(float(self.interval_s)) or float(self.interval_s) <= 0):
            raise ValueError("interval_s must be finite and positive")
        if self.max_points is not None and (not isinstance(self.max_points, int) or self.max_points <= 0):
            raise ValueError("max_points must be a positive integer")
        if not isinstance(self.include_initial, bool):
            raise ValueError("include_initial must be boolean")
        if any(not isinstance(item, str) or not item for item in self.event_types):
            raise ValueError("event_types must contain non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode, "include_initial": self.include_initial}
        if self.interval_s is not None:
            result["interval_s"] = float(self.interval_s)
        if self.max_points is not None:
            result["max_points"] = self.max_points
        if self.event_types:
            result["event_types"] = list(self.event_types)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "SamplingPolicy":
        value = value or {}
        return cls(
            mode=str(value.get("mode", "every_step")),
            interval_s=value.get("interval_s"),
            max_points=value.get("max_points"),
            include_initial=bool(value.get("include_initial", True)),
            event_types=tuple(str(item) for item in value.get("event_types", ())),
        )


@dataclass(frozen=True)
class TraceProvenance:
    """Portable link from a trace dataset back to a published run."""

    run_id: str | None = None
    manifest_sha256: str | None = None
    package_sha256: str | None = None
    source_commit: str | None = None
    producer: str = "pe_sim.traces"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.run_id is not None:
            _validate_identifier(self.run_id, "run_id")
        _validate_hash(self.manifest_sha256, "manifest_sha256")
        _validate_hash(self.package_sha256, "package_sha256")
        if self.source_commit is not None and (not isinstance(self.source_commit, str) or not self.source_commit.strip()):
            raise ValueError("source_commit must be a non-empty string when provided")
        if not isinstance(self.producer, str) or not self.producer.strip():
            raise ValueError("producer must be non-empty")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("provenance metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"producer": self.producer}
        for key in ("run_id", "manifest_sha256", "package_sha256", "source_commit"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.metadata:
            result["metadata"] = _thaw_json(self.metadata)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "TraceProvenance":
        value = value or {}
        return cls(
            run_id=value.get("run_id"),
            manifest_sha256=value.get("manifest_sha256"),
            package_sha256=value.get("package_sha256"),
            source_commit=value.get("source_commit"),
            producer=str(value.get("producer", "pe_sim.traces")),
            metadata=dict(value.get("metadata", {})),
        )


def _validate_value(definition: SignalDefinition, value: Any) -> Any:
    if definition.dtype == "float64":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"signal {definition.signal_id} requires a finite numeric value")
        return float(value)
    if definition.dtype == "int64":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"signal {definition.signal_id} requires an integer value")
        return int(value)
    if definition.dtype == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"signal {definition.signal_id} requires a boolean value")
        return bool(value)
    if not isinstance(value, str):
        raise ValueError(f"signal {definition.signal_id} requires a string value")
    return value


@dataclass(frozen=True)
class TraceSample:
    """One timestamped, sparse observation of registered signals."""

    time_s: float
    values: Mapping[str, Any]
    event: str | None = None
    quality: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.time_s)) or float(self.time_s) < 0:
            raise ValueError("trace sample time_s must be finite and non-negative")
        if not isinstance(self.values, Mapping):
            raise ValueError("trace sample values must be a mapping")
        if self.event is not None and (not isinstance(self.event, str) or not self.event):
            raise ValueError("trace event must be a non-empty string when provided")
        if not isinstance(self.quality, Mapping):
            raise ValueError("trace quality must be a mapping")
        for key, value in self.quality.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("trace quality keys and values must be strings")
        clean_values = _freeze_json(dict(self.values))
        clean_quality = _freeze_json(dict(self.quality))
        _json_safe(_thaw_json(clean_values))
        object.__setattr__(self, "values", clean_values)
        object.__setattr__(self, "quality", clean_quality)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"time_s": float(self.time_s), "values": _thaw_json(self.values)}
        if self.event is not None:
            result["event"] = self.event
        if self.quality:
            result["quality"] = _thaw_json(self.quality)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceSample":
        if not isinstance(value, Mapping):
            raise ValueError("trace sample must be an object")
        return cls(
            time_s=float(value.get("time_s")),
            values=dict(value.get("values", {})),
            event=value.get("event"),
            quality=dict(value.get("quality", {})),
        )


@dataclass(frozen=True)
class TraceDataset:
    """Immutable, validated trace collection ready for export or plotting."""

    signals: tuple[SignalDefinition, ...] = ()
    samples: tuple[TraceSample, ...] = ()
    sampling_policy: SamplingPolicy = field(default_factory=SamplingPolicy)
    provenance: TraceProvenance = field(default_factory=TraceProvenance)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported trace schema_version: {self.schema_version}")
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "samples", tuple(self.samples))
        if not isinstance(self.provenance, TraceProvenance):
            raise TypeError("trace provenance must be a TraceProvenance")
        object.__setattr__(self, "provenance", _freeze_provenance(self.provenance))
        registry = SignalRegistry(self.signals)
        previous_time: float | None = None
        for sample in self.samples:
            if not isinstance(sample, TraceSample):
                raise TypeError("samples must contain TraceSample objects")
            if previous_time is not None and sample.time_s < previous_time - 1e-12:
                raise ValueError("trace sample timestamps must be monotonic")
            previous_time = sample.time_s
            unknown = set(sample.values) - set(registry)
            if unknown:
                raise ValueError("trace sample contains unregistered signals: " + ", ".join(sorted(unknown)))
            for signal_id, value in sample.values.items():
                _validate_value(registry.get(signal_id), value)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("trace metadata must be a mapping")
        clean_metadata = _freeze_json(dict(self.metadata))
        _json_safe(_thaw_json(clean_metadata))
        object.__setattr__(self, "metadata", clean_metadata)

    @property
    def registry(self) -> SignalRegistry:
        return SignalRegistry(self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signals": [item.to_dict() for item in self.signals],
            "sampling_policy": self.sampling_policy.to_dict(),
            "provenance": self.provenance.to_dict(),
            "metadata": _thaw_json(self.metadata),
            "samples": [item.to_dict() for item in self.samples],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceDataset":
        if not isinstance(value, Mapping):
            raise ValueError("trace dataset must be an object")
        return cls(
            schema_version=str(value.get("schema_version", "")),
            signals=tuple(SignalDefinition.from_dict(item) for item in value.get("signals", ())),
            sampling_policy=SamplingPolicy.from_dict(value.get("sampling_policy")),
            provenance=TraceProvenance.from_dict(value.get("provenance")),
            metadata=dict(value.get("metadata", {})),
            samples=tuple(TraceSample.from_dict(item) for item in value.get("samples", ())),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "TraceDataset":
        return cls.from_dict(json.loads(value))


class TraceCollector:
    """Stateful collector enforcing registry, time, and sampling contracts."""

    def __init__(
        self,
        registry: SignalRegistry,
        *,
        sampling_policy: SamplingPolicy | None = None,
        provenance: TraceProvenance | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(registry, SignalRegistry):
            raise TypeError("registry must be a SignalRegistry")
        self.registry = registry
        self.sampling_policy = sampling_policy or SamplingPolicy()
        self.provenance = provenance or TraceProvenance()
        self.metadata = dict(metadata or {})
        _json_safe(self.metadata)
        self._samples: list[TraceSample] = []
        self._last_time_s: float | None = None
        self._last_recorded_time_s: float | None = None
        self.dropped_count = 0

    def record_to_sink(
        self,
        time_s: float,
        values: Mapping[str, Any],
        sink: "ObservationSink | None" = None,
        *,
        event: str | None = None,
        quality: Mapping[str, str] | None = None,
        force: bool = False,
    ) -> bool:
        """Record an observation and optionally publish the current dataset.

        This is an explicit integration point for a Runner or live viewer.
        With ``sink=None`` it is exactly equivalent to :meth:`record`.  A
        sink is called only when a sample was accepted, and receives an
        immutable, validated dataset snapshot.
        """

        accepted = self.record(time_s, values, event=event, quality=quality, force=force)
        if accepted and sink is not None:
            sink.publish(self.dataset())
        return accepted

    @property
    def samples(self) -> tuple[TraceSample, ...]:
        return tuple(self._samples)

    def record(
        self,
        time_s: float,
        values: Mapping[str, Any],
        *,
        event: str | None = None,
        quality: Mapping[str, str] | None = None,
        force: bool = False,
    ) -> bool:
        if not isinstance(values, Mapping):
            raise ValueError("trace values must be a mapping")
        time_s = float(time_s)
        if not math.isfinite(time_s) or time_s < 0:
            raise ValueError("trace time_s must be finite and non-negative")
        if self._last_time_s is not None and time_s < self._last_time_s - 1e-12:
            raise ValueError("trace collector timestamps must be monotonic")
        self._last_time_s = time_s
        unknown = set(values) - set(self.registry)
        if unknown:
            raise ValueError("trace values contain unregistered signals: " + ", ".join(sorted(unknown)))
        validated = {str(key): _validate_value(self.registry.get(str(key)), value) for key, value in values.items()}
        policy = self.sampling_policy
        should_record = force or policy.mode == "every_step"
        if policy.mode == "fixed_interval" and not force:
            should_record = self._last_recorded_time_s is None and policy.include_initial
            if self._last_recorded_time_s is not None:
                should_record = time_s - self._last_recorded_time_s >= float(policy.interval_s) - 1e-12
        elif policy.mode == "event" and not force:
            should_record = event is not None and (not policy.event_types or event in policy.event_types)
        elif policy.mode == "manual" and not force:
            should_record = False
        if not should_record:
            return False
        if policy.max_points is not None and len(self._samples) >= policy.max_points:
            self.dropped_count += 1
            return False
        sample = TraceSample(time_s, validated, event=event, quality=quality or {})
        self._samples.append(sample)
        self._last_recorded_time_s = time_s
        return True

    def record_observation(self, observation: Any, *, include_truth: bool = False, event: str | None = None, force: bool = False) -> bool:
        """Record a PlantObservation-like object without importing contracts."""

        if not hasattr(observation, "time_s") or not hasattr(observation, "measurement"):
            raise TypeError("observation must expose time_s and measurement")
        values = dict(observation.measurement)
        if include_truth and hasattr(observation, "truth"):
            values.update({str(key): value for key, value in observation.truth.items() if str(key) in self.registry})
        return self.record(float(observation.time_s), values, event=event, force=force)

    def dataset(self) -> TraceDataset:
        metadata = dict(self.metadata)
        metadata.setdefault("dropped_count", self.dropped_count)
        return TraceDataset(
            signals=self.registry.definitions,
            samples=tuple(self._samples),
            sampling_policy=self.sampling_policy,
            provenance=self.provenance,
            metadata=metadata,
        )


class ObservationSink(Protocol):
    """Opt-in consumer for validated trace snapshots.

    Implementations may update a local viewer, write a sidecar, or bridge to
    another process.  They must not mutate the supplied dataset.
    """

    def publish(self, dataset: TraceDataset) -> None:
        ...


# Short aliases make the contract easier to discover without duplicating types.
Signal = SignalDefinition
TracePoint = TraceSample

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "SignalDefinition",
    "Signal",
    "SignalRegistry",
    "SamplingPolicy",
    "TraceProvenance",
    "TraceSample",
    "TracePoint",
    "TraceDataset",
    "TraceCollector",
    "ObservationSink",
]
