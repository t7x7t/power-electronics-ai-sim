"""Kernel-side verification of a Plant adapter's self-reported identity.

``manifest_identity()`` is producer-declared.  These tests pin the property
that matters for agent-produced adapters: the kernel recomputes the reviewed
solve-path digest and refuses to treat a contradicted claim as evidence.
"""

from __future__ import annotations

import json

import pytest

from pe_sim import BoostPlant, BuckPlant
from pe_sim.contracts import ExperimentSpec, Timebase
from pe_sim.identity import (
    CONTRADICTED_VERIFICATION_STATUSES,
    implementation_digest,
    verify_adapter_identity,
)
from pe_sim.runtime import FakePIController, Runner, _derive_evidence_level, _require_formal_evidence


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _manifest_for(tmp_path, plant) -> dict:
    spec = ExperimentSpec(
        "verify",
        "verify-run",
        plant_id=getattr(plant, "topology", "plant"),
        controller_id="pi",
        timebase=Timebase(duration_s=1e-4, control_period_s=1e-5),
        output_dir=str(tmp_path),
    )
    result = Runner().run(spec, plant, FakePIController())
    return json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_reference_plant_run_records_kernel_verified_identity(tmp_path):
    manifest = _manifest_for(tmp_path, _buck())
    verification = manifest["plant"]["verification"]

    assert verification["status"] == "verified"
    assert verification["backend_id"] == "buck"
    assert verification["recorded_sha256"] == implementation_digest("buck")
    assert verification["recomputed_sha256"] == verification["recorded_sha256"]
    assert verification["module_file"] == "reference_plants.py"


def test_boost_plant_is_verified_against_the_same_solve_path(tmp_path):
    plant = BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )

    verification = _manifest_for(tmp_path, plant)["plant"]["verification"]

    assert verification["status"] == "verified"
    assert verification["backend_id"] == "boost"
    assert verification["recomputed_sha256"] == implementation_digest("boost")


def test_verification_is_not_part_of_the_identity_digest(tmp_path):
    """Recomputing the same adapter must not look like a model change."""

    first = _manifest_for(tmp_path / "a", _buck())
    second = _manifest_for(tmp_path / "b", _buck())

    assert first["plant"]["hash"] == second["plant"]["hash"]
    assert first["plant"]["identity"] == second["plant"]["identity"]
    assert "verification" not in first["plant"]["identity"]


def test_subclass_outside_the_solve_path_is_not_verified():
    class RogueBuck(BuckPlant):
        backend_id = "buck"

    plant = RogueBuck(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )

    verification = verify_adapter_identity(plant, plant.manifest_identity())

    assert verification["status"] == "module_outside_solve_path"
    assert verification["status"] in CONTRADICTED_VERIFICATION_STATUSES
    assert verification["module_file"] == __file__.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def test_mismatched_digest_is_reported_as_a_contradiction():
    class Liar:
        backend_id = "buck"

        def manifest_identity(self):
            return {
                "kind": "ideal_averaged_reference",
                "implementation": {
                    "sha256": "0" * 64,
                    "review": {"status": "reviewed", "backend_id": "buck"},
                },
            }

    liar = Liar()
    verification = verify_adapter_identity(liar, liar.manifest_identity())

    assert verification["status"] == "digest_mismatch"
    assert verification["recorded_sha256"] == "0" * 64
    assert verification["recomputed_sha256"] == implementation_digest("buck")


def test_adapter_without_backend_id_is_unverifiable_not_passing():
    class Anonymous:
        def manifest_identity(self):
            return {"kind": "runtime_component"}

    anonymous = Anonymous()
    verification = verify_adapter_identity(anonymous, anonymous.manifest_identity())

    assert verification["status"] == "unverifiable"
    assert verification["backend_id"] is None
    assert verification["status"] not in CONTRADICTED_VERIFICATION_STATUSES


def test_unknown_backend_id_is_unverifiable():
    class Unknown:
        backend_id = "not-a-reviewed-backend"

        def manifest_identity(self):
            return {"implementation": {"sha256": "a" * 64, "review": {"status": "reviewed"}}}

    unknown = Unknown()
    verification = verify_adapter_identity(unknown, unknown.manifest_identity())

    assert verification["status"] == "unverifiable"
    assert "no reviewed solve path is declared" in verification["reasons"][0]


def test_controller_manifest_has_no_verification_block(tmp_path):
    """Controllers do not declare a reviewed solve path, so nothing is emitted."""

    manifest = _manifest_for(tmp_path, _buck())

    assert "verification" not in manifest["controller"]


def _formal_plant(verification_status: str) -> dict:
    return {
        "id": "buck",
        "hash": "1" * 64,
        "identity": {
            "kind": "ideal_averaged_reference",
            "implementation": {
                "sha256": implementation_digest("buck"),
                "review": {"status": "reviewed", "backend_id": "buck"},
            },
        },
        "verification": {"status": verification_status, "backend_id": "buck", "reasons": ["synthetic"]},
    }


def _formal_spec() -> ExperimentSpec:
    return ExperimentSpec(
        "formal",
        "formal-run",
        "buck",
        "pi",
        Timebase(duration_s=1e-4, control_period_s=1e-5),
        seed=7,
        contracts={
            "safety": {"id": "s", "hash": "a" * 64},
            "qualification": {"id": "q", "hash": "b" * 64},
        },
        output_dir="runs",
    )


class _Worktree:
    status = "clean"


class _Git:
    source_commit = "c" * 40
    working_tree_status = "clean"


def _environment() -> dict:
    return {"status": "known", "backends": [{"status": "known"}]}


@pytest.mark.parametrize("status", sorted(CONTRADICTED_VERIFICATION_STATUSES))
def test_formal_comparison_rejects_a_contradicted_adapter(status):
    with pytest.raises(ValueError, match="kernel-verified"):
        _require_formal_evidence(
            _formal_spec(),
            _Worktree(),
            _Git(),
            _formal_plant(status),
            {"id": "pi", "hash": "2" * 64, "identity": {"kind": "controller"}},
            _environment(),
        )


def test_formal_comparison_accepts_a_verified_adapter():
    _require_formal_evidence(
        _formal_spec(),
        _Worktree(),
        _Git(),
        _formal_plant("verified"),
        {"id": "pi", "hash": "2" * 64, "identity": {"kind": "controller"}},
        _environment(),
    )


def test_comparable_level_requires_kernel_verification():
    common = {
        "status": "RUN_OK",
        "mode": "formal_comparison",
        "worktree": _Worktree(),
        "environment": _environment(),
        "qualification": {"level": "full"},
    }

    assert _derive_evidence_level(plant=_formal_plant("verified"), **common) == "comparable"
    assert _derive_evidence_level(plant=_formal_plant("digest_mismatch"), **common) == "limited"
    unverified = _formal_plant("verified")
    unverified.pop("verification")
    assert _derive_evidence_level(plant=unverified, **common) == "limited"
