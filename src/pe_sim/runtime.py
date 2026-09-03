"""Deterministic minimal runner and fake backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import math
import time
import json

from .artifacts import ArtifactWriter, artifact_index
from .contracts import ActionRequest, ExperimentSpec, PlantObservation
from .qualification import qualify_samples
from .provenance import collect_git_provenance
from .safety import check_observation, project_action


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    status: str
    qualification: Mapping[str, Any]


class FakePlant:
    """First-order plant exposing truth separately from controller measurements."""

    def __init__(self, gain: float = 1.0, tau_s: float = 0.01):
        self.gain, self.tau_s = float(gain), float(tau_s)
        self.time_s, self.state = 0.0, 0.0

    def capabilities(self) -> set[str]:
        return {"continuous_time"}

    def reset(self, initial_state: Mapping[str, Any]) -> Mapping[str, Any]:
        self.time_s = 0.0
        self.state = float(initial_state.get("value", 0.0))
        return {"value": self.state}

    def observe(self) -> PlantObservation:
        return PlantObservation(self.time_s, {"vout": self.state}, {"vout": self.state, "secret_gain": self.gain}, 0)

    def advance(self, action: float, duration: float, external_input: Mapping[str, Any] | None = None) -> PlantObservation:
        if duration <= 0 or not math.isfinite(duration):
            raise ValueError("duration must be positive and finite")
        target = self.gain * float(action)
        alpha = 1.0 - math.exp(-duration / self.tau_s)
        self.state += alpha * (target - self.state)
        self.time_s += duration
        return self.observe()

    def snapshot(self) -> Mapping[str, Any]:
        return {"time_s": self.time_s, "value": self.state}

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        self.time_s, self.state = float(snapshot["time_s"]), float(snapshot["value"])
        return {"value": self.state}


class FakeLoadPlant(FakePlant):
    """Second generic backend: first-order plant with a scheduled load disturbance."""

    def advance(self, action: float, duration: float, external_input: Mapping[str, Any] | None = None) -> PlantObservation:
        load = float((external_input or {}).get("load", 0.0))
        if not math.isfinite(load):
            raise ValueError("non-finite external input")
        result = super().advance(action, duration)
        self.state -= load * duration
        return self.observe()


class FakePIController:
    def __init__(self, kp: float = 1.0):
        self.kp = float(kp)
        self.state: dict[str, Any] = {}

    def reset(self, controller_state: Mapping[str, Any] | None, seed: int) -> Mapping[str, Any]:
        del seed
        self.state = dict(controller_state or {})
        return dict(self.state)

    def observe(self, measurement: Mapping[str, float], command: Mapping[str, Any], timing: Mapping[str, Any], state: Mapping[str, Any]) -> ActionRequest:
        del state
        error = float(command.get("vref", 1.0)) - float(measurement["vout"])
        value = self.kp * error
        now = float(timing["sample_time_s"])
        return ActionRequest(value=value, produced_time_s=now, target_time_s=now)


class Runner:
    def run(self, spec: ExperimentSpec, plant: Any, controller: Any) -> RunResult:
        # Capture the source baseline before this run creates any artifacts.
        git = collect_git_provenance()
        writer = ArtifactWriter(spec.output_dir, spec.run_id)
        status = "RUNNING"
        samples: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        error: str | None = None
        try:
            if not hasattr(plant, "capabilities"):
                raise TypeError("plant missing capabilities()")
            if spec.initial_state.mode == "qualified_snapshot":
                snapshot = json.loads(Path(spec.initial_state.snapshot_path).read_text(encoding="utf-8"))
                plant.restore(snapshot)
            else:
                plant.reset(spec.initial_state.to_dict())
            controller_state = controller.reset(None, spec.seed if spec.seed is not None else 0)
            n_steps = int(math.ceil(spec.timebase.duration_s / spec.timebase.control_period_s))
            for index in range(n_steps):
                obs = plant.observe()
                check_observation(obs.measurement)
                now = float(obs.time_s)
                if not hasattr(controller, "observe"):
                    raise TypeError("controller missing observe()")
                command = _command_at(spec.input_schedule, now)
                request = controller.observe(obs.controller_measurement(), command, {"sample_time_s": now, "age_steps": obs.age_steps, "control_period_s": spec.timebase.control_period_s}, controller_state)
                if not isinstance(request, ActionRequest):
                    raise TypeError("controller must return ActionRequest")
                endpoint = now + spec.timebase.control_period_s
                if request.produced_time_s < now - 1e-12 or request.target_time_s < request.produced_time_s - 1e-12 or request.target_time_s > endpoint + 1e-12:
                    raise ValueError("action timestamp violates timebase")
                action, clamp_reason = project_action(request.value)
                next_obs = plant.advance(action, spec.timebase.control_period_s, external_input=command)
                if next_obs.time_s <= now or not math.isfinite(next_obs.time_s):
                    raise ValueError("plant time did not advance")
                samples.append({"step_index": index, "time_s": now, "vout": obs.measurement["vout"], "action": action, "action_requested": request.value, "clamp_reason": clamp_reason})
                events.append({"step_index": index, "sample_time_s": now, "action_time_s": request.target_time_s, "next_time_s": next_obs.time_s})
            ok, reasons = qualify_samples(samples)
            status = "RUN_OK" if ok else "RUN_FAILED"
            qualification = {"passed": ok, "reasons": reasons}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = "RUN_FAILED"
            qualification = {"passed": False, "reasons": ["runtime_error"]}
        writer.write_json("config.snapshot.json", spec.to_dict())
        writer.write_json("environment.json", {"python": "unknown", "runner": "pe_sim"})
        writer.write_json("qualification.json", qualification)
        writer.write_json("safety.json", {"passed": error is None, "error": error})
        writer.write_json("metrics.json", {"sample_count": len(samples)})
        writer.write_json("events.json", events)
        writer.write_json("samples.json", samples)
        writer.write_json("logs/run.json", {"status": status, "error": error, "sample_count": len(samples)})
        try:
            import numpy as np
            np.savez(writer.tmp / "samples.npz", **{key: np.asarray([row[key] for row in samples]) for key in ("step_index", "time_s", "vout", "action", "action_requested")})
        except Exception:
            # JSON remains the canonical artifact when NumPy is unavailable.
            pass
        manifest = {"schema_version": spec.schema_version, "experiment_id": spec.experiment_id, "run_id": spec.run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **git.to_manifest_fields(), "environment": {"runner": "pe_sim", "git": {"source_commit": git.source_commit, "branch": git.branch, "working_tree_status": git.working_tree_status}}, "plant": {"id": spec.plant_id, "hash": "0" * 64}, "controller": {"id": spec.controller_id, "hash": "0" * 64}, "contracts": {k: v["hash"] for k, v in spec.contracts.items()}, "timebase": spec.timebase.to_dict(), "initial_state": spec.initial_state.to_dict(), "random_seed": spec.seed, "artifacts": {}, "status": status, "qualification": qualification, "safety": {"passed": error is None}, "evidence_level": "functional", "error": error}
        writer.write_json("manifest.json", manifest)
        run_dir = writer.finalize()
        # Index is written after publication, then manifest is atomically replaced.
        manifest["artifacts"] = artifact_index(run_dir)
        (run_dir / "manifest.json").write_bytes(__import__("pe_sim.artifacts", fromlist=["canonical_json"]).canonical_json(manifest))
        return RunResult(run_dir, status, qualification)


def _command_at(schedule: tuple[Mapping[str, Any], ...], time_s: float) -> dict[str, Any]:
    command: dict[str, Any] = {"vref": 1.0}
    for item in sorted(schedule, key=lambda x: float(x.get("time_s", 0.0))):
        start = float(item.get("time_s", 0.0))
        if not math.isfinite(start) or start < 0:
            raise ValueError("invalid input schedule time")
        if start <= time_s + 1e-12:
            command.update({k: v for k, v in item.items() if k != "time_s"})
    return command
