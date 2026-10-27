"""Machine-checkable public API stability surface for ``pe_sim``.

Classification rule (this is the whole contract; there is no deprecation
process and no version negotiation layered on top):

    A name is **stable** when an external adapter author or an evidence
    verifier needs it to (a) implement a Plant/Controller/check plugin
    against the documented contracts, (b) run an experiment through the
    public entry point, or (c) verify a published run package and its
    authenticity record. Everything else is **experimental** and may
    change in any minor release.

Practical consequences:

- A **stable** name is a promise. Renaming it, removing it, or changing its
  meaning is a breaking change for third-party adapters and for anyone
  re-verifying a published run package. Such a change is only allowed
  together with an explicit update of ``tests/fixtures/public_api.json``,
  which makes the change visible in review.
- An **experimental** name is intentionally reachable from the top level
  (so that nothing is hidden), but carries no compatibility promise. It may
  be renamed, reshaped, or removed in any minor release.
- A classification is a judgement about *who depends on the name*, not a
  statement about code quality. Moving a name between the two sets is a
  reviewable event, not a silent refactor.

``pe_sim.__all__`` is defined as ``sorted(STABLE_API | EXPERIMENTAL_API)``,
so the two sets together are exactly the documented top-level surface.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


API_STABILITY_SCHEMA = "pe-sim.public-api.v1"

# ---------------------------------------------------------------------------
# Stable surface
# ---------------------------------------------------------------------------
# Grouped by the contract a third party actually programs against. Comments
# name the dependency, not the module layout, because that is the reason a
# name is stable.
STABLE_API: frozenset[str] = frozenset(
    {
        # Experiment document, observation, capability negotiation, and the
        # error taxonomy an adapter is allowed to raise or must expect.
        "ActionRequest",
        "BackendProcessError",
        "BackendProcessFailure",
        "BackendTimeout",
        "BackendTimeoutError",
        "CapabilityConfigurationMismatchError",
        "CapabilityNegotiationError",
        "CapabilityRuntimeUnavailableError",
        "CapabilitySet",
        "ConvergenceError",
        "ExperimentSpec",
        "MissingCapabilityError",
        "ModelNumericalError",
        "NumericalNonconvergenceError",
        "PlantAdvanceError",
        "PlantExecutionError",
        "PlantObservation",
        "SimulationCancelledError",
        "SimulationExecutionError",
        "Timebase",
        "UnsupportedCapabilityError",
        "ensure_schema_compatible",
        "negotiate_capabilities",
        "validate_experiment_document",
        # Public entry point: options, policies, and the run loop.
        "ActionPolicy",
        "AuditPolicy",
        "BoundedActionPolicy",
        "FiniteActionPolicy",
        "LifecycleTransitionError",
        "RecoveryPolicy",
        "RunOptions",
        "TimingPolicy",
        "run_experiment",
        # Runtime surface returned to callers and the fakes shipped for
        # adapter authors to develop against.
        "FakeLoadPlant",
        "FakePIController",
        "FakePlant",
        "RunResult",
        "Runner",
        # Reference plants named by the documented examples.
        "BoostPlant",
        "BuckPlant",
        # Check plugin contract: context, findings, report, and errors.
        "CheckContext",
        "CheckFailureError",
        "CheckFinding",
        "CheckReport",
        "CheckResult",
        "DataValidityError",
        "ObservationValidityPlugin",
        "QualificationCheckError",
        "QualificationPlugin",
        "SafetyCheckError",
        "SafetyPlugin",
        "ValidityPlugin",
        # Qualification plugins and the helper that runs them.
        "BasicQualificationPlugin",
        "QualificationOutcome",
        "qualify_samples",
        # Package identity: what a verifier recomputes over a run directory.
        "artifact_index",
        "manifest_digest",
        "package_digest",
        # Read-only inspection of a published run package.
        "RunInspection",
        "RunInspectionError",
        "inspect_run",
        # Authenticity record: write it, verify it, read the verdict.
        "AttestationVerification",
        "AttestationWriteResult",
        "attest_run",
        "verify_attestation",
        # Model claim: the declared envelope a verifier compares against.
        "ModelClaim",
        "NumericalLimit",
        "OperatingRange",
        "ParameterProvenance",
        "ParameterRecord",
        # Applicability enforcement against a model claim.
        "ApplicabilityError",
        "ApplicabilityPolicy",
        "ApplicabilitySummary",
        "ModelEnvelopePlugin",
        # Credibility assessment recorded in the evidence chain.
        "CredibilityAssessment",
        "CredibilityFactor",
        "CredibilityPolicy",
        "assess_credibility",
        # Reproducibility comparison with a declared tolerance.
        "ReproducibilityReport",
        "ToleranceSpec",
        "compare_runs",
    }
)

# ---------------------------------------------------------------------------
# Experimental surface
# ---------------------------------------------------------------------------
# Everything else. Grouped by module so that a reviewer can see what is being
# kept reachable without compatibility promises.
EXPERIMENTAL_API: frozenset[str] = frozenset(
    {
        # topology / traces / outputs: stage-11 descriptive and streaming
        # surfaces whose schemas are still moving.
        "TOPOLOGY_SCHEMA",
        "TopologyComponent",
        "TopologyDescriptor",
        "TopologyNet",
        "TopologyPort",
        "boost_topology_descriptor",
        "buck_topology_descriptor",
        "export_topology_descriptor",
        "ngspice_buck_topology_descriptor",
        "topology_to_adjacency",
        "topology_to_dot",
        "TRACE_SCHEMA_VERSION",
        "ObservationSink",
        "SamplingPolicy",
        "Signal",
        "SignalDefinition",
        "SignalRegistry",
        "TraceCollector",
        "TraceDataset",
        "TracePoint",
        "TraceProvenance",
        "TraceSample",
        "OUTPUT_SCHEMA_VERSION",
        "OutputCollector",
        "OutputDataset",
        "OutputDefinition",
        "OutputEntry",
        "OutputFieldDefinition",
        "OutputRegistry",
        # visualization_service: local viewer, no external dependents.
        "SERVICE_SCHEMA",
        "VisualizationServiceError",
        "create_server",
        "serve",
        # postprocess: metrics are an export convenience, not a contract.
        "MetricDefinition",
        "MetricRegistry",
        "PostprocessEligibilityError",
        "RunEvidence",
        "UnknownMetricError",
        "build_evidence_report",
        "compare_metric_runs",
        "default_metric_registry",
        "export_metrics",
        "load_run_evidence",
        "summarize_run",
        "write_evidence_report",
        # baseline: report formatting.
        "build_baseline_report",
        "write_baseline_report",
        # evidence: stage-12 classification helpers.
        "EvidenceClassificationError",
        "classify_report",
        "classify_stage10",
        "classify_summary",
        # dirty / environment: local preconditions, reworked freely.
        "DirtyAnalysis",
        "FormalComparisonError",
        "analyze_git_worktree",
        "require_formal_comparison",
        "EnvironmentCheck",
        "check_recommended_environment",
        "environment_check_to_dict",
        # conformance: adapter self-checks that track the contracts.
        "ConformanceReport",
        "check_controller_adapter",
        "check_plant_adapter",
        # default_contracts: convenience references, not a schema.
        "DEFAULT_CONTRACT_DEFINITIONS",
        "default_contract_ref",
        "default_contract_refs",
        # backends: ngspice/pyspice adapter internals.
        "NETLIST_TEMPLATE_VERSION",
        "BuckNetlistParameters",
        "NativeBuckNetlistBuilder",
        "NgspiceBuckPlant",
        "NgspiceExecutableAdapter",
        "NgspiceProcess",
        "NgspiceRunResult",
        "NgspiceSolverSettings",
        "PyspiceBuckNetlistBuilder",
        "PyspiceUnavailableError",
        "TraceTable",
        "WrdataParseError",
        "BackendConfigError",
        "BackendDescriptor",
        "BackendNotFoundError",
        "BackendResolution",
        "InstallRecipe",
        "config_template",
        "current_platform",
        "get_backend",
        "list_backends",
        "load_backend_config",
        "netlist_digest",
        "parse_ngspice_version",
        "parse_wrdata",
        "render_buck_netlist",
        "resolve_backend_executable",
        "resolve_ngspice_executable",
        "scan_for_backend",
        "scan_for_ngspice",
        "user_config_path",
        "write_user_config",
        # provenance: collectors used by the built-in backends.
        "BackendProvenanceAdapter",
        "ExecutableBackendAdapter",
        "GitProvenance",
        "collect_backend_provenance",
        "collect_environment_provenance",
        "collect_git_provenance",
        "not_assessed_backend",
        # qualification_rules: rule-pack internals.
        "AnalyticalEquilibriumRule",
        "EquilibriumTolerance",
        "NumericalHealthRule",
        "PlantOracle",
        "QualificationRulePack",
        "SteadyStateRule",
        "SteadyStateThresholds",
        # numerical_health: log parsing helper.
        "NumericalHealth",
        "parse_log",
        # identity: implementation pin queries, not an adapter contract.
        "implementation_digest",
        "review_status",
        "verify_adapter_identity",
        "register_solve_path",
        # plant_registry: the extension point for third-party plants.  Kept
        # experimental on purpose: the registration shape may still change, and
        # adding a plant must not grow the stable surface.
        "ENTRY_POINT_GROUP",
        "PLANT_CATALOG_SCHEMA",
        "TRUST_LEVELS",
        "PlantRegistration",
        "discover_entry_points",
        "plant_catalog",
        "plant_ids",
        "register_plant",
        "registered_plants",
        "resolve_plant",
        # catalog: discovery of run packages.  Read-only, and explicitly not a
        # verification path, so it carries no compatibility promise yet.
        "CatalogError",
        "RunCatalog",
        "RunEntry",
        "scan_runs",
        # compatibility (Phase C, D4): read-with-policy verdicts for packages.
        "COMPATIBILITY_SCHEMA",
        "COMPATIBILITY_STATUSES",
        "CompatibilityVerdict",
        "assess_package_compatibility",
        # discovery root (Phase C, D3): explicit > PE_SIM_RUNS_ROOT > ./runs.
        "DEFAULT_RUNS_DIRNAME",
        "RUNS_ROOT_ENV",
        "discovery_root",
        # run interaction: versioned progress events, the terminal-result
        # schema tag, the rate policy, and cancellation tokens.  This is the
        # Phase B client surface; field sets may still grow.
        "EVENT_RUN_FINISHED",
        "EVENT_RUN_PROGRESS",
        "EVENT_RUN_STARTED",
        "RUN_EVENT_SCHEMA",
        "RUN_RESULT_SCHEMA",
        "ProgressPolicy",
        "RunEvent",
        "RunEventEmitter",
        "CancelToken",
        "CallbackCancelToken",
        "FileCancelToken",
        # rejection and failure codes: the branchable vocabulary for a client.
        # Experimental until a client has depended on it for a release.
        "KERNEL_FAILURE_CODES",
        "REJECTION_SCHEMA",
        "REQUEST_REJECTION_CODES",
        "RequestRejected",
        "rejection_document",
        # evidence-reading views: schema tags for inspect-run / attest outputs.
        "INSPECTION_SCHEMA",
        "ATTESTATION_VERIFICATION_SCHEMA",
        "ATTESTATION_WRITE_SCHEMA",
        # claim conclusion: one read-only answer to "what may I claim?".
        "CLAIM_CONCLUSIONS",
        "CLAIM_RULE_VERSION",
        "CLAIM_SCHEMA",
        "ClaimConclusion",
        "derive_claim",
        # time model and engine boundary (Phase A2): who owns the clock, what a
        # trace contract promises, and how a route is priced.  The whole-run
        # execution route is declared but not implemented yet.
        "RUNTIME_EVIDENCE_LEVELS",
        "TIME_MODELS",
        "TIME_MODEL_STEPPED",
        "TIME_MODEL_WHOLE_RUN",
        "TRACE_CONTRACT_SCHEMA",
        "TraceContract",
        "WholeRunPlant",
        "WholeRunRequest",
        "WholeRunResult",
        "cap_evidence_level",
        "evidence_cap",
        "formal_comparison_allowed",
        "route_limitations",
        "validate_whole_run_result",
        # attestation: record internals below the verification verdict.
        "AnchorRecord",
        "AttestationDocument",
        "AttestationPayload",
        "GitAnchorStatus",
        "SignatureRecord",
        "build_payload",
        "payload_digest",
        "register_signature_verifier",
        "verify_git_anchor",
        # model_claim / credibility helpers that only assemble inputs.
        "collect_model_claim",
        "collect_parameter_provenance",
        "assess_manifest",
        # reproducibility helpers beyond the compared report.
        "RepetitionAudit",
        "audit_repeated_runs",
        "audit_same_machine_runs",
        "build_cross_machine_evidence",
        "cross_machine_evidence",
        # artifacts helpers used while recovering interrupted runs.
        "canonical_json",
        "recover_abandoned_runs",
        "scan_abandoned_runs",
        "sha256_bytes",
        "validate_run_location",
        # contract validation helpers with no documented downstream use.
        "snapshot_digest",
        "snapshot_file_hash",
        "validate_observation_visibility",
        "validate_safe_identifier",
        # qualification document formatting.
        "qualification_document",
        # study layer (S7): plan and execute parameter sweeps, Monte Carlo and
        # periodicity studies; the aggregate is per-point, never comparable.
        "MAX_STUDY_POINTS",
        "STATISTIC_REDUCERS",
        "STUDY_AGGREGATE_SCHEMA",
        "STUDY_COMPARABILITY_REASONS",
        "STUDY_KINDS",
        "STUDY_LIMITATIONS",
        "STUDY_POINT_EVIDENCE_LEVEL",
        "STUDY_REPORT_SCHEMA",
        "STUDY_SCHEMA",
        "StudyPlan",
        "StudyPlanError",
        "StudyPoint",
        "StudyPointRecord",
        "StudyRunResult",
        "aggregate_study",
        "build_study_report",
        "evaluate_periodicity",
        "monte_carlo_draws",
        "monte_carlo_draws_sha256",
        "parse_study_document",
        "run_study",
        "statistics_for_point",
        "write_study_report",
        # model capability seam (S8): declared small-signal and loss models.
        "CAPABILITY_ORIGINS",
        "CAPABILITY_SCHEMA",
        "CAPABILITY_UNAVAILABLE_REASONS",
        "LINEARIZATION_METHODS",
        "CapabilityUnavailable",
        "LossBreakdown",
        "OperatingPoint",
        "SmallSignalModel",
        "capability_provenance",
        "load_builtin_model_capabilities",
        "loss_model_for",
        "register_loss_model",
        "register_small_signal_model",
        "registered_model_capabilities",
        "small_signal_model_for",
        "validate_capability_provenance",
        # frequency-domain analysis: sweeps, loop gain, stability margins.
        "AC_RESULT_SCHEMA",
        "frequency_response",
        "loop_gain_response",
        "stability_margins",
        # impedance sweeps and the Middlebrook magnitude screen.
        "IMPEDANCE_KINDS",
        "IMPEDANCE_SCHEMA",
        "impedance_response",
        "middlebrook_ratio",
        # declared lossy converter adapter and its opt-in registration.
        "LossyBuckPlant",
        "register_loss_provider",
    }
)

if not STABLE_API or not EXPERIMENTAL_API:
    raise RuntimeError("public API classification must not be empty")

_overlap = STABLE_API & EXPERIMENTAL_API
if _overlap:
    raise RuntimeError(
        f"public API classification is ambiguous: {sorted(_overlap)}"
    )


def _surface() -> dict[str, list[str]]:
    return {
        "stable": sorted(STABLE_API),
        "experimental": sorted(EXPERIMENTAL_API),
    }


def public_api() -> dict[str, Any]:
    """Return the classified public surface together with a content digest."""

    surface = _surface()
    payload = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    return {
        "schema": API_STABILITY_SCHEMA,
        "stable": surface["stable"],
        "experimental": surface["experimental"],
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


__all__ = ["API_STABILITY_SCHEMA", "EXPERIMENTAL_API", "STABLE_API", "public_api"]
