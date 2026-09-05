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
from .provenance import GitProvenance, collect_environment_provenance, collect_git_provenance
from .artifacts import artifact_index, canonical_json, manifest_digest, package_digest, recover_abandoned_runs, scan_abandoned_runs, sha256_bytes
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
from .reproducibility import ReproducibilityReport, compare_runs
from .baseline import build_baseline_report, write_baseline_report

__all__ = ["ActionRequest", "CapabilitySet", "CapabilityNegotiationError", "MissingCapabilityError", "UnsupportedCapabilityError", "CapabilityConfigurationMismatchError", "CapabilityRuntimeUnavailableError", "SimulationExecutionError", "NumericalNonconvergenceError", "BackendTimeoutError", "BackendProcessError", "SimulationCancelledError", "PlantAdvanceError", "ModelNumericalError", "ConvergenceError", "BackendTimeout", "BackendProcessFailure", "PlantExecutionError", "ExperimentSpec", "PlantObservation", "Runner", "RunResult", "RunOptions", "TimingPolicy", "RecoveryPolicy", "AuditPolicy", "ActionPolicy", "BoundedActionPolicy", "FiniteActionPolicy", "LifecycleTransitionError", "run_experiment", "Timebase", "FakePlant", "FakeLoadPlant", "FakePIController", "BuckPlant", "BoostPlant", "BasicQualificationPlugin", "qualify_samples", "ConformanceReport", "check_controller_adapter", "check_plant_adapter", "GitProvenance", "collect_git_provenance", "collect_environment_provenance", "artifact_index", "canonical_json", "manifest_digest", "package_digest", "scan_abandoned_runs", "recover_abandoned_runs", "sha256_bytes", "DirtyAnalysis", "FormalComparisonError", "analyze_git_worktree", "require_formal_comparison", "ensure_schema_compatible", "negotiate_capabilities", "snapshot_digest", "snapshot_file_hash", "validate_observation_visibility", "CheckContext", "CheckFailureError", "CheckFinding", "CheckReport", "CheckResult", "DataValidityError", "QualificationCheckError", "SafetyCheckError", "SafetyPlugin", "ValidityPlugin", "QualificationPlugin", "ObservationValidityPlugin", "EnvironmentCheck", "check_recommended_environment", "environment_check_to_dict", "ReproducibilityReport", "compare_runs", "build_baseline_report", "write_baseline_report"]
