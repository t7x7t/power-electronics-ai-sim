"""Minimal auditable simulation runtime."""

import sys

from .contracts import (
    ActionRequest,
    CapabilityNegotiationError,
    MissingCapabilityError,
    UnsupportedCapabilityError,
    CapabilityConfigurationMismatchError,
    CapabilityRuntimeUnavailableError,
    SimulationExecutionError,
    NumericalNonconvergenceError,
    BackendTimeoutError,
    BackendProcessError,
    SimulationCancelledError,
    PlantAdvanceError,
    ModelNumericalError,
    ConvergenceError,
    BackendTimeout,
    BackendProcessFailure,
    PlantExecutionError,
    CapabilitySet,
    ExperimentSpec,
    PlantObservation,
    Timebase,
    ensure_schema_compatible,
    validate_experiment_document,
    validate_safe_identifier,
    negotiate_capabilities,
    snapshot_digest,
    snapshot_file_hash,
    validate_observation_visibility,
)
from .runtime import Runner, RunResult, FakePlant, FakeLoadPlant, FakePIController
from .runner import ActionPolicy, AuditPolicy, BoundedActionPolicy, FiniteActionPolicy, LifecycleTransitionError, RecoveryPolicy, RunOptions, TimingPolicy, run_experiment
from .reference_plants import BuckPlant, BoostPlant
from .applicability import (
    ApplicabilityError,
    ApplicabilityPolicy,
    ApplicabilitySummary,
    ModelEnvelopePlugin,
)
from .model_claim import (
    ModelClaim,
    NumericalLimit,
    OperatingRange,
    ParameterProvenance,
    ParameterRecord,
    collect_model_claim,
    collect_parameter_provenance,
)
from .qualification import (
    BasicQualificationPlugin,
    QualificationOutcome,
    qualification_document,
    qualify_samples,
)
from .qualification_rules import (
    AnalyticalEquilibriumRule,
    EquilibriumTolerance,
    NumericalHealthRule,
    PlantOracle,
    QualificationRulePack,
    SteadyStateRule,
    SteadyStateThresholds,
)
from .numerical_health import NumericalHealth, parse_log
from .credibility import (
    CredibilityAssessment,
    CredibilityFactor,
    CredibilityPolicy,
    assess_credibility,
    assess_manifest,
)
from .attestation import (
    ATTESTATION_VERIFICATION_SCHEMA,
    ATTESTATION_WRITE_SCHEMA,
    AnchorRecord,
    AttestationDocument,
    AttestationPayload,
    AttestationVerification,
    AttestationWriteResult,
    GitAnchorStatus,
    SignatureRecord,
    attest_run,
    build_payload,
    payload_digest,
    register_signature_verifier,
    verify_attestation,
    verify_git_anchor,
)
from .error_codes import (
    KERNEL_FAILURE_CODES,
    REJECTION_SCHEMA,
    REQUEST_REJECTION_CODES,
    RequestRejected,
    rejection_document,
)
from .inspection import INSPECTION_SCHEMA, RunInspection, RunInspectionError, inspect_run
from .claim import CLAIM_CONCLUSIONS, CLAIM_RULE_VERSION, CLAIM_SCHEMA, ClaimConclusion, derive_claim
from .time_model import (
    RUNTIME_EVIDENCE_LEVELS,
    TIME_MODELS,
    TIME_MODEL_STEPPED,
    TIME_MODEL_WHOLE_RUN,
    TRACE_CONTRACT_SCHEMA,
    TraceContract,
    WholeRunPlant,
    WholeRunRequest,
    WholeRunResult,
    cap_evidence_level,
    evidence_cap,
    formal_comparison_allowed,
    route_limitations,
    validate_whole_run_result,
)
from .catalog import CatalogError, RunCatalog, RunEntry, scan_runs
from .compatibility import (
    COMPATIBILITY_SCHEMA,
    COMPATIBILITY_STATUSES,
    CompatibilityVerdict,
    assess_package_compatibility,
)
from .run_store import DEFAULT_RUNS_DIRNAME, RUNS_ROOT_ENV, discovery_root
from .cancellation import CallbackCancelToken, CancelToken, FileCancelToken
from .run_events import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_PROGRESS,
    EVENT_RUN_STARTED,
    RUN_EVENT_SCHEMA,
    RUN_RESULT_SCHEMA,
    ProgressPolicy,
    RunEvent,
    RunEventEmitter,
)
from .identity import implementation_digest, register_solve_path, review_status, verify_adapter_identity
from .plant_registry import (
    ENTRY_POINT_GROUP,
    PLANT_CATALOG_SCHEMA,
    TRUST_LEVELS,
    PlantRegistration,
    discover_entry_points,
    plant_catalog,
    plant_ids,
    register_plant,
    registered_plants,
    resolve as resolve_plant,
)
from .conformance import ConformanceReport, check_controller_adapter, check_plant_adapter
from .provenance import (
    BackendProvenanceAdapter,
    ExecutableBackendAdapter,
    GitProvenance,
    collect_backend_provenance,
    collect_environment_provenance,
    collect_git_provenance,
    not_assessed_backend,
)
from .artifacts import artifact_index, canonical_json, manifest_digest, package_digest, recover_abandoned_runs, scan_abandoned_runs, sha256_bytes, validate_run_location
from .dirty import DirtyAnalysis, FormalComparisonError, analyze_git_worktree, require_formal_comparison
from .checks import (
    CheckContext,
    CheckFailureError,
    CheckFinding,
    CheckReport,
    CheckResult,
    DataValidityError,
    QualificationCheckError,
    SafetyCheckError,
    SafetyPlugin,
    ValidityPlugin,
    QualificationPlugin,
    ObservationValidityPlugin,
)
from .environment import EnvironmentCheck, check_recommended_environment, environment_check_to_dict
from .reproducibility import ReproducibilityReport, RepetitionAudit, ToleranceSpec, audit_repeated_runs, audit_same_machine_runs, build_cross_machine_evidence, compare_runs, cross_machine_evidence
from .baseline import build_baseline_report, write_baseline_report
from .postprocess import (
    MetricDefinition,
    MetricRegistry,
    PostprocessEligibilityError,
    RunEvidence,
    UnknownMetricError,
    build_evidence_report,
    compare_metric_runs,
    default_metric_registry,
    export_metrics,
    load_run_evidence,
    summarize_run,
    write_evidence_report,
)
from .evidence import EvidenceClassificationError, classify_report, classify_stage10, classify_summary
from .topology import TOPOLOGY_SCHEMA, TopologyNet, TopologyPort, TopologyComponent, TopologyDescriptor, buck_topology_descriptor, boost_topology_descriptor, ngspice_buck_topology_descriptor, export_topology_descriptor, topology_to_dot, topology_to_adjacency
from .traces import (
    TRACE_SCHEMA_VERSION,
    SignalDefinition,
    Signal,
    SignalRegistry,
    SamplingPolicy,
    TraceProvenance,
    TraceSample,
    TracePoint,
    TraceDataset,
    TraceCollector,
    ObservationSink,
)
from .default_contracts import DEFAULT_CONTRACT_DEFINITIONS, default_contract_ref, default_contract_refs
from .outputs import (
    OUTPUT_SCHEMA_VERSION,
    OutputFieldDefinition,
    OutputDefinition,
    OutputRegistry,
    OutputEntry,
    OutputDataset,
    OutputCollector,
)
from .visualization_service import SERVICE_SCHEMA, VisualizationServiceError, create_server, serve
from .backends import (
    NETLIST_TEMPLATE_VERSION,
    BuckNetlistParameters,
    NativeBuckNetlistBuilder,
    NgspiceBuckPlant,
    NgspiceExecutableAdapter,
    NgspiceProcess,
    NgspiceRunResult,
    NgspiceSolverSettings,
    PyspiceBuckNetlistBuilder,
    PyspiceUnavailableError,
    TraceTable,
    WrdataParseError,
    BackendConfigError,
    BackendDescriptor,
    BackendNotFoundError,
    BackendResolution,
    InstallRecipe,
    config_template,
    current_platform,
    get_backend,
    list_backends,
    load_backend_config,
    netlist_digest,
    parse_ngspice_version,
    parse_wrdata,
    render_buck_netlist,
    resolve_backend_executable,
    resolve_ngspice_executable,
    scan_for_backend,
    scan_for_ngspice,
    user_config_path,
    write_user_config,
)

from .public_api import EXPERIMENTAL_API, STABLE_API

__all__ = sorted(STABLE_API | EXPERIMENTAL_API)

_missing = [name for name in __all__ if not hasattr(sys.modules[__name__], name)]
if _missing:
    raise RuntimeError(f"public API names are not defined: {_missing}")
