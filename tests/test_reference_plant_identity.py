"""The native reference plants use the same reviewed-identity pin as backends.

Two guarantees are covered here:

- the default ``buck``/``boost`` paths are pinned and report ``reviewed``;
- the solve-path digest is a source identity, so a CRLF checkout and an LF
  checkout produce the same value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pe_sim import BuckPlant, BoostPlant, reference_plants
from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim import identity
from pe_sim.identity import (
    implementation_digest,
    implementation_sources,
    load_reviewed_identities,
    review_status,
)
from pe_sim.inspection import inspect_run
from pe_sim.reference_plants import _AveragedConverter

from test_workbench_export import _published_buck_run


def _pinned(backend_id: str) -> str:
    return load_reviewed_identities()["backends"][backend_id]["implementation_sha256"]


def _buck() -> BuckPlant:
    return BuckPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _boost() -> BoostPlant:
    return BoostPlant(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
        integration_step_s=1e-6,
    )


def _factor(credibility: dict, factor_id: str) -> dict:
    return next(item for item in credibility["factors"] if item["factor_id"] == factor_id)


def _rewrite_manifest(run_dir: Path, manifest: dict) -> None:
    """Recompute the derived package hashes after changing the manifest."""

    manifest["artifacts"] = artifact_index(run_dir)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run_dir, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def test_ngspice_buck_digest_matches_the_checked_in_pin():
    # The checked-in JSON is the single source of truth for the reviewed digest.
    # Duplicating it as a literal here would only force a second edit on every
    # legitimate change and would not add an independent check.
    assert implementation_digest("ngspice-buck") == _pinned("ngspice-buck")
    assert review_status("ngspice-buck")["status"] == "reviewed"


def test_reference_plants_share_one_pinned_solve_path():
    digest = implementation_digest("buck")

    assert digest == implementation_digest("boost")
    assert digest == _pinned("buck")
    assert digest == _pinned("boost")
    assert implementation_sources("buck") == implementation_sources("boost")
    assert set(implementation_sources("buck")) == {"reference_plants.py"}


def test_reference_plants_report_a_reviewed_status():
    assert review_status("buck")["status"] == "reviewed"
    assert review_status("boost")["status"] == "reviewed"


@pytest.mark.parametrize("factory", [_buck, _boost])
def test_manifest_identity_records_the_review_pin_without_a_source_alias(factory):
    plant = factory()
    implementation = plant.manifest_identity()["implementation"]

    assert implementation["sha256"] == _pinned(plant.backend_id)
    assert implementation["solve_path_modules"] == ["reference_plants.py"]
    assert implementation["review"]["status"] == "reviewed"
    assert "source_sha256" not in implementation


def test_solve_path_digest_is_independent_of_checkout_line_endings(tmp_path, monkeypatch):
    source = Path(reference_plants.__file__).resolve()
    normalized = source.read_bytes().replace(b"\r\n", b"\n")
    crlf = normalized.replace(b"\n", b"\r\n")
    assert crlf != normalized

    (tmp_path / "reference_plants.py").write_bytes(crlf)
    monkeypatch.setattr(identity, "_PACKAGE_ROOT", tmp_path)

    assert implementation_sources("buck")["reference_plants.py"] == hashlib.sha256(normalized).hexdigest()


def test_solve_path_digest_matches_the_normalized_module_bytes():
    source = Path(reference_plants.__file__).resolve()
    normalized = source.read_bytes().replace(b"\r\n", b"\n")

    assert implementation_sources("buck")["reference_plants.py"] == hashlib.sha256(normalized).hexdigest()


def test_the_base_class_does_not_guess_a_reviewed_identity():
    class UnlabelledConverter(_AveragedConverter):
        topology = "unlabelled"

    plant = UnlabelledConverter(
        input_voltage_v=12.0,
        inductance_h=100e-6,
        capacitance_f=470e-6,
        load_resistance_ohm=10.0,
    )

    with pytest.raises(AttributeError):
        plant.manifest_identity()


def test_envelope_declares_the_unreviewed_runner_loop():
    envelope = _buck().validity_envelope().to_dict()
    rules = {item["rule_id"]: item["reason"] for item in envelope["uncheckable_rules"]}

    assert "implementation.runner_loop" in rules
    assert "runtime.py" in rules["implementation.runner_loop"]


def test_published_buck_run_passes_the_implementation_review_factor(tmp_path):
    run_dir, _, manifest = _published_buck_run(tmp_path)
    identity_document = _buck().manifest_identity()
    manifest["plant"]["hash"] = identity_document["sha256"]
    manifest["plant"]["identity"] = identity_document
    _rewrite_manifest(run_dir, manifest)

    result = inspect_run(run_dir)

    assert result.status == "ok", result.problems
    credibility = result.summary["credibility"]
    factor = _factor(credibility, "implementation_review")
    assert factor["status"] == "pass", factor
    assert factor["evidence"]["recorded_status"] == "reviewed"
    assert factor["evidence"]["current_checkout_status"] == "reviewed"
