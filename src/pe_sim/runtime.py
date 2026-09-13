"""Deterministic runner, compatibility fixtures, and auditable lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
import json
import math
import time

from .artifacts import (
    ArtifactWriter,
    artifact_index,
    canonical_json,
    manifest_digest,
    package_digest,
    scan_abandoned_runs,
    sha256_bytes,
    validate_run_location,
)
from .contracts import ActionRequest, ExperimentSpec, PlantObservation, SimulationCancelledError, negotiate_capabilities, snapshot_file_hash, validate_observation_visibility
from .qualification import qualify_samples, BasicQualificationPlugin
from .provenance import collect_environment_provenance, collect_git_provenance
from .dirty import analyze_git_worktree, require_formal_comparison
from .safety import ActionPolicy, FiniteActionPolicy, check_observation, project_action
from .runner.audit import AuditRecorder
from .runner.lifecycle import LifecycleStateMachine
from .runner.policies import RunOptions
from .checks import (
    CheckContext,
    CheckFailureError,
    CheckReport,
    DataValidityError,
    SafetyCheckError,
    ObservationValidityPlugin,
    normalise_check_result,
    plugin_identity,
    run_plugin,
)


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    status: str
    qualification: Mapping[str, Any]


# Private aliases preserve the old implementation names for downstream code.
_Audit = AuditRecorder
_StateMachine = LifecycleStateMachine

# Failures that belong in the observation validity evidence even when an
# adapter raises before it can return a PlantObservation instance.
_OBSERVATION_FAILURE_CATEGORIES = frozenset(
    {
        "nonfinite_observation",
        "invalid_measurement",
        "timestamp_violation",
        "future_observation",
        "stale_observation",
        "invalid_observation_age",
        "missing_measurement",
        "unit_mismatch",
        "measurement_out_of_range",
    }
)


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

    def backend_provenance(self) -> dict[str, Any]:
        """Declare the project-owned deterministic reference-controller path.

        The environment collector treats every participating component as a
        provenance source.  This records the controller's numerical identity
        without pretending it is an external circuit solver.
        """
        return {
            "status": "known",
            "name": "pe_sim.reference_pi_controller",
            "version": "reference-v1",
            "solver_settings": {"arithmetic": "IEEE-754-binary64", "state": "stateless-proportional"},
            "limitations": ["project-owned reference controller; not a tuning or deployment claim"],
        }

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
    def run(self, spec: ExperimentSpec, plant: Any, controller: Any, mode: str = "exploratory", *, checkpoint_interval_steps: int | None = None, resume_from: str | Path | None = None, interrupt_after_steps: int | None = None, cancel_after_steps: int | None = None, measurement_key: str | None = None, safety_checker: Callable[[Mapping[str, float]], Any] | None = None, qualification_checker: Callable[[list[Mapping[str, Any]]], tuple[bool, list[str]]] | None = None, action_policy: ActionPolicy | None = None, safety_plugins: tuple[Any, ...] | None = None, validity_plugins: tuple[Any, ...] | None = None, qualification_plugins: tuple[Any, ...] | None = None, options: RunOptions | None = None) -> RunResult:
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
                "cancel_after_steps": cancel_after_steps,
                "measurement_key": measurement_key,
                "safety_checker": safety_checker,
                "qualification_checker": qualification_checker,
                "action_policy": action_policy,
                "safety_plugins": safety_plugins,
                "validity_plugins": validity_plugins,
                "qualification_plugins": qualification_plugins,
            }
            policy_values = {
                "checkpoint_interval_steps": options.recovery.checkpoint_interval_steps,
                "resume_from": options.recovery.resume_from,
                "interrupt_after_steps": options.recovery.interrupt_after_steps,
                "cancel_after_steps": options.recovery.cancel_after_steps,
                "measurement_key": options.measurement_key,
                "safety_checker": options.safety_checker,
                "qualification_checker": options.qualification_checker,
                "action_policy": options.action_policy,
                "safety_plugins": options.safety_plugins,
                "validity_plugins": options.validity_plugins,
                "qualification_plugins": options.qualification_plugins,
            }
            for name, explicit in values.items():
                selected = policy_values[name]
                empty_plugin_selection = name.endswith("_plugins") and selected == ()
                if explicit is not None and selected is not None and not empty_plugin_selection and explicit != selected:
                    raise ValueError(f"{name} conflicts with RunOptions")
                if explicit is None:
                    values[name] = selected
            checkpoint_interval_steps = values["checkpoint_interval_steps"]
            resume_from = values["resume_from"]
            interrupt_after_steps = values["interrupt_after_steps"]
            cancel_after_steps = values["cancel_after_steps"]
            measurement_key = values["measurement_key"]
            safety_checker = values["safety_checker"]
            qualification_checker = values["qualification_checker"]
            action_policy = values["action_policy"]
            safety_plugins = values["safety_plugins"]
            validity_plugins = values["validity_plugins"]
            qualification_plugins = values["qualification_plugins"]
            if not options.timing.allow_target_time:
                # Enforced when each ActionRequest is validated below.
                target_time_allowed = False
            else:
                target_time_allowed = True
        else:
            target_time_allowed = True
            safety_plugins = tuple(safety_plugins or ())
            validity_plugins = tuple(validity_plugins or ())
            qualification_plugins = tuple(qualification_plugins or ())
        if mode not in {"exploratory", "formal_comparison"}:
            raise ValueError("mode must be 'exploratory' or 'formal_comparison'")
        if checkpoint_interval_steps is not None and checkpoint_interval_steps <= 0:
            raise ValueError("checkpoint_interval_steps must be positive")
        if interrupt_after_steps is not None and interrupt_after_steps <= 0:
            raise ValueError("interrupt_after_steps must be positive")
        if cancel_after_steps is not None and cancel_after_steps <= 0:
            raise ValueError("cancel_after_steps must be positive")
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
        # Run-directory and formal-evidence gates must execute before any
        # adapter reset or temporary artifact creation.
        validate_run_location(spec.output_dir, spec.run_id)
        plant_manifest_preflight = _component_manifest(spec.plant_id, plant, configuration=spec.plant_config)
        controller_manifest_preflight = _component_manifest(spec.controller_id, controller, configuration=spec.controller_config)
        environment_preflight = collect_environment_provenance((plant, controller))
        controller_state_protocol = _controller_state_protocol(controller)
        if mode == "formal_comparison":
            _require_formal_evidence(spec, worktree, git, plant_manifest_preflight, controller_manifest_preflight, environment_preflight)
        abandoned_runs = scan_abandoned_runs(spec.output_dir)
        writer = ArtifactWriter(spec.output_dir, spec.run_id)
        audit, machine = _Audit(enabled=options.audit.enabled if options is not None else True), _StateMachine()
        machine.transition("RUNNING", "run_started")
        samples: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        error: str | None = None
        failure: dict[str, Any] | None = None
        qualification: dict[str, Any] = {"passed": False, "reasons": ["not_run"]}
        safety_report = CheckReport.ok()
        validity_report = CheckReport.ok()
        qualification_report = CheckReport.ok()
        capabilities: tuple[Any, Any] | None = None
        next_step_index, last_action, current_time = 0, 0.0, 0.0
        # Absolute simulation time at which this run starts.  Keeping this
        # separate from ``current_time`` lets a resumed run retain the same
        # final deadline instead of adding a fresh duration on every resume.
        run_start_time = 0.0
        controller_state: Mapping[str, Any] = {}
        recovery_provenance: dict[str, Any] = {
            "requested": resume_from is not None,
            "resumed": False,
            "source": None,
        }
        if abandoned_runs:
            events.append({"event": "abandoned_run_scan", "count": len(abandoned_runs), "runs": abandoned_runs})
        # The generic validity plugin is enabled by default. Empty unit/range
        # declarations retain compatibility while still checking finite,
        # monotonic, visible, present, and fresh observations.
        primary_key_for_checks: str | None = measurement_key or str(spec.plant_config.get("primary_measurement", spec.controller_config.get("measurement_key", "vout")))
        configured_validity = tuple(validity_plugins)
        # Supplying an ObservationValidityPlugin replaces the default
        # instance, allowing a caller to deliberately relax or extend its
        # age/field policy. Other validity plugins compose with the default.
        if any(isinstance(item, ObservationValidityPlugin) for item in configured_validity):
            validity_chain = configured_validity
        else:
            validity_chain = (ObservationValidityPlugin(required_measurements=(primary_key_for_checks,)),) + configured_validity
        safety_chain = tuple(safety_plugins)
        qualification_chain = (BasicQualificationPlugin(),) + tuple(qualification_plugins)
        try:
            if not callable(getattr(plant, "capabilities", None)):
                raise TypeError("plant missing capabilities()")
            plant_caps = audit.call("plant", "capabilities", lambda: plant.capabilities())
            controller_caps = audit.call("controller", "capabilities", lambda: controller.capabilities()) if callable(getattr(controller, "capabilities", None)) else None
            capabilities = negotiate_capabilities(plant_caps, controller_caps, spec.timebase, spec.required_capabilities, spec.input_schedule)
            checkpoint = _load_checkpoint(resume_from) if resume_from is not None else None
            if checkpoint is not None:
                source_info = checkpoint.pop("_recovery_source", None)
                if isinstance(source_info, Mapping):
                    source_status = source_info.get("status")
                    if source_status not in {None, "INCOMPLETE", "RUN_FAILED"}:
                        raise ValueError("resume source must be INCOMPLETE or RUN_FAILED")
                    recovery_provenance.update({
                        "resumed": True,
                        "source": dict(source_info),
                    })
                next_index = int(checkpoint.get("next_step_index", -1))
                if next_index < 0:
                    raise ValueError("checkpoint next_step_index must be non-negative")
                checkpoint_time = float(checkpoint.get("time_s", 0.0))
                if not math.isfinite(checkpoint_time) or checkpoint_time < 0:
                    raise ValueError("checkpoint time_s must be finite and non-negative")
                checkpoint_samples = checkpoint.get("samples", [])
                checkpoint_events = checkpoint.get("events", [])
                if not isinstance(checkpoint_samples, list) or not isinstance(checkpoint_events, list):
                    raise ValueError("checkpoint samples and events must be arrays")
                if len(checkpoint_samples) > next_index:
                    raise ValueError("checkpoint samples exceed next_step_index")
                if not isinstance(checkpoint.get("plant_snapshot"), Mapping):
                    raise ValueError("checkpoint plant_snapshot must be an object")
                if not isinstance(checkpoint.get("controller_state", {}), Mapping):
                    raise ValueError("checkpoint controller_state must be an object")
                last_checkpoint_action = float(checkpoint.get("last_action", 0.0))
                if not math.isfinite(last_checkpoint_action):
                    raise ValueError("checkpoint last_action must be finite")
                if checkpoint.get("experiment_id") != spec.experiment_id or checkpoint.get("plant_id") != spec.plant_id or checkpoint.get("controller_id") != spec.controller_id:
                    raise ValueError("checkpoint identity does not match experiment")
                expected = checkpoint.get("checkpoint_hash")
                if not isinstance(expected, str) or len(expected) != 64:
                    raise ValueError("checkpoint hash missing")
                unsigned = dict(checkpoint)
                unsigned.pop("checkpoint_hash", None)
                if sha256_bytes(canonical_json(unsigned)) != expected:
                    raise ValueError("checkpoint hash mismatch")
                expected_plant_hash = _component_manifest(spec.plant_id, plant, configuration=spec.plant_config)["hash"]
                if checkpoint.get("plant_hash") != expected_plant_hash:
                    raise ValueError("checkpoint plant identity mismatch")
                expected_controller_hash = _component_manifest(spec.controller_id, controller, configuration=spec.controller_config)["hash"]
                if checkpoint.get("controller_hash") != expected_controller_hash:
                    raise ValueError("checkpoint controller identity mismatch")
                expected_contracts = {key: value["hash"] for key, value in spec.contracts.items()}
                if checkpoint.get("contract_hashes") != expected_contracts:
                    raise ValueError("checkpoint contract identity mismatch")
                expected_spec_hash = _experiment_spec_hash(spec)
                if checkpoint.get("experiment_spec_hash") != expected_spec_hash:
                    raise ValueError("checkpoint experiment specification mismatch")
                protocol = str(checkpoint.get("controller_state_protocol", "reset_serialized_state"))
                if protocol not in {"snapshot_restore", "reset_serialized_state"}:
                    raise ValueError("checkpoint controller state protocol is unsupported")
                if protocol != controller_state_protocol:
                    raise ValueError("checkpoint controller state protocol mismatch")
                if protocol == "snapshot_restore" and not (callable(getattr(controller, "snapshot", None)) and callable(getattr(controller, "restore", None))):
                    raise ValueError("checkpoint requires controller snapshot()/restore() protocol")
                audit.call("plant", "restore", lambda: plant.restore(checkpoint["plant_snapshot"]), step_index=checkpoint.get("next_step_index"))
                controller_state = checkpoint.get("controller_state", {})
                if callable(getattr(controller, "restore", None)):
                    audit.call("controller", "restore", lambda: controller.restore(controller_state), step_index=checkpoint.get("next_step_index"))
                else:
                    controller_state = audit.call("controller", "reset", lambda: controller.reset(controller_state, spec.seed if spec.seed is not None else 0))
                    if not isinstance(controller_state, Mapping):
                        raise TypeError("controller.reset() must return a serializable mapping")
                samples, events = list(checkpoint.get("samples", [])), list(checkpoint.get("events", []))
                next_step_index, last_action, current_time = int(checkpoint.get("next_step_index", len(samples))), float(checkpoint.get("last_action", 0.0)), float(checkpoint.get("time_s", 0.0))
                # Checkpoints created before the explicit run-start field are
                # still readable; infer their origin from the completed full
                # windows as a conservative compatibility fallback.
                run_start_time = float(checkpoint.get("run_start_time", current_time - min(next_step_index * spec.timebase.control_period_s, spec.timebase.duration_s)))
                if not math.isfinite(run_start_time):
                    raise ValueError("checkpoint run_start_time must be finite")
                events.append({"event": "resume", "from_step_index": next_step_index, "time_s": current_time, "source": recovery_provenance.get("source")})
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
                if not isinstance(controller_state, Mapping):
                    raise TypeError("controller.reset() must return a serializable mapping")
            # Restored/qualified snapshots may carry a non-zero simulation
            # clock. Discover that clock before constructing the first window.
            if resume_from is None:
                initial_observation = audit.call("plant", "observe", lambda: plant.observe())
                if not isinstance(initial_observation, PlantObservation):
                    raise TypeError("plant.observe() must return PlantObservation")
                current_time = float(initial_observation.time_s)
                initial_context = CheckContext(
                    spec=spec,
                    step_index=-1,
                    current_time_s=current_time,
                    primary_measurement=primary_key_for_checks,
                    phase="initial_observation",
                )
                initial_validity = _run_plugins_audited(
                    validity_chain,
                    "check_observation",
                    initial_observation,
                    initial_context,
                    audit,
                    step_index=-1,
                    time_s=current_time,
                )
                validity_report = validity_report.merge(initial_validity)
                if not initial_validity.passed:
                    raise DataValidityError("initial observation validity check failed", initial_validity, phase="initial_observation")
                initial_safety = _run_plugins_audited(
                    safety_chain,
                    "check_observation",
                    initial_observation,
                    initial_context,
                    audit,
                    step_index=-1,
                    time_s=current_time,
                )
                safety_report = safety_report.merge(initial_safety)
                if not initial_safety.passed:
                    raise SafetyCheckError("safety plugin rejected initial observation", initial_safety, phase="initial_observation")
                run_start_time = current_time
            ratio = spec.timebase.duration_s / spec.timebase.control_period_s
            nearest = round(ratio)
            n_steps = int(nearest if abs(ratio - nearest) <= 1e-12 else math.ceil(ratio))
            previous_observation_time: float | None = None
            primary_key = measurement_key or str(spec.plant_config.get("primary_measurement", spec.controller_config.get("measurement_key", "vout")))
            run_end_time = run_start_time + spec.timebase.duration_s
            for index in range(next_step_index, n_steps):
                cycle_start = current_time
                window_duration = min(spec.timebase.control_period_s, run_end_time - cycle_start)
                if window_duration <= spec.timebase.event_tolerance_s:
                    raise ValueError("control window has no remaining duration")
                offset = spec.timebase.sample_offset_s
                if offset >= window_duration - spec.timebase.event_tolerance_s:
                    raise ValueError("sample_offset_s must be smaller than the remaining control window")
                if offset > spec.timebase.event_tolerance_s:
                    advanced = _advance_segments(
                        plant,
                        last_action,
                        offset,
                        _command_at(spec.input_schedule, cycle_start),
                        spec.timebase.plant_step_s,
                        audit,
                        index,
                        expected_end_s=cycle_start + offset,
                        tolerance_s=spec.timebase.event_tolerance_s,
                    )
                    current_time = float(advanced.time_s) if advanced is not None else cycle_start + offset
                obs = audit.call("plant", "observe", lambda: plant.observe(), step_index=index, time_s=current_time)
                if not isinstance(obs, PlantObservation):
                    raise TypeError("plant.observe() must return PlantObservation")
                check_fn = safety_checker or check_observation
                legacy_safety = audit.call("safety", "check_observation", lambda: check_fn(obs.measurement), step_index=index, time_s=obs.time_s)
                safety_report = safety_report.merge(normalise_check_result(legacy_safety, plugin_id="legacy-safety"))
                if not safety_report.passed:
                    raise SafetyCheckError("safety observation check failed", safety_report, phase="observation")
                validity_context = CheckContext(
                    spec=spec,
                    step_index=index,
                    current_time_s=current_time,
                    previous_observation_time_s=previous_observation_time,
                    primary_measurement=primary_key_for_checks,
                    phase="observation",
                )
                current_validity = _run_plugins_audited(validity_chain, "check_observation", obs, validity_context, audit, step_index=index, time_s=current_time)
                validity_report = validity_report.merge(current_validity)
                if not current_validity.passed:
                    raise DataValidityError("observation validity check failed", current_validity, phase="observation")
                current_safety = _run_plugins_audited(safety_chain, "check_observation", obs, validity_context, audit, step_index=index, time_s=current_time)
                safety_report = safety_report.merge(current_safety)
                if not current_safety.passed:
                    raise SafetyCheckError("safety plugin rejected observation", current_safety, phase="observation")
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
                if request.state_update is not None:
                    controller_state = dict(request.state_update)
                endpoint, tolerance = cycle_start + window_duration, spec.timebase.event_tolerance_s
                if request.produced_time_s < current_time - tolerance or request.produced_time_s > endpoint + tolerance or request.target_time_s < current_time - tolerance or request.target_time_s > endpoint + tolerance:
                    raise ValueError("action timestamp violates timebase")
                if not target_time_allowed and abs(float(request.target_time_s) - current_time) > tolerance:
                    raise ValueError("timing policy disallows delayed target-time actions")
                action, clamp_reason = audit.call("safety", "project_action", lambda: selected_action_policy.project(request.value), step_index=index, time_s=current_time)
                action_safety = _run_plugins_audited(safety_chain, "check_action", {"request": request, "action": action, "measurement": obs.measurement}, CheckContext(spec=spec, step_index=index, current_time_s=current_time, previous_observation_time_s=previous_observation_time, primary_measurement=primary_key_for_checks, phase="action"), audit, step_index=index, time_s=current_time)
                safety_report = safety_report.merge(action_safety)
                if not action_safety.passed:
                    raise SafetyCheckError("safety plugin rejected action", action_safety, phase="action")
                target = min(max(float(request.target_time_s), current_time), endpoint)
                hold = max(0.0, target - current_time)
                if hold > tolerance:
                    advanced = _advance_segments(
                        plant,
                        last_action,
                        hold,
                        command,
                        spec.timebase.plant_step_s,
                        audit,
                        index,
                        expected_end_s=target,
                        tolerance_s=tolerance,
                    )
                    if advanced is not None:
                        current_time = float(advanced.time_s)
                remaining = max(0.0, endpoint - current_time)
                _advance_segments(
                    plant,
                    action,
                    remaining,
                    command,
                    spec.timebase.plant_step_s,
                    audit,
                    index,
                    expected_end_s=endpoint,
                    tolerance_s=tolerance,
                )
                next_obs = audit.call("plant", "observe", lambda: plant.observe(), step_index=index, time_s=endpoint)
                if not isinstance(next_obs, PlantObservation):
                    raise TypeError("plant.advance() did not produce PlantObservation")
                next_validity = _run_plugins_audited(
                    validity_chain,
                    "check_observation",
                    next_obs,
                    CheckContext(spec=spec, step_index=index, current_time_s=endpoint, previous_observation_time_s=check_time, primary_measurement=primary_key_for_checks, phase="advance_observation"),
                    audit,
                    step_index=index,
                    time_s=endpoint,
                )
                validity_report = validity_report.merge(next_validity)
                if not next_validity.passed:
                    raise DataValidityError("advanced observation validity check failed", next_validity, phase="advance_observation")
                if next_obs.time_s <= current_time or not math.isfinite(next_obs.time_s):
                    raise ValueError("plant time did not advance")
                if abs(float(next_obs.time_s) - endpoint) > tolerance:
                    raise ValueError("plant did not reach the requested time window endpoint")
                row = {"step_index": index, "time_s": current_time, "action": action, "action_requested": request.value, "clamp_reason": clamp_reason}
                row.update({str(key): float(value) for key, value in obs.measurement.items()})
                samples.append(row)
                events.append({"step_index": index, "sample_time_s": current_time, "action_time_s": request.target_time_s, "next_time_s": next_obs.time_s, "action_applied_time_s": target})
                current_time, last_action = float(next_obs.time_s), action
                if checkpoint_interval_steps and (index + 1) % checkpoint_interval_steps == 0:
                    _write_checkpoint(writer, spec, plant, controller, controller_state, samples, events, index + 1, current_time, last_action, audit, run_start_time=run_start_time, controller_state_protocol=controller_state_protocol)
                if interrupt_after_steps is not None and len(samples) >= interrupt_after_steps:
                    raise KeyboardInterrupt(f"interrupted after {len(samples)} steps")
                if cancel_after_steps is not None and len(samples) >= cancel_after_steps:
                    raise SimulationCancelledError(f"cancelled after {len(samples)} steps")
            qualification_fn = qualification_checker or qualify_samples
            legacy_qualification = audit.call("qualification", "check", lambda: qualification_fn(samples), step_index=n_steps, time_s=current_time)
            qualification_report = qualification_report.merge(normalise_check_result(legacy_qualification, plugin_id="legacy-qualification"))
            plugin_qualification = _run_plugins_audited(
                qualification_chain,
                "check_samples",
                samples,
                CheckContext(spec=spec, step_index=n_steps, current_time_s=current_time, primary_measurement=primary_key_for_checks, phase="postrun"),
                audit,
                step_index=n_steps,
                time_s=current_time,
            )
            qualification_report = qualification_report.merge(plugin_qualification)
            qualification = {
                "passed": bool(qualification_report.passed),
                "reasons": qualification_report.reasons,
                "checks": qualification_report.to_dict(),
            }
            machine.transition("RUN_OK", "completed", step_index=n_steps, time_s=current_time)
            if qualification_report.passed:
                machine.transition("QUALIFIED", "qualification_passed", step_index=n_steps, time_s=current_time)
            else:
                machine.transition("DISQUALIFIED", "qualification_failed", step_index=n_steps, time_s=current_time)
        except KeyboardInterrupt as exc:
            error, failure = f"{type(exc).__name__}: {exc}", {"category": "interrupted", "exception_type": type(exc).__name__, "message": str(exc), "recoverable": True, "checkpoint_available": callable(getattr(plant, "snapshot", None))}
            events.append({"event": "failure", **failure, "step_index": len(samples), "time_s": current_time})
            machine.transition("INCOMPLETE", "interrupted", step_index=len(samples), time_s=current_time)
            try:
                _write_checkpoint(writer, spec, plant, controller, controller_state, samples, events, len(samples), current_time, last_action, audit, run_start_time=run_start_time, controller_state_protocol=controller_state_protocol)
            except Exception as checkpoint_exc:
                failure.update({"checkpoint_available": False, "checkpoint_error": f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"})
                events.append({"event": "checkpoint_failure", "error": failure["checkpoint_error"], "step_index": len(samples), "time_s": current_time})
            qualification = {"passed": False, "reasons": ["incomplete", "interrupted"], "checks": qualification_report.to_dict()}
        except SimulationCancelledError as exc:
            error, failure = f"{type(exc).__name__}: {exc}", {"category": "cancelled", "exception_type": type(exc).__name__, "message": str(exc), "recoverable": True, "checkpoint_available": callable(getattr(plant, "snapshot", None))}
            events.append({"event": "failure", **failure, "step_index": len(samples), "time_s": current_time})
            machine.transition("INCOMPLETE", "cancelled", step_index=len(samples), time_s=current_time)
            try:
                _write_checkpoint(writer, spec, plant, controller, controller_state, samples, events, len(samples), current_time, last_action, audit, run_start_time=run_start_time, controller_state_protocol=controller_state_protocol)
            except Exception as checkpoint_exc:
                failure.update({"checkpoint_available": False, "checkpoint_error": f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"})
                events.append({"event": "checkpoint_failure", "error": failure["checkpoint_error"], "step_index": len(samples), "time_s": current_time})
            qualification = {"passed": False, "reasons": ["incomplete", "cancelled"], "checks": qualification_report.to_dict()}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failure = _failure_record(exc, audit, step_index=len(samples), time_s=current_time)
            if isinstance(exc, CheckFailureError) and exc.report.findings:
                failure["findings"] = [item.to_dict() for item in exc.report.findings]
            elif failure["category"] in _OBSERVATION_FAILURE_CATEGORIES:
                # Plant adapters may reject malformed values while constructing
                # PlantObservation, before the validity plugin can inspect it.
                # Preserve the same machine-readable finding in the evidence
                # report so a failed observation never appears valid.
                finding = CheckReport.failure(
                    failure["category"],
                    failure["message"],
                    rule_id=f"runner.{failure['category']}",
                    evidence={
                        "exception_type": failure["exception_type"],
                        "component": failure.get("component"),
                        "method": failure.get("method"),
                        "step_index": failure["step_index"],
                        "time_s": failure["time_s"],
                    },
                    plugin_id="runner-observation-boundary",
                )
                validity_report = validity_report.merge(finding)
                failure["findings"] = [item.to_dict() for item in finding.findings]
            events.append({"event": "failure", **failure})
            machine.transition("RUN_FAILED", failure["category"], step_index=len(samples), time_s=current_time)
            reasons = [failure["category"]]
            # Preserve the legacy generic reason for callers that only
            # understand the pre-v1 runtime error vocabulary.
            if failure["category"] == "timing_violation":
                reasons.append("runtime_error")
            failure.setdefault("recoverable", False)
            failure.setdefault("checkpoint_available", (writer.tmp / "checkpoint.json").exists())
            qualification = {"passed": False, "reasons": reasons, "checks": qualification_report.to_dict()}
        status = machine.status
        writer.write_json("config.snapshot.json", spec.to_dict())
        plant_manifest = _component_manifest(spec.plant_id, plant, capabilities[0] if capabilities else None, spec.plant_config)
        controller_manifest = _component_manifest(spec.controller_id, controller, capabilities[1] if capabilities else None, spec.controller_config)
        environment = environment_preflight
        environment["git"] = {
            "source_commit": git.source_commit,
            "branch": git.branch,
            "working_tree_status": git.working_tree_status,
        }
        writer.write_json("environment.json", environment)
        writer.write_json("qualification.json", qualification)
        safety_passed = error is None and safety_report.passed and validity_report.passed
        writer.write_json("safety.json", {"passed": safety_passed, "error": error, "checks": safety_report.to_dict(), "validity": validity_report.to_dict()})
        writer.write_json(
            "metrics.json",
            {
                "schema_version": "run-metrics-v1",
                "sample_schema_version": "samples-v1",
                "sample_count": len(samples),
                "audit_call_count": len(audit.calls),
            },
        )
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
        worktree_evidence_sha256 = sha256_bytes(canonical_json(worktree_manifest))
        git_fields = git.to_manifest_fields()
        provenance = dict(git_fields["provenance"])
        provenance.update(
            {
                "worktree_analysis_sha256": worktree_evidence_sha256,
                "source_commit": git.source_commit,
                "plant_sha256": plant_manifest["hash"],
                "controller_sha256": controller_manifest["hash"],
                "environment_status": environment["status"],
            }
        )
        manifest = {"schema_version": spec.schema_version, "experiment_id": spec.experiment_id, "run_id": spec.run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **git_fields, "provenance": provenance, "worktree_analysis": worktree_manifest, "worktree_analysis_sha256": worktree_evidence_sha256, "run_mode": mode, "environment": environment, "plant": plant_manifest, "controller": controller_manifest, "action_policy": _action_policy_manifest(selected_action_policy), "contracts": {k: v["hash"] for k, v in spec.contracts.items()}, "timebase": spec.timebase.to_dict(), "initial_state": spec.initial_state.to_dict(), "random_seed": spec.seed, "artifacts": {}, "status": status, "qualification": qualification, "safety": {"passed": safety_passed, "checks": safety_report.to_dict(), "validity": validity_report.to_dict()}, "evidence_level": "functional", "error": error, "failure": failure, "recovery": recovery_provenance, "abandoned_runs": abandoned_runs, "capabilities": {"plant": capabilities[0].to_dict(), "controller": capabilities[1].to_dict()} if capabilities else {}, "checks": {"safety": safety_report.to_dict(), "validity": validity_report.to_dict(), "qualification": qualification_report.to_dict(), "plugins": {"safety": [plugin_identity(item) for item in safety_chain], "validity": [plugin_identity(item) for item in validity_chain], "qualification": [plugin_identity(item) for item in qualification_chain]}}, "audit": {"call_count": len(audit.calls)}, "state_transition_matrix": machine.allowed_transitions(), "state_transitions": machine.transitions}
        writer.write_json("logs/run.json", {"status": status, "error": error, "sample_count": len(samples)})
        # Complete the immutable publication summary while the run is still
        # hidden in the writer's temporary directory.  Publication then moves
        # the whole prepared package in one directory-level transaction.
        manifest["artifacts"] = artifact_index(writer.tmp)
        manifest["manifest_sha256"] = manifest_digest(manifest)
        manifest["package_sha256"] = package_digest(writer.tmp, manifest)
        manifest["hashes"] = {
            "manifest_sha256": manifest["manifest_sha256"],
            "package_sha256": manifest["package_sha256"],
        }
        writer.write_json("manifest.json", manifest)
        run_dir = writer.finalize()
        return RunResult(run_dir, status, qualification)


def _advance_segments(plant: Any, action: float, duration: float, external: Mapping[str, Any], plant_step: float | None, audit: _Audit, step_index: int, *, expected_end_s: float | None = None, tolerance_s: float = 1e-12) -> PlantObservation | None:
    if duration <= 1e-15:
        return None
    quantum, remaining = float(plant_step or duration), float(duration)
    last_observation: PlantObservation | None = None
    while remaining > 1e-15:
        segment = min(quantum, remaining)
        value = audit.call("plant", "advance", lambda segment=segment: plant.advance(action, segment, external_input=external), step_index=step_index)
        if isinstance(value, PlantObservation):
            last_observation = value
        remaining -= segment
    if expected_end_s is not None and isinstance(last_observation, PlantObservation):
        if not math.isfinite(float(last_observation.time_s)) or abs(float(last_observation.time_s) - float(expected_end_s)) > float(tolerance_s):
            raise ValueError("plant did not reach the requested time window endpoint")
    return last_observation


def _experiment_spec_hash(spec: ExperimentSpec) -> str:
    """Hash execution semantics while excluding output/run naming metadata."""

    value = spec.to_dict()
    value.pop("run_id", None)
    value.pop("output", None)
    return sha256_bytes(canonical_json(value))


def _require_formal_evidence(spec: ExperimentSpec, worktree: Any, git: Any, plant: Mapping[str, Any], controller: Mapping[str, Any], environment: Mapping[str, Any]) -> None:
    """Reject formal comparison unless all portable identity evidence exists."""

    if spec.seed is None:
        raise ValueError("formal comparison requires an explicit random seed")
    if any(str(spec.contracts[name].get("hash", "")).lower() == "0" * 64 for name in ("safety", "qualification")):
        raise ValueError("formal comparison requires non-placeholder contract hashes")
    if not isinstance(git.source_commit, str) or not len(git.source_commit) >= 40 or any(ch not in "0123456789abcdefABCDEF" for ch in git.source_commit):
        raise ValueError("formal comparison requires a complete source commit identity")
    if getattr(worktree, "status", None) != "clean" or getattr(git, "working_tree_status", None) != "clean":
        raise ValueError("formal comparison requires a clean source worktree")
    for label, component in (("Plant", plant), ("Controller", controller)):
        identity = component.get("identity")
        digest = component.get("hash")
        if not isinstance(identity, Mapping) or not identity or not isinstance(digest, str) or len(digest) != 64 or set(digest) == {"0"}:
            raise ValueError(f"formal comparison requires complete {label} identity evidence")
    if str(environment.get("status")) != "known":
        raise ValueError("formal comparison requires known environment provenance")
    backends = environment.get("backends", ())
    if not isinstance(backends, (list, tuple)) or any(str(item.get("status")) != "known" for item in backends if isinstance(item, Mapping)):
        raise ValueError("formal comparison requires known backend provenance")


def _run_plugins_audited(plugins: Any, method: str, subject: Any, context: CheckContext, audit: _Audit, *, step_index: int, time_s: float) -> CheckReport:
    """Run extension checks through the same ordered audit stream as adapters."""

    report = CheckReport.ok()
    for plugin in plugins:
        identity = plugin_identity(plugin)
        result = audit.call(
            context.phase,
            identity["id"],
            lambda plugin=plugin: run_plugin(plugin, method, subject, context),
            step_index=step_index,
            time_s=time_s,
        )
        report = report.merge(result)
    return report


def _failure_record(exc: BaseException, audit: _Audit, *, step_index: int, time_s: float) -> dict[str, Any]:
    """Classify adapter/backend exceptions without requiring a backend SDK."""

    explicit = getattr(exc, "category", None)
    category = str(explicit) if explicit else None
    message = str(exc)
    lowered = f"{type(exc).__name__} {message}".lower()
    last_failed = next((row for row in reversed(audit.calls) if row.get("ok") is False), None)
    component = last_failed.get("component") if last_failed else None
    method = last_failed.get("method") if last_failed else None
    if category in {None, "runtime_error", "simulation_execution_error"}:
        if isinstance(exc, SimulationCancelledError) or "cancel" in lowered:
            category = "cancelled"
        elif isinstance(exc, TimeoutError) or "timeout" in lowered or "timed out" in lowered:
            category = "backend_timeout"
        elif any(token in lowered for token in ("converg", "singular matrix", "timestep", "time step")):
            category = "numerical_nonconvergence"
        elif any(token in lowered for token in ("process", "subprocess", "return code", "exit status", "backend crashed")):
            category = "backend_process_failure"
        elif "observation time" in lowered and "finite" in lowered:
            category = "timestamp_violation"
        elif "measurement_units" in lowered or "unit mismatch" in lowered or "unsupported unit" in lowered:
            category = "unit_mismatch"
        elif "age_steps" in lowered and ("negative" in lowered or "invalid" in lowered):
            category = "invalid_observation_age"
        elif any(token in lowered for token in ("observation values must be finite", "non-finite measurement", "nonfinite measurement", "measurement is not finite")):
            category = "nonfinite_observation"
        elif component == "safety" and method == "project_action" and "action" in lowered:
            category = "action_invalid"
        elif any(token in lowered for token in ("non-finite", "nonfinite", "nan", "infinity", "infinite", "illegal numeric")):
            category = "model_numerical_error"
        elif component == "plant" and method in {"advance", "observe", "restore", "reset"}:
            category = "plant_advance_failure" if method == "advance" else "plant_execution_failure"
        elif "plant time did not advance" in lowered or "advanced beyond" in lowered or "did not reach the requested time window endpoint" in lowered:
            category = "plant_advance_failure"
        elif "timestamp" in lowered or "timebase" in lowered:
            category = "timing_violation"
        elif "action" in lowered:
            category = "action_invalid"
        else:
            category = "runtime_error"
    finding = getattr(exc, "finding", None)
    result: dict[str, Any] = {
        "category": category,
        "exception_type": type(exc).__name__,
        "message": message,
        "phase": getattr(exc, "phase", "runtime"),
        "step_index": step_index,
        "time_s": time_s,
    }
    if component is not None:
        result["component"] = component
    if method is not None:
        result["method"] = method
    if finding is not None:
        result["rule_id"] = finding.rule_id
        result["evidence"] = dict(finding.evidence)
    return result


def _write_checkpoint(writer: ArtifactWriter, spec: ExperimentSpec, plant: Any, controller: Any, controller_state: Mapping[str, Any], samples: list[dict[str, Any]], events: list[dict[str, Any]], next_step_index: int, time_s: float, last_action: float, audit: _Audit, *, run_start_time: float | None = None, controller_state_protocol: str | None = None) -> None:
    if not callable(getattr(plant, "snapshot", None)):
        raise TypeError("plant missing snapshot() for checkpoint")
    snapshot = audit.call("plant", "snapshot", lambda: plant.snapshot(), step_index=next_step_index, time_s=time_s)
    if callable(getattr(controller, "snapshot", None)):
        controller_state = audit.call("controller", "snapshot", lambda: controller.snapshot(), step_index=next_step_index, time_s=time_s)
    protocol = controller_state_protocol or _controller_state_protocol(controller)
    checkpoint = {"schema_version": spec.schema_version, "experiment_id": spec.experiment_id, "plant_id": spec.plant_id, "controller_id": spec.controller_id, "experiment_spec_hash": _experiment_spec_hash(spec), "plant_hash": _component_manifest(spec.plant_id, plant, configuration=spec.plant_config)["hash"], "controller_hash": _component_manifest(spec.controller_id, controller, configuration=spec.controller_config)["hash"], "contract_hashes": {key: value["hash"] for key, value in spec.contracts.items()}, "controller_state_protocol": protocol, "next_step_index": next_step_index, "time_s": time_s, "run_start_time": float(run_start_time if run_start_time is not None else time_s), "last_action": last_action, "plant_snapshot": dict(snapshot), "controller_state": dict(controller_state or {}), "samples": samples, "events": events}
    checkpoint["checkpoint_hash"] = sha256_bytes(canonical_json(checkpoint))
    writer.write_json("checkpoint.json", checkpoint)
    writer.write_json(f"checkpoints/step-{next_step_index:06d}.json", checkpoint)


def _controller_state_protocol(controller: Any) -> str:
    """Return the explicit protocol used to persist controller state."""

    has_snapshot = callable(getattr(controller, "snapshot", None))
    has_restore = callable(getattr(controller, "restore", None))
    if has_snapshot != has_restore:
        raise TypeError("controller must provide both snapshot() and restore(), or neither")
    if has_snapshot:
        return "snapshot_restore"
    # The fallback protocol is deliberately explicit in each checkpoint: the
    # serialized mapping returned by reset() is passed back to reset() on
    # resume. Controllers with hidden state must implement snapshot/restore.
    return "reset_serialized_state"


def _load_checkpoint(source: str | Path | None) -> dict[str, Any] | None:
    if source is None:
        return None
    path = Path(source)
    source_dir: Path | None = path if path.is_dir() else path.parent
    # A direct checkpoints/step-*.json path still belongs to its run
    # directory; use that directory's Manifest for terminal-state validation.
    if source_dir is not None and source_dir.name == "checkpoints":
        source_dir = source_dir.parent
    source_manifest: dict[str, Any] | None = None
    if source_dir is not None:
        manifest_path = source_dir / "manifest.json"
        if manifest_path.exists():
            try:
                loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded_manifest, Mapping):
                    source_manifest = dict(loaded_manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                raise ValueError("resume source manifest is unreadable")
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
    result = dict(value)
    source_info: dict[str, Any] = {
        # Keep recovery provenance portable; a local checkout path is not
        # evidence and must not leak into a published Manifest.
        "source_name": source_dir.name if source_dir is not None else path.parent.name,
        "checkpoint": path.name,
        "checkpoint_hash": result.get("checkpoint_hash"),
    }
    if source_manifest is not None:
        source_info.update({
            "run_id": source_manifest.get("run_id"),
            "status": source_manifest.get("status"),
            "manifest_sha256": source_manifest.get("manifest_sha256"),
        })
    result["_recovery_source"] = source_info
    return result


_RUNTIME_STATE_FIELDS = frozenset(
    {
        "time",
        "time_s",
        "state",
        "controller_state",
        "history",
        "events",
        "last_action",
        "last_duty",
    }
)


def _hashable_value(value: Any) -> Any:
    """Convert common adapter configuration values to JSON-safe primitives."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("component identity contains a non-finite value")
        return value
    if isinstance(value, Mapping):
        return {str(key): _hashable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hashable_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        values = [_hashable_value(item) for item in value]
        return sorted(values, key=lambda item: canonical_json(item))
    if isinstance(value, Path):
        return value.as_posix()
    # Dataclass-like configuration objects often expose an explicit mapping.
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _hashable_value(to_dict())
    raise TypeError(f"component identity value is not JSON serializable: {type(value).__name__}")


def _component_manifest(component_id: str, component: Any, declared_capabilities: Any = None, configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
    identity_fn = getattr(component, "manifest_identity", None)
    if callable(identity_fn):
        identity = dict(identity_fn())
    else:
        identity = {
            "kind": "runtime_component",
            "module": type(component).__module__,
            "class": type(component).__qualname__,
        }
        if declared_capabilities is not None:
            normalized = declared_capabilities.to_dict() if hasattr(declared_capabilities, "to_dict") else declared_capabilities
            identity["capabilities"] = normalized
        attributes = getattr(component, "__dict__", {})
        if isinstance(attributes, Mapping):
            attributes_config = {
                str(name): value
                for name, value in attributes.items()
                if not str(name).startswith("_") and str(name) not in _RUNTIME_STATE_FIELDS
            }
            if attributes_config:
                identity["configuration"] = attributes_config
    if configuration:
        # Bind the ExperimentSpec's adapter configuration as well. This keeps
        # identity stable for adapters whose constructor intentionally accepts
        # no arguments and obtains settings from the run specification.
        identity["run_configuration"] = configuration
    identity = _hashable_value(identity)
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
