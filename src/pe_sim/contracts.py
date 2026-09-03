"""Public contracts shared by plants, controllers and the runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
import math
import re
import hashlib
import json

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CURRENT_SCHEMA_VERSION = "0.1"


def _version_parts(version: str) -> tuple[int, int]:
    if not re.fullmatch(r"^[0-9]+\.[0-9]+$", str(version)):
        raise ValueError("schema_version must be MAJOR.MINOR")
    major, minor = str(version).split(".")
    return int(major), int(minor)


def ensure_schema_compatible(version: str, supported: str = CURRENT_SCHEMA_VERSION) -> None:
    """Reject versions whose meaning cannot be safely interpreted.

    A compatible document has the same major version and a minor version no
    newer than the reader.  Future minor versions are rejected fail-closed;
    callers must explicitly migrate them instead of silently dropping fields.
    """
    major, minor = _version_parts(version)
    supported_major, supported_minor = _version_parts(supported)
    if major != supported_major or minor > supported_minor:
        raise ValueError(f"unsupported schema_version {version}; supported through {supported}")


@dataclass(frozen=True)
class CapabilitySet:
    """Normalized component capabilities while retaining legacy set support."""

    names: frozenset[str] = frozenset()
    event_types: frozenset[str] = frozenset()
    control_period_s: float | None = None
    observation_period_s: float | None = None

    def __post_init__(self) -> None:
        for name, value in (("control_period_s", self.control_period_s), ("observation_period_s", self.observation_period_s)):
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"{name} must be finite and positive")

    def supports(self, name: str) -> bool:
        return name in self.names

    @classmethod
    def from_value(cls, value: Any) -> "CapabilitySet":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            names = value.get("names", value.get("features", value.get("capabilities", ())))
            events = value.get("event_types", ())
            if isinstance(names, str):
                names = (names,)
            if isinstance(events, str):
                events = (events,)
            return cls(frozenset(str(item) for item in names), frozenset(str(item) for item in events), value.get("control_period_s"), value.get("observation_period_s"))
        return cls(frozenset(str(item) for item in value))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"names": sorted(self.names)}
        if self.event_types:
            result["event_types"] = sorted(self.event_types)
        if self.control_period_s is not None:
            result["control_period_s"] = self.control_period_s
        if self.observation_period_s is not None:
            result["observation_period_s"] = self.observation_period_s
        return result


def negotiate_capabilities(
    plant_capabilities: Any,
    controller_capabilities: Any = None,
    timebase: "Timebase | None" = None,
    required: tuple[str, ...] | list[str] = (),
    event_schedule: tuple[Mapping[str, Any], ...] = (),
) -> tuple[CapabilitySet, CapabilitySet]:
    """Validate declared capabilities and return normalized plant/controller sets.

    Legacy components returning ``set[str]`` remain valid.  Requirements are
    explicit: an event schedule requires Plant ``events`` or ``event_driven``;
    ``plant:foo`` and ``controller:foo`` target one side, while an unqualified
    name may be provided by either component.  Missing capabilities fail
    closed with a deterministic error.
    """
    plant = CapabilitySet.from_value(plant_capabilities)
    controller = CapabilitySet.from_value(controller_capabilities)
    missing: list[str] = []
    for requirement in required:
        if ":" in requirement:
            owner, name = requirement.split(":", 1)
            supported = plant if owner == "plant" else controller if owner == "controller" else None
            if supported is None or not supported.supports(name):
                missing.append(requirement)
        elif not plant.supports(requirement) and not controller.supports(requirement):
            missing.append(requirement)
    # A normal command schedule (for example ``{"vref": 0.8}``) is not an
    # event stream.  Require an explicit event marker before applying the
    # event capability gate.
    has_explicit_events = any(
        isinstance(item, Mapping)
        and (item.get("event") not in (None, "") or item.get("event_type") not in (None, ""))
        for item in event_schedule
    )
    if has_explicit_events and not (plant.supports("events") or plant.supports("event_driven")):
        missing.append("plant:events")
    if has_explicit_events and plant.event_types:
        for item in event_schedule:
            if not isinstance(item, Mapping):
                continue
            event_name = item.get("event_type", item.get("event"))
            if event_name not in (None, "") and str(event_name) not in plant.event_types:
                missing.append(f"plant:event_type:{event_name}")
    if timebase is not None and timebase.plant_step_s is not None and timebase.plant_step_s != timebase.control_period_s:
        if not (plant.supports("continuous_time") or plant.supports("multirate_control")):
            missing.append("plant:multirate_control")
    if missing:
        raise ValueError("missing capabilities: " + ", ".join(sorted(set(missing))))
    return plant, controller


def validate_observation_visibility(observation: "PlantObservation", current_time_s: float, previous_time_s: float | None = None) -> None:
    """Ensure Controller never receives a future or regressing observation."""
    if observation.time_s > float(current_time_s) + 1e-12:
        raise ValueError("observation timestamp is in the future")
    if previous_time_s is not None and observation.time_s < previous_time_s - 1e-12:
        raise ValueError("observation timestamp regressed")


def snapshot_file_hash(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return a stable digest for in-memory snapshot equality checks."""
    payload = json.dumps(dict(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Timebase:
    unit: str = "s"
    control_period_s: float = 1e-3
    duration_s: float = 10e-3
    plant_step_s: float | None = None
    sample_offset_s: float = 0.0
    event_tolerance_s: float = 1e-12

    def __post_init__(self) -> None:
        if self.unit != "s":
            raise ValueError("timebase unit must be 's'")
        for name in ("control_period_s", "duration_s"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.plant_step_s is not None and (not math.isfinite(self.plant_step_s) or self.plant_step_s <= 0):
            raise ValueError("plant_step_s must be finite and positive")
        if not math.isfinite(self.sample_offset_s) or self.sample_offset_s < 0 or self.sample_offset_s > self.control_period_s:
            raise ValueError("sample_offset_s must lie in [0, control_period_s]")
        if not math.isfinite(self.event_tolerance_s) or self.event_tolerance_s < 0:
            raise ValueError("event_tolerance_s must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        result = {"unit": self.unit, "control_period_s": self.control_period_s, "duration_s": self.duration_s, "sample_offset_s": self.sample_offset_s, "event_tolerance_s": self.event_tolerance_s}
        if self.plant_step_s is not None:
            result["plant_step_s"] = self.plant_step_s
        return result


@dataclass(frozen=True)
class InitialState:
    mode: str = "cold_start"
    snapshot_path: str | None = None
    snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"cold_start", "warm_start", "qualified_snapshot"}:
            raise ValueError("unsupported initial state mode")
        if self.mode == "qualified_snapshot" and (not self.snapshot_path or not self.snapshot_hash):
            raise ValueError("qualified_snapshot requires snapshot_path and snapshot_hash")
        if self.snapshot_hash is not None and not _SHA256_RE.fullmatch(self.snapshot_hash):
            raise ValueError("snapshot_hash must be a SHA-256 hex digest")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "snapshot_path": self.snapshot_path, "snapshot_hash": self.snapshot_hash}


@dataclass(frozen=True)
class PlantObservation:
    time_s: float
    measurement: Mapping[str, float]
    truth: Mapping[str, float] = field(default_factory=dict)
    age_steps: int = 0
    measurement_units: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.time_s)):
            raise ValueError("observation time must be finite")
        if self.age_steps < 0:
            raise ValueError("age_steps must be non-negative")
        for values in (self.measurement, self.truth):
            for value in values.values():
                if not math.isfinite(float(value)):
                    raise ValueError("observation values must be finite")
        unknown_units = set(self.measurement_units) - set(self.measurement)
        if unknown_units:
            raise ValueError("measurement_units contains unknown fields")

    def controller_measurement(self) -> dict[str, float]:
        return {str(k): float(v) for k, v in self.measurement.items()}


