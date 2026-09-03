"""Public contracts shared by plants, controllers and the runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
import math
import re

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Timebase:
    unit: str = "s"
    control_period_s: float = 1e-3
    duration_s: float = 10e-3
    plant_step_s: float | None = None
    sample_offset_s: float = 0.0

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

    def to_dict(self) -> dict[str, Any]:
        result = {"unit": self.unit, "control_period_s": self.control_period_s, "duration_s": self.duration_s, "sample_offset_s": self.sample_offset_s}
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

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.time_s)):
            raise ValueError("observation time must be finite")
        if self.age_steps < 0:
            raise ValueError("age_steps must be non-negative")
        for values in (self.measurement, self.truth):
            for value in values.values():
                if not math.isfinite(float(value)):
                    raise ValueError("observation values must be finite")

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

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.run_id or not self.plant_id or not self.controller_id:
            raise ValueError("experiment and component identifiers are required")
        if self.seed is not None and (not isinstance(self.seed, int) or self.seed < 0):
            raise ValueError("seed must be a non-negative integer or null")
        if not re.fullmatch(r"^[0-9]+\.[0-9]+$", self.schema_version):
            raise ValueError("schema_version must be MAJOR.MINOR")
        if self.result_retention not in {"keep", "on_failure", "temporary"}:
            raise ValueError("unsupported result retention policy")
        for name in ("safety", "qualification"):
            ref = self.contracts.get(name)
            if not isinstance(ref, Mapping) or not _SHA256_RE.fullmatch(str(ref.get("hash", ""))):
                raise ValueError(f"contracts.{name} requires a SHA-256 hash")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "experiment_id": self.experiment_id, "run_id": self.run_id, "plant_id": self.plant_id, "controller_id": self.controller_id, "timebase": self.timebase.to_dict(), "initial_state": self.initial_state.to_dict(), "input_schedule": [dict(item) for item in self.input_schedule], "seed": self.seed, "contracts": {k: dict(v) for k, v in self.contracts.items()}, "output": {"directory": self.output_dir, "retention": self.result_retention}}

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
        )
