"""Minimal auditable simulation runtime."""

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
from .qualification import BasicQualificationPlugin, qualify_samples
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
from .reproducibility import ReproducibilityReport, RepetitionAudit, audit_repeated_runs, audit_same_machine_runs, build_cross_machine_evidence, compare_runs, cross_machine_evidence
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
from .topology import TOPOLOGY_SCHEMA, TopologyNet, TopologyPort, TopologyComponent, TopologyDescriptor, buck_topology_descriptor, boost_topology_descriptor, export_topology_descriptor, topology_to_dot, topology_to_adjacency
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

__all__ = ["ActionRequest", "CapabilitySet", "CapabilityNegotiationError", "MissingCapabilityError", "UnsupportedCapabilityError", "CapabilityConfigurationMismatchError", "CapabilityRuntimeUnavailableError", "SimulationExecutionError", "NumericalNonconvergenceError", "BackendTimeoutError", "BackendProcessError", "SimulationCancelledError", "PlantAdvanceError", "ModelNumericalError", "ConvergenceError", "BackendTimeout", "BackendProcessFailure", "PlantExecutionError", "ExperimentSpec", "PlantObservation", "Runner", "RunResult", "RunOptions", "TimingPolicy", "RecoveryPolicy", "AuditPolicy", "ActionPolicy", "BoundedActionPolicy", "FiniteActionPolicy", "LifecycleTransitionError", "run_experiment", "Timebase", "FakePlant", "FakeLoadPlant", "FakePIController", "BuckPlant", "BoostPlant", "BasicQualificationPlugin", "qualify_samples", "ConformanceReport", "check_controller_adapter", "check_plant_adapter", "BackendProvenanceAdapter", "ExecutableBackendAdapter", "GitProvenance", "collect_backend_provenance", "collect_git_provenance", "collect_environment_provenance", "not_assessed_backend", "artifact_index", "canonical_json", "manifest_digest", "package_digest", "scan_abandoned_runs", "recover_abandoned_runs", "sha256_bytes", "validate_run_location", "DirtyAnalysis", "FormalComparisonError", "analyze_git_worktree", "require_formal_comparison", "ensure_schema_compatible", "validate_experiment_document", "validate_safe_identifier", "negotiate_capabilities", "snapshot_digest", "snapshot_file_hash", "validate_observation_visibility", "CheckContext", "CheckFailureError", "CheckFinding", "CheckReport", "CheckResult", "DataValidityError", "QualificationCheckError", "SafetyCheckError", "SafetyPlugin", "ValidityPlugin", "QualificationPlugin", "ObservationValidityPlugin", "EnvironmentCheck", "check_recommended_environment", "environment_check_to_dict", "ReproducibilityReport", "RepetitionAudit", "compare_runs", "audit_repeated_runs", "audit_same_machine_runs", "cross_machine_evidence", "build_cross_machine_evidence", "build_baseline_report", "write_baseline_report", "PostprocessEligibilityError", "UnknownMetricError", "MetricDefinition", "MetricRegistry", "default_metric_registry", "RunEvidence", "load_run_evidence", "summarize_run", "export_metrics", "compare_metric_runs", "build_evidence_report", "write_evidence_report", "EvidenceClassificationError", "classify_summary", "classify_report", "classify_stage10", "TRACE_SCHEMA_VERSION", "SignalDefinition", "Signal", "SignalRegistry", "SamplingPolicy", "TraceProvenance", "TraceSample", "TracePoint", "TraceDataset", "TraceCollector", "ObservationSink", "DEFAULT_CONTRACT_DEFINITIONS", "default_contract_ref", "default_contract_refs"]

__all__ += ['TOPOLOGY_SCHEMA','TopologyNet','TopologyPort','TopologyComponent','TopologyDescriptor','buck_topology_descriptor','boost_topology_descriptor','export_topology_descriptor','topology_to_dot','topology_to_adjacency']
__all__ += ['OUTPUT_SCHEMA_VERSION','OutputFieldDefinition','OutputDefinition','OutputRegistry','OutputEntry','OutputDataset','OutputCollector']
__all__ += ['SERVICE_SCHEMA','VisualizationServiceError','create_server','serve']
