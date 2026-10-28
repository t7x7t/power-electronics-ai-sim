"""Route parity: stepped and whole_run publish the same evidence shape.

Both execution routes call ``runtime._build_run_manifest``, but the artifacts
themselves are written by two separate code paths (``Runner.run`` and
``whole_run_route.run_whole_run_route``).  Nothing in the product couples the
two writers, so this module guards the *published* shape instead of the shared
helper:

1. both routes publish the shared required artifacts;
2. both routes publish exactly the same artifact key set, with no artifact
   outside the measured shape;
3. the route-specific artifact difference is an explicit constant (currently
   empty) rather than an accident;
4. both manifests carry the shared top-level keys;
5. the whole-run route declares its evidence cap and limitations; and
6. the one artifact a route can legitimately lack is the stepped checkpoint,
   which the whole-run route refuses before it touches disk.

Measured shape (probe run, seed 17, exploratory mode): both routes publish the
same 13 artifact keys.  ``manifest.json`` is deliberately *not* one of them --
``pe_sim.artifacts.artifact_index`` skips the manifest and the ``.run-state``
publication marker -- so the manifest is asserted as a file on disk instead of
as an index entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pe_sim.builtin_plants import PlantBuildContext, load_builtin_plants
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.plant_registry import get_plant
from pe_sim.reference_plants import BuckPlant
from pe_sim.runtime import FakePIController, Runner

#: Artifacts both routes must publish.  ``manifest.json`` is omitted on
#: purpose: ``artifact_index`` excludes it from ``manifest["artifacts"]``, so
#: it is asserted as a file below.  The measured intersection covers every
#: other JSON evidence document plus the run log.
SHARED_REQUIRED_ARTIFACTS = frozenset(
    {
        "audit.json",
        "config.snapshot.json",
        "environment.json",
        "events.json",
        "logs/run.json",
        "metrics.json",
        "preflight.json",
        "qualification.json",
        "safety.json",
        "samples.json",
        "state_transitions.json",
    }
)

#: Measured artifacts that are either writer plumbing (``logs/.keep`` keeps the
#: log directory in the package) or an explicitly optional sidecar
#: (``samples.npz`` is skipped when numpy is unavailable; ``samples.json`` is
#: the evidence).  They are allowed on both routes, never required.
MEASURED_OPTIONAL_ARTIFACTS = frozenset({"logs/.keep", "samples.npz"})

#: Route-specific artifacts measured on plain exploratory runs.  Both sets are
#: empty: the two routes publish identical keys, so any addition here is drift
#: that must be reviewed rather than silently accepted.
EXPECTED_STEPPED_ONLY: frozenset[str] = frozenset()
EXPECTED_WHOLE_RUN_ONLY: frozenset[str] = frozenset()

#: Manifest keys both routes must carry.  This is the evidence contract a
#: consumer may rely on without asking which route produced the package.
SHARED_MANIFEST_KEYS = frozenset(
    {
        "artifacts",
        "capabilities",
        "evidence_level",
        "initial_state",
        "qualification",
        "route",
        "safety",
        "status",
        "timebase",
    }
)

#: Measured top-level manifest keys that only the whole-run route publishes:
#: the post-hoc enforcement label and the historical ``route_limitations``
#: compatibility key.
EXPECTED_WHOLE_RUN_ONLY_MANIFEST_KEYS = frozenset(
    {"enforcement", "route_limitations"}
)

#: Checkpoint artifacts the stepped route publishes when checkpointing is
#: enabled (one checkpoint per window plus the latest-checkpoint root file).
EXPECTED_STEPPED_CHECKPOINT_ARTIFACTS = frozenset(
    {"checkpoint.json"}
    | {f"checkpoints/step-{index:06d}.json" for index in range(1, 5)}
)


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-7,
    )


def _stepped_spec(root: Path, run_id: str) -> ExperimentSpec:
    return ExperimentSpec(
        "route-artifact-parity",
        run_id,
        "buck",
        "pi",
        Timebase(duration_s=4e-5, control_period_s=1e-5),
        output_dir=str(root),
        seed=17,
    )


def _whole_run_spec(root: Path, run_id: str) -> ExperimentSpec:
    return ExperimentSpec(
        "route-artifact-parity",
        run_id,
        "whole-run-buck",
        "pi",
        Timebase(duration_s=0.02, control_period_s=1e-3),
        output_dir=str(root),
        seed=17,
    )


def _whole_run_plant(spec: ExperimentSpec) -> Any:
    registration = get_plant("whole-run-buck")
    return registration.factory(
        {"cadence_s": 1e-3, "load_resistance_ohm": 1.0},
        PlantBuildContext(timebase=spec.timebase),
    )


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def published_routes(tmp_path_factory) -> dict[str, dict[str, Any]]:
    """Publish one minimal run per route and return manifests plus paths."""

    root = tmp_path_factory.mktemp("route-artifact-parity")

    stepped_spec = _stepped_spec(root, "parity-stepped")
    stepped_result = Runner().run(
        stepped_spec, _buck(), FakePIController(kp=0.2)
    )
    assert stepped_result.status == "QUALIFIED"

    load_builtin_plants()
    whole_spec = _whole_run_spec(root, "parity-whole-run")
    whole_result = Runner().run(
        whole_spec,
        _whole_run_plant(whole_spec),
        FakePIController(kp=0.2),
        time_model=get_plant("whole-run-buck").time_model,
    )
    assert whole_result.status == "QUALIFIED"

    return {
        "stepped": {
            "run_dir": Path(stepped_result.run_dir),
            "manifest": _read_manifest(Path(stepped_result.run_dir)),
        },
        "whole_run": {
            "run_dir": Path(whole_result.run_dir),
            "manifest": _read_manifest(Path(whole_result.run_dir)),
        },
    }


def _artifact_keys(published_routes: dict[str, dict[str, Any]], route: str) -> set[str]:
    return set(published_routes[route]["manifest"]["artifacts"])


def test_both_routes_publish_the_shared_required_artifacts(published_routes):
    for route, published in published_routes.items():
        artifacts = set(published["manifest"]["artifacts"])
        missing = SHARED_REQUIRED_ARTIFACTS - artifacts
        assert missing == set(), f"{route} is missing shared artifacts: {sorted(missing)}"
        # The manifest is the package index, not an indexed artifact.
        assert (published["run_dir"] / "manifest.json").is_file()
        assert "manifest.json" not in artifacts


def test_both_routes_publish_the_same_artifact_key_set(published_routes):
    stepped = _artifact_keys(published_routes, "stepped")
    whole_run = _artifact_keys(published_routes, "whole_run")

    assert stepped == whole_run
    # No artifact may appear beyond the measured shape on either route.
    assert stepped - MEASURED_OPTIONAL_ARTIFACTS == set(SHARED_REQUIRED_ARTIFACTS)
    assert whole_run - MEASURED_OPTIONAL_ARTIFACTS == set(SHARED_REQUIRED_ARTIFACTS)


def test_route_specific_artifacts_match_the_expected_drift(published_routes):
    stepped = _artifact_keys(published_routes, "stepped")
    whole_run = _artifact_keys(published_routes, "whole_run")

    assert frozenset(stepped - whole_run) == EXPECTED_STEPPED_ONLY
    assert frozenset(whole_run - stepped) == EXPECTED_WHOLE_RUN_ONLY


def test_both_manifests_share_the_required_top_level_keys(published_routes):
    for route, published in published_routes.items():
        manifest = published["manifest"]
        missing = SHARED_MANIFEST_KEYS - set(manifest)
        assert missing == set(), f"{route} is missing manifest keys: {sorted(missing)}"

    # The measured top-level difference is whole-run metadata only; the
    # stepped manifest publishes nothing the whole-run manifest lacks.
    stepped_keys = set(published_routes["stepped"]["manifest"])
    whole_run_keys = set(published_routes["whole_run"]["manifest"])
    assert frozenset(whole_run_keys - stepped_keys) == EXPECTED_WHOLE_RUN_ONLY_MANIFEST_KEYS
    assert stepped_keys - whole_run_keys == set()


def test_whole_run_declares_its_route_limitations_and_evidence_cap(published_routes):
    route = published_routes["whole_run"]["manifest"]["route"]
    limitations = set(route["limitations"])

    assert route["evidence_cap"] == "limited"
    assert "in_run_enforcement_unavailable" in limitations
    assert "post_hoc_validation_only" in limitations


def test_checkpoint_artifacts_are_stepped_only(tmp_path):
    """Checkpointing is a stepped-route capability, not a shape drift.

    This test pins the only route-specific artifact family the product has:
    the stepped route can checkpoint, and the whole-run route refuses the
    request before writing anything.
    """

    result = Runner().run(
        _stepped_spec(tmp_path, "parity-stepped-checkpoint"),
        _buck(),
        FakePIController(kp=0.2),
        checkpoint_interval_steps=1,
    )
    artifacts = set(_read_manifest(Path(result.run_dir))["artifacts"])
    checkpoint_artifacts = frozenset(
        key for key in artifacts if key == "checkpoint.json" or key.startswith("checkpoints/")
    )
    assert checkpoint_artifacts == EXPECTED_STEPPED_CHECKPOINT_ARTIFACTS


def test_whole_run_refuses_checkpointing_before_writing(tmp_path):
    load_builtin_plants()
    spec = _whole_run_spec(tmp_path, "parity-whole-run-checkpoint")
    registration = get_plant("whole-run-buck")

    with pytest.raises(ValueError, match="was not started"):
        Runner().run(
            spec,
            _whole_run_plant(spec),
            FakePIController(kp=0.2),
            checkpoint_interval_steps=1,
            time_model=registration.time_model,
        )

    # Refused before the writer ran: no published package, no temporary run.
    assert not (tmp_path / spec.run_id).exists()
    assert list(tmp_path.glob(f".{spec.run_id}.*")) == []
