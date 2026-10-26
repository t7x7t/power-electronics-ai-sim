"""Executable whole-run route: engine-owned time, post-hoc-only evidence.

A whole-run engine receives one :class:`~pe_sim.time_model.WholeRunRequest`,
integrates its own horizon, and returns one complete trace.  The kernel cannot
stand between the engine and the clock, so this route deliberately does **not**
claim:

- in-run safety, validity, or applicability enforcement (there are no window
  boundaries to check at);
- checkpoint, resume, or deterministic step interruption (there are no steps
  the kernel owns);
- formal comparison (``formal_comparison_allowed("whole_run")`` is ``False``);
- that the kernel-provided Controller was driven (the engine owns its control
  loop on this route).

What it does claim is narrower and checkable: the declared trace contract is
validated after the fact, the result is capped at ``limited`` evidence, the
manifest records both route limitations and ``enforcement = "post_hoc_only"``,
and qualification runs through the same ``check_samples`` plugin path as a
stepped run.

This module names no engine, topology, or vendor.  It imports reusable private
helpers from :mod:`pe_sim.runtime` rather than re-implementing the manifest,
because a second package layout would be a second definition of evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .applicability import ApplicabilityError, ApplicabilityPolicy, ModelEnvelopePlugin
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
from .checks import (
    CheckContext,
    CheckReport,
    DataValidityError,
    ObservationValidityPlugin,
    SafetyCheckError,
    normalise_check_result,
    run_plugins,
)
from .contracts import ExperimentSpec, SimulationCancelledError
from .dirty import analyze_git_worktree
from .jsonutil import json_safe
from .model_claim import collect_model_claim, collect_parameter_provenance
from .provenance import collect_environment_provenance, collect_git_provenance
from .qualification import (
    BasicQualificationPlugin,
    qualification_document,
    qualify_samples,
)
from .run_events import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_PROGRESS,
    EVENT_RUN_STARTED,
    RunEventEmitter,
)
from .runner.audit import AuditRecorder
from .runner.lifecycle import LifecycleStateMachine
from .runner.policies import RunOptions
from .runtime import (
    RunResult,
    _action_policy_bounds,
    _action_policy_manifest,
    _applicability_manifest,
    _build_run_manifest,
    _component_manifest,
    _derive_evidence_level,
    _failure_record,
    _preflight_document,
    _require_registered_adapter,
    _run_plugins_audited,
)
from .time_model import (
    CONTROL_LOCATION_MODEL,
    EVENT_GRANULARITY_WINDOW,
    TIME_MODEL_WHOLE_RUN,
    RouteCapabilities,
    TraceContract,
    WholeRunRequest,
    WholeRunResult,
    route_inherent_limitations_for,
    validate_whole_run_result,
)

#: Failure category recorded when the engine's trace violates the contract it
#: declared before running.  The route is the kernel side of that judgement.
TRACE_CONTRACT_VIOLATION = "trace_contract_violation"

#: The route cannot enforce anything while the engine runs.  This value is
#: written to the manifest; it is a claim about what happened, not a policy.
POST_HOC_ONLY = "post_hoc_only"


class _TraceContractViolation(RuntimeError):
    """Kernel-side classification of a trace that failed post-hoc validation."""

    category = TRACE_CONTRACT_VIOLATION


def _refuse_unsupported_requests(
    spec: ExperimentSpec,
    plant: Any,
    *,
    mode: str,
    checkpoint_interval_steps: int | None,
    resume_from: str | Path | None,
    interrupt_after_steps: int | None,
    cancel_after_steps: int | None,
    applicability_policy: ApplicabilityPolicy,
) -> None:
    """Refuse combinations the route cannot honour before touching disk."""

    if mode == "formal_comparison":
        raise ValueError(
            f"plant {spec.plant_id!r} declares time_model {TIME_MODEL_WHOLE_RUN!r}; "
            "formal_comparison is not allowed on the whole-run route because the "
            "kernel cannot enforce constraints while the engine owns the clock"
        )
    recovery = {
        "checkpoint_interval_steps": checkpoint_interval_steps,
        "resume_from": resume_from,
        "interrupt_after_steps": interrupt_after_steps,
        "cancel_after_steps": cancel_after_steps,
    }
    selected = [name for name, value in recovery.items() if value is not None]
    if selected:
        raise ValueError(
            f"plant {spec.plant_id!r} declares time_model {TIME_MODEL_WHOLE_RUN!r}; "
            f"{', '.join(selected)} cannot be honoured on the whole-run route "
            "because the kernel does not own the engine's steps, so the run "
            f"{spec.run_id!r} was not started"
        )
    if applicability_policy.mode == "enforce":
        raise ValueError(
            f"plant {spec.plant_id!r} declares time_model {TIME_MODEL_WHOLE_RUN!r}; "
            "applicability enforcement is not available on the whole-run route "
            "because there are no kernel-owned windows to check; use record, "
            "warn, or off and rely on the post-hoc evidence"
        )
    if not callable(getattr(plant, "trace_contract", None)):
        raise ValueError(
            f"plant {spec.plant_id!r} declares time_model {TIME_MODEL_WHOLE_RUN!r} "
            "but is missing trace_contract()"
        )
    if not callable(getattr(plant, "run_whole", None)):
        raise ValueError(
            f"plant {spec.plant_id!r} declares time_model {TIME_MODEL_WHOLE_RUN!r} "
            "but is missing run_whole(request)"
        )


def _resolve_trace_contract(plant: Any, audit: AuditRecorder) -> TraceContract:
    """Call ``trace_contract()`` exactly once and insist on the declared type."""

    contract = audit.call("plant", "trace_contract", plant.trace_contract)
    if not isinstance(contract, TraceContract):
        raise ValueError(
            f"plant {type(plant).__qualname__}.trace_contract() returned "
            f"{type(contract).__name__}, expected a TraceContract"
        )
    return contract


def _build_request(spec: ExperimentSpec) -> WholeRunRequest:
    """Translate the experiment document into the engine-facing request."""

    return WholeRunRequest(
        duration_s=spec.timebase.duration_s,
        initial_state=spec.initial_state.to_dict(),
        seed=spec.seed,
        metadata={
            "input_schedule": [dict(item) for item in spec.input_schedule],
            "control_period_s": spec.timebase.control_period_s,
            "run_id": spec.run_id,
            "experiment_id": spec.experiment_id,
        },
    )


def _trace_problems(
    result: Any, contract: TraceContract, duration_s: float
) -> tuple[str, ...]:
    """Return validator problems without letting a protocol error crash the run."""

    if not isinstance(result, WholeRunResult):
        return (
            "whole-run engine returned "
            f"{type(result).__name__}, expected WholeRunResult",
        )
    try:
        return validate_whole_run_result(result, contract, duration_s=duration_s)
    except ValueError as exc:
        return (f"trace contract validation could not run: {exc}",)


def _trace_rows(result: WholeRunResult) -> list[dict[str, Any]]:
    """Convert a validated trace to the normal ``samples.json`` row shape."""

    signal_names = list(result.signals)
    rows: list[dict[str, Any]] = []
    for index, time_value in enumerate(result.time_s):
        row: dict[str, Any] = {"step_index": index, "time_s": float(time_value)}
        for name in signal_names:
            row[str(name)] = float(result.signals[name][index])
        rows.append(row)
    return rows


def _last_time_s(samples: list[dict[str, Any]]) -> float:
    return float(samples[-1]["time_s"]) if samples else 0.0


def _write_samples_npz(writer: ArtifactWriter, samples: list[dict[str, Any]]) -> None:
    """Write the optional numeric sidecar without making it a required artifact."""

    try:
        import numpy as np

        numeric_keys = [
            key
            for key in ("step_index", "time_s", "vout", "action", "action_requested")
            if samples and all(key in row for row in samples)
        ]
        if samples:
            first = samples[0]
            for key, value in first.items():
                if key in numeric_keys:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_keys.append(str(key))
        if numeric_keys:
            np.savez(
                writer.tmp / "samples.npz",
                **{
                    key: np.asarray([row[key] for row in samples])
                    for key in numeric_keys
                },
            )
    except Exception:
        # The sidecar is a convenience; samples.json is the evidence.
        return


def _blocking_category(report: CheckReport, fallback: str) -> str:
    for finding in report.findings:
        if finding.blocking:
            return finding.category
    return fallback


def run_whole_run_route(
    spec: ExperimentSpec,
    plant: Any,
    controller: Any,
    *,
    mode: str,
    options: RunOptions | None,
    checkpoint_interval_steps: int | None,
    resume_from: str | Path | None,
    interrupt_after_steps: int | None,
    cancel_after_steps: int | None,
    measurement_key: str | None,
    qualification_checker: (
        Callable[[list[Mapping[str, Any]]], tuple[bool, list[str]]] | None
    ),
    action_policy: Any,
    safety_plugins: tuple[Any, ...],
    validity_plugins: tuple[Any, ...],
    qualification_plugins: tuple[Any, ...],
    control_location: str = CONTROL_LOCATION_MODEL,
    event_granularity: str = EVENT_GRANULARITY_WINDOW,
) -> RunResult:
    """Execute one whole-run experiment and publish a normal run package.

    The function is deliberately linear: every refusal happens before
    ``ArtifactWriter`` is constructed, and the engine is called exactly once.
    """

    # An engine that owns the clock cannot leave the actuator to the kernel.
    # The record therefore refuses ``control_location='kernel'`` before any
    # refusal logic, filesystem work, or engine call.
    route = RouteCapabilities(
        time_model=TIME_MODEL_WHOLE_RUN,
        control_location=control_location,
        event_granularity=event_granularity,
    )

    applicability_policy = (
        options.applicability if options is not None else ApplicabilityPolicy()
    ).resolve(mode)
    _refuse_unsupported_requests(
        spec,
        plant,
        mode=mode,
        checkpoint_interval_steps=checkpoint_interval_steps,
        resume_from=resume_from,
        interrupt_after_steps=interrupt_after_steps,
        cancel_after_steps=cancel_after_steps,
        applicability_policy=applicability_policy,
    )
    audit = AuditRecorder(enabled=options.audit.enabled if options is not None else True)
    contract = _resolve_trace_contract(plant, audit)
    request = _build_request(spec)
    progress_policy = options.progress if options is not None else None
    cancel_token = options.cancellation if options is not None else None
    event_emitter = RunEventEmitter(
        progress_policy.sink if progress_policy is not None else None
    )

    # Everything below this line is still pre-filesystem: no writer exists, so
    # a refusal here cannot leave a partial package behind.
    worktree = analyze_git_worktree()
    git = collect_git_provenance()
    validate_run_location(spec.output_dir, spec.run_id)
    # A direct caller can reach this route without ``Runner.run``'s preflight.
    # The adapter/registration agreement is therefore checked here too, still
    # before ``ArtifactWriter`` exists.
    _require_registered_adapter(spec, plant)
    plant_manifest_preflight = _component_manifest(
        spec.plant_id, plant, configuration=spec.plant_config
    )
    controller_manifest_preflight = _component_manifest(
        spec.controller_id, controller, configuration=spec.controller_config
    )
    environment_preflight = collect_environment_provenance((plant, controller))
    applicability_claim = collect_model_claim(plant)
    applicability_provenance = collect_parameter_provenance(plant)
    applicability_plugin = (
        ModelEnvelopePlugin(
            applicability_claim,
            applicability_policy,
            provenance=applicability_provenance,
            actuator_bounds=_action_policy_bounds(action_policy),
        )
        if applicability_policy.enabled
        else None
    )
    applicability_chain: tuple[Any, ...] = (
        (applicability_plugin,) if applicability_plugin is not None else ()
    )
    applicability_report = CheckReport.ok()
    if applicability_plugin is not None:
        applicability_report = run_plugins(
            applicability_chain,
            "check_preflight",
            spec,
            CheckContext(
                spec=spec,
                phase="preflight",
                metadata={
                    "run_mode": mode,
                    "time_model": TIME_MODEL_WHOLE_RUN,
                    "enforcement": POST_HOC_ONLY,
                },
            ),
        )
        if not applicability_report.passed:
            raise ApplicabilityError(
                "model envelope rejected the run preflight",
                applicability_report,
                phase="preflight",
            )
    abandoned_runs = scan_abandoned_runs(spec.output_dir)
    writer = ArtifactWriter(spec.output_dir, spec.run_id)
    writer.write_json(
        "preflight.json",
        _preflight_document(
            spec,
            mode=mode,
            claim=applicability_claim,
            provenance=applicability_provenance,
            plugin=applicability_plugin,
            report=applicability_report,
            environment=environment_preflight,
            git=git,
            worktree=worktree,
        ),
    )

    machine = LifecycleStateMachine()
    machine.transition("RUNNING", "run_started")
    samples: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    trace_problems: tuple[str, ...] = ()
    error: str | None = None
    failure: dict[str, Any] | None = None
    capabilities: tuple[Any, Any] | None = None
    qualification: dict[str, Any] = {
        "passed": False,
        "level": "failed",
        "reasons": ["not_run"],
        "limitations": [],
    }
    safety_report = CheckReport.ok()
    validity_report = CheckReport.ok()
    qualification_report = CheckReport.ok()
    primary_key_for_checks = measurement_key or str(
        spec.plant_config.get(
            "primary_measurement",
            spec.controller_config.get("measurement_key", "vout"),
        )
    )
    validity_chain: tuple[Any, ...] = ()
    qualification_chain: tuple[Any, ...] = ()
    safety_chain: tuple[Any, ...] = tuple(safety_plugins)

    event_emitter.emit(
        EVENT_RUN_STARTED,
        spec.run_id,
        {
            "plant_id": spec.plant_id,
            "controller_id": spec.controller_id,
            "mode": mode,
            "time_model": TIME_MODEL_WHOLE_RUN,
            "duration_s": spec.timebase.duration_s,
            "control_period_s": spec.timebase.control_period_s,
            "engine_owned_time": True,
            "steps_total": None,
        },
    )

    if cancel_token is not None and cancel_token.cancelled():
        failure = {
            "category": "cancelled",
            "exception_type": "SimulationCancelledError",
            "message": "cancellation requested by the client",
            "phase": "runtime",
            "step_index": 0,
            "time_s": 0.0,
            "recoverable": True,
            "checkpoint_available": False,
            "cancellation_observed": "before_engine_call",
        }
        error = f"{SimulationCancelledError.__name__}: {failure['message']}"
        events.append({"event": "failure", **failure})
        machine.transition("INCOMPLETE", "cancelled", step_index=0, time_s=0.0)
        qualification = qualification_document(
            qualification_report,
            level="failed",
            reasons=["incomplete", "cancelled"],
        )
    else:
        event_emitter.emit(
            EVENT_RUN_PROGRESS,
            spec.run_id,
            {
                "phase": "engine_start",
                "sample_count": 0,
                "samples_total": None,
                "time_s": 0.0,
                "fraction": 0.0,
            },
        )
        try:
            capabilities = _negotiate_capabilities(spec, plant, controller)
            result = audit.call(
                "plant", "run_whole", lambda: plant.run_whole(request)
            )
            cancelled_after = cancel_token is not None and cancel_token.cancelled()
            trace_problems = _trace_problems(
                result, contract, spec.timebase.duration_s
            )
            if not trace_problems:
                samples = _trace_rows(result)
            completed_time_s = _last_time_s(samples) if samples else 0.0
            event_emitter.emit(
                EVENT_RUN_PROGRESS,
                spec.run_id,
                {
                    "phase": "engine_return",
                    "sample_count": len(samples),
                    "samples_total": len(samples),
                    "time_s": completed_time_s,
                    "fraction": 1.0,
                },
            )
            if cancelled_after:
                failure = {
                    "category": "cancelled",
                    "exception_type": "SimulationCancelledError",
                    "message": "cancellation requested by the client",
                    "phase": "runtime",
                    "step_index": len(samples),
                    "time_s": completed_time_s,
                    "recoverable": True,
                    "checkpoint_available": False,
                    # The engine completed before the request was observed.
                    # The existing cancellation semantics still win: the
                    # client asked to stop, so the package is INCOMPLETE and
                    # says exactly how far the run had already progressed.
                    "cancellation_observed": "after_engine_return",
                }
                error = f"{SimulationCancelledError.__name__}: {failure['message']}"
                events.append({"event": "failure", **failure})
                reasons = ["incomplete", "cancelled"]
                if trace_problems:
                    events.append(
                        {
                            "event": TRACE_CONTRACT_VIOLATION,
                            "category": TRACE_CONTRACT_VIOLATION,
                            "problems": list(trace_problems),
                            "step_index": len(samples),
                            "time_s": completed_time_s,
                        }
                    )
                    reasons.append(TRACE_CONTRACT_VIOLATION)
                machine.transition(
                    "INCOMPLETE",
                    "cancelled",
                    step_index=len(samples),
                    time_s=completed_time_s,
                )
                qualification = qualification_document(
                    qualification_report, level="failed", reasons=reasons
                )
            elif trace_problems:
                violation = _TraceContractViolation(
                    "whole-run trace did not satisfy its declared TraceContract: "
                    + "; ".join(trace_problems)
                )
                error = f"{type(violation).__name__}: {violation}"
                failure = _failure_record(
                    violation, audit, step_index=0, time_s=0.0
                )
                # The problems are the evidence; keep them verbatim rather
                # than paraphrasing them into a single message.
                failure["problems"] = list(trace_problems)
                events.append(
                    {
                        "event": TRACE_CONTRACT_VIOLATION,
                        "category": TRACE_CONTRACT_VIOLATION,
                        "problems": list(trace_problems),
                        "step_index": 0,
                        "time_s": 0.0,
                    }
                )
                machine.transition(
                    "RUN_FAILED",
                    TRACE_CONTRACT_VIOLATION,
                    step_index=0,
                    time_s=0.0,
                )
                qualification = qualification_document(
                    qualification_report,
                    level="failed",
                    reasons=[TRACE_CONTRACT_VIOLATION],
                )
            else:
                postrun_metadata = _postrun_metadata(plant)
                context = CheckContext(
                    spec=spec,
                    step_index=len(samples),
                    current_time_s=completed_time_s,
                    primary_measurement=primary_key_for_checks,
                    phase="postrun",
                    metadata={
                        "time_model": TIME_MODEL_WHOLE_RUN,
                        "enforcement": POST_HOC_ONLY,
                        "post_hoc": True,
                        **postrun_metadata,
                    },
                )
                configured_validity = tuple(validity_plugins)
                if any(
                    isinstance(item, ObservationValidityPlugin)
                    for item in configured_validity
                ):
                    validity_chain = configured_validity
                else:
                    validity_chain = (
                        ObservationValidityPlugin(
                            required_measurements=(primary_key_for_checks,)
                        ),
                    ) + configured_validity
                qualification_chain = (BasicQualificationPlugin(),) + tuple(
                    qualification_plugins
                )
                safety_report = _run_plugins_audited(
                    safety_chain,
                    "check_samples",
                    samples,
                    context,
                    audit,
                    step_index=len(samples),
                    time_s=completed_time_s,
                )
                validity_report = _run_plugins_audited(
                    validity_chain,
                    "check_samples",
                    samples,
                    context,
                    audit,
                    step_index=len(samples),
                    time_s=completed_time_s,
                )
                post_hoc_failure: dict[str, Any] | None = None
                if safety_report.blocking:
                    post_hoc_failure = _record_post_hoc_failure(
                        SafetyCheckError(
                            "safety plugin rejected the post-hoc samples",
                            safety_report,
                            phase="postrun",
                        ),
                        safety_report,
                        audit,
                        events,
                        machine,
                        step_index=len(samples),
                        time_s=completed_time_s,
                    )
                elif validity_report.blocking:
                    post_hoc_failure = _record_post_hoc_failure(
                        DataValidityError(
                            "validity plugin rejected the post-hoc samples",
                            validity_report,
                            phase="postrun",
                        ),
                        validity_report,
                        audit,
                        events,
                        machine,
                        step_index=len(samples),
                        time_s=completed_time_s,
                    )
                if post_hoc_failure is not None:
                    failure = post_hoc_failure
                    error = (
                        f"{failure.get('exception_type')}: {failure.get('message')}"
                    )
                    qualification = qualification_document(
                        qualification_report,
                        level="failed",
                        reasons=[str(failure.get("category"))],
                    )
                elif not (safety_report.blocking or validity_report.blocking):
                    qualification_fn = qualification_checker or qualify_samples
                    legacy_qualification = audit.call(
                        "qualification",
                        "check",
                        lambda: qualification_fn(samples),
                        step_index=len(samples),
                        time_s=completed_time_s,
                    )
                    qualification_report = qualification_report.merge(
                        normalise_check_result(
                            legacy_qualification, plugin_id="legacy-qualification"
                        )
                    )
                    plugin_qualification = _run_plugins_audited(
                        qualification_chain,
                        "check_samples",
                        samples,
                        context,
                        audit,
                        step_index=len(samples),
                        time_s=completed_time_s,
                    )
                    qualification_report = qualification_report.merge(
                        plugin_qualification
                    )
                    qualification = qualification_document(qualification_report)
                    machine.transition(
                        "RUN_OK",
                        "completed",
                        step_index=len(samples),
                        time_s=completed_time_s,
                    )
                    if qualification_report.passed:
                        machine.transition(
                            "QUALIFIED",
                            "qualification_passed",
                            step_index=len(samples),
                            time_s=completed_time_s,
                        )
                    else:
                        machine.transition(
                            "DISQUALIFIED",
                            "qualification_failed",
                            step_index=len(samples),
                            time_s=completed_time_s,
                        )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failure = _failure_record(
                exc, audit, step_index=len(samples), time_s=_last_time_s(samples)
            )
            events.append({"event": "failure", **failure})
            if machine.status == "RUNNING":
                machine.transition(
                    "RUN_FAILED",
                    str(failure["category"]),
                    step_index=len(samples),
                    time_s=_last_time_s(samples),
                )
                qualification = qualification_document(
                    qualification_report,
                    level="failed",
                    reasons=[str(failure["category"])],
                )

    status = machine.status
    writer.write_json("config.snapshot.json", spec.to_dict())
    plant_manifest = _component_manifest(
        spec.plant_id,
        plant,
        capabilities[0] if capabilities else None,
        spec.plant_config,
    )
    controller_manifest = _component_manifest(
        spec.controller_id,
        controller,
        capabilities[1] if capabilities else None,
        spec.controller_config,
    )
    environment = environment_preflight
    environment["git"] = {
        "source_commit": git.source_commit,
        "branch": git.branch,
        "working_tree_status": git.working_tree_status,
    }
    writer.write_json("environment.json", environment)
    writer.write_json("qualification.json", qualification)
    safety_passed = (
        error is None
        and safety_report.passed
        and validity_report.passed
        and applicability_report.passed
    )
    applicability_manifest = _applicability_manifest(
        applicability_plugin, applicability_report
    )
    writer.write_json(
        "safety.json",
        {
            "passed": safety_passed,
            "error": error,
            "checks": safety_report.to_dict(),
            "validity": validity_report.to_dict(),
            "applicability": applicability_manifest,
            "enforcement": POST_HOC_ONLY,
        },
    )
    writer.write_json(
        "metrics.json",
        {
            "schema_version": "run-metrics-v1",
            "sample_schema_version": "samples-v1",
            "sample_count": len(samples),
            "audit_call_count": len(audit.calls),
            "progress_events_emitted": event_emitter.delivered,
            "progress_events_dropped": event_emitter.dropped,
        },
    )
    writer.write_json("events.json", events)
    writer.write_json("state_transitions.json", machine.transitions)
    writer.write_json("audit.json", audit.to_dict())
    writer.write_json("samples.json", samples)
    _write_samples_npz(writer, samples)
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
    evidence_level = _derive_evidence_level(
        status,
        mode,
        worktree,
        environment,
        plant_manifest,
        qualification,
        capabilities=route,
    )
    manifest = _build_run_manifest(
        spec,
        time_model=TIME_MODEL_WHOLE_RUN,
        control_location=route.control_location,
        event_granularity=route.event_granularity,
        mode=mode,
        git_fields=git_fields,
        provenance=provenance,
        worktree_manifest=worktree_manifest,
        worktree_analysis_sha256=worktree_evidence_sha256,
        environment=environment,
        plant_manifest=plant_manifest,
        controller_manifest=controller_manifest,
        action_policy_manifest=_action_policy_manifest(action_policy),
        applicability_manifest=applicability_manifest,
        status=status,
        qualification=qualification,
        safety_passed=safety_passed,
        evidence_level=evidence_level,
        error=error,
        failure=failure,
        recovery={
            "requested": False,
            "resumed": False,
            "source": None,
        },
        abandoned_runs=abandoned_runs,
        capabilities=(
            {
                "plant": capabilities[0].to_dict(),
                "controller": capabilities[1].to_dict(),
            }
            if capabilities
            else {}
        ),
        safety_report=safety_report,
        validity_report=validity_report,
        qualification_report=qualification_report,
        applicability_report=applicability_report,
        safety_chain=safety_chain,
        validity_chain=validity_chain,
        qualification_chain=qualification_chain,
        applicability_chain=applicability_chain,
        audit=audit,
        machine=machine,
        extra={
            "route_limitations": list(route_inherent_limitations_for(route)),
            "enforcement": POST_HOC_ONLY,
        },
    )
    log_document: dict[str, Any] = {
        "status": status,
        "error": error,
        "sample_count": len(samples),
        "time_model": TIME_MODEL_WHOLE_RUN,
    }
    if trace_problems:
        log_document["problems"] = list(trace_problems)
    writer.write_json("logs/run.json", log_document)
    manifest["artifacts"] = artifact_index(writer.tmp)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(writer.tmp, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    writer.write_json("manifest.json", manifest)
    run_dir = writer.finalize()
    event_emitter.emit(
        EVENT_RUN_FINISHED,
        spec.run_id,
        {
            "status": status,
            "run_dir": str(run_dir),
            "sample_count": len(samples),
            "qualification_level": qualification.get("level"),
            "evidence_level": evidence_level,
            "error": error,
            "failure_category": failure.get("category")
            if isinstance(failure, Mapping)
            else None,
        },
    )
    return RunResult(
        run_dir,
        status,
        qualification,
        evidence_level,
        failure.get("category") if isinstance(failure, Mapping) else None,
    )


def _negotiate_capabilities(
    spec: ExperimentSpec, plant: Any, controller: Any
) -> tuple[Any, Any]:
    """Reuse the public capability negotiation without requiring a controller."""

    from .contracts import negotiate_capabilities

    plant_caps = (
        plant.capabilities() if callable(getattr(plant, "capabilities", None)) else set()
    )
    controller_caps = (
        controller.capabilities()
        if callable(getattr(controller, "capabilities", None))
        else None
    )
    return negotiate_capabilities(
        plant_caps,
        controller_caps,
        spec.timebase,
        spec.required_capabilities,
        spec.input_schedule,
    )


def _postrun_metadata(plant: Any) -> dict[str, Any]:
    """Collect descriptive engine health for qualification plugins."""

    evidence_fn = getattr(plant, "backend_evidence", None)
    if not callable(evidence_fn):
        return {}
    try:
        evidence = evidence_fn()
    except Exception:
        return {}
    if not isinstance(evidence, Mapping):
        return {}
    health = evidence.get("numerical_health")
    if not isinstance(health, Mapping):
        return {}
    return {"backend_numerical_health": json_safe(health)}


def _record_post_hoc_failure(
    exc: Exception,
    report: CheckReport,
    audit: AuditRecorder,
    events: list[dict[str, Any]],
    machine: LifecycleStateMachine,
    *,
    step_index: int,
    time_s: float,
) -> dict[str, Any]:
    """Publish a post-hoc check failure with its findings attached."""

    failure = _failure_record(exc, audit, step_index=step_index, time_s=time_s)
    failure["findings"] = [item.to_dict() for item in report.findings]
    events.append({"event": "failure", **failure})
    machine.transition(
        "RUN_FAILED", str(failure["category"]), step_index=step_index, time_s=time_s
    )
    return failure


__all__ = ["TRACE_CONTRACT_VIOLATION", "run_whole_run_route"]
