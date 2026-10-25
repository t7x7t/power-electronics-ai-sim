"""Kernel-side verification of a Plant adapter's self-reported identity.

``manifest_identity()`` is producer-declared.  These tests pin the property
that matters for agent-produced adapters: the kernel recomputes the reviewed
solve-path digest and refuses to treat a contradicted claim as evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pe_sim import BoostPlant, BuckPlant
from pe_sim import reference_plants as reference_plants_module
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


def _solved_reference_plants_file() -> Path:
    """Return the reviewed ``reference_plants.py`` inside the package."""

    return Path(reference_plants_module.__file__).resolve()


def _load_module(path: Path, module_name: str):
    """Execute a module from an arbitrary file and clean up its sys.modules entry."""

    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:  # pragma: no cover - defensive cleanup
        sys.modules.pop(module_name, None)
        raise
    return module


def test_reference_plant_run_records_kernel_verified_identity(tmp_path):
    manifest = _manifest_for(tmp_path, _buck())
    verification = manifest["plant"]["verification"]

    assert verification["status"] == "verified"
    assert verification["backend_id"] == "buck"
    assert verification["recorded_sha256"] == implementation_digest("buck")
    assert verification["recomputed_sha256"] == verification["recorded_sha256"]
    assert verification["module_file"] == "reference_plants.py"
    # The manifest must stay portable: only a package-relative path is recorded.
    assert verification["module_path"] == "reference_plants.py"
    assert verification["solve_path_files"] == ["reference_plants.py"]
    # The location evidence names what the class itself contributes and the
    # namespace binding it compared, in the same portable form.
    assert verification["class_bound_in_declared_module"] is True
    assert verification["class_code_files"] == ["reference_plants.py"]
    assert verification["class_code_outside_solve_path"] == []


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
    assert verification["module_file"] == Path(__file__).name
    # An out-of-package class records no absolute path; the verdict carries the
    # decision and the basename identifies the file.
    assert verification["module_path"] is None
    assert verification["solve_path_files"] == ["reference_plants.py"]


def test_same_basename_module_outside_the_package_is_not_verified(tmp_path):
    """A file *named* ``reference_plants.py`` elsewhere is not the reviewed file.

    Regression for P0-1: the location check used to compare bare file names, so
    any same-named module on ``sys.path`` could supply the class that ran while
    the recomputed digest and review pin still matched.
    """

    rogue_file = tmp_path / "reference_plants.py"
    rogue_file.write_text(
        "from pe_sim.reference_plants import BuckPlant\n"
        "\n"
        "\n"
        "class RogueSameNameBuck(BuckPlant):\n"
        '    """Inherits the reviewed behaviour from a same-named file."""\n',
        encoding="utf-8",
    )

    module_name = "rogue_same_name_reference_plants"
    try:
        module = _load_module(rogue_file, module_name)
        rogue = module.RogueSameNameBuck(
            input_voltage_v=12.0,
            inductance_h=100e-6,
            capacitance_f=470e-6,
            load_resistance_ohm=10.0,
            integration_step_s=1e-6,
        )
        verification = verify_adapter_identity(rogue, rogue.manifest_identity())
    finally:
        sys.modules.pop(module_name, None)

    assert verification["status"] == "module_outside_solve_path"
    assert verification["status"] in CONTRADICTED_VERIFICATION_STATUSES
    # The digest and review claims are genuinely consistent: only the location
    # is wrong.  Verification must reject on location alone.
    assert verification["recomputed_sha256"] == implementation_digest("buck")
    assert verification["recorded_review_status"] == "reviewed"
    assert verification["module_file"] == "reference_plants.py"
    assert verification["module_path"] is None
    assert verification["solve_path_files"] == ["reference_plants.py"]


def test_copied_solve_path_module_is_not_verified(tmp_path):
    """Identical content elsewhere is still outside the reviewed solve path."""

    source = _solved_reference_plants_file()
    copy_dir = tmp_path / "copy_of_pe_sim"
    copy_dir.mkdir()
    copied_file = copy_dir / source.name
    copied_file.write_bytes(source.read_bytes())
    assert copied_file.read_bytes() == source.read_bytes()

    # A dotted name whose parent is ``pe_sim`` keeps the copied module's relative
    # imports resolvable without touching the installed package.
    module_name = "pe_sim._copied_reference_plants_for_test"
    try:
        module = _load_module(copied_file, module_name)
        copied_plant = module.BuckPlant(
            input_voltage_v=12.0,
            inductance_h=100e-6,
            capacitance_f=470e-6,
            load_resistance_ohm=10.0,
            integration_step_s=1e-6,
        )
        verification = verify_adapter_identity(copied_plant, copied_plant.manifest_identity())
    finally:
        sys.modules.pop(module_name, None)

    assert verification["status"] == "module_outside_solve_path"
    assert verification["status"] in CONTRADICTED_VERIFICATION_STATUSES
    # Content equality is real here: the digest and review status both pass, so
    # the rejection can only come from the resolved location.
    assert verification["recomputed_sha256"] == implementation_digest("buck")
    assert verification["module_path"] is None
    assert verification["module_file"] == "reference_plants.py"


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


_TAMPERED_BODY = (
    "from pe_sim.reference_plants import BuckPlant as _RealBuckPlant\n"
    "\n"
    "\n"
    "class {name}(_RealBuckPlant):\n"
    "    def _rk4_step(self, duty, step):\n"
    "        self.output_voltage_v = 123.456\n"
)


def _tampered_plant(tmp_path, *, name: str, forge_module: bool):
    """Build a tampered plant from a file that is not the reviewed package."""

    rogue_file = tmp_path / "reference_plants.py"
    rogue_file.write_text(_TAMPERED_BODY.format(name=name), encoding="utf-8")
    module_name = f"rogue_forged_{name.lower()}_{'forged' if forge_module else 'plain'}"
    module = _load_module(rogue_file, module_name)
    if forge_module:
        # ``inspect.getfile()`` reads ``__module__``, so this is what makes the
        # old, path-only check report the reviewed file.
        module.__dict__[name].__module__ = "pe_sim.reference_plants"
        module.__dict__[name].__qualname__ = name
    plant = module.__dict__[name](
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )
    return module, plant


def test_forged_module_attribute_does_not_verify_a_foreign_class(tmp_path):
    """Regression for P0-1 residual: ``__module__`` is writable.

    A class that names a reviewed module satisfies the path check, because
    ``inspect.getfile()`` resolves the claimed module.  Verification must also
    confirm that the claimed module binds *this* class object.
    """

    module, plant = _tampered_plant(tmp_path, name="BuckPlant", forge_module=True)
    try:
        verification = verify_adapter_identity(plant, plant.manifest_identity())
    finally:
        sys.modules.pop(module.__name__, None)

    assert verification["status"] == "module_outside_solve_path"
    assert verification["status"] in CONTRADICTED_VERIFICATION_STATUSES
    # The claimed path looks right: that is exactly why it cannot be trusted.
    assert verification["module_path"] == "reference_plants.py"
    assert verification["class_bound_in_declared_module"] is False
    assert verification["class_code_outside_solve_path"] == ["_rk4_step"]
    assert any("is not the object bound as" in reason for reason in verification["reasons"])
    # The self-declared identity is genuinely consistent, so the rejection can
    # only come from the location evidence.
    assert verification["recorded_sha256"] == verification["recomputed_sha256"]
    assert verification["recorded_review_status"] == "reviewed"


def test_alias_subclass_with_forged_module_is_not_verified(tmp_path):
    """Even a behaviour-identical subclass is not the reviewed class object."""

    rogue_file = tmp_path / "reference_plants.py"
    rogue_file.write_text(
        "from pe_sim.reference_plants import BuckPlant as _RealBuckPlant\n"
        "\n"
        "\n"
        "class BuckPlant(_RealBuckPlant):\n"
        "    pass\n"
        "\n"
        "\n"
        "BuckPlant.__module__ = 'pe_sim.reference_plants'\n"
        "BuckPlant.__qualname__ = 'BuckPlant'\n",
        encoding="utf-8",
    )
    module = _load_module(rogue_file, "rogue_alias_forged_buck")
    try:
        plant = module.BuckPlant(
            input_voltage_v=12.0,
            inductance_h=100e-6,
            capacitance_f=470e-6,
            load_resistance_ohm=10.0,
            integration_step_s=1e-6,
        )
        verification = verify_adapter_identity(plant, plant.manifest_identity())
    finally:
        sys.modules.pop(module.__name__, None)

    assert verification["status"] == "module_outside_solve_path"
    assert verification["class_bound_in_declared_module"] is False
    # No own code at all, so only the namespace check can reject it.
    assert verification["class_code_outside_solve_path"] == []


def test_rebound_foreign_class_in_reviewed_module_is_not_verified(tmp_path, monkeypatch):
    """Binding a foreign class into the reviewed namespace is not enough.

    Once the reviewed module binds the rogue object, the namespace check
    passes, so the class body itself has to be checked: its own methods must
    have been compiled from declared solve-path files.
    """

    module, plant = _tampered_plant(tmp_path, name="BuckPlant", forge_module=True)
    try:
        monkeypatch.setattr(
            reference_plants_module, "BuckPlant", module.BuckPlant
        )
        verification = verify_adapter_identity(plant, plant.manifest_identity())
    finally:
        sys.modules.pop(module.__name__, None)

    assert verification["status"] == "module_outside_solve_path"
    assert verification["class_bound_in_declared_module"] is True
    assert verification["class_code_outside_solve_path"] == ["_rk4_step"]
    assert any("outside the declared solve path" in reason for reason in verification["reasons"])


def test_tampered_adapter_run_is_capped_below_comparison(tmp_path):
    """The forged adapter may still run, but not as compared evidence."""

    module, plant = _tampered_plant(tmp_path, name="BuckPlant", forge_module=True)
    try:
        manifest = _manifest_for(tmp_path, plant)
    finally:
        sys.modules.pop(module.__name__, None)

    verification = manifest["plant"]["verification"]
    assert verification["status"] == "module_outside_solve_path"
    assert manifest["evidence_level"] != "comparable"
    # Feed the real verdict through the formal-comparison gate: the tampered
    # implementation must not reach `comparable` even though it ran cleanly.
    formal_plant = _formal_plant("verified")
    formal_plant["verification"] = verification
    with pytest.raises(ValueError, match="kernel-verified"):
        _require_formal_evidence(
            _formal_spec(),
            _Worktree(),
            _Git(),
            formal_plant,
            {"id": "pi", "hash": "2" * 64, "identity": {"kind": "controller"}},
            _environment(),
        )
