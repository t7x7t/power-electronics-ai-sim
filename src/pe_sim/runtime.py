"""Deterministic runner, compatibility fixtures, and auditable lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
import json
import math
import time

from .artifacts import ArtifactWriter, artifact_index, canonical_json, sha256_bytes
from .contracts import ActionRequest, ExperimentSpec, PlantObservation, negotiate_capabilities, snapshot_file_hash, validate_observation_visibility
from .qualification import qualify_samples
from .provenance import collect_git_provenance
from .dirty import analyze_git_worktree, require_formal_comparison
from .safety import ActionPolicy, FiniteActionPolicy, check_observation, project_action
from .runner.audit import AuditRecorder
from .runner.lifecycle import LifecycleStateMachine
from .runner.policies import RunOptions


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    status: str
    qualification: Mapping[str, Any]


# Private aliases preserve the old implementation names for downstream code.
_Audit = AuditRecorder
_StateMachine = LifecycleStateMachine


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
        super().advance(action, duration)
        self.state -= load * duration
        return self.observe()


class FakePIController:
    def __init__(self, kp: float = 1.0):
        self.kp = float(kp)
        self.state: dict[str, Any] = {}

    def manifest_identity(self) -> dict[str, Any]:
        return {"kind": "fixture_controller", "module": type(self).__module__, "class": type(self).__qualname__, "parameters": {"kp": self.kp}, "capabilities": []}

    def reset(self, controller_state: Mapping[str, Any] | None, seed: int) -> Mapping[str, Any]:
        del seed
        self.state = dict(controller_state or {})
        return dict(self.state)

    def observe(self, measurement: Mapping[str, float], command: Mapping[str, Any], timing: Mapping[str, Any], state: Mapping[str, Any]) -> ActionRequest:
        del state
        key = str(command.get("measurement_key", "vout"))
        error = float(command.get("vref", 1.0)) - float(measurement[key])
        now = float(timing["sample_time_s"])
        return ActionRequest(value=self.kp * error, produced_time_s=now, target_time_s=now)


class Runner:
    """Run a Plant/Controller pair with explicit timing and evidence."""
    def run(self, spec: ExperimentSpec, plant: Any, controller: Any, mode: str = "exploratory", *, checkpoint_interval_steps: int | None = None, resume_from: str | Path | None = None, interrupt_after_steps: int | None = None, measurement_key: str | None = None, safety_checker: Callable[[Mapping[str, float]], Any] | None = None, qualification_checker: Callable[[list[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None, action_policy: ActionPolicy | None = None, options: RunOptions | None = None) -> RunResult:
        if options is not None:
            if not isinstance(options, RunOptions):
                raise TypeError("options must be a RunOptions instance")
            options.validate(spec)
            if mode == "exploratory":
                mode = options.mode
            elif mode != options.mode:
                raise ValueError("mode conflicts with RunOptions.mode")
            values = {
                "checkpoint_interval_steps": checkpoint_interval_steps,
                "resume_from": resume_from,
                "interrupt_after_steps": interrupt_after_steps,
                "measurement_key": measurement_key,
                "safety_checker": safety_checker,
                "qualification_checker": qualification_checker,
                "action_policy": action_policy,
            }
            policy_values = {
                "checkpoint_interval_steps": options.recovery.checkpoint_interval_steps,
                "resume_from": options.recovery.resume_from,
                "interrupt_after_steps": options.recovery.interrupt_after_steps,
                "measurement_key": options.measurement_key,
                "safety_checker": options.safety_checker,
                "qualification_checker": options.qualification_checker,
                "action_policy": options.action_policy,
            }
            for name, explicit in values.items():
                selected = policy_values[name]
                if explicit is not None and selected is not None and explicit != selected:
                    raise ValueError(f"{name} conflicts with RunOptions")
                if explicit is None:
                    values[name] = selected
            checkpoint_interval_steps = values["checkpoint_interval_steps"]
            resume_from = values["resume_from"]
            interrupt_after_steps = values["interrupt_after_steps"]
            measurement_key = values["measurement_key"]
            safety_checker = values["safety_checker"]
            qualification_checker = values["qualification_checker"]
            action_policy = values["action_policy"]
            if not options.timing.allow_target_time:
                # Enforced when each ActionRequest is validated below.
                target_time_allowed = False
            else:
                target_time_allowed = True
        else:
            target_time_allowed = True
        if mode not in {"exploratory", "formal_comparison"}:
            raise ValueError("mode must be 'exploratory' or 'formal_comparison'")
        if checkpoint_interval_steps is not None and checkpoint_interval_steps <= 0:
            raise ValueError("checkpoint_interval_steps must be positive")
        if interrupt_after_steps is not None and interrupt_after_steps <= 0:
            raise ValueError("interrupt_after_steps must be positive")
        if action_policy is not None and not callable(getattr(action_policy, "project", None)):
            raise TypeError("action_policy must provide project(value)")
        selected_action_policy = action_policy
        if selected_action_policy is None:
            factory = getattr(plant, "action_policy", None)
            if callable(getattr(factory, "project", None)):
                selected_action_policy = factory
            elif callable(factory):
                selected_action_policy = factory()
            else:
                selected_action_policy = FiniteActionPolicy()
        if not callable(getattr(selected_action_policy, "project", None)):
            raise TypeError("selected action policy must provide project(value)")
        worktree = analyze_git_worktree()
        if mode == "formal_comparison":
            require_formal_comparison(worktree)
        git = collect_git_provenance()
        writer = ArtifactWriter(spec.output_dir, spec.run_id)
        audit, machine = _Audit(enabled=options.audit.enabled if options is not None else True), _StateMachine()
        machine.transition("RUNNING", "run_started")
        samples: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        error: str | None = None
        failure: dict[str, Any] | None = None
        qualification: dict[str, Any] = {"passed": False, "reasons": ["not_run"]}
        capabilities: tuple[Any, Any] | None = None
        next_step_index, last_action, current_time = 0, 0.0, 0.0
        controller_state: Mapping[str, Any] = {}
        try:
            if not callable(getattr(plant, "capabilities", None)):
                raise TypeError("plant missing capabilities()")
            plant_caps = audit.call("plant", "capabilities", lambda: plant.capabilities())
            controller_caps = audit.call("controller", "capabilities", lambda: controller.capabilities()) if callable(getattr(controller, "capabilities", None)) else None
            capabilities = negotiate_capabilities(plant_caps, controller_caps, spec.timebase, spec.required_capabilities, spec.input_schedule)
            checkpoint = _load_checkpoint(resume_from) if resume_from is not None else None
            if checkpoint is not None:
                if checkpoint.get("experiment_id") != spec.experiment_id or checkpoint.get("plant_id") != spec.plant_id or checkpoint.get("controller_id") != spec.controller_id:
                    raise ValueError("checkpoint identity does not match experiment")
                if checkpoint.get("checkpoint_hash"):
                    expected = str(checkpoint["checkpoint_hash"])
                    unsigned = dict(checkpoint)
                    unsigned.pop("checkpoint_hash", None)
                    if sha256_bytes(canonical_json(unsigned)) != expected:
                        raise ValueError("checkpoint hash mismatch")
                if checkpoint.get("plant_hash") and checkpoint["plant_hash"] != _component_manifest(spec.plant_id, plant)["hash"]:
                    raise ValueError("checkpoint plant identity mismatch")
                if checkpoint.get("controller_hash") and checkpoint["controller_hash"] != _component_manifest(spec.controller_id, controller)["hash"]:
                    raise ValueError("checkpoint controller identity mismatch")
                audit.call("plant", "restore", lambda: plant.restore(checkpoint["plant_snapshot"]), step_index=checkpoint.get("next_step_index"))
                controller_state = checkpoint.get("controller_state", {})
                if callable(getattr(controller, "restore", None)):
                    audit.call("controller", "restore", lambda: controller.restore(controller_state), step_index=checkpoint.get("next_step_index"))
                else:
                    controller_state = audit.call("controller", "reset", lambda: controller.reset(controller_state, spec.seed if spec.seed is not None else 0))
                samples, events = list(checkpoint.get("samples", [])), list(checkpoint.get("events", []))
                next_step_index, last_action, current_time = int(checkpoint.get("next_step_index", len(samples))), float(checkpoint.get("last_action", 0.0)), float(checkpoint.get("time_s", 0.0))
                events.append({"event": "resume", "from_step_index": next_step_index, "time_s": current_time})
            else:
                if spec.initial_state.mode == "qualified_snapshot":
                    snapshot_path = Path(spec.initial_state.snapshot_path)
                    if snapshot_file_hash(str(snapshot_path)) != spec.initial_state.snapshot_hash:
                        raise ValueError("qualified snapshot hash mismatch")
                    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    if not isinstance(snapshot, Mapping):
                        raise ValueError("qualified snapshot must contain an object")
                    audit.call("plant", "restore", lambda: plant.restore(snapshot))
                else:
                    if not callable(getattr(plant, "reset", None)):
                        raise TypeError("plant missing reset()")
                    audit.call("plant", "reset", lambda: plant.reset(spec.initial_state.to_dict()))
                if not callable(getattr(controller, "reset", None)):
                    raise TypeError("controller missing reset()")
                controller_state = audit.call("controller", "reset", lambda: controller.reset(None, spec.seed if spec.seed is not None else 0))
            # Restored/qualified snapshots may carry a non-zero simulation
            # clock. Discover that clock before constructing the first window.
            if resume_from is None:
                initial_observation = audit.call("plant", "observe", lambda: plant.observe())
                if not isinstance(initial_observation, PlantObservation):
                    raise TypeError("plant.observe() must return PlantObservation")
                current_time = float(initial_observation.time_s)
            n_steps = int(math.ceil(spec.timebase.duration_s / spec.timebase.control_period_s))
            previous_observation_time: float | None = None
            primary_key = measurement_key or str(spec.plant_config.get("primary_measurement", spec.controller_config.get("measurement_key", "vout")))
            for index in range(next_step_index, n_steps):
                cycle_start = current_time
                offset = spec.timebase.sample_offset_s
                if offset > spec.timebase.event_tolerance_s:
                    _advance_segments(plant, last_action, offset, _command_at(spec.input_schedule, cycle_start), spec.timebase.plant_step_s, audit, index)
                    current_time += offset
                obs = audit.call("plant", "observe", lambda: plant.observe(), step_index=index, time_s=current_time)
                if not isinstance(obs, PlantObservation):
                    raise TypeError("plant.observe() must return PlantObservation")
                check_fn = safety_checker or check_observation
                audit.call("safety", "check_observation", lambda: check_fn(obs.measurement), step_index=index, time_s=obs.time_s)
                check_time = float(obs.time_s)
                validate_observation_visibility(obs, check_time, previous_observation_time)
                previous_observation_time, current_time = check_time, check_time
                command = _command_at(spec.input_schedule, current_time)
                controller_command = dict(command)
                controller_command.setdefault("measurement_key", primary_key)
                timing = {"sample_time_s": current_time, "age_steps": obs.age_steps, "control_period_s": spec.timebase.control_period_s, "plant_step_s": spec.timebase.plant_step_s or spec.timebase.control_period_s, "sample_offset_s": offset, "step_index": index}
                request = audit.call("controller", "observe", lambda: controller.observe(obs.controller_measurement(), controller_command, timing, controller_state), step_index=index, time_s=current_time)
                if not isinstance(request, ActionRequest):
                    raise TypeError("controller must return ActionRequest")
                endpoint, tolerance = cycle_start + spec.timebase.control_period_s, spec.timebase.event_tolerance_s
                if request.produced_time_s < current_time - tolerance or request.produced_time_s > endpoint + tolerance or request.target_time_s < current_time - tolerance or request.target_time_s > endpoint + tolerance:
                    raise ValueError("action timestamp violates timebase")
                if not target_time_allowed and abs(float(request.target_time_s) - current_time) > tolerance:
                    raise ValueError("timing policy disallows delayed target-time actions")
                action, clamp_reason = audit.call("safety", "project_action", lambda: selected_action_policy.project(request.value), step_index=index, time_s=current_time)
                target = min(max(float(request.target_time_s), current_time), endpoint)
                hold = max(0.0, target - current_time)
                if hold > tolerance:
                    _advance_segments(plant, last_action, hold, command, spec.timebase.plant_step_s, audit, index)
                remaining = max(0.0, endpoint - max(target, current_time))
                _advance_segments(plant, action, remaining, command, spec.timebase.plant_step_s, audit, index)
                next_obs = audit.call("plant", "observe", lambda: plant.observe(), step_index=index, time_s=endpoint)
                if next_obs.time_s <= current_time or not math.isfinite(next_obs.time_s):
                    raise ValueError("plant time did not advance")
                if next_obs.time_s > endpoint + tolerance:
                    raise ValueError("plant advanced beyond the requested time window")
                row = {"step_index": index, "time_s": current_time, "action": action, "action_requested": request.value, "clamp_reason": clamp_reason}
                row.update({str(key): float(value) for key, value in obs.measurement.items()})
                samples.append(row)
                events.append({"step_index": index, "sample_time_s": current_time, "action_time_s": request.target_time_s, "next_time_s": next_obs.time_s, "action_applied_time_s": target})
                current_time, last_action = float(next_obs.time_s), action
                if checkpoint_interval_steps and (index + 1) % checkpoint_interval_steps == 0:
                    _write_checkpoint(writer, spec, plant, controller, controller_state, samples, events, index + 1, current_time, last_action, audit)
                if interrupt_after_steps is not None and len(samples) >= interrupt_after_steps:
                    raise KeyboardInterrupt(f"interrupted after {len(samples)} steps")
            qualification_fn = qualification_checker or qualify_samples
            ok, reasons = audit.call("qualification", "check", lambda: qualification_fn(samples), step_index=n_steps, time_s=current_time)
            qualification = {"passed": bool(ok), "reasons": list(reasons)}
            machine.transition("RUN_OK", "completed", step_index=n_steps, time_s=current_time)
        except KeyboardInterrupt as exc:
            error, failure = f"{type(exc).__name__}: {exc}", {"category": "interrupted", "exception_type": type(exc).__name__, "message": str(exc)}
            machine.transition("INCOMPLETE", "interrupted", step_index=len(samples), time_s=current_time)
            _write_checkpoint(writer, spec, plant, controller, controller_state, samples, events, len(samples), current_time, last_action, audit)
            qualification = {"passed": False, "reasons": ["incomplete", "interrupted"]}
        except Exception as exc:
            error, failure = f"{type(exc).__name__}: {exc}", {"category": getattr(exc, "category", "runtime_error"), "exception_type": type(exc).__name__, "message": str(exc)}
            machine.transition("RUN_FAILED", failure["category"], step_index=len(samples), time_s=current_time)
            qualification = {"passed": False, "reasons": [failure["category"]]}
        status = machine.status
        writer.write_json("config.snapshot.json", spec.to_dict())
        writer.write_json("environment.json", {"python": "unknown", "runner": "pe_sim"})
        writer.write_json("qualification.json", qualification)
        writer.write_json("safety.json", {"passed": error is None, "error": error})
        writer.write_json("metrics.json", {"sample_count": len(samples), "audit_call_count": len(audit.calls)})
        writer.write_json("events.json", events)
        writer.write_json("state_transitions.json", machine.transitions)
        writer.write_json("audit.json", audit.to_dict())
        writer.write_json("samples.json", samples)
        try:
            import numpy as np
            numeric_keys = [key for key in ("step_index", "time_s", "vout", "action", "action_requested") if samples and all(key in row for row in samples)]
            if numeric_keys:
                np.savez(writer.tmp / "samples.npz", **{key: np.asarray([row[key] for row in samples]) for key in numeric_keys})
        except Exception:
            pass
        worktree_manifest = worktree.to_dict()
        worktree_manifest.pop("repo_root", None)
        manifest = {"schema_version": spec.schema_version, "experiment_id": spec.experiment_id, "run_id": spec.run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **git.to_manifest_fields(), "worktree_analysis": worktree_manifest, "run_mode": mode, "environment": {"runner": "pe_sim", "git": {"source_commit": git.source_commit, "branch": git.branch, "working_tree_status": git.working_tree_status}}, "plant": _component_manifest(spec.plant_id, plant, capabilities[0] if capabilities else None), "controller": _component_manifest(spec.controller_id, controller, capabilities[1] if capabilities else None), "action_policy": _action_policy_manifest(selected_action_policy), "contracts": {k: v["hash"] for k, v in spec.contracts.items()}, "timebase": spec.timebase.to_dict(), "initial_state": spec.initial_state.to_dict(), "random_seed": spec.seed, "artifacts": {}, "status": status, "qualification": qualification, "safety": {"passed": error is None}, "evidence_level": "functional", "error": error, "failure": failure, "capabilities": {"plant": capabilities[0].to_dict(), "controller": capabilities[1].to_dict()} if capabilities else {}, "audit": {"call_count": len(audit.calls)}, "state_transitions": machine.transitions}
        writer.write_json("logs/run.json", {"status": status, "error": error, "sample_count": len(samples)})
        writer.write_json("manifest.json", manifest)
        run_dir = writer.finalize()
        manifest["artifacts"] = artifact_index(run_dir)
        (run_dir / "manifest.json").write_bytes(canonical_json(manifest))
        return RunResult(run_dir, status, qualification)


def _advance_segments(plant: Any, action: float, duration: float, external: Mapping[str, Any], plant_step: float | None, audit: _Audit, step_index: int) -> None:
    if duration <= 1e-15:
        return
    quantum, remaining = float(plant_step or duration), float(duration)
    while remaining > 1e-15:
        segment = min(quantum, remaining)
        audit.call("plant", "advance", lambda segment=segment: plant.advance(action, segment, external_input=external), step_index=step_index)
        remaining -= segment


def _write_checkpoint(writer: ArtifactWriter, spec: ExperimentSpec, plant: Any, controller: Any, controller_state: Mapping[str, Any], samples: list[dict[str, Any]], events: list[dict[str, Any]], next_step_index: int, time_s: float, last_action: float, audit: _Audit) -> None:
    if not callable(getattr(plant, "snapshot", None)):
        raise TypeError("plant missing snapshot() for checkpoint")
    snapshot = audit.call("plant", "snapshot", lambda: plant.snapshot(), step_index=next_step_index, time_s=time_s)
    if callable(getattr(controller, "snapshot", None)):
        controller_state = audit.call("controller", "snapshot", lambda: controller.snapshot(), step_index=next_step_index, time_s=time_s)
    checkpoint = {"schema_version": spec.schema_version, "experiment_id": spec.experiment_id, "plant_id": spec.plant_id, "controller_id": spec.controller_id, "plant_hash": _component_manifest(spec.plant_id, plant)["hash"], "controller_hash": _component_manifest(spec.controller_id, controller)["hash"], "contract_hashes": {key: value["hash"] for key, value in spec.contracts.items()}, "next_step_index": next_step_index, "time_s": time_s, "last_action": last_action, "plant_snapshot": dict(snapshot), "controller_state": dict(controller_state or {}), "samples": samples, "events": events}
    checkpoint["checkpoint_hash"] = sha256_bytes(canonical_json(checkpoint))
    writer.write_json("checkpoint.json", checkpoint)
    writer.write_json(f"checkpoints/step-{next_step_index:06d}.json", checkpoint)


def _load_checkpoint(source: str | Path | None) -> dict[str, Any] | None:
    if source is None:
        return None
    path = Path(source)
    if path.is_dir():
        direct = path / "checkpoint.json"
        if direct.exists():
            path = direct
        else:
            candidates = sorted((path / "checkpoints").glob("step-*.json"))
            if not candidates:
                raise FileNotFoundError("no checkpoint found")
            path = candidates[-1]
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint must contain an object")
    return dict(value)


def _component_manifest(component_id: str, component: Any, declared_capabilities: Any = None) -> dict[str, Any]:
    identity_fn = getattr(component, "manifest_identity", None)
    if callable(identity_fn):
        identity = dict(identity_fn())
    else:
        identity = {"kind": "runtime_component", "module": type(component).__module__, "class": type(component).__qualname__}
        if declared_capabilities is not None:
            normalized = declared_capabilities.to_dict() if hasattr(declared_capabilities, "to_dict") else declared_capabilities
            identity["capabilities"] = normalized
    return {"id": component_id, "hash": sha256_bytes(canonical_json(identity)), "identity": identity}


def _action_policy_manifest(policy: ActionPolicy) -> dict[str, Any]:
    """Serialize policy identity without requiring custom policies to be JSONable."""
    identity: dict[str, Any] = {
        "module": type(policy).__module__,
        "class": type(policy).__qualname__,
    }
    for name in ("name", "minimum", "maximum"):
        if hasattr(policy, name):
            value = getattr(policy, name)
            if isinstance(value, (str, int, float, bool)) or value is None:
                identity[name] = value
    return {"hash": sha256_bytes(canonical_json(identity)), "identity": identity}


def _command_at(schedule: tuple[Mapping[str, Any], ...], time_s: float) -> dict[str, Any]:
    command: dict[str, Any] = {"vref": 1.0}
    for item in sorted(schedule, key=lambda x: float(x.get("time_s", 0.0))):
        start = float(item.get("time_s", 0.0))
        if not math.isfinite(start) or start < 0:
            raise ValueError("invalid input schedule time")
        if start <= time_s + 1e-12:
            command.update({k: v for k, v in item.items() if k != "time_s"})
    return command
