"""Minimal auditable simulation runtime."""

from .contracts import (
    ActionRequest,
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
from .reference_plants import BuckPlant, BoostPlant
from .conformance import ConformanceReport, check_controller_adapter, check_plant_adapter
from .provenance import GitProvenance, collect_git_provenance
from .dirty import DirtyAnalysis, FormalComparisonError, analyze_git_worktree, require_formal_comparison

__all__ = ["ActionRequest", "CapabilitySet", "ExperimentSpec", "PlantObservation", "Runner", "RunResult", "Timebase", "FakePlant", "FakeLoadPlant", "FakePIController", "BuckPlant", "BoostPlant", "ConformanceReport", "check_controller_adapter", "check_plant_adapter", "GitProvenance", "collect_git_provenance", "DirtyAnalysis", "FormalComparisonError", "analyze_git_worktree", "require_formal_comparison", "ensure_schema_compatible", "negotiate_capabilities", "snapshot_digest", "snapshot_file_hash", "validate_observation_visibility"]