@dataclass(frozen=True)
class ActionRequest:
    value: float
    unit: str = "normalized"
    produced_time_s: float = 0.0
    target_time_s: float = 0.0
    diagnostics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.value)):
            raise ValueError("action must be finite")
        if self.unit != "normalized":
            raise ValueError("unsupported action unit")
        if not math.isfinite(float(self.produced_time_s)) or not math.isfinite(float(self.target_time_s)):
            raise ValueError("action timestamps must be finite")
        if self.target_time_s < self.produced_time_s:
            raise ValueError("target_time_s cannot precede produced_time_s")


class PlantAdapter(Protocol):
    def capabilities(self) -> set[str]: ...
    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def observe(self) -> PlantObservation: ...
    def advance(self, action: float, duration: float, external_input: Mapping[str, Any] | None = None) -> PlantObservation: ...
    def snapshot(self) -> Mapping[str, Any]: ...
    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ControllerAdapter(Protocol):
    def reset(self, controller_state: Mapping[str, Any] | None, seed: int) -> Mapping[str, Any]: ...
    def observe(self, measurement: Mapping[str, float], command: Mapping[str, Any], timing: Mapping[str, Any], state: Mapping[str, Any]) -> ActionRequest: ...


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    run_id: str
    plant_id: str
    controller_id: str
    timebase: Timebase
    initial_state: InitialState = field(default_factory=InitialState)
    input_schedule: tuple[Mapping[str, Any], ...] = ()
    seed: int | None = 0
    contracts: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: {"safety": {"id": "default-safety", "hash": "0" * 64}, "qualification": {"id": "default-qualification", "hash": "0" * 64}})
    output_dir: str = "runs"
    result_retention: str = "keep"
    schema_version: str = "0.1"
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.run_id or not self.plant_id or not self.controller_id:
            raise ValueError("experiment and component identifiers are required")
        if self.seed is not None and (not isinstance(self.seed, int) or self.seed < 0):
            raise ValueError("seed must be a non-negative integer or null")
        ensure_schema_compatible(self.schema_version)
        if self.result_retention not in {"keep", "on_failure", "temporary"}:
            raise ValueError("unsupported result retention policy")
        for name in ("safety", "qualification"):
            ref = self.contracts.get(name)
            if not isinstance(ref, Mapping) or not _SHA256_RE.fullmatch(str(ref.get("hash", ""))):
                raise ValueError(f"contracts.{name} requires a SHA-256 hash")
        if any(not isinstance(item, str) or not item for item in self.required_capabilities):
            raise ValueError("required_capabilities must contain non-empty strings")
        for item in self.input_schedule:
            timestamp = float(item.get("time_s", 0.0))
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("invalid input schedule time")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "experiment_id": self.experiment_id, "run_id": self.run_id, "plant_id": self.plant_id, "controller_id": self.controller_id, "timebase": self.timebase.to_dict(), "initial_state": self.initial_state.to_dict(), "input_schedule": [dict(item) for item in self.input_schedule], "seed": self.seed, "contracts": {k: dict(v) for k, v in self.contracts.items()}, "output": {"directory": self.output_dir, "retention": self.result_retention}, "required_capabilities": list(self.required_capabilities)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperimentSpec":
        tb = value.get("timebase", {})
        initial = value.get("initial_state", {})
        output = value.get("output", {})
        contracts = value.get("contracts")
        if contracts is None:
            contracts = {
                "safety": {"id": value.get("safety_contract", "default-safety"), "hash": "0" * 64},
                "qualification": {"id": value.get("qualification_rule", "default-qualification"), "hash": "0" * 64},
            }
        return cls(
            experiment_id=str(value["experiment_id"]),
            run_id=str(value.get("run_id", value["experiment_id"])),
            plant_id=str(value["plant_id"]),
            controller_id=str(value["controller_id"]),
            timebase=Timebase(**dict(tb)),
            initial_state=InitialState(**dict(initial)),
            input_schedule=tuple(value.get("input_schedule", ())),
            seed=value.get("seed", 0),
            contracts=contracts,
            output_dir=str(output.get("directory", value.get("output_dir", "runs"))),
            result_retention=str(output.get("retention", "keep")),
            schema_version=str(value.get("schema_version", "0.1")),
            required_capabilities=tuple(str(item) for item in value.get("required_capabilities", ())),
        )
